"""Announce newly published posts on Mastodon and Bluesky, once each.

Run at publish time, after the site is already live (so the announcement's link
resolves and Mastodon can render a link-preview card). For every post in
content/ whose frontmatter carries 'announce: pending', this:

  1. Posts to each configured network ('<prefix>: <title>' + summary +
     canonical URL + one hashtag per article tag). The prefix is '#blog' for an
     article and '#podcast' for an episode, both overridable per site under
     'announce:' in site.yaml - a listener following the podcast hashtag should
     not have to filter the writing out of it, and vice versa. The same split
     decides the language the status is tagged with (announce.language /
     announce.podcast_language, defaulting to site.language and
     podcast.language), because the announcement is the post's own title and
     summary and a site can write in one language and record in another.
  2. Bookmarks the post, so it survives any auto-cleanup of old statuses.
  3. Writes the resulting coordinates into the frontmatter - 'mastodon_host' +
     'mastodon_id' and/or 'bluesky_uri' - which the site build turns into the
     comment thread for that article. Replies from both networks are blended
     into one thread (see engine/templates/static/comments.js).

Eligibility is per provider: a post is announced on a given network when it is
marked pending AND has no coordinates for that network yet, and the marker is
removed only once every *enabled* network has them. That makes the whole thing
re-runnable and survives partial failure - if Bluesky is down but Mastodon is
not, the toot goes out, the post stays pending, and the next run posts only the
missing half. Posts predating this feature carry no marker and are never
touched, so enabling a new network does not retro-announce the archive.

Configuration comes from the environment (sourced from publish.local.sh). Each
network is optional and independently skipped when unset:

  MASTODON_SERVER       e.g. mastodon.social   (host of your instance)
  MASTODON_TOKEN        access token with write:statuses + write:bookmarks
  MASTODON_ID           your @handle, used only for logging

  BLUESKY_HANDLE        e.g. you.bsky.social
  BLUESKY_APP_PASSWORD  an app password (bsky.app -> Settings -> App Passwords),
                        NOT your account password
  BLUESKY_PDS           optional; defaults to bsky.social

With nothing configured, announcing is skipped with a warning - the build and
deploy still succeed. Exit code is 2 when any frontmatter changed (so publish.sh
knows a rebuild + re-sync is needed), 0 otherwise.
"""

import os
import re
import sys
import json
import time
import datetime
import urllib.parse
import urllib.request

# Imported from urls.py rather than hard-coded so the canonical URL in the
# announcement always matches the site's own links.
# No hardcoded fallback for these. There used to be one, holding a duplicate
# copy of the site URL - which meant a broken import was undetectable while the
# two happened to agree, and would have silently announced a wrong link once they
# did not. An announcement cannot be un-published, so failing here is the safer
# outcome.
from urls import SITE_URL, POSTS_DIR

# Deliberately NOT inside the try/except above: that fallback exists so a broken
# import can't stop an announcement going out with the right URL, but a
# filesystem layout we can't resolve means we'd scan the wrong directory and
# silently announce nothing. Better to fail loudly at import time.
from paths import CONTENT_DIR

# The two announcement prefixes, so which hashtag opens a post is the site's
# decision rather than this file's, and the two languages they are posted under
# (see config._announce_prefix / config._announce_language).
from config import (ANNOUNCE_PREFIX, ANNOUNCE_PODCAST_PREFIX,
                    ANNOUNCE_LANGUAGE, ANNOUNCE_PODCAST_LANGUAGE)

# Only for is_audio(): deciding whether a post is an episode uses exactly the
# same list of extensions the build does, so the hashtag can never disagree
# with what actually ends up in the podcast feed.
import audio

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

# Seconds to wait between two successive announcements. Neither network's hard
# rate limit needs this - Mastodon allows 300 statuses per 3 hours and Bluesky
# far more - but posting a backlog back-to-back looks like spam rather than like
# a person, and an account with no posting history behind it has no reputation
# to absorb that. The pause only ever applies *between* posts, so the common
# case of one new post per publish is unaffected. Set ANNOUNCE_DELAY_SECONDS=0
# to disable.
ANNOUNCE_DELAY_SECONDS = float(os.environ.get("ANNOUNCE_DELAY_SECONDS", "15"))

# Bluesky's post text is capped at 300 grapheme clusters AND 3000 UTF-8 bytes,
# both enforced server-side. The stdlib cannot count graphemes, so the budget is
# measured in codepoints instead: a codepoint count is never lower than a
# grapheme count (combining marks and emoji ZWJ sequences only ever collapse
# several codepoints into one grapheme), so staying under this is always safe.
BLUESKY_MAX_CHARS = 300
BLUESKY_MAX_BYTES = 3000

# Mastodon's default status limit. This used to be treated as "generous enough
# not to need trimming", which held right up until a post with 17 tags produced
# a 549-character announcement and the instance answered 422. The hashtag list
# scales with the article's tags, so the ceiling is reachable by ordinary posts.
# Instances may configure a different limit (some allow far more); 500 is the
# default and the safe assumption, and ANNOUNCE_MASTODON_MAX_CHARS overrides it
# for an instance that is known to differ.
MASTODON_MAX_CHARS = int(os.environ.get("ANNOUNCE_MASTODON_MAX_CHARS", "500"))

# Mastodon substitutes a fixed weight for every URL when it measures a status,
# so a long canonical link costs no more than a short one. Counting the raw
# string instead would under-estimate the room available and trim posts that fit.
MASTODON_URL_WEIGHT = 23


def _request(url, data=None, headers=None):
    """Send a request and return the parsed JSON response.

    Transient failures are retried with backoff. Raises on final failure so the
    caller can leave the post pending and move on to the next one."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                # createBookmark and friends answer 200 with an empty body.
                return json.loads(body) if body.strip() else {}
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                backoff = 2 ** attempt
                print(f"   ⚠️  Request failed (attempt "
                      f"{attempt}/{MAX_RETRIES}): {e} - retrying in {backoff}s...")
                time.sleep(backoff)
    raise last_error


def parse_frontmatter(text):
    """Split into (frontmatter_str, body). Returns (None, text) when absent.
    Mirrors ingest.parse_frontmatter so both tools treat posts identically."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
    if match:
        return match.group(1), match.group(2)
    return None, text


def _fm_value(fm, key):
    """Read a scalar frontmatter value (handles optional surrounding quotes)."""
    m = re.search(rf'^{key}:\s*(.+?)\s*$', fm, re.MULTILINE)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def _fm_tags(fm):
    """Parse the 'tags:' line, which ingest.py writes as a JSON array."""
    m = re.search(r'^tags:\s*(.+?)\s*$', fm, re.MULTILINE)
    if not m:
        return []
    try:
        val = json.loads(m.group(1))
        return val if isinstance(val, list) else [val]
    except Exception:
        return []


def _standalone_link(line):
    """The link target of a line that is *nothing but* a link, else None.

    A deliberate copy of build_blog._standalone_link rather than an import:
    announce.py runs against the markdown on disk and must not drag the whole
    build (and its config validation) in behind a hashtag. The rule it encodes
    is six lines long and has not changed since audio embedding was added; if
    it ever does, these two must be changed together."""
    stripped = line.strip()
    md_link = re.fullmatch(r'\[[^\]]*\]\(\s*(\S+?)\s*\)', stripped)
    if md_link:
        return md_link.group(1)
    if re.fullmatch(r'https?://\S+', stripped):
        return stripped
    return None


def is_episode(fm, body):
    """True when this post is a podcast episode.

    Mirrors build_blog.embed_audio's rule - a standalone audio link on its own
    line makes a post an episode, the first one wins - because that is what
    decides whether the post gets an <enclosure> in the podcast feed. Announcing
    by a different rule would eventually tag something '#podcast' that no
    podcast app can subscribe to.

    'podcast: false' opts an episode out of the show while keeping its player,
    so such a post is announced as an article: the hashtag follows the feed, not
    the audio file.

    Not derived from 'episode_number': that key is optional and author-written
    (nothing in ingest.py or fetch_podlove.py sets it), so an episode without
    one would silently announce as an article."""
    if str(_fm_value(fm, "podcast") or "").strip().lower() == "false":
        return False
    for line in body.split("\n"):
        src = _standalone_link(line)
        if src and audio.is_audio(src):
            return True
    return False


def hashtagify(tag):
    """Turn an article tag into a valid hashtag.

    Hashtags can't contain spaces or punctuation on either network, so
    'AI Ethics' becomes '#AIEthics' - the words are joined in CamelCase, which
    also keeps them readable and screen-reader friendly. Returns '' for tags
    that reduce to nothing (so they can be filtered out)."""
    words = re.findall(r'[A-Za-z0-9]+', tag)
    if not words:
        return ""
    # Preserve an already-capitalised word (AI, API); otherwise title-case it.
    cased = [w if w[:1].isupper() else w.capitalize() for w in words]
    return "#" + "".join(cased)


def build_announcement(title, summary, url, tags, limit=None,
                       prefix=ANNOUNCE_PREFIX):
    """Compose the announcement text: the prefix hashtag, title, summary, the
    canonical link, then one hashtag per tag.

    `prefix` is ANNOUNCE_PREFIX for an article and ANNOUNCE_PODCAST_PREFIX for
    an episode; the caller decides which (see is_episode). It is counted against
    `limit` like any other text, so a longer prefix costs hashtags at the margin
    rather than overflowing the post.

    Both networks pass a `limit` - Bluesky's 300 and Mastodon's 500 are both
    reachable, the latter by nothing more exotic than an article carrying a lot
    of tags. The text is trimmed to fit in priority order: hashtags go first,
    then the summary is truncated, and only if the title alone still overflows
    is it cut. The title and the URL are what the announcement is *for*, so they
    are the last things sacrificed. Callers measure the limit the way their
    network does (see MASTODON_URL_WEIGHT); here it is a plain character count."""
    hashtags = " ".join(h for h in (hashtagify(t) for t in tags) if h)

    def assemble(summary_text, tag_text):
        parts = [f"{prefix}: {title}"]
        if summary_text:
            parts.append(summary_text)
        parts.append(url)
        if tag_text:
            parts.append(tag_text)
        return "\n\n".join(parts)

    text = assemble(summary, hashtags)
    if limit is None or len(text) <= limit:
        return text

    text = assemble(summary, "")
    if len(text) <= limit:
        return text

    # Trim the summary to whatever room is left, on a word boundary.
    overflow = len(text) - limit + 1      # +1 for the ellipsis
    if summary and overflow < len(summary):
        trimmed = summary[:len(summary) - overflow].rstrip()
        trimmed = trimmed.rsplit(" ", 1)[0] if " " in trimmed else trimmed
        text = assemble(trimmed + "…", "")
        if len(text) <= limit:
            return text

    # No summary left to give: the title itself is too long.
    text = assemble("", "")
    if len(text) > limit:
        room = limit - len(assemble("", "")) + len(title) - 1
        text = assemble("", "").replace(title, title[:max(room, 0)].rstrip() + "…")
    return text


# --- Mastodon ---------------------------------------------------------------

def _mastodon_server():
    """Return the Mastodon instance host without scheme or trailing slash."""
    raw = (os.environ.get("MASTODON_SERVER") or "").strip()
    raw = re.sub(r"^https?://", "", raw).rstrip("/")
    return raw


def _mastodon_enabled():
    return bool(os.environ.get("MASTODON_TOKEN")) and bool(_mastodon_server())


def _mastodon_post(path, fields):
    """POST form-encoded fields to the Mastodon API and return parsed JSON."""
    return _request(
        f"https://{_mastodon_server()}{path}",
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['MASTODON_TOKEN']}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def _mastodon_language(tag):
    """A BCP 47 tag reduced to what Mastodon will accept for a status.

    Mastodon validates the status language against a list of ISO 639 codes and
    answers 422 for anything not on it, so a perfectly good tag like 'de-DE'
    would fail the whole post rather than the field. The primary subtag is
    always on the list and always means the same language, so drop the region.
    An empty or unusable value returns '' and the field is left off entirely -
    Mastodon then guesses, which is what it did before this existed."""
    primary = re.split(r'[-_]', str(tag or '').strip())[0].lower()
    return primary if re.fullmatch(r'[a-z]{2,3}', primary) else ""


def announce_mastodon(title, summary, url, tags, prefix=ANNOUNCE_PREFIX,
                      language=ANNOUNCE_LANGUAGE):
    """Toot the post and return its frontmatter coordinates."""
    host = _mastodon_server()
    # Mastodon counts every link as a flat MASTODON_URL_WEIGHT characters however
    # long it really is, so the raw string may exceed the limit by exactly what
    # the link over-measures. Handing build_announcement the plain limit would
    # trim posts that were never actually too long - and since trimming drops the
    # whole hashtag block first, that costs every hashtag for nothing.
    allowance = MASTODON_MAX_CHARS + max(0, len(url) - MASTODON_URL_WEIGHT)
    fields = {
        "status": build_announcement(title, summary, url, tags,
                                     limit=allowance, prefix=prefix),
        "visibility": "public",
    }
    # The announcement quotes the post's own title and summary, so it is in the
    # site's language, not the engine's. Omitted rather than guessed when the
    # configured tag is unusable (see _mastodon_language).
    lang = _mastodon_language(language)
    if lang:
        fields["language"] = lang
    status = _mastodon_post("/api/v1/statuses", fields)
    status_id = str(status["id"])
    # Bookmark so the toot survives any future cleanup of old statuses.
    try:
        _mastodon_post(f"/api/v1/statuses/{status_id}/bookmark", {})
    except Exception as e:
        print(f"   ⚠️  Posted, but bookmarking failed ({e}). Continuing.")
    handle = (os.environ.get("MASTODON_ID") or "").strip()
    link = status.get("url") or f"https://{host}/@{handle}/{status_id}"
    return {"mastodon_host": host, "mastodon_id": f'"{status_id}"'}, link


# --- Bluesky ----------------------------------------------------------------

def _bluesky_pds():
    """The host that holds the account. For accounts on Bluesky's own
    infrastructure this is bsky.social, which routes to the real PDS. Note this
    is NOT public.api.bsky.app - that host serves unauthenticated reads only and
    ignores credentials entirely."""
    raw = (os.environ.get("BLUESKY_PDS") or "bsky.social").strip()
    return re.sub(r"^https?://", "", raw).rstrip("/")


def _bluesky_enabled():
    return bool(os.environ.get("BLUESKY_HANDLE")) and \
        bool(os.environ.get("BLUESKY_APP_PASSWORD"))


_BLUESKY_SESSION = None


def _bluesky_session():
    """Log in once per run and reuse the token.

    createSession is rate-limited to 30 per five minutes per account, and this
    script announces a whole batch in one go, so re-authenticating per post
    would be both wasteful and a real ceiling on a large backfill. The access
    token is short-lived, but a single publish run finishes well inside its
    lifetime, so there is no refresh path to maintain."""
    global _BLUESKY_SESSION
    if _BLUESKY_SESSION is None:
        _BLUESKY_SESSION = _bluesky_call("com.atproto.server.createSession", {
            "identifier": os.environ["BLUESKY_HANDLE"].strip(),
            "password": os.environ["BLUESKY_APP_PASSWORD"].strip(),
        })
    return _BLUESKY_SESSION


def _bluesky_call(nsid, payload, token=None):
    """POST a JSON body to an XRPC endpoint on the account's PDS."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _request(
        f"https://{_bluesky_pds()}/xrpc/{nsid}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )


def build_facets(text, url):
    """Rich-text facets for the link and every hashtag in `text`.

    Bluesky auto-links nothing: a bare URL renders as inert text and a '#tag' is
    just characters unless the post carries a facet saying otherwise. So both
    have to be described explicitly here.

    Facet offsets are UTF-8 BYTE offsets, not character offsets - the lexicon is
    explicit about it. Every offset below is therefore derived by encoding the
    text *up to* the match and taking its length, which stays correct when the
    title or summary contains an em-dash, a curly quote, or an emoji. Computing
    them with str.index() would be right only for pure ASCII."""
    def byte_offset(char_index):
        return len(text[:char_index].encode("utf-8"))

    facets = []
    at = text.find(url)
    if at != -1:
        start = byte_offset(at)
        facets.append({
            "index": {"byteStart": start,
                      "byteEnd": start + len(url.encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
        })
    for match in re.finditer(r'#([A-Za-z0-9]+)', text):
        start = byte_offset(match.start())
        facets.append({
            # The byte range covers the '#' but the tag value must not include
            # it - getting that backwards is the usual reason a hashtag posts
            # as dead text.
            "index": {"byteStart": start,
                      "byteEnd": start + len(match.group(0).encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#tag",
                          "tag": match.group(1)}],
        })
    facets.sort(key=lambda f: f["index"]["byteStart"])
    return facets


def announce_bluesky(title, summary, url, tags, prefix=ANNOUNCE_PREFIX,
                     language=ANNOUNCE_LANGUAGE):
    """Post to Bluesky and return its frontmatter coordinates."""
    session = _bluesky_session()
    text = build_announcement(title, summary, url, tags,
                              limit=BLUESKY_MAX_CHARS, prefix=prefix)
    if len(text.encode("utf-8")) > BLUESKY_MAX_BYTES:
        # Only reachable with text that averages ten bytes per character, which
        # prose does not. Refuse rather than let the server reject it.
        raise ValueError("announcement exceeds Bluesky's 3000-byte limit")

    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.datetime.now(datetime.timezone.utc)
                             .isoformat().replace("+00:00", "Z"),
        # Same reasoning as the Mastodon status language: the text is the
        # post's own words. Unlike Mastodon, Bluesky takes a full BCP 47 tag,
        # so a regional variant is kept as written; an empty setting leaves the
        # field out and the app falls back to guessing.
        **({"langs": [str(language).strip()]} if str(language).strip() else {}),
        "facets": build_facets(text, url),
        # Bluesky does no link crawling, so a post with a URL in it shows no
        # preview unless the card is supplied. Title only, deliberately: the
        # summary is already the body of the post above it, and repeating it in
        # the card made every announcement read twice. An empty 'description'
        # renders the card as a compact title + domain chip, which is the point
        # of keeping it at all - a real tap target instead of bare blue text.
        # No 'thumb' either, because a thumbnail must be uploaded as a blob
        # under 1MB and that would drag image resizing (and the optional Pillow
        # dependency) into the announce path for a decoration.
        "embed": {
            "$type": "app.bsky.embed.external",
            "external": {
                "uri": url,
                "title": title[:300],
                "description": "",
            },
        },
    }
    created = _bluesky_call("com.atproto.repo.createRecord", {
        "repo": session["did"],
        "collection": "app.bsky.feed.post",
        "record": record,
    }, token=session["accessJwt"])

    uri = created["uri"]
    # Bookmark so the post survives any future cleanup, mirroring the Mastodon
    # side. Best-effort on purpose: bookmarks live outside the repo and it is
    # not documented whether an app password may write them, so a refusal here
    # must not cost us an announcement that already succeeded.
    try:
        _bluesky_call("app.bsky.bookmark.createBookmark", {
            "uri": uri, "cid": created["cid"],
        }, token=session["accessJwt"])
    except Exception as e:
        print(f"   ⚠️  Posted, but bookmarking failed ({e}). Continuing.")

    parts = uri.split("/")
    link = (f"https://bsky.app/profile/{parts[2]}/post/{parts[4]}"
            if len(parts) == 5 else uri)
    return {"bluesky_uri": f'"{uri}"'}, link


# --- The provider registry --------------------------------------------------
# Shaped like engine/llm.py's provider split: each entry says what it needs, how
# to tell whether it is configured, and how to announce - so announce_all()
# below never mentions a specific network.
PROVIDERS = (
    {
        "name": "Mastodon",
        "keys": ("mastodon_host", "mastodon_id"),
        "enabled": _mastodon_enabled,
        "announce": announce_mastodon,
    },
    {
        "name": "Bluesky",
        "keys": ("bluesky_uri",),
        "enabled": _bluesky_enabled,
        "announce": announce_bluesky,
    },
)

_PENDING_RE = re.compile(r'^announce:\s*pending\s*$', re.MULTILINE)


def _has_coordinates(fm, provider):
    """True when this post already carries every key the provider writes."""
    return all(_fm_value(fm, key) for key in provider["keys"])


def _merge_frontmatter(fm, new_lines, keep_pending):
    """Write coordinate lines in, and settle the 'announce: pending' marker.

    New lines take the marker's place, which keeps them where the author expects
    and avoids growing the frontmatter in a different spot each run. The marker
    survives when some enabled network still has nothing, so the next run
    retries only what is missing."""
    out = []
    replaced = False
    for line in fm.split("\n"):
        if _PENDING_RE.match(line):
            out.extend(new_lines)
            if keep_pending:
                out.append("announce: pending")
            replaced = True
            continue
        out.append(line)
    if not replaced:
        out.extend(new_lines)
    return "\n".join(out)


def announce_all():
    """Announce every pending post on every configured network, then rewrite
    its frontmatter. Returns the number of posts whose frontmatter changed."""
    enabled = [p for p in PROVIDERS if p["enabled"]()]
    if not enabled:
        print("   ⚠️  No social credentials set - skipping announcements. "
              "(Posts stay 'announce: pending' and will post next time.)")
        return 0
    if not os.path.isdir(CONTENT_DIR):
        return 0

    print(f"   Announcing on: {', '.join(p['name'] for p in enabled)}")
    changed = 0
    # Whether any network write has happened yet in this run; gates the pause so
    # the first announcement goes out immediately.
    posted_any = False

    for filename in sorted(os.listdir(CONTENT_DIR)):
        if not filename.endswith(".md") or filename in ("index.md", "changelog.md"):
            continue
        path = os.path.join(CONTENT_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        fm, body = parse_frontmatter(text)
        if fm is None:
            continue
        # Only posts explicitly marked pending are eligible.
        if not _PENDING_RE.search(fm):
            continue

        title = _fm_value(fm, "title") or "New post"
        summary = _fm_value(fm, "summary") or ""
        tags = _fm_tags(fm)
        # Which hashtag opens the announcement, and which language it is
        # posted under. Both decided once per post rather than per network, so
        # the two never disagree about what a post is - and both follow the
        # same is_episode() call, so a show recorded in another language than
        # the site is written in cannot be tagged '#podcast' in one place and
        # announced as English in another.
        episode = is_episode(fm, body)
        prefix = ANNOUNCE_PODCAST_PREFIX if episode else ANNOUNCE_PREFIX
        language = ANNOUNCE_PODCAST_LANGUAGE if episode else ANNOUNCE_LANGUAGE
        # Prefer the explicit slug (URL) so the announced link matches the site;
        # older posts without one fall back to the filename, as before.
        slug = _fm_value(fm, "slug") or filename[:-3]
        if not slug.endswith(".html"):
            slug += ".html"
        url = f"{SITE_URL}/{POSTS_DIR}/{slug}"

        todo = [p for p in enabled if not _has_coordinates(fm, p)]
        new_lines = []
        failed = False
        for provider in todo:
            # Pause between posts, never before the first one (see
            # ANNOUNCE_DELAY_SECONDS). Counted per network write rather than per
            # post, because that is what the receiving side actually sees.
            if posted_any and ANNOUNCE_DELAY_SECONDS > 0:
                print(f"   ⏳ Waiting {ANNOUNCE_DELAY_SECONDS:g}s before the "
                      f"next announcement...")
                time.sleep(ANNOUNCE_DELAY_SECONDS)
            print(f"📣 Announcing '{title}' on {provider['name']}...")
            posted_any = True
            try:
                coordinates, link = provider["announce"](title, summary, url,
                                                        tags, prefix, language)
            except Exception as e:
                print(f"   ⚠️  Failed to announce '{filename}' on "
                      f"{provider['name']} ({e}). Left pending; will retry "
                      f"next publish.")
                failed = True
                continue
            new_lines += [f"{key}: {value}" for key, value in coordinates.items()]
            print(f"   ✅ Posted and bookmarked: {link}")

        # Nothing accomplished and nothing to tidy - leave the file untouched so
        # the marker survives for the next run.
        if not new_lines and failed:
            continue

        new_fm = _merge_frontmatter(fm, new_lines, keep_pending=failed)
        new_text = f"---\n{new_fm.strip(chr(10))}\n---\n\n{body.lstrip(chr(10))}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
        changed += 1

    if changed:
        print(f"📣 Updated {changed} post(s).")
    else:
        print("   No pending posts to announce.")
    return changed


if __name__ == "__main__":
    count = announce_all()
    # Exit 2 signals publish.sh that frontmatter changed and the site needs a
    # rebuild + re-sync so the new comment threads appear.
    sys.exit(2 if count else 0)
