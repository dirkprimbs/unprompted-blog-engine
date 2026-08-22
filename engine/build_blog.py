import os
import re
import yaml
import markdown
import shutil
import json
import html
import hashlib
import mimetypes
import urllib.request
from urllib.parse import urlparse
from datetime import datetime
import email.utils

import llm    # shared model layer: roles, call_model, generate_alt_text
import images # build-time image optimisation (optional Pillow dependency)
from urls import (
    SITE_URL, POSTS_DIR, TAGS_DIR, FEEDS_DIR,
    post_href, tag_page_name, tag_href, tag_feed_name, tag_feed_href, home_href,
    slugify_tag, slug_for, read_slug, htaccess_content,
)
# Filesystem layout. Note the split: urls.py above supplies URL space (including
# POSTS_DIR/TAGS_DIR/FEEDS_DIR, which double as public/ subdirectory names),
# while paths.py supplies absolute on-disk locations.
from config import (
    SITE_NAME, SITE_DESCRIPTION, FEED_DESCRIPTION, AUTHOR_NAME,
    AI_LABEL, AI_EXPLAINER, AI_EXPLAINER_URL,
    AUTHOR_EMAIL, LINK_ABOUT, LINK_MASTODON, FEDIVERSE_CREATOR,
    LINK_BLUESKY, BLUESKY_CREATOR,
    PAGE_SIZE, FEED_ITEMS, VISIBLE_TAGS, WORDS_PER_MINUTE, TAG_EMOJI,
    IMAGE_MAX_WIDTH, IMAGE_JPEG_QUALITY, IMAGE_MIN_BYTES,
)
from paths import (
    REPO_ROOT, TEMPLATE_PATH, STATIC_SOURCE_DIRS,
    PUBLIC_DIR, PUBLIC_ASSETS_DIR,
    CONTENT_DIR, CONTENT_ASSETS_DIR, PAGES_DIR,
    LINK_MANIFEST_PATH, EXISTING_TAGS_PATH, COMMENT_MODERATION_PATH,
)

# --- GLOBAL TRACKER FOR SMART SYNCHRONIZATION ---
# This tracks absolute paths of files generated during the current build run.
GENERATED_FILES = set()

# Bytes saved by image optimisation this run, one entry per image, so the build
# can report the total. Only ever appended to when something actually changed.
IMAGE_BYTES_SAVED = []

def esc(value):
    return html.escape(str(value), quote=True)

def tag_emoji_html(tag):
    """The configured emoji for a tag, as a span, or '' when there isn't one.

    aria-hidden because it decorates a label that is already there: a screen
    reader should announce "Photography", not "camera Photography". Emitted only
    in visible chrome (badges, pills, tag-page heading) - never in feed
    categories, the search index or JSON-LD, which consume the tag as data."""
    emoji = TAG_EMOJI.get(str(tag).strip().lower())
    if not emoji:
        return ''
    return f'<span class="tag-emoji" aria-hidden="true">{esc(emoji)}</span>'


def reading_time_minutes(markdown_text):
    text = markdown_text
    text = re.sub(r'```.*?```', ' ', text, flags=re.DOTALL)   # fenced code blocks
    text = re.sub(r'`[^`]*`', ' ', text)                       # inline code
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', text)          # images
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)       # links -> visible text
    text = re.sub(r'https?://\S+', ' ', text)                  # bare URLs
    words = len(text.split())
    return max(1, round(words / WORDS_PER_MINUTE))

def plain_text(html_body):
    text = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', ' ', html_body,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()

def load_template():
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(
            f"❌ Template file not found at '{TEMPLATE_PATH}'. "
            f"Please ensure the engine's template is present."
        )
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()

def write_file_if_changed(filepath, content):
    abs_path = os.path.abspath(filepath)
    GENERATED_FILES.add(abs_path)
    
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                old_content = f.read()
            if old_content == content:
                return # Skip redundant writing to preserve local modification timestamps
        except Exception:
            pass
            
    # Ensure nested directories exist before writing
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

def copy_asset_if_changed(src, dest):
    abs_path = os.path.abspath(dest)
    GENERATED_FILES.add(abs_path)
    
    if os.path.exists(dest):
        try:
            if os.path.getsize(src) == os.path.getsize(dest):
                return # Skip redundant copying to preserve local modification timestamps
        except Exception:
            pass
            
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)

# Matches a Markdown image: ![alt](src "optional title"). Groups: alt, src, title.
IMG_RE = re.compile(r'!\[([^\]]*)\]\(\s*([^)\s]+)(\s+"[^"]*")?\s*\)')

# Extracts an 11-char YouTube video ID from the common URL shapes
# (watch?v=, youtu.be/, /embed/, /shorts/).
YT_ID_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:[^ ]*&)?v=|embed/|shorts/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})'
)


def _youtube_id(url):
    m = YT_ID_RE.search(url)
    return m.group(1) if m else None


def _youtube_thumbnail_src(video_id):
    name = f"yt_{video_id}.jpg"
    cache_path = os.path.join(CONTENT_ASSETS_DIR, name)
    public_path = os.path.join(PUBLIC_ASSETS_DIR, name)
    rel = f"assets/{name}"

    if not os.path.exists(cache_path):
        # Prefer the higher-res thumbnail; fall back to hqdefault (always exists).
        data = None
        for variant in ("maxresdefault", "hqdefault"):
            url = f"https://i.ytimg.com/vi/{video_id}/{variant}.jpg"
            try:
                req = urllib.request.Request(
                    url, headers={'User-Agent': 'unprompted-blog/1.0'})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                break
            except Exception:
                continue
        if data is None:
            print(f"   ⚠️  Could not fetch YouTube thumbnail for {video_id} - "
                  f"hotlinking Google's CDN as a fallback (this contacts Google "
                  f"on page load). Re-run the build with network access to "
                  f"self-host it.")
            return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, 'wb') as fh:
            fh.write(data)

    # Mirror the cached copy into public/ for the compiled site.
    if os.path.exists(cache_path):
        copy_asset_if_changed(cache_path, public_path)
    return rel


def embed_youtube(markdown_text):
    def facade(video_id):
        vid = esc(video_id)
        thumb = esc(_youtube_thumbnail_src(video_id))
        return (
            f'<div class="yt-facade" data-yt="{vid}" '
            f'role="button" tabindex="0" '
            f'aria-label="Play YouTube video">'
            f'<img class="yt-thumb" loading="lazy" '
            f'src="{thumb}" '
            f'alt="YouTube video thumbnail">'
            f'<span class="yt-play" aria-hidden="true"></span>'
            f'</div>'
        )

    out_lines = []
    for line in markdown_text.split('\n'):
        stripped = line.strip()
        # Bare URL on its own line, or a markdown link that is the whole line.
        md_link = re.fullmatch(r'\[[^\]]*\]\(\s*(\S+?)\s*\)', stripped)
        candidate = None
        if md_link:
            candidate = md_link.group(1)
        elif re.fullmatch(r'https?://\S+', stripped):
            candidate = stripped
        if candidate:
            vid = _youtube_id(candidate)
            if vid:
                out_lines.append(facade(vid))
                continue
        out_lines.append(line)
    return '\n'.join(out_lines)


_COMMENT_MODERATION_CACHE = None


def load_comment_moderation():
    """Read comment_moderation.json once per build and return its 'posts' map.

    Missing file means "nothing moderated" - the common case, and not an error.
    A malformed file IS an error: this is the control that keeps replies the
    author removed from being reproduced, so silently falling back to an empty
    blocklist would quietly republish them. Fail the build instead."""
    global _COMMENT_MODERATION_CACHE
    if _COMMENT_MODERATION_CACHE is not None:
        return _COMMENT_MODERATION_CACHE
    if not os.path.exists(COMMENT_MODERATION_PATH):
        _COMMENT_MODERATION_CACHE = {}
        return _COMMENT_MODERATION_CACHE
    with open(COMMENT_MODERATION_PATH, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
        except ValueError as exc:
            raise SystemExit(
                f"❌ {COMMENT_MODERATION_PATH} is not valid JSON ({exc}).\n"
                "   Refusing to build: a broken moderation file would silently "
                "restore replies you blocked."
            )
    posts = data.get('posts', {}) if isinstance(data, dict) else None
    if not isinstance(posts, dict):
        raise SystemExit(
            f"❌ {COMMENT_MODERATION_PATH} must be an object with a 'posts' key "
            "mapping each slug to its moderation entry."
        )
    _COMMENT_MODERATION_CACHE = posts
    return _COMMENT_MODERATION_CACHE


def _classify_moderation_id(raw):
    """Which network a moderation ID names, from its shape alone.

    Mastodon status IDs are all digits; Bluesky posts are 'at://' URIs. The two
    formats cannot be confused, so the author's file stays one flat list per
    post and nothing already in it needs rewriting.

    Returns None for anything else, which the caller treats as fatal. A bsky.app
    permalink is deliberately NOT accepted: it carries a handle rather than a
    DID, and resolving one to the other needs a network call this build has no
    business making. Every rendered comment carries its exact ID in a
    data-comment-id attribute - copy it from the page."""
    if raw.isdigit():
        return 'mastodon'
    if raw.startswith('at://'):
        return 'bluesky'
    return None


def _moderation_ids(entry, key):
    """Split a moderation entry's ID list by network, as strings.

    IDs are compared as strings client-side, and each network gets its own
    attribute on the page, so the split happens here rather than in the browser.
    An ID that matches no known format is fatal, on the same principle as the
    rest of this file: silently ignoring it would quietly un-block a reply."""
    values = entry.get(key, []) if isinstance(entry, dict) else []
    if not isinstance(values, list):
        raise SystemExit(
            f"❌ {COMMENT_MODERATION_PATH}: '{key}' must be a list of status IDs."
        )
    split = {'mastodon': [], 'bluesky': []}
    for value in values:
        raw = str(value).strip()
        if not raw:
            continue
        network = _classify_moderation_id(raw)
        if network is None:
            raise SystemExit(
                f"❌ {COMMENT_MODERATION_PATH}: '{key}' contains {raw!r}, which "
                "is neither a Mastodon status ID (all digits) nor a Bluesky "
                "post URI (starting 'at://').\n"
                "   Refusing to build: an ID the engine cannot place would "
                "silently stop applying."
            )
        split[network].append(raw)
    return split


def moderation_key(meta):
    """The slug a post is keyed by in comment_moderation.json.

    slug_for() yields the filename form ('aicare.html'); the author writes the
    clean URL name in frontmatter and thinks of the post that way, so the
    moderation file is keyed on that and the extension is stripped here."""
    slug = str(meta.get('slug') or '')
    return slug[:-5] if slug.endswith('.html') else slug


def validate_comment_moderation(known_keys):
    """Fail the build on a moderation entry that matches no post.

    A mistyped or stale slug means every ID under it silently stops applying -
    the site would quietly start showing a reply that was removed, with nothing
    in the output to say so. Since this file is small and hand-edited, refusing
    to build is the cheaper failure."""
    unknown = sorted(set(load_comment_moderation()) - set(known_keys))
    if unknown:
        raise SystemExit(
            f"❌ {COMMENT_MODERATION_PATH} has entries for unknown posts: "
            f"{', '.join(unknown)}.\n"
            "   Key each entry by the post's slug without '.html' (e.g. "
            "\"aicare\"). Remove entries for posts that no longer exist."
        )


def _author_acct():
    """The site author's fediverse handle as Mastodon reports it in 'acct'.

    site.yaml stores fediverse_creator as '@user@instance'; the API's acct field
    is 'user@instance' for remote accounts and bare 'user' for accounts local to
    the instance being queried. Strip the leading '@' so the client can match
    either form. Empty when unset, which just disables author detection."""
    return str(FEDIVERSE_CREATOR or '').strip().lstrip('@')


def _author_bluesky():
    """The site author's Bluesky identity, for badging their own replies.

    Accepts either a handle ('you.bsky.social') or a DID ('did:plc:...'); the
    client compares against both fields the API returns, so which one the author
    configured does not matter. Empty when unset - author detection is simply
    off, exactly as it is for a site with no fediverse_creator."""
    return str(BLUESKY_CREATOR or '').strip().lstrip('@')


def bluesky_permalink(uri):
    """The public bsky.app URL for an 'at://' post URI.

    An AT-URI is 'at://<authority>/<collection>/<rkey>', and the web permalink
    is /profile/<authority>/post/<rkey>. The authority is kept as-is rather than
    resolved to a handle: post.uri always carries the DID, and DIDs are stable
    where handles are not - a reader who follows this link after the author
    renames themselves still lands on the post.

    A malformed URI is fatal. announce.py writes this key itself, so a bad value
    can only come from hand-editing, and the alternative is a comment thread
    that silently renders empty."""
    parts = str(uri).split('/')
    # at: / '' / authority / collection / rkey
    if len(parts) != 5 or parts[0] != 'at:' or parts[1] or not parts[2] or not parts[4]:
        raise SystemExit(
            f"❌ Invalid bluesky_uri: {uri!r}.\n"
            "   Expected the form "
            "'at://did:plc:xxxx/app.bsky.feed.post/rkey'."
        )
    return f"https://bsky.app/profile/{parts[2]}/post/{parts[4]}"


def build_comments_block(meta):
    """The comments section for a post, blending both networks into one thread.

    Emitted when the post has coordinates for either provider - a post announced
    only on Mastodon (every post predating Bluesky support) is unchanged, and one
    announced on both merges the two conversations client-side."""
    host = meta.get('mastodon_host')
    status_id = meta.get('mastodon_id')
    has_mastodon = bool(host) and status_id not in (None, '')
    bluesky_uri = str(meta.get('bluesky_uri') or '').strip()
    if not has_mastodon and not bluesky_uri:
        return ''
    if str(meta.get('comments', '')).strip().lower() == 'false':
        return ''

    # Per-post moderation. Only this thread's IDs are emitted - shipping the
    # whole blocklist to every page would advertise, on unrelated posts, what
    # was suppressed elsewhere.
    entry = load_comment_moderation().get(moderation_key(meta), {})
    blocked = _moderation_ids(entry, 'blocked')
    approved = _moderation_ids(entry, 'approved')
    any_blocked = bool(blocked['mastodon'] or blocked['bluesky'])
    # 'comments: curated' flips this thread from default-open (show everything
    # except the blocklist) to default-closed (show only the approve list, plus
    # the author's own replies). The escape hatch for a thread that goes bad.
    curated = str(meta.get('comments', '')).strip().lower() == 'curated'

    # Thread permalinks, also used by the noscript fallback. MASTODON_ID isn't
    # available at build time, so link to the status endpoint, which redirects
    # to the canonical thread URL on the instance.
    links = []
    attrs = [
        f'data-comments-mode="{"curated" if curated else "open"}"',
        f'data-blocked-mastodon="{esc(",".join(blocked["mastodon"]))}"',
        f'data-blocked-bluesky="{esc(",".join(blocked["bluesky"]))}"',
        f'data-approved-mastodon="{esc(",".join(approved["mastodon"]))}"',
        f'data-approved-bluesky="{esc(",".join(approved["bluesky"]))}"',
    ]
    if has_mastodon:
        toot_url = f"https://{esc(str(host))}/web/statuses/{esc(str(status_id))}"
        attrs += [
            f'data-mastodon-host="{esc(str(host))}"',
            f'data-mastodon-id="{esc(str(status_id))}"',
            f'data-author-acct="{esc(_author_acct())}"',
            # Mastodon bodies are third-party HTML and need the sanitizer.
            # comments.js is copied to public/ verbatim rather than rendered
            # through the template, so the versioned URL has to reach it as
            # data rather than as a %ASSET_V% placeholder. Emitted only here:
            # a Bluesky-only thread is plain text and never loads DOMPurify.
            f'data-sanitizer-src="/dompurify.min.js?v={_asset_version()}"',
        ]
        links.append((toot_url, "Mastodon"))
    if bluesky_uri:
        skeet_url = bluesky_permalink(bluesky_uri)
        attrs += [
            f'data-bluesky-uri="{esc(bluesky_uri)}"',
            f'data-author-bluesky="{esc(_author_bluesky())}"',
        ]
        links.append((skeet_url, "Bluesky"))

    # "Reply on Mastodon or Bluesky to join in" - each network named and linked,
    # so a reader knows both routes lead to the same thread on this page.
    joined = " or ".join(
        f'<a href="{url}" target="_blank" rel="nofollow noopener">{name}</a>'
        for url, name in links
    )
    lead = "Selected replies from" if curated else "Replies come from"
    sources = " and ".join(name for _, name in links)
    # Only say the thread is moderated where moderation actually applies. On an
    # untouched thread the notice would be noise, and it would imply replies
    # were removed when none were.
    moderation_note = (
        "This thread is moderated, so not every reply appears here."
        if (curated or any_blocked) else ""
    )
    return f"""
        <section class="comments" {' '.join(attrs)}>
            <h2 class="comments-title">Comments</h2>
            <p class="comments-intro">
                {lead} {sources}. Reply on {joined} to join in.
                {moderation_note}
            </p>
            <button type="button" class="comments-load">💬 Load comments</button>
            <noscript>
                <p>Enable JavaScript to load comments, or view the discussion on
                {joined}.</p>
            </noscript>
            <div class="comments-list" aria-live="polite"></div>
        </section>
    """


def _is_remote(src):
    return src.startswith('http://') or src.startswith('https://')


def _captioning_available():
    if llm.IMAGE.get("provider") == "openrouter":
        return bool(os.environ.get("OPENROUTER_API_KEY"))
    return True  # local Ollama - assume reachable


def _guess_mime(path_or_url):
    mime, _ = mimetypes.guess_type(path_or_url)
    return mime or 'image/jpeg'


def _fetch_remote_bytes(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'unprompted-blog/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            mime = resp.headers.get_content_type() or _guess_mime(url)
            return data, mime
    except Exception as exc:
        print(f"   ⚠️  Could not fetch remote image for alt text ({url}): {exc}")
        return None, None


def _optimize_content_asset(disk_path):
    """Optimise one asset already sitting in content/assets, and return
    (path_on_disk, 'assets/<name>') for whatever the post should now point at.

    A conversion writes a new file beside the original and leaves the original
    alone, so the returned name is not always the name that went in - which is
    exactly why this hands back the reference to use rather than assuming the
    caller can reconstruct it.
    """
    new_path, saved = images.optimize(
        disk_path,
        max_width=IMAGE_MAX_WIDTH,
        jpeg_quality=IMAGE_JPEG_QUALITY,
        min_bytes=IMAGE_MIN_BYTES,
    )
    if saved:
        IMAGE_BYTES_SAVED.append(saved)
        print(f"   🗜️  {os.path.basename(disk_path)} → "
              f"{os.path.basename(new_path)} (-{saved // 1024} KB)")
    return new_path, f"assets/{os.path.basename(new_path)}"


def process_content_media():
    if not os.path.exists(CONTENT_DIR):
        return

    content_assets = CONTENT_ASSETS_DIR
    can_caption = _captioning_available()
    if not images.AVAILABLE:
        print("   ⚠️  Image optimisation skipped (needs Pillow: "
              "pip install -r requirements.txt). Images are hosted as-is.")
    if not can_caption:
        print("   ⚠️  Alt-text generation skipped (IMAGE role needs "
              "OPENROUTER_API_KEY). Images are still copied and referenced.")

    for filename in sorted(os.listdir(CONTENT_DIR)):
        if not filename.endswith('.md') or filename in ('index.md', 'changelog.md'):
            continue
        filepath = os.path.join(CONTENT_DIR, filename)
        post_base = os.path.splitext(filename)[0]
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()

        # Collect replacements first, then apply, so overlapping edits to the
        # same file don't disturb match offsets mid-iteration.
        replacements = []  # (original_markdown, new_markdown)

        for m in IMG_RE.finditer(text):
            alt, src, title = m.group(1), m.group(2), m.group(3) or ''
            src = src.strip()
            new_src = src
            alt_needed = not alt.strip()
            image_for_alt = None  # (bytes, mime) to caption from, if needed

            if _is_remote(src):
                if alt_needed and can_caption:
                    data, mime = _fetch_remote_bytes(src)
                    if data:
                        image_for_alt = (data, mime)

            elif src.startswith('assets/'):
                # Already processed on a prior build; source lives in content/assets.
                disk = os.path.join(CONTENT_DIR, src)
                if os.path.exists(disk):
                    # Optimisation runs here too, not just for new images, so
                    # posts published before this step existed get the benefit
                    # on the next build (that is what --rebuild is for). Once
                    # converted, the post points at the .jpg and this is a
                    # header-read no-op on every later build.
                    disk, new_src = _optimize_content_asset(disk)
                    copy_asset_if_changed(disk, os.path.join(PUBLIC_DIR, new_src))
                    if alt_needed and can_caption:
                        with open(disk, 'rb') as fh:
                            image_for_alt = (fh.read(), _guess_mime(disk))
                else:
                    print(f"   ⚠️  Hosted image missing on disk ('{filename}'): {src}")

            else:
                # Brand-new local reference by an arbitrary path (relative to the
                # post, or absolute anywhere on disk).
                resolved = os.path.abspath(os.path.join(os.path.dirname(filepath), src))
                if os.path.exists(resolved):
                    dest_name = f"{post_base}_{os.path.basename(resolved)}"
                    os.makedirs(content_assets, exist_ok=True)
                    hosted = os.path.join(content_assets, dest_name)
                    copy_asset_if_changed(resolved, hosted)
                    # Optimise the hosted copy, never the author's original file
                    # wherever it happens to live on disk.
                    hosted, new_src = _optimize_content_asset(hosted)
                    copy_asset_if_changed(
                        hosted, os.path.join(PUBLIC_ASSETS_DIR, os.path.basename(hosted)))
                    if alt_needed and can_caption:
                        with open(hosted, 'rb') as fh:
                            image_for_alt = (fh.read(), _guess_mime(hosted))
                else:
                    print(f"   ⚠️  Image not found on disk ('{filename}'): {src}")

            new_alt = alt
            if image_for_alt is not None:
                try:
                    generated = llm.generate_alt_text(*image_for_alt)
                    if generated:
                        new_alt = generated
                        print(f"   🖼️  Alt text for {os.path.basename(new_src)}: {generated}")
                except SystemExit:
                    # llm.call_model exits the process on repeated API failure;
                    # don't take the whole build down over one caption.
                    print(f"   ⚠️  Alt-text generation failed for {src} - leaving as-is.")

            if new_alt != alt or new_src != src:
                replacements.append((m.group(0), f"![{new_alt}]({new_src}{title})"))

        if replacements:
            for old, new in replacements:
                text = text.replace(old, new, 1)
            write_file_if_changed(filepath, text)
            print(f"   ✍️  Updated media references in {filename}")


def parse_markdown_file(filepath, valid_slugs=None):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
    if match:
        frontmatter_str = match.group(1)
        markdown_text = match.group(2)
        frontmatter = yaml.safe_load(frontmatter_str)
        if not isinstance(frontmatter, dict):
            raise ValueError(f"Frontmatter in '{filepath}' did not parse to a mapping (got {type(frontmatter).__name__})")
    else:
        frontmatter = {}
        markdown_text = content

    frontmatter.setdefault("title", "Untitled")
    frontmatter.setdefault("date", "Unknown")
    frontmatter.setdefault("tags", [])
    frontmatter.setdefault("summary", "")
    if not isinstance(frontmatter.get("tags"), list):
        frontmatter["tags"] = [frontmatter["tags"]] if frontmatter.get("tags") else []

    # --- IN-MEMORY SELF HEALING LINK PASS ---
    if valid_slugs:
        def clean_dead_links(match_obj):
            anchor_text = match_obj.group(1)
            link_target = match_obj.group(2)
            if link_target.startswith(('http://', 'https://')):
                return match_obj.group(0)
            if link_target not in valid_slugs:
                print(f"🩹 Healing broken link in-memory ('{os.path.basename(filepath)}'): [{anchor_text}]({link_target}) -> '{anchor_text}'")
                return anchor_text
            return match_obj.group(0)
        markdown_text = re.sub(r'\[([^\]]+)\]\(([^)]+\.html)\)', clean_dead_links, markdown_text)
        
    # Local image copying and path rewriting are handled up front by
    # process_content_media(), which normalises every local image to a
    # content-relative 'assets/<name>' path and mirrors it into content/assets/.
    # Here we only make sure those hosted assets are present in public/ for the
    # compiled site (defensive - process_content_media already copies them), and
    # we remember the first one as this post's social-card image (og:image).
    hosted = re.findall(r'!\[.*?\]\(\s*(assets/[^)\s"]+)', markdown_text)
    for local_path in hosted:
        src_disk = os.path.join(CONTENT_DIR, local_path)
        if os.path.exists(src_disk):
            copy_asset_if_changed(src_disk, os.path.join(PUBLIC_DIR, local_path))
    frontmatter['first_image'] = hosted[0] if hosted else None

    # Reading-time estimate from the prose body (before it's transformed into
    # embeds/HTML). Stored on the meta dict so both article pages and post
    # cards can show it without recomputing.
    frontmatter['reading_time'] = reading_time_minutes(markdown_text)

    # Enforce the plain-hyphen voice rule at build time: turn any em-dash or
    # en-dash into ' - ' regardless of which model wrote the post. Whitespace
    # around the dash is collapsed to single spaces; the match is kept on one
    # line so it can never swallow a newline.
    markdown_text = re.sub(r'[ \t]*[—–][ \t]*', ' - ', markdown_text)

    # Turn standalone YouTube links into click-to-play facades before conversion.
    markdown_text = embed_youtube(markdown_text)

    html_content = markdown.markdown(markdown_text, extensions=['extra', 'codehilite'])
    html_content = _absolutize_body(html_content, valid_slugs or set())
    return frontmatter, html_content

# Hosts that count as "us". Absolute self-links (sometimes written by hand to
# survive the healing pass) must keep opening in the same tab, so we compare
# hostnames rather than string-prefixing SITE_URL - that way http:// and www.
# variants of our own domain are still recognised as internal.
_OWN_HOSTS = {urlparse(SITE_URL).netloc.lower().removeprefix('www.')}

def _is_external(href):
    parsed = urlparse(href)
    # Only real web links leave the site; mailto:, tel:, #anchors and
    # root-relative paths all have no http(s) scheme and stay put.
    if parsed.scheme not in ('http', 'https'):
        return False
    return parsed.netloc.lower().removeprefix('www.') not in _OWN_HOSTS

def _externalize_links(html_body):
    # Off-site links open in a new tab. rel="noopener" severs the new page's
    # window.opener handle; noreferrer is deliberately omitted so sites we cite
    # still see us in their referrers.
    def _mark(m):
        href, rest = m.group(1), m.group(2)
        # 'rest' preserves any attributes Markdown added after href (title=, and
        # codehilite's class=). Hand-written HTML that already sets target= is
        # left exactly as the author wrote it.
        if not _is_external(href) or 'target=' in rest:
            return m.group(0)
        return f'<a href="{href}" target="_blank" rel="noopener"{rest}>'
    return re.sub(r'<a href="([^"]+)"([^>]*)>', _mark, html_body)

# One rendered <img> tag, with its attributes captured for inspection.
_IMG_TAG_RE = re.compile(r'<img\b([^>]*?)\s*/?>')

# Dimensions are read once per file per build: the same image is rendered into
# a post page and both feeds, and each lookup is a filesystem hit.
_DIMENSION_CACHE = {}


def _cached_dimensions(disk_path):
    if disk_path not in _DIMENSION_CACHE:
        _DIMENSION_CACHE[disk_path] = images.dimensions(disk_path)
    return _DIMENSION_CACHE[disk_path]


def _enhance_images(html_body):
    """Add width/height and lazy loading to every self-hosted <img>.

    Markdown emits a bare `<img alt src>`, which leaves the browser no way to
    reserve space before the bytes arrive - the text below jumps down as each
    photo lands, which is exactly what Cumulative Layout Shift measures. The
    intrinsic size is on disk, so the build can supply it.

    The attributes are safe alongside the stylesheet's `max-width: 100%;
    height: auto` because that pair tells the browser to treat width/height as
    an aspect ratio rather than a fixed size - the image still scales to the
    column, it just no longer changes the page's height when it arrives.

    Author-written attributes always win: anything that already has a width or
    a loading attribute is left untouched. Remote images are skipped, since
    their size cannot be known without fetching them.
    """
    def _stamp(match):
        attrs = match.group(1)
        src = re.search(r'src="([^"]+)"', attrs)
        if not src:
            return match.group(0)
        path = src.group(1)
        if not path.startswith(('assets/', '/assets/')):
            return match.group(0)

        additions = ''
        if 'width=' not in attrs and 'height=' not in attrs:
            disk = os.path.join(CONTENT_ASSETS_DIR, os.path.basename(path))
            size = _cached_dimensions(disk) if os.path.exists(disk) else None
            if size:
                additions += f' width="{size[0]}" height="{size[1]}"'
        if 'loading=' not in attrs:
            # decoding="async" keeps a large photo off the main thread while it
            # is being decoded, which matters more than it sounds on a phone.
            additions += ' loading="lazy" decoding="async"'
        return f'<img{attrs}{additions}>' if additions else match.group(0)

    return _IMG_TAG_RE.sub(_stamp, html_body)


def _absolutize_body(html_body, valid_slugs):
    # Sizing happens before the src rewrite purely so this reads in pipeline
    # order; _enhance_images accepts either form of the path.
    html_body = _enhance_images(html_body)

    # Images live at /assets/ regardless of the page's depth.
    html_body = re.sub(r'src="assets/', 'src="/assets/', html_body)

    # Bare '<slug>.html' backlinks -> '/posts/<slug>.html' (known slugs only).
    def _fix_link(m):
        target = m.group(1)
        return f'href="{post_href(target)}"' if target in valid_slugs else m.group(0)
    # First char class excludes ", :, / so protocol-absolute and already-root
    # paths are skipped; only bare relative '*.html' hrefs are considered.
    return re.sub(r'href="([^":/][^"]*\.html)"', _fix_link, html_body)

def generate_post_feed_html(posts_list, title_text, current_page=1, total_pages=1, page_href=home_href):
    cards = []
    for post in posts_list:
        tags_html = "".join([f'<a href="{tag_href(slugify_tag(t))}" class="tag-badge">{tag_emoji_html(t)}{esc(t)}</a>' for t in post.get('tags', [])])
        card = f"""
        <div class="post-card">
            <h2><a href="{post_href(post['slug'])}">{esc(post['title'])}</a></h2>
            <p class="summary">{esc(post.get('summary', ''))}</p>
            <div class="meta" style="margin-bottom:0;">
                <span>{post['date']}</span> • <span>{post.get('reading_time', 1)} min read</span> • {tags_html}
            </div>
        </div>
        """
        card = card.strip()
        cards.append(card)

    feed_body = "".join(cards) if cards else "<p>No stories found.</p>"

    pagination_html = ""
    if total_pages > 1:
        prev_link = '<span class="disabled">← Newer</span>'
        next_link = '<span class="disabled">Older →</span>'

        if current_page > 1:
            prev_link = f'<a href="{page_href(current_page-1)}">← Newer</a>'

        if current_page < total_pages:
            next_link = f'<a href="{page_href(current_page+1)}">Older →</a>'

        pagination_html = f"""
        <div class="pagination">
            {prev_link}
            <span class="page-info">Page {current_page} of {total_pages}</span>
            {next_link}
        </div>
        """
        
    return f"<h1>{title_text}</h1>" + feed_body + pagination_html

def render_standalone_pages(base_template, sitemap_urls):
    """Render content_pipeline/pages/*.md to /<name>.html at the site root.

    Standalone pages (about, colophon) are not posts: no date, no tags, no
    backlinking, no feed or archive entry. They go through the same base.html
    as everything else, so the header, footer, search and theme toggle come
    from one template instead of a copy that quietly stops matching the site.

    The filename is the URL: pages/about.md -> /about.html. Frontmatter takes
    'title' and 'description'; the body is plain Markdown."""
    if not os.path.isdir(PAGES_DIR):
        return
    for filename in sorted(os.listdir(PAGES_DIR)):
        if not filename.endswith('.md'):
            continue
        name = filename[:-3]
        filepath = os.path.join(PAGES_DIR, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
        if match:
            meta = yaml.safe_load(match.group(1)) or {}
            body_md = match.group(2)
        else:
            meta, body_md = {}, content
        if not isinstance(meta, dict):
            raise SystemExit(
                f"❌ Frontmatter in '{filepath}' did not parse to a mapping."
            )

        title = str(meta.get('title', name.replace('-', ' ').title()))
        description = str(meta.get('description', SITE_DESCRIPTION))
        body_html = markdown.markdown(body_md, extensions=['extra', 'codehilite'])
        # Standalone pages don't go through _absolutize_body (they have no
        # backlink healing to do), so they need the image sizing applied here.
        body_html = _enhance_images(body_html)

        page_html = safe_render(base_template, {
            "%PAGE_TITLE%": f"{esc(title)} — {esc(SITE_NAME)}",
            "%META_DESCRIPTION%": esc(description),
            "%OG_TYPE%": "website",
            "%PAGE_SLUG%": f"{name}.html",
            "%STRUCTURED_DATA%": "",
            "%MAIN_CONTENT%": f'<div class="article-content page-content">{body_html}</div>',
        })
        write_file_if_changed(os.path.join(PUBLIC_DIR, f"{name}.html"), page_html)
        sitemap_urls.append(f"{SITE_URL}/{name}.html")
        print(f"📄 Rendered standalone page: /{name}.html")


# Theme files the template links by name. Their URLs carry a version query so
# they can be cached for a year and still update the moment they change.
_VERSIONED_ASSETS = ('style.css', 'fonts.css', 'search.js', 'comments.js',
                     'dompurify.min.js')

_ASSET_VERSION = None


def _asset_version():
    """A short hash of the theme files, used as ?v= on every link to them.

    Without this the stylesheet cannot safely be cached: its URL never changes,
    so any long max-age leaves returning readers on the old CSS until it
    expires. With it, the URL changes exactly when the bytes do, which is what
    makes the year-long immutable caching in htaccess_content() correct rather
    than reckless.

    One version covers all four files rather than one each. They change
    together rarely, and the cost of over-invalidating is re-fetching a few
    tens of kilobytes on the rare build where the theme was touched.
    """
    global _ASSET_VERSION
    if _ASSET_VERSION is not None:
        return _ASSET_VERSION
    digest = hashlib.sha256()
    for name in _VERSIONED_ASSETS:
        # Later static sources shadow earlier ones on disk (publish.sh copies
        # them in order), so the last existing copy is the one actually served.
        served = None
        for directory in STATIC_SOURCE_DIRS:
            candidate = os.path.join(directory, name)
            if os.path.exists(candidate):
                served = candidate
        if served:
            with open(served, 'rb') as fh:
                digest.update(fh.read())
    _ASSET_VERSION = digest.hexdigest()[:8]
    return _ASSET_VERSION


def safe_render(template, mappings):
    # 1. Ensure SITE_URL is configured for the outer template layout
    if "%SITE_URL%" not in mappings:
        mappings["%SITE_URL%"] = SITE_URL
    # Site identity from site.yaml, injected into every page. Escaped because
    # these are author-supplied and land inside HTML attributes - an apostrophe
    # in a site name or a stray quote in the footer text would otherwise break
    # out of a content="..." and mangle the markup.
    for key, value in (
        ("%SITE_NAME%", SITE_NAME),
        ("%AUTHOR_EMAIL%", AUTHOR_EMAIL),
        ("%LINK_ABOUT%", LINK_ABOUT),
        ("%AI_LABEL%", AI_LABEL),
        ("%AI_EXPLAINER%", AI_EXPLAINER),
        ("%AI_EXPLAINER_URL%", AI_EXPLAINER_URL),
    ):
        mappings.setdefault(key, esc(value))
    # Both social links are the whole element, not just an href: each is
    # optional, and a site that uses neither must emit no link at all rather
    # than an <a> pointing nowhere. rel="me" is what lets the profile on the
    # other end verify this domain back.
    mappings.setdefault("%LINK_MASTODON_ITEM%", (
        f'<a href="{esc(LINK_MASTODON)}" target="_blank" rel="me noopener">Mastodon</a>'
        if LINK_MASTODON else ''
    ))
    mappings.setdefault("%LINK_BLUESKY_ITEM%", (
        f'<a href="{esc(LINK_BLUESKY)}" target="_blank" rel="me noopener">Bluesky</a>'
        if LINK_BLUESKY else ''
    ))
    # Same reasoning one level up: the meta tag exists to make Mastodon show the
    # author's handle on link previews, so with no handle configured the whole
    # tag goes rather than shipping content="" on every page.
    mappings.setdefault("%FEDIVERSE_CREATOR_META%", (
        f'<meta name="fediverse:creator" content="{esc(FEDIVERSE_CREATOR)}">'
        if FEDIVERSE_CREATOR else ''
    ))
    # Cache-busting stamp for the theme files (see _asset_version). Not escaped
    # because it is a hex digest this build computed, not author input.
    mappings.setdefault("%ASSET_V%", _asset_version())
    # The comments renderer is only fetched by pages that actually have a
    # thread, so it defaults to nothing (same shape as %OG_IMAGE_TAGS% below).
    if "%COMMENTS_SCRIPT%" not in mappings:
        mappings["%COMMENTS_SCRIPT%"] = ""
    # Only posts with an image of their own emit og:image/twitter:image tags.
    # Everything else (homepage, tag pages, and imageless posts) omits them.
    if "%OG_IMAGE_TAGS%" not in mappings:
        mappings["%OG_IMAGE_TAGS%"] = ""
    # Per-page extra RSS alternate link (tag pages point at their tag feed).
    # Defaults empty so article/home pages only advertise the main feed.
    if "%ALT_FEED_LINK%" not in mappings:
        mappings["%ALT_FEED_LINK%"] = ""

    # 2. POP the main content out of the dictionary to isolate and protect it
    main_content = mappings.pop("%MAIN_CONTENT%", None)
    
    # 3. Safely render the outer skeletal layout (Head tags, titles, links)
    output = template
    for key, value in mappings.items():
        output = output.replace(key, str(value))
        
    # 4. Inject the protected article body as the final, isolated step
    if main_content is not None:
        output = output.replace("%MAIN_CONTENT%", str(main_content))

    return output

def rss_item_xml(post):
    title_escaped = html.escape(post['title'], quote=True)
    summary_escaped = html.escape(post.get('summary', ''), quote=True)

    # Grab the HTML body and promote its already root-absolute asset/link URLs
    # (e.g. /assets/x.png, /posts/y.html - see _absolutize_body) to fully
    # qualified ones so images and internal navigation work inside a feed reader.
    body_content = post.get('html_body', '')
    body_content = re.sub(r'src="/assets/', f'src="{SITE_URL}/assets/', body_content)
    body_content = re.sub(r'href="(/[^"]+\.html)"', f'href="{SITE_URL}\\1"', body_content)

    try:
        dt = datetime.strptime(str(post['date']), "%Y-%m-%d")
        rss_date = email.utils.formatdate(dt.timestamp(), usegmt=True)
    except (ValueError, TypeError):
        rss_date = email.utils.formatdate(usegmt=True)

    return f"""        <item>
            <title>{title_escaped}</title>
            <link>{SITE_URL}{post_href(post['slug'])}</link>
            <guid isPermaLink="true">{SITE_URL}{post_href(post['slug'])}</guid>
            <description>{summary_escaped}</description>
            <content:encoded><![CDATA[{body_content}]]></content:encoded>
            <pubDate>{rss_date}</pubDate>
        </item>"""

def rss_feed_xml(posts_list, feed_title, feed_desc, self_path, last_build_date_rfc):
    channels = "\n".join(rss_item_xml(p) for p in posts_list)
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
    <title>{html.escape(feed_title, quote=True)}</title>
    <link>{SITE_URL}/index.html</link>
    <description>{html.escape(feed_desc, quote=True)}</description>
    <language>en-us</language>
    <lastBuildDate>{last_build_date_rfc}</lastBuildDate>
    <atom:link href="{SITE_URL}/{self_path}" rel="self" type="application/rss+xml" />
{channels}
</channel>
</rss>"""

def feed_build_date(posts_list):
    if posts_list:
        try:
            dt = datetime.strptime(str(posts_list[0]['date']), "%Y-%m-%d")
            return email.utils.formatdate(dt.timestamp(), usegmt=True)
        except (ValueError, TypeError):
            pass
    return email.utils.formatdate(usegmt=True)

def build_site():
    # Load HTML layout template from external file
    try:
        base_template = load_template()
    except Exception as exc:
        print(exc)
        return

    # FIXED: We do NOT delete 'public/' on each compile! Doing so breaks file system timestamps.
    os.makedirs(PUBLIC_DIR, exist_ok=True)

    posts = []
    tags_map = {}
    sitemap_urls = [f"{SITE_URL}/index.html"]

    if not os.path.exists(CONTENT_DIR):
        os.makedirs(CONTENT_DIR)

    # Images first: copy local images into assets/, self-host paths in the
    # source .md, and generate any missing alt text - all before parsing.
    print("🖼️  Processing images (copy to assets/, optimise, generate alt text)...")
    process_content_media()
    if IMAGE_BYTES_SAVED:
        print(f"   🗜️  Optimised {len(IMAGE_BYTES_SAVED)} image(s), "
              f"saving {sum(IMAGE_BYTES_SAVED) / 1024 / 1024:.1f} MB. Originals "
              f"are kept in {os.path.relpath(CONTENT_ASSETS_DIR, REPO_ROOT)} and can be "
              f"deleted once you are happy with the result.")

    # Build the set of legal internal link targets BEFORE parsing any post, so
    # the in-memory link-healing pass sees every real slug. Slugs come from
    # frontmatter (falling back to filename), matching how each page is emitted.
    valid_slugs = {
        read_slug(os.path.join(CONTENT_DIR, f), f)
        for f in os.listdir(CONTENT_DIR)
        if f.endswith('.md') and f not in ['index.md', 'changelog.md']
    }
    print(f"🔍 Registered {len(valid_slugs)} valid markdown targets for context-aware link verification.")

    skipped_files = []
    for filename in os.listdir(CONTENT_DIR):
        if filename.endswith('.md') and filename not in ['index.md', 'changelog.md']:
            filepath = os.path.join(CONTENT_DIR, filename)
            try:
                meta, html_body = parse_markdown_file(filepath, valid_slugs=valid_slugs)
            except Exception as exc:
                print(f"⚠️  Skipping '{filename}' — failed to parse: {exc}")
                skipped_files.append(filename)
                continue

            slug = slug_for(filename, meta)
            meta['slug'] = slug
            meta['file'] = filename
            meta['html_body'] = html_body
            posts.append(meta)
            sitemap_urls.append(f"{SITE_URL}{post_href(slug)}")
            
            for tag in meta.get('tags', []):
                if tag not in tags_map:
                    tags_map[tag] = []
                tags_map[tag].append(meta)

    if skipped_files:
        print(f"⚠️  {len(skipped_files)} file(s) skipped due to parse errors: {', '.join(skipped_files)}")

    posts.sort(key=lambda x: str(x.get('date', '')), reverse=True)

    # Checked once, against every post, before any page is written.
    validate_comment_moderation(moderation_key(p) for p in posts)

    for idx, meta in enumerate(posts):
        tags_html = "".join([f'<a href="{tag_href(slugify_tag(t))}" class="tag-badge">{tag_emoji_html(t)}{esc(t)}</a>' for t in meta.get('tags', [])])

        nav_html = ""
        prev_block = ""
        next_block = ""

        if idx < len(posts) - 1:
            older_post = posts[idx + 1]
            prev_block = f"""
            <a href="{post_href(older_post['slug'])}" class="article-nav-block prev">
                <span class="article-nav-label">← Previous Story</span>
                <span class="article-nav-title">{esc(older_post['title'])}</span>
            </a>
            """

        if idx > 0:
            newer_post = posts[idx - 1]
            next_block = f"""
            <a href="{post_href(newer_post['slug'])}" class="article-nav-block next">
                <span class="article-nav-label">Next Story →</span>
                <span class="article-nav-title">{esc(newer_post['title'])}</span>
            </a>
            """
            
        if prev_block or next_block:
            nav_html = f"""
            <nav class="article-nav-bounds">
                {prev_block if prev_block else '<div style="flex:1;"></div>'}
                {next_block if next_block else '<div style="flex:1;"></div>'}
            </nav>
            """

        comments_html = build_comments_block(meta)

        article_html = f"""
            <h1>{esc(meta['title'])}</h1>
            <div class="meta">
                <span>{meta['date']}</span> • <span>{meta['reading_time']} min read</span> • <span>{tags_html}</span>
            </div>
            <div class="article-content">
                {_externalize_links(meta['html_body'])}
            </div>
            {nav_html}
            {comments_html}
        """

        # Social-card image: only posts that contain an image of their own get
        # og:image/twitter:image tags. There is no site-default card. The URL is
        # absolute so scrapers (Mastodon, LinkedIn, Slack) resolve it regardless
        # of the page it's embedded on.
        first_image = meta.get('first_image')
        og_image = f"{SITE_URL}/{first_image}" if first_image else None
        if og_image:
            og_image_tags = (
                f'<meta property="og:image" content="{esc(og_image)}">\n'
                f'    <meta property="og:image:alt" content="{esc(SITE_NAME)}">\n'
                f'    <meta name="twitter:card" content="summary_large_image">\n'
                f'    <meta name="twitter:image" content="{esc(og_image)}">'
            )
        else:
            og_image_tags = ""

        schema_data = {
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "headline": meta['title'],
            "datePublished": str(meta['date']),
            "description": meta.get('summary', ''),
            "author": {
                "@type": "Person",
                "name": AUTHOR_NAME,
                "url": SITE_URL
            }
        }
        if og_image:
            schema_data["image"] = og_image
        json_ld = f'<script type="application/ld+json">{json.dumps(schema_data)}</script>'

        full_html = safe_render(base_template, {
            "%PAGE_TITLE%": f"{esc(meta['title'])} — {esc(SITE_NAME)}",
            "%META_DESCRIPTION%": esc(meta.get('summary', f'An article from {SITE_NAME} blog.')),
            "%OG_TYPE%": "article",
            "%PAGE_SLUG%": f"{POSTS_DIR}/{meta['slug']}",
            "%OG_IMAGE_TAGS%": og_image_tags,
            "%STRUCTURED_DATA%": json_ld,
            "%COMMENTS_SCRIPT%": (
                f'<script src="/comments.js?v={_asset_version()}" defer></script>'
                if comments_html else ''
            ),
            "%MAIN_CONTENT%": article_html
        })

        # FIXED: Utilizing smart compilation writer
        write_file_if_changed(os.path.join(PUBLIC_DIR, POSTS_DIR, meta['slug']), full_html)

    sorted_tags = sorted(tags_map.keys(), key=lambda t: len(tags_map[t]), reverse=True)

    tag_pills_visible = []
    tag_pills_hidden = []
    
    for i, tag in enumerate(sorted_tags):
        count = len(tags_map[tag])
        tag_slug = slugify_tag(tag)
        pill = f'<a href="{tag_href(tag_slug)}" class="tag-pill">{tag_emoji_html(tag)}{esc(tag)}<span class="tag-count">({count})</span></a>'

        if i < VISIBLE_TAGS:
            tag_pills_visible.append(pill)
        else:
            tag_pills_hidden.append(pill)

        sitemap_urls.append(f"{SITE_URL}{tag_href(tag_slug)}")
        
    hidden_tags_html = ""
    if tag_pills_hidden:
        hidden_tags_html = f"""
        <details class="more-tags">
            <summary>Show more topics...</summary>
            <div class="tags-hidden-wrapper">
                {"".join(tag_pills_hidden)}
            </div>
        </details>
        """

    tag_cloud_html = f"""
    <div class="tag-cloud-container">
        <div class="tag-cloud-title">Recommended Topics</div>
        <div class="tag-cloud">
            {"".join(tag_pills_visible) if tag_pills_visible else "<p style='color:var(--text-muted); font-size:14px;'>No topics discovered yet.</p>"}
            {hidden_tags_html}
        </div>
    </div>
    """

    # JSON updates remain standard writes since they aren't part of 'public/' deployment
    manifest_posts = [
        {
            "title": post.get('title', 'Untitled'),
            "slug": post['slug'],
            # On-disk content filename, so ingest's weave step can read a
            # candidate's source text without assuming filename == slug.
            "file": post.get('file'),
            "tags": post.get('tags', []),
            "summary": post.get('summary', ''),
            # 'themes' is the editorial, coverage-oriented line used to triage
            # backlink candidates during ingestion. Older posts without it fall
            # back to the reader-facing summary so triage still has something.
            "themes": post.get('themes') or post.get('summary', '')
        }
        for post in posts
    ]
    with open(LINK_MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest_posts, f, indent=2)
        
    with open(EXISTING_TAGS_PATH, "w", encoding="utf-8") as f:
        json.dump(list(tags_map.keys()), f, indent=2)

    total_posts = len(posts)
    total_home_pages = max(1, (total_posts + PAGE_SIZE - 1) // PAGE_SIZE)

    for page_idx in range(total_home_pages):
        current_page_num = page_idx + 1
        start_sub = page_idx * PAGE_SIZE
        end_sub = start_sub + PAGE_SIZE
        chunk = posts[start_sub:end_sub]
        
        feed_html = generate_post_feed_html(chunk, "Latest Stories", current_page=current_page_num, total_pages=total_home_pages, page_href=home_href)
        homepage_content = tag_cloud_html + feed_html
        
        index_file_name = "index.html" if current_page_num == 1 else f"index-{current_page_num}.html"
        if current_page_num > 1:
            sitemap_urls.append(f"{SITE_URL}/{index_file_name}")
            
        index_html = safe_render(base_template, {
            "%PAGE_TITLE%": f"{esc(SITE_NAME)} — Home (Page {current_page_num})" if current_page_num > 1 else f"{esc(SITE_NAME)} — Home",
            "%META_DESCRIPTION%": esc(SITE_DESCRIPTION),
            "%OG_TYPE%": "website",
            "%PAGE_SLUG%": index_file_name,
            "%STRUCTURED_DATA%": "",
            "%MAIN_CONTENT%": homepage_content
        })
        # FIXED: Utilizing smart compilation writer
        write_file_if_changed(os.path.join(PUBLIC_DIR, index_file_name), index_html)

    for tag, tagged_posts in tags_map.items():
        tagged_posts.sort(key=lambda x: str(x.get('date', '')), reverse=True)
        tag_slug = slugify_tag(tag)
        
        total_tag_posts = len(tagged_posts)
        total_tag_pages = max(1, (total_tag_posts + PAGE_SIZE - 1) // PAGE_SIZE)
        
        for page_idx in range(total_tag_pages):
            current_page_num = page_idx + 1
            start_sub = page_idx * PAGE_SIZE
            end_sub = start_sub + PAGE_SIZE
            chunk = tagged_posts[start_sub:end_sub]
            
            heading = (
                f"Stories tagged in {tag_emoji_html(tag)}'{esc(tag)}' "
                f'<a class="feed-subscribe" href="{tag_feed_href(tag_slug)}" title="Subscribe to the \'{esc(tag)}\' RSS feed">'
                f'<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 11a9 9 0 0 1 9 9"></path><path d="M4 4a16 16 0 0 1 16 16"></path><circle cx="5" cy="19" r="1"></circle></svg>'
                f'RSS</a>'
            )
            tag_feed_html = generate_post_feed_html(chunk, heading, current_page=current_page_num, total_pages=total_tag_pages, page_href=lambda n, s=tag_slug: tag_href(s, n))

            tag_file_name = tag_page_name(tag_slug, current_page_num)
            if current_page_num > 1:
                sitemap_urls.append(f"{SITE_URL}{tag_href(tag_slug, current_page_num)}")

            alt_feed_link = (
                f'<link rel="alternate" type="application/rss+xml" '
                f'title="{esc(SITE_NAME)} - {esc(tag)} RSS Feed" '
                f'href="{SITE_URL}{tag_feed_href(tag_slug)}" />'
            )
            tag_html = safe_render(base_template, {
                "%PAGE_TITLE%": f"Topic: {esc(tag)} (Page {current_page_num}) — {esc(SITE_NAME)}",
                "%META_DESCRIPTION%": f"Explore articles matching the topic '{esc(tag)}' on {esc(SITE_NAME)}.",
                "%OG_TYPE%": "website",
                "%PAGE_SLUG%": f"{TAGS_DIR}/{tag_file_name}",
                "%ALT_FEED_LINK%": alt_feed_link,
                "%STRUCTURED_DATA%": "",
                "%MAIN_CONTENT%": tag_feed_html
            })
            # FIXED: Utilizing smart compilation writer
            write_file_if_changed(os.path.join(PUBLIC_DIR, TAGS_DIR, tag_file_name), tag_html)
            
    # --- AUTOMATED SITEMAP OUT ---
    render_standalone_pages(base_template, sitemap_urls)

    sitemap_entries = "".join([f"<url><loc>{url}</loc></url>" for url in sitemap_urls])
    sitemap_xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{sitemap_entries}</urlset>'
    # FIXED: Utilizing smart compilation writer
    write_file_if_changed(os.path.join(PUBLIC_DIR, "sitemap.xml"), sitemap_xml)
    
    # --- FULL-TEXT RSS FEED GENERATION ---
    # Main feed: the 15 most recent posts across all topics. Its build date is
    # the site-wide newest post (that's what this feed's content reflects).
    rss_full_xml = rss_feed_xml(
        posts[:FEED_ITEMS],
        SITE_NAME,
        FEED_DESCRIPTION,
        "feed.xml",
        feed_build_date(posts),
    )
    # FIXED: Utilizing smart compilation writer
    write_file_if_changed(os.path.join(PUBLIC_DIR, "feed.xml"), rss_full_xml)

    # Per-tag feeds: one feed.xml per topic, so a reader can subscribe to just
    # the subjects they care about. Same 15-item cap and full-content format as
    # the main feed. tags_map is already populated and its lists were sorted
    # newest-first when the tag pages were built above. Each feed's build date is
    # its OWN newest post, so editing one post only re-uploads the feeds it's
    # actually in - not every unrelated tag feed.
    for tag, tagged_posts in tags_map.items():
        tag_slug = slugify_tag(tag)
        tag_feed_xml = rss_feed_xml(
            tagged_posts[:FEED_ITEMS],
            f"{SITE_NAME} - {tag}",
            f"Posts tagged '{tag}' on {SITE_NAME}.",
            # self_path is relative to SITE_URL; strip the leading '/'.
            tag_feed_href(tag_slug).lstrip('/'),
            feed_build_date(tagged_posts),
        )
        write_file_if_changed(os.path.join(PUBLIC_DIR, FEEDS_DIR, tag_feed_name(tag_slug)), tag_feed_xml)

    # --- CLIENT-SIDE SEARCH INDEX ---
    # Full-text index consumed by static/search.js: title/summary/tags/date for
    # ranking and display, plus the article body stripped to plain text so search
    # matches words from anywhere in a post, not just the summary. It grows
    # roughly linearly with total prose; it's served gzipped and lazy-loaded (see
    # htaccess_content() and static/search.js), so readers only fetch it when they
    # actually search. To cap growth on a large archive, slice plain_text(...).
    search_index = [
        {
            "title": p.get('title', 'Untitled'),
            "url": post_href(p['slug']),
            "date": str(p.get('date', '')),
            "tags": p.get('tags', []),
            "summary": p.get('summary', ''),
            "text": plain_text(p.get('html_body', '')),
        }
        for p in posts
    ]
    write_file_if_changed(
        os.path.join(PUBLIC_DIR, "search-index.json"),
        json.dumps(search_index, ensure_ascii=False, separators=(',', ':')),
    )

    # --- LEGACY URL REDIRECTS + PERFORMANCE HEADERS ---
    # One generated .htaccess: 301s from the old flat URLs to the sectioned
    # layout, plus text compression and cache headers for the search assets.
    write_file_if_changed(os.path.join(PUBLIC_DIR, ".htaccess"), htaccess_content())

    # --- AUTOMATIC CLEANUP OF STALE & REMOVED FILES ---
    # Files that publish.sh will inject into public/ AFTER this build, from each
    # of the static source directories (the engine theme, then the site's own
    # files). They aren't in GENERATED_FILES because this build didn't write
    # them, so without this whitelist the sweep below deletes them every run -
    # and silently, since the next copy restores them with mtimes intact and the
    # FTP mirror never sees a difference. The relpath base is the per-source dir,
    # NOT a single literal, or the resulting paths point outside public/ and the
    # whitelist protects nothing.
    static_targets = set()
    for static_dir in STATIC_SOURCE_DIRS:
        if not os.path.exists(static_dir):
            continue
        for root, _, files in os.walk(static_dir):
            for file in files:
                rel_path = os.path.relpath(os.path.join(root, file), static_dir)
                static_targets.add(os.path.abspath(os.path.join(PUBLIC_DIR, rel_path)))

    # Sweep and destroy obsolete files in public/
    for root, _, files in os.walk(PUBLIC_DIR):
        for file in files:
            full_path = os.path.abspath(os.path.join(root, file))
            if full_path not in GENERATED_FILES and full_path not in static_targets:
                print(f"🗑️ Cleaned up stale file: {os.path.relpath(full_path, PUBLIC_DIR)}")
                try:
                    os.remove(full_path)
                except Exception as exc:
                    print(f"⚠️ Failed to delete stale file '{full_path}': {exc}")

    # Clean up empty subdirectories left behind by deleted files
    for root, dirs, _ in os.walk(PUBLIC_DIR, topdown=False):
        for d in dirs:
            dir_path = os.path.join(root, d)
            try:
                if not os.listdir(dir_path):
                    os.rmdir(dir_path)
            except Exception:
                pass
        
    print(f"🚀 Compiled {len(posts)} posts cleanly. Manifest files up to date.")

if __name__ == "__main__":
    build_site()
