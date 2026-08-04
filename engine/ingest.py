import sys
import os
import re
import json
import time
import datetime

# Model layer (endpoints, roles, retrying request plumbing) lives in llm.py and
# is shared with build_blog.py. ALL_ROLES is re-exported so `import ingest;
# ingest.ALL_ROLES` (used by publish.sh's diagnostics) keeps working.
from llm import (
    call_model, label, check_required_keys, MAX_RETRIES,
    UTILITY, POLISH_PROSE, POLISH_OUTLINE, TRIAGE, WEAVE, ALL_ROLES,
)
# Filesystem layout (see paths.py) - so this script works from any directory.
from paths import CONTENT_DIR, VOICE_PATH, LINK_MANIFEST_PATH, EXISTING_TAGS_PATH
# Prompt text and per-prompt temperature live in prompts.yaml, not in this file.
# To change what a model is told, edit that file - not this one.
import prompts

# --- PIPELINE TUNING ---
# Draft is treated as an "outline" (route to POLISH_OUTLINE) when at least this
# fraction of its non-blank lines are bullets / numbered items / headings. In
# the polish-model-test corpus, prose drafts sat at 0-14% and outline drafts at
# 71-100%, so anything in the wide gap between is unambiguous; 0.35 splits it.
OUTLINE_LINE_RATIO = 0.35

# Editorial linking is a ceiling, not a target: link only where a genuine
# connection exists, never force a link, zero is a fine outcome.
MAX_BACKLINKS = 5      # most inline backlinks to weave into a post
MAX_CANDIDATES = 5     # most posts the triage step shortlists for full reading
# The polished article's own retry budget reuses llm.MAX_RETRIES (imported
# above), matching the model layer's transient-failure retries.
# The polished article should not be dramatically shorter than the source
# notes (the goal is to keep every idea). If it is, treat it as a truncated
# generation and retry. 0.5 = at least half the source word count.
MIN_POLISH_RATIO = 0.5
# Sampling temperature is no longer set here: each prompt carries its own in
# prompts.yaml, so wording and the randomness it is tuned for stay together.

# Every prompt the ingest pipeline can call. Checked before generation starts so
# a typo'd or half-edited prompts.yaml fails immediately with a clear message.
REQUIRED_PROMPTS = (
    'polish', 'suggest_tags', 'consolidate_tags', 'normalize_title',
    'generate_summary', 'generate_title', 'generate_themes',
    'triage_backlinks', 'weave_backlink',
)

def parse_frontmatter(text):
    """Split a Markdown file into (frontmatter_str, body). Returns (None, text)
    when no frontmatter block is present."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
    if match:
        return match.group(1), match.group(2)
    return None, text

def _strip_frontmatter_and_media(text):
    """Return only the lines that carry the author's own prose/notes, so the
    prose-vs-outline classifier isn't fooled by markup. Drops a leading YAML
    frontmatter block, fenced code, image lines, and bare URLs (e.g. YouTube
    links, which are neither prose nor outline)."""
    _, body = parse_frontmatter(text)
    lines = []
    in_fence = False
    for line in body.splitlines():
        if re.match(r'\s*```', line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r'!\[', stripped):            # image
            continue
        if re.match(r'https?://\S+$', stripped):  # bare URL on its own line
            continue
        lines.append(stripped)
    return lines

def classify_draft(raw_draft):
    """Classify a draft as 'prose' or 'outline' by the share of its content
    lines that are bullets, numbered items, or headings.

    This is deterministic and local - no model call - so routing never sends the
    draft anywhere just to decide where to send it. The threshold comes from the
    polish-model-test corpus (prose drafts 0-14% bullet lines, outline drafts
    71-100%); see OUTLINE_LINE_RATIO."""
    lines = _strip_frontmatter_and_media(raw_draft)
    if not lines:
        return "prose"
    markers = sum(1 for l in lines if re.match(r'([-*+]\s|\d+\.\s|#{1,6}\s)', l))
    return "outline" if markers / len(lines) >= OUTLINE_LINE_RATIO else "prose"

def select_polish_role(raw_draft):
    """Pick the POLISH role for this draft: local model for prose (light
    copy-edit, stays private), strong remote model for outlines (real writing)."""
    return POLISH_OUTLINE if classify_draft(raw_draft) == "outline" else POLISH_PROSE

def _normalize_dashes(text):
    """Enforce VOICE.md rule #2 (plain spaced hyphens, never em-dashes)
    deterministically, so style compliance never depends on the model obeying.
    Some models - notably smaller local ones and Mistral - emit em/en-dashes
    despite the instruction. An em/en-dash used as punctuation becomes ' - ';
    surrounding whitespace is collapsed so we don't get double spaces."""
    return re.sub(r'\s*[—–]\s*', ' - ', text)

def step_1_polish_content(raw_draft, voice_guidelines, role=None):
    """Step 1: Translation, formatting, and voice only. Linking is handled
    later by the dedicated backlink step, once candidates have been triaged.

    The prompt's primary directive is idea preservation: the raw draft is
    notes, and the model's job is to turn them into flowing prose WITHOUT
    dropping any point, metaphor, or example. Prose polish is secondary.

    `role` selects the model. When None (the normal path) it is chosen by
    select_polish_role() from the draft's shape; the test harness passes an
    explicit role to force a specific model."""
    if role is None:
        role = select_polish_role(raw_draft)
    # A small model occasionally returns a truncated fragment instead of the
    # full article. Since the primary directive is "keep every idea", guard
    # against gross length collapse: retry if the result is implausibly short
    # relative to the source notes, then fail loudly rather than write a stub.
    source_words = len(raw_draft.split())
    min_words = max(30, int(source_words * MIN_POLISH_RATIO))
    prompt = prompts.render('polish', voice_guidelines=voice_guidelines,
                            raw_draft=raw_draft)
    for attempt in range(1, MAX_RETRIES + 1):
        result = call_model(prompt, role=role,
                            temperature=prompts.temperature('polish'))
        if len(result.split()) >= min_words:
            return _normalize_dashes(result)
        print(f"⚠️  Polish output looks truncated "
              f"({len(result.split())} words vs {source_words} in source; "
              f"attempt {attempt}/{MAX_RETRIES}) - retrying...")
    print(f"❌ Polish step kept returning a short/truncated result "
          f"(min expected {min_words} words). Aborting so a stub is not written.")
    sys.exit(1)

def _clip_tags(tags, max_words=2):
    """Enforce short tags deterministically: reject any tag longer than
    `max_words` words outright (a too-long tag is treated as invalid, not
    truncated), drop empties, and de-duplicate case-insensitively while
    preserving order. Applied after every tag step so tag length never depends
    on the model obeying an instruction."""
    seen = set()
    out = []
    for t in tags:
        tag = str(t).strip()
        words = tag.split()
        if not words or len(words) > max_words:
            continue
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            out.append(tag)
    return out

def step_2_suggest_tags(polished_body):
    """Step 2: Suggest highly accurate candidate tags based ONLY on content (unbiased)."""
    prompt = prompts.render('suggest_tags', polished_body=polished_body)
    response = call_model(prompt, role=UTILITY,
                          temperature=prompts.temperature('suggest_tags'))
    try:
        # Strip any accidental markdown blocks the LLM might return
        clean_res = response.replace("```json", "").replace("```", "").strip()
        tags = json.loads(clean_res)
    except Exception:
        # Fallback split if JSON parsing fails
        tags = [t.strip().strip('"') for t in response.replace('[','').replace(']','').split(',')]
    return _clip_tags(tags)

def step_3_consolidate_tags(candidates, existing_tags):
    """Step 3: Map candidate tags to the existing taxonomy, or approve new ones."""
    prompt = prompts.render('consolidate_tags',
                            candidates=json.dumps(candidates),
                            existing_tags=json.dumps(existing_tags))
    response = call_model(prompt, role=UTILITY,
                          temperature=prompts.temperature('consolidate_tags'))
    try:
        clean_res = response.replace("```json", "").replace("```", "").strip()
        tags = json.loads(clean_res)
    except Exception:
        tags = candidates
    return _clip_tags(tags)

def _single_line(text):
    """Collapse a model response to one clean line suitable for a quoted YAML
    scalar. Small models sometimes return several candidate lines (e.g. four
    alternative titles) or a bulleted list; we take the first real line so the
    frontmatter never ends up with embedded newlines or list markers.

    Models also wrap the output in quotes despite being told not to. A wrapping
    pair of quotes is stripped (so the title doesn't render as 'like this'); only
    then are any *embedded* double quotes neutralised to single quotes, since the
    value ends up inside a double-quoted YAML scalar."""
    def clean(s):
        s = re.sub(r'[ \t]*[—–][ \t]*', ' - ', s).strip()
        # Strip one wrapping pair of matching quotes the model added around the
        # whole value (' or "), which would otherwise show up literally.
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            s = s[1:-1].strip()
        # Neutralise any remaining embedded double quotes for safe quoting.
        return s.replace('"', "'").strip()
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*0123456789. ").strip()
        if line:
            return clean(line)
    return clean(text.strip())

def extract_h1(raw_draft):
    """Return the text of the draft's first Markdown H1 ('# ...'), or None.

    When the author gave the post a heading, that is their chosen title, so we
    use it instead of generating one. Strips the leading '#', any surrounding
    bold '**', and whitespace. Skips a leading YAML frontmatter block so a
    'title:' line is never mistaken for a heading."""
    _, body = parse_frontmatter(raw_draft)
    for line in body.splitlines():
        m = re.match(r'\s*#\s+(.*\S)\s*$', line)
        if m:
            return m.group(1).strip().strip('*').strip()
    return None

def normalize_title(h1, voice_guidelines):
    """Turn the author's own heading into the post's title, in English.

    The body is translated German->English during polish, so a German heading
    would be the one place foreign text leaks into the title, filename, and URL.
    This runs one small LOCAL (UTILITY) call to translate if needed and pass
    through unchanged if the heading is already English. Kept tight and cold so
    a small model doesn't paraphrase or pad a title the author already chose."""
    prompt = prompts.render('normalize_title',
                            voice_guidelines=voice_guidelines, h1=h1)
    return _single_line(call_model(
        prompt, role=UTILITY, temperature=prompts.temperature('normalize_title')))

def slugify_title(title):
    """Make a clean, lowercase, hyphenated URL slug from a title (no extension).
    Keeps ASCII letters/digits, turns any run of other characters into a single
    hyphen, and trims leading/trailing hyphens. Mirrors build_blog.slugify_tag
    but is stricter about punctuation since this becomes a public URL."""
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    return slug or "post"

def content_filename(date, title):
    """Compose the on-disk content filename 'YYYY-MM-DD_Title_with_underscores.md'.
    Unlike the slug, the title's own casing is preserved (it's for humans
    browsing the folder); spaces become underscores and any character outside
    [A-Za-z0-9_] is dropped, so commas/apostrophes/colons simply vanish. Repeat
    underscores are collapsed."""
    words = re.sub(r'[^A-Za-z0-9]+', '_', title).strip('_')
    words = re.sub(r'_+', '_', words) or "post"
    return f"{date}_{words}.md"

def generate_summary(polished_body, title, voice_guidelines):
    """Generate a crisp, one-sentence summary for the metadata.

    The summary is the author's own blurb for their own post (it shows on cards,
    in RSS, and in the Mastodon announcement), so it must be written in the
    author's first-person voice - never as a detached outside observer. Small
    local models default to a book-report register ("the author argues that...")
    unless the prompt explicitly forbids it.

    Receives the title so it can COMPLEMENT it rather than restate it (the two
    sit stacked on the card, so an echo wastes the blurb), and VOICE.md so it
    actually sounds like the author."""
    prompt = prompts.render('generate_summary',
                            voice_guidelines=voice_guidelines,
                            title=title, polished_body=polished_body)
    return _single_line(call_model(
        prompt, role=UTILITY, temperature=prompts.temperature('generate_summary')))

def generate_title(polished_body, voice_guidelines):
    """Generate a personal, conversational, non-academic title."""
    prompt = prompts.render('generate_title',
                            voice_guidelines=voice_guidelines,
                            polished_body=polished_body)
    return _single_line(call_model(
        prompt, role=UTILITY, temperature=prompts.temperature('generate_title')))

def generate_themes(polished_body):
    """Generate an editorial 'themes' line used only for backlink triage.
    Unlike the reader-facing summary (a hook), this favours coverage: it names
    the concrete topics, ideas, and angles a post touches so a later post can
    recognise a genuine connection."""
    prompt = prompts.render('generate_themes', polished_body=polished_body)
    return _single_line(call_model(
        prompt, role=UTILITY, temperature=prompts.temperature('generate_themes')))

def load_ledger():
    """Load the enriched article ledger regenerated by build_blog.py."""
    path = LINK_MANIFEST_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _tag_tokens(tags):
    """Lower-cased word tokens across a list of tags, for fuzzy overlap.
    "AI Ethics" -> {"ai", "ethics"}; "Open-Source" -> {"open", "source"}."""
    tokens = set()
    for t in tags:
        for tok in re.split(r'[^a-z0-9]+', str(t).lower()):
            if tok:
                tokens.add(tok)
    return tokens

def triage_candidates(final_tags, ledger, self_slug=None):
    """Shortlist existing posts worth reading in full before linking.

    First narrows the ledger deterministically to posts sharing at least one
    tag TOKEN with this draft (the candidate pool), then lets the TRIAGE model pick
    the most promising few. Returns a list of slugs (possibly empty).

    Matching is token-based rather than exact so that a compound tag like
    "AI Ethics" still overlaps existing tags "AI" and "Ethics" - tag
    consolidation does not always reduce to the existing vocabulary, and triage
    is meant to cast a wide net (the weave step is the quality gate).

    self_slug is the slug this post will be written to; it is excluded so a post
    can never link to itself (relevant when re-ingesting an existing post, whose
    slug is already in the ledger from a prior build)."""
    draft_tokens = _tag_tokens(final_tags)
    pool = [
        e for e in ledger
        if isinstance(e, dict) and e.get("slug") and e.get("slug") != self_slug
        and draft_tokens & _tag_tokens(e.get("tags", []))
    ]

    if not pool:
        return []

    # With a tiny pool there is nothing to shortlist - read all of it.
    if len(pool) <= MAX_CANDIDATES:
        return [e["slug"] for e in pool]

    catalog = [
        {"slug": e["slug"], "title": e.get("title", ""),
         "tags": e.get("tags", []), "themes": e.get("themes", "")}
        for e in pool
    ]
    valid_slugs = {e["slug"] for e in pool}
    prompt = prompts.render('triage_backlinks',
                            max_candidates=MAX_CANDIDATES,
                            tags=json.dumps(final_tags),
                            catalog=json.dumps(catalog, indent=2))
    response = call_model(prompt, role=TRIAGE,
                          temperature=prompts.temperature('triage_backlinks'))
    try:
        clean_res = response.replace("```json", "").replace("```", "").strip()
        chosen = json.loads(clean_res)
        chosen = [s for s in chosen if s in valid_slugs]
        if chosen:
            return chosen[:MAX_CANDIDATES]
    except Exception:
        pass
    # Fallback: could not parse a shortlist - read the whole (bounded) pool.
    return [e["slug"] for e in pool][:MAX_CANDIDATES]

def weave_backlinks(polished_body, candidate_slugs, voice_guidelines, slug_to_file=None):
    """Read the full text of shortlisted posts and weave up to MAX_BACKLINKS
    natural inline Markdown links into the polished draft. Returns the body
    unchanged when no genuine connection exists.

    slug_to_file maps each post's slug to its on-disk content filename. It is
    needed because a post's filename (dated) no longer equals its slug; we look
    the source file up here instead of reconstructing it from the slug. For
    older posts whose slug still matches the filename, the fallback reproduces
    the old behaviour."""
    slug_to_file = slug_to_file or {}
    candidates = []
    for slug in candidate_slugs:
        filename = slug_to_file.get(slug) or slug.replace(".html", ".md")
        md_path = os.path.join(CONTENT_DIR, filename)
        if not os.path.exists(md_path):
            continue
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                fm, body = parse_frontmatter(f.read())
            title = ""
            if fm:
                m = re.search(r'^title:\s*"?(.*?)"?\s*$', fm, re.MULTILINE)
                if m:
                    title = m.group(1)
            candidates.append({"slug": slug, "title": title, "body": body.strip()})
        except Exception:
            continue

    print(f"   Shortlisted: {candidate_slugs}")
    if not candidates:
        return polished_body

    # Weave one candidate at a time, threading the growing body through each
    # call. A small model reliably places a single link but loses track when
    # asked to consider several full articles at once, so we keep each decision
    # focused. Stop once the ceiling is reached.
    body = polished_body
    linked = []
    for c in candidates:
        if len(linked) >= MAX_BACKLINKS:
            break
        # Don't offer a post that is already linked (protects against loops).
        if c["slug"] in body:
            continue
        woven = _weave_one(body, c, voice_guidelines)
        if woven and c["slug"] in woven and c["slug"] not in body:
            body = woven
            linked.append(c["slug"])

    print(f"   Links woven in: {sorted(set(linked)) if linked else 'none'}")
    return body

def _weave_one(polished_body, candidate, voice_guidelines):
    """Ask the model to add a single inline link to one candidate post, or
    return the body unchanged if there is no honest connection."""
    prompt = prompts.render('weave_backlink',
                            voice_guidelines=voice_guidelines,
                            candidate_title=candidate['title'],
                            candidate_slug=candidate['slug'],
                            candidate_body=candidate['body'],
                            polished_body=polished_body)
    woven = call_model(prompt, role=WEAVE,
                       temperature=prompts.temperature('weave_backlink'))
    # Guard against the model wrapping output in a code fence.
    woven = re.sub(r'^```(?:markdown)?\s*\n?', '', woven)
    woven = re.sub(r'\n?```\s*$', '', woven).strip()

    # The only allowed edit is adding a link, so the woven body should be
    # essentially the same length as the input. A small model sometimes returns
    # a truncated fragment instead; since linking is optional, discard any
    # result that lost substantial content and keep the original body.
    if not woven or len(woven.split()) < len(polished_body.split()) * MIN_POLISH_RATIO:
        return polished_body
    return woven

def build_frontmatter(title, date, tags, summary, themes, slug):
    """Construct the YAML frontmatter block. Title/summary/themes are wrapped
    in double quotes per VOICE.md; embedded double quotes are neutralised by
    the generators before they reach here.

    'slug' is the post's public URL (without .html). It is written explicitly so
    the on-disk filename (dated, for human file management) can differ from the
    URL (short, clean); build_blog.py and announce.py read this field and fall
    back to the filename only for older posts that lack it.

    'announce: pending' marks the post as eligible for a one-time Mastodon
    announcement at publish time (see announce.py). Once tooted, the publish
    step replaces this line with the resulting mastodon_host/mastodon_id, so a
    post is never announced twice. Older posts that predate this feature have
    no 'announce' line and are therefore never auto-tooted."""
    return f"""---
title: "{title}"
slug: "{slug}"
date: {date}
tags: {json.dumps(tags)}
summary: "{summary}"
themes: "{themes}"
announce: pending
---
"""

def backfill_themes():
    """Add a 'themes' frontmatter line to any content/*.md post missing one.
    Idempotent: posts that already have themes are left untouched, and the
    article body is never modified."""
    check_required_keys()
    prompts.validate(('generate_themes',))
    content_dir = CONTENT_DIR
    if not os.path.isdir(content_dir):
        print("ℹ️  No content/ directory - nothing to backfill.")
        return

    updated = 0
    skipped = 0
    for filename in sorted(os.listdir(content_dir)):
        if not filename.endswith(".md") or filename in ("index.md", "changelog.md"):
            continue
        path = os.path.join(content_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        fm, body = parse_frontmatter(text)
        if fm is None:
            print(f"⚠️  {filename}: no frontmatter block, skipping.")
            skipped += 1
            continue
        if re.search(r'^themes:\s*', fm, re.MULTILINE):
            skipped += 1
            continue

        print(f"🧾 Backfilling themes for {filename} ({label(UTILITY)})...")
        themes = generate_themes(body)
        new_fm = fm.rstrip("\n") + f'\nthemes: "{themes}"'
        # Preserve the conventional blank line between frontmatter and body
        # (parse_frontmatter absorbs it into the separator), and leave the body
        # text itself untouched.
        new_text = f"---\n{new_fm}\n---\n\n{body.lstrip(chr(10))}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
        updated += 1

    print(f"✅ Backfill complete: {updated} updated, {skipped} already had themes or were skipped.")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 ingest.py <path_to_draft>")
        print("       python3 ingest.py --backfill   (add themes to existing posts)")
        sys.exit(1)

    if sys.argv[1] == "--backfill":
        backfill_themes()
        return

    # Fail fast before any generation: a missing remote key or a broken/
    # incomplete prompt file should stop us now, not halfway through a batch.
    check_required_keys()
    prompts.validate(REQUIRED_PROMPTS)

    draft_path = sys.argv[1]

    with open(draft_path, 'r', encoding='utf-8') as f:
        raw_draft = f.read()

    # Read contexts
    voice_guidelines = open(VOICE_PATH, "r", encoding="utf-8").read() if os.path.exists(VOICE_PATH) else ""
    existing_tags = json.loads(open(EXISTING_TAGS_PATH, "r", encoding="utf-8").read()) if os.path.exists(EXISTING_TAGS_PATH) else []

    # --- Polish: route by draft shape (prose -> local, outline -> remote) ---
    kind = classify_draft(raw_draft)
    polish_role = select_polish_role(raw_draft)
    print(f"🧠 Polishing content - draft looks like {kind}, "
          f"using {label(polish_role)}...")
    polished_body = step_1_polish_content(raw_draft, voice_guidelines,
                                          role=polish_role)

    # --- Light model: short, structured metadata ---
    print(f"🏷️  Generating unbiased tag candidates ({label(UTILITY)})...")
    candidates = step_2_suggest_tags(polished_body)
    print(f"   Candidates discovered: {candidates}")

    print(f"🗂️  Reconciling tags with existing taxonomy ({label(UTILITY)})...")
    final_tags = step_3_consolidate_tags(candidates, existing_tags)
    print(f"   Finalized tags: {final_tags}")

    # Title: the author's own H1 wins (translated to English if needed);
    # otherwise the model names the post.
    h1 = extract_h1(raw_draft)
    if h1:
        print(f"✍️  Using author's heading as title, normalising to English ({label(UTILITY)})...")
        title = normalize_title(h1, voice_guidelines)
    else:
        print(f"✍️  Generating title ({label(UTILITY)})...")
        title = generate_title(polished_body, voice_guidelines)
    slug = slugify_title(title)
    print(f"   Title: {title!r}  ->  slug: {slug!r}")
    # Summary gets the chosen title so it can complement rather than echo it.
    print(f"✍️  Generating summary & themes ({label(UTILITY)})...")
    summary = generate_summary(polished_body, title, voice_guidelines)
    themes = generate_themes(polished_body)

    # --- Heavy model: editorial backlinking (triage then weave) ---
    print(f"🔎 Triaging backlink candidates ({label(TRIAGE)})...")
    ledger = load_ledger()
    self_slug = f"{slug}.html"
    # Map each ledger slug to its on-disk content file so weave can read the
    # source (filenames are dated now and no longer equal the slug). Older posts
    # predate the 'file' field; weave falls back to slug->filename for those.
    slug_to_file = {e["slug"]: e.get("file") for e in ledger
                    if isinstance(e, dict) and e.get("slug")}
    candidate_slugs = triage_candidates(final_tags, ledger, self_slug=self_slug)
    if candidate_slugs:
        print(f"🔗 Weaving relevant backlinks ({label(WEAVE)})...")
        polished_body = weave_backlinks(polished_body, candidate_slugs,
                                        voice_guidelines, slug_to_file=slug_to_file)
    else:
        print("   No tag-overlapping posts to link - skipping backlinks.")

    today = datetime.date.today().strftime("%Y-%m-%d")
    output_path = os.path.join(CONTENT_DIR, content_filename(today, title))

    # Construct YAML frontmatter safely
    markdown_output = build_frontmatter(title, today, final_tags, summary,
                                        themes, slug) + f"\n{polished_body}\n"

    os.makedirs(CONTENT_DIR, exist_ok=True)
    # Write in one shot; on any failure remove a partial file so publish.sh
    # never has to guess the output name to clean it up.
    try:
        with open(output_path, 'w', encoding='utf-8') as out:
            out.write(markdown_output)
    except Exception:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise

    print(f"🎉 Successfully wrote structured post to {output_path}!")

if __name__ == "__main__":
    main()
