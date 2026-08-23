import os
import re
import sys
import yaml
import markdown
import shutil
import json
import html
import hashlib
import uuid
import mimetypes
import urllib.request
from urllib.parse import urlparse
from datetime import datetime, timezone
import email.utils

import llm    # shared model layer: roles, call_model, generate_alt_text
import images # build-time image optimisation (optional Pillow dependency)
import audio  # episode length/size/MIME (optional mutagen dependency)
from urls import (
    SITE_URL, POSTS_DIR, TAGS_DIR, FEEDS_DIR, AUDIO_DIR,
    PODCAST_FEED_NAME, PODCAST_PAGE_NAME,
    post_href, tag_page_name, tag_href, tag_feed_name, tag_feed_href, home_href,
    audio_href, podcast_feed_href, podcast_href,
    slugify_tag, slug_for, read_slug, htaccess_content,
)
# Filesystem layout. Note the split: urls.py above supplies URL space (including
# POSTS_DIR/TAGS_DIR/FEEDS_DIR, which double as public/ subdirectory names),
# while paths.py supplies absolute on-disk locations.
from config import (
    SITE_NAME, SITE_DESCRIPTION, FEED_DESCRIPTION, AUTHOR_NAME,
    AI_LABEL, AI_EXPLAINER, AI_EXPLAINER_URL,
    AUTHOR_EMAIL, LINK_ABOUT, LINK_MASTODON, FEDIVERSE_CREATOR,
    LINK_BLUESKY, BLUESKY_CREATOR, SITE_TAGLINE, NAV,
    PAGE_SIZE, FEED_ITEMS, VISIBLE_TAGS, WORDS_PER_MINUTE, TAG_EMOJI,
    IMAGE_MAX_WIDTH, IMAGE_JPEG_QUALITY, IMAGE_MIN_BYTES,
    PODCAST, PODCAST_ENABLED, REDIRECTS,
)
from paths import (
    REPO_ROOT, TEMPLATE_PATH, STATIC_SOURCE_DIRS,
    PUBLIC_DIR, PUBLIC_ASSETS_DIR,
    CONTENT_DIR, CONTENT_ASSETS_DIR, CONTENT_AUDIO_DIR, PUBLIC_AUDIO_DIR,
    PAGES_DIR,
    LINK_MANIFEST_PATH, EXISTING_TAGS_PATH, COMMENT_MODERATION_PATH,
    REMOTE_AUDIO_LEDGER_PATH,
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


def _standalone_link(line):
    """The link target of a line that is *nothing but* a link, else None.

    Accepts a bare URL on its own line or a Markdown link that is the whole
    line. This is the engine's rule for "the author meant to embed this, not to
    mention it": an inline link inside a sentence stays an inline link, because
    replacing it with a player or a video would break the sentence around it.

    Shared by embed_youtube() and embed_audio() so the two features can never
    drift into disagreeing about what counts as a standalone link - which is
    exactly the kind of difference an author would experience as the engine
    being arbitrary.
    """
    stripped = line.strip()
    md_link = re.fullmatch(r'\[[^\]]*\]\(\s*(\S+?)\s*\)', stripped)
    if md_link:
        return md_link.group(1)
    if re.fullmatch(r'https?://\S+', stripped):
        return stripped
    return None


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
        candidate = _standalone_link(line)
        if candidate:
            vid = _youtube_id(candidate)
            if vid:
                out_lines.append(facade(vid))
                continue
        out_lines.append(line)
    return '\n'.join(out_lines)


_COMMENT_MODERATION_CACHE = None


# --- Episode audio -----------------------------------------------------------

# Byte lengths for remote enclosures, loaded once per build. See
# REMOTE_AUDIO_LEDGER_PATH: an <enclosure> must declare a length and a file on
# somebody else's host cannot be stat'd, so we ask once and remember.
_REMOTE_AUDIO = None


def _remote_audio_ledger():
    global _REMOTE_AUDIO
    if _REMOTE_AUDIO is None:
        try:
            with open(REMOTE_AUDIO_LEDGER_PATH, 'r', encoding='utf-8') as fh:
                loaded = json.load(fh)
            _REMOTE_AUDIO = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _REMOTE_AUDIO = {}
    return _REMOTE_AUDIO


def _remote_audio_size(url):
    """Byte length of a remote enclosure, asking the host at most once ever.

    A migrated show keeps its back catalogue on its old host, so these URLs are
    the normal case rather than an exotic one, and re-asking on every build would
    make an offline build impossible and a fifty-episode build slow. The answer
    is cached in content_pipeline/ beside the other ledgers.

    Returns 0 when the host will not say. That is a legal enclosure length and
    every client tolerates it - it costs a progress bar until the download
    starts, which is a great deal better than refusing to build the feed.
    """
    ledger = _remote_audio_ledger()
    if url in ledger:
        return int(ledger[url] or 0)
    size = 0
    try:
        req = urllib.request.Request(
            url, method='HEAD', headers={'User-Agent': 'unprompted-blog/1.0'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            size = int(resp.headers.get('Content-Length') or 0)
    except Exception:
        print(f"   ⚠️  Could not read the size of {url} - the feed will declare "
              f"length 0. Set audio_bytes: in the post to fix it.")
    ledger[url] = size
    try:
        os.makedirs(os.path.dirname(REMOTE_AUDIO_LEDGER_PATH), exist_ok=True)
        with open(REMOTE_AUDIO_LEDGER_PATH, 'w', encoding='utf-8') as fh:
            json.dump(ledger, fh, indent=2, sort_keys=True)
    except Exception:
        pass
    return size


def _player_html(episode):
    """The episode player: cover, show name, one big play control, actions.

    Progressive enhancement, which matters more here than anywhere else on the
    site: the markup ships a real <audio controls>, and podcast.js only hides it
    once it has successfully taken over. A page whose player fails to initialise
    is still a page you can play the episode from - and an episode page that
    cannot play its episode has no content at all.
    """
    stated = audio.format_duration(episode.get('seconds')) or ''
    cover = (PODCAST or {}).get('cover', '')
    cover_img = (f'<img src="{esc(cover)}" alt="{esc((PODCAST or {}).get("title", ""))}'
                 f' cover art">' if cover else '')
    duration_attr = f' data-duration="{esc(stated)}"' if stated else ''
    label = esc(stated) if stated else 'Play'
    return (
        '<div class="episode-player">'
        '<div class="episode-player-head">'
        f'{cover_img}'
        '<div>'
        f'<p class="episode-show">{esc((PODCAST or {}).get("title", SITE_NAME))}</p>'
        f'<p class="episode-name">{esc(episode.get("post_title", ""))}</p>'
        '</div>'
        '</div>'
        f'<audio controls preload="metadata" src="{esc(episode["url"])}"></audio>'
        '<div class="episode-controls" hidden>'
        f'<button class="episode-play" type="button" aria-pressed="false"{duration_attr}>'
        '<svg class="icon-play" width="16" height="16" viewBox="0 0 24 24" '
        'fill="currentColor" aria-hidden="true"><path d="M7 4l14 8-14 8z"/></svg>'
        '<svg class="icon-pause" width="16" height="16" viewBox="0 0 24 24" '
        'fill="currentColor" aria-hidden="true" hidden>'
        '<path d="M7 4h4v16H7zM14 4h4v16h-4z"/></svg>'
        f'<span class="t">{label}</span>'
        '</button>'
        '<div class="episode-progress" role="slider" tabindex="0" '
        'aria-label="Seek within the episode" aria-valuemin="0" '
        'aria-valuemax="100" aria-valuenow="0"><span class="bar"></span></div>'
        f'<span class="episode-elapsed">0:00{" / " + esc(stated) if stated else ""}'
        '</span>'
        '</div>'
        '<div class="episode-actions">'
        f'<a href="{esc(episode["url"])}" download>'
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>'
        '</svg>Download</a>'
        f'<a href="{esc(podcast_href())}">'
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
        '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>'
        '</svg>All episodes</a>'
        f'<a href="{esc(podcast_feed_href())}">'
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2.5" stroke-linecap="round">'
        '<path d="M4 11a9 9 0 0 1 9 9"></path>'
        '<path d="M4 4a16 16 0 0 1 16 16"></path>'
        '<circle cx="5" cy="19" r="1"></circle></svg>Subscribe</a>'
        '</div>'
        '</div>'
    )


def embed_audio(markdown_text, meta):
    """Turn a standalone audio link into a player, and describe the episode.

    The audio counterpart of embed_youtube(), using the same _standalone_link()
    rule so the two behave identically: a link alone on its line becomes a
    player, a link inside a sentence stays a link.

    Returns (markdown, episode_or_None). The episode dict is what the feeds
    need - url, bytes, seconds, mime - and it is put on the post's meta so
    build_site() can tell an episode from an ordinary post without re-parsing
    anything.

    **First link wins.** A post may embed several players (an episode and its
    trailer), but RSS allows exactly one enclosure per item, so only the first
    becomes the episode. Choosing the first rather than the largest or the last
    means the author decides by writing order, which is the only rule that is
    obvious from reading the post.
    """
    episode = None
    out_lines = []
    for line in markdown_text.split('\n'):
        src = _standalone_link(line)
        if not src or not audio.is_audio(src):
            out_lines.append(line)
            continue

        if _is_remote(src):
            url = src
            size = meta.get('audio_bytes') or _remote_audio_size(src)
            seconds = None
            mime = audio.mime_for(src)
        else:
            name = os.path.basename(src)
            disk = os.path.join(CONTENT_AUDIO_DIR, name)
            probed = audio.probe(disk)
            if probed is None:
                # Referenced but not on disk: leave the line alone rather than
                # rendering a player for a file that would 404.
                print(f"   ⚠️  Audio referenced but not hosted: {src}")
                out_lines.append(line)
                continue
            url = audio_href(name)
            size, seconds, mime = probed
            if meta.get('audio_bytes'):
                size = int(meta['audio_bytes'])

        current = {
            'url': url,
            'bytes': int(size or 0),
            'seconds': seconds,
            'mime': mime,
            'post_title': str(meta.get('title', '')),
        }
        if episode is None:
            episode = current
        out_lines.append(_player_html(current))

    return '\n'.join(out_lines), episode


# --- Episode identity --------------------------------------------------------

# The fixed namespace the Podcasting 2.0 spec defines for <podcast:guid>. Not a
# value to invent: every generator must derive a show's GUID inside this
# namespace or two directories will disagree about which show they are holding.
_PODCAST_NAMESPACE = uuid.UUID('ead4c236-bf58-58c6-a2c6-a6b28d128cb6')


def show_guid():
    """The channel-level <podcast:guid>: a UUIDv5 of the feed URL, protocol and
    trailing slash removed, per the Podcasting 2.0 spec.

    Deterministic by definition, so unlike an episode GUID this is computed on
    every build rather than stored. It is what lets Podcast Index and the
    directories that follow it keep tracking the show if the feed itself ever
    moves.
    """
    # A show that already published a GUID keeps it. The spec derives the
    # value from the feed URL but is explicit that it must not change when the
    # feed moves - and a migration is exactly a feed move, so deriving it here
    # would hand the show a new identity in every directory that follows
    # Podcast Index.
    if PODCAST_ENABLED and PODCAST.get('guid'):
        return PODCAST['guid']
    bare = f"{SITE_URL}{podcast_feed_href()}"
    bare = re.sub(r'^https?://', '', bare).rstrip('/')
    return str(uuid.uuid5(_PODCAST_NAMESPACE, bare))


def ensure_episode_guids(posts):
    """Give every episode a permanent GUID, writing it into the post's
    frontmatter the first time and never touching it again.

    **This is the single most consequential value the engine emits.** A podcast
    client decides "have I already got this episode?" by GUID alone. If a GUID
    ever changes, every subscriber's app treats that episode as brand new and
    re-downloads it, and a feed-wide change re-delivers the entire back
    catalogue and fires a notification per episode. Apple's rule is simply that
    it must never change, for any reason.

    Which is why it cannot be derived at render time from anything visible. A
    URL-derived GUID breaks the day a slug is tidied or the site moves domain -
    both things this engine makes easy and neither of which feels like it should
    have consequences. So the value is minted once and *stored*, in the same
    content_pipeline/content/*.md the build already writes image paths and alt
    text back into.

    The minted value is a UUIDv5 of the site URL and slug, so regenerating a
    lost file reproduces it. But once written it is read, never recomputed, and
    never validated: an episode migrated from another host carries that host's
    GUID verbatim - `podlove-2015-01-01t12:00:00+00:00-a1b2c3` is a perfectly
    good GUID and rewriting it as a tidy UUID would re-deliver the archive it
    was preserved to protect.
    """
    for post in posts:
        if not post.get('episode') or str(post.get('guid') or '').strip():
            continue
        minted = str(uuid.uuid5(_PODCAST_NAMESPACE, f"{SITE_URL}/{post['slug']}"))
        # meta['file'] is a bare filename - the ledger's convention - so it has
        # to be resolved against CONTENT_DIR here.
        filepath = os.path.join(CONTENT_DIR, str(post.get('file') or ''))

        # Both failures below are fatal on purpose. An unwritten GUID still
        # produces a valid-looking feed today, and then changes the first time
        # the slug or the site URL does - re-delivering the whole back catalogue
        # to every subscriber. That is invisible locally and unfixable after the
        # fact, so it must stop the build rather than warn into a scrollback.
        if not post.get('file') or not os.path.isfile(filepath):
            print(f"❌ Cannot store a permanent GUID for episode "
                  f"'{post['slug']}': its source file was not found at "
                  f"{filepath}.")
            sys.exit(1)
        with open(filepath, 'r', encoding='utf-8') as fh:
            text = fh.read()
        match = re.match(r'^(---\s*\n)(.*?\n)(---\s*\n)(.*)', text, re.DOTALL)
        if not match:
            print(f"❌ Cannot store a permanent GUID for episode "
                  f"'{post['slug']}': {post['file']} has no frontmatter block.")
            print(f"💡 An episode needs one - see content_pipeline/TEMPLATE.md.")
            sys.exit(1)
        # Appended to the end of the frontmatter block rather than sorted into
        # it: this file is the author's, and an engine that reorders their keys
        # to insert one makes every diff unreadable.
        text = (f"{match.group(1)}{match.group(2)}guid: \"{minted}\"\n"
                f"{match.group(3)}{match.group(4)}")
        write_file_if_changed(filepath, text)
        post['guid'] = minted
        print(f"   🔑 Minted permanent episode GUID for {post['slug']}: {minted}")


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


def _host_audio_links(text, filepath, filename, audio_claims):
    """Copy any standalone local audio link into the audio directories and point
    the Markdown at the hosted copy. Returns (text, changed).

    The image equivalent of this namespaces every file as `<post>_<name>`,
    because `screenshot.png` collides constantly. Audio deliberately does not:
    an enclosure URL is quoted in feeds, in directories and in other people's
    players, and it has to survive for years, so it stays exactly what the author
    named it. Predictable audio URLs are also what let a show migrating from
    another host redirect its back catalogue with one rewrite rule instead of a
    table with a line per episode.

    The cost of that choice is that two posts can name the same file, so a
    collision is a hard error naming both posts rather than a silent overwrite -
    the failure it prevents is one episode serving another's audio, which no
    listener would report as a bug and everyone would experience as one.

    Remote URLs are left completely alone. That is what lets a migrated show
    keep serving its old episodes from wherever they already live while new ones
    are hosted here, in one feed, with no redirects at all.
    """
    changed = False
    out_lines = []
    for line in text.split('\n'):
        src = _standalone_link(line)
        if not src or not audio.is_audio(src) or _is_remote(src):
            out_lines.append(line)
            continue

        # Already hosted: nothing to copy on a rebuild, but still mirror it, so
        # a public/ wiped between builds is repopulated and the file registers
        # with the stale sweep instead of being deleted as unknown.
        if src.startswith(f"{AUDIO_DIR}/"):
            name = os.path.basename(src)
            disk = os.path.join(CONTENT_AUDIO_DIR, name)
            if os.path.exists(disk):
                _claim_audio(audio_claims, name, filename, disk)
                copy_asset_if_changed(disk, os.path.join(PUBLIC_AUDIO_DIR, name))
            else:
                print(f"   ⚠️  Hosted audio missing on disk ('{filename}'): {src}")
            out_lines.append(line)
            continue

        resolved = os.path.abspath(
            os.path.expanduser(os.path.join(os.path.dirname(filepath), src)))
        if not os.path.exists(resolved):
            print(f"   ⚠️  Audio not found on disk ('{filename}'): {src}")
            out_lines.append(line)
            continue

        name = os.path.basename(resolved)
        hosted = os.path.join(CONTENT_AUDIO_DIR, name)
        _claim_audio(audio_claims, name, filename, hosted)
        os.makedirs(CONTENT_AUDIO_DIR, exist_ok=True)
        copy_asset_if_changed(resolved, hosted)
        copy_asset_if_changed(hosted, os.path.join(PUBLIC_AUDIO_DIR, name))
        size = os.path.getsize(hosted)
        print(f"   🎧 Hosted episode audio: {name} ({size // 1024:,} KB)")

        # Rewrite in place, keeping whatever link text the author wrote. The
        # replacement is still a standalone link, so the next build recognises it
        # through the same code path rather than needing a second syntax.
        label = re.match(r'\s*\[([^\]]*)\]', line.strip())
        label = label.group(1) if label else 'Listen'
        out_lines.append(f"[{label}]({AUDIO_DIR}/{name})")
        changed = True

    return '\n'.join(out_lines), changed


def _claim_audio(audio_claims, name, filename, disk_path):
    """Record which post owns a hosted audio filename; fail loudly on a clash.

    Two posts referencing the *same* file on disk is fine and common - a trailer
    and the episode it trails, say. Two different files with one basename is not,
    because the second copy would overwrite the first and one episode's feed
    entry would quietly start serving the other's audio.
    """
    previous = audio_claims.get(name)
    if previous and previous[0] != filename and previous[1] != disk_path:
        print(f"❌ Two posts claim the audio filename '{name}':")
        print(f"   {previous[0]}")
        print(f"   {filename}")
        print(f"💡 Episode filenames become public URLs and are not namespaced "
              f"per post. Rename one of the files.")
        sys.exit(1)
    audio_claims.setdefault(name, (filename, disk_path))


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
    if PODCAST_ENABLED and not audio.AVAILABLE:
        print("   ⚠️  Episode durations unavailable (needs mutagen: "
              "pip install -r requirements.txt). The feed omits "
              "<itunes:duration>; everything else is unaffected.")

    # Which post claimed which hosted audio filename. Audio keeps the author's
    # own filename rather than being namespaced per post (see _host_audio_links),
    # so this is what turns a collision into an error instead of one episode
    # silently overwriting another's file.
    audio_claims = {}

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

        text, audio_replaced = _host_audio_links(text, filepath, filename,
                                                 audio_claims)

        if replacements or audio_replaced:
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

    # Then standalone audio links into players. Runs after the reading-time
    # estimate above, which should count the shownotes rather than the markup
    # that replaces the link.
    markdown_text, episode = embed_audio(markdown_text, frontmatter)
    frontmatter['episode'] = episode

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

# One id for the header's subscribe trigger. Fixed rather than random so the
# markup is identical on every page and byte-for-byte stable between builds -
# a changing id would rewrite every page in public/ on every build and make the
# FTP mirror re-upload the whole site.
_SUBSCRIBE_BUTTON_ID = 'header-subscribe'


def _subscribe_item_html():
    """The Subscribe control in the header, on every page of a podcast site.

    Podlove's widget normally renders itself as a large button wherever its
    script tag sits, which is not a thing that fits in a row of nav links. It
    also supports being hidden and driven from an element of the author's own
    (`data-hide` plus `data-buttonid`, and a trigger carrying the matching
    class), which is what this uses: an ordinary link, styled like its
    neighbours, that opens Podlove's app chooser.

    The plain href is the feed, so the link is useful before the script loads
    and remains useful if it never does - the popup is an enhancement over a
    link that already works.
    """
    if not PODCAST_ENABLED:
        return ''
    return (
        f'{_podlove_button_html(hidden=True)}'
        f'<a href="{esc(podcast_feed_href())}" '
        f'class="subscribe-link podlove-subscribe-button-{_SUBSCRIBE_BUTTON_ID}">'
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round" style="flex-shrink:0">'
        '<path d="M12 2a10 10 0 0 0-10 10c0 3.5 1.8 6.6 4.5 8.4"/>'
        '<path d="M12 6a6 6 0 0 0-6 6c0 2 1 3.8 2.5 4.9"/>'
        '<circle cx="12" cy="12" r="2.2"/>'
        '<path d="M10.6 22h2.8l-.7-6h-1.4z"/>'
        '</svg>Subscribe</a>'
    )


def _podlove_button_html(hidden=False):
    """The vendored Podlove subscribe button, configured inline.

    Config goes in a global rather than through the widget's `data-json-url`
    option on purpose: a failed JSON fetch there is swallowed with a
    console.debug and no button appears, which is exactly the kind of failure
    nobody notices on their own site. Inline, the config either renders with the
    page or does not exist.

    The `javascripts/` segment in the src is load-bearing - the widget derives
    the location of its stylesheet, its iframe and its ninety-odd app logos by
    stripping that segment off its own src. Move the file and everything else
    404s. See engine/fetch_podlove.py.
    """
    if not PODCAST_ENABLED:
        return ''
    cfg = PODCAST
    cover = cfg['cover']
    data = {
        'title': cfg['title'],
        'subtitle': cfg['subtitle'],
        'description': cfg['description'],
        # Root-relative, not absolutised. The button renders in an iframe on
        # this same origin, so a relative path resolves correctly there - and
        # unlike an absolute one it also resolves while previewing on
        # 127.0.0.1, where the configured domain does not answer and the cover
        # would otherwise be a broken image in the popup.
        'cover': cover,
        'feeds': [{
            'type': 'audio',
            'format': 'mp3',
            'url': f"{SITE_URL}{podcast_feed_href()}",
            'variant': 'high',
        }],
    }
    if cfg['links'].get('apple'):
        data['feeds'][0]['directory-url-itunes'] = cfg['links']['apple']
    payload = json.dumps(data, ensure_ascii=False)
    # </script> inside a JSON string would close this block early; the escape is
    # invisible to JSON.parse.
    payload = payload.replace('</', '<\\/')
    return (
        f'<script>window.podcastData = {payload};</script>'
        f'<script class="podlove-subscribe-button" '
        f'src="/subscribe-button/javascripts/app.js" '
        f'data-json-data="podcastData" data-language="{esc(PODCAST["language"][:2])}" '
        f'data-size="big" data-style="filled" data-format="rectangle" '
        f'data-color="{esc(cfg["button_color"])}"'
        + (f' data-hide="true" data-buttonid="{_SUBSCRIBE_BUTTON_ID}"'
           if hidden else '')
        + '></script>'
        + ('' if hidden else
           f'<noscript><a href="{esc(podcast_feed_href())}">'
           f'Subscribe to the feed</a></noscript>')
    )


def _format_bytes(size):
    """A file size a listener can act on: MB for an episode, KB for a clip.

    Integer megabytes alone would print '0 MB' for anything under one, which is
    what a trailer or a short bonus track looks like - and a download size of
    zero reads as an error rather than as 'small'.
    """
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.0f} MB"
    return f"{max(1, size // 1024)} KB"


def _episode_row_html(post):
    """One episode in the list on /podcast.html: date chip, cover, title,
    summary, and the facts a listener decides on before playing."""
    episode = post['episode']
    raw = str(post.get('date', ''))
    try:
        dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        day, month, year = dt.strftime("%d"), dt.strftime("%b"), dt.strftime("%Y")
    except ValueError:
        day, month, year = '', raw, ''

    cover = (PODCAST or {}).get('cover', '')
    art = (f'<img class="episode-art" src="{esc(cover)}" alt="">' if cover else '')

    facts = []
    if post.get('episode_number') or post.get('episode_no'):
        facts.append(f"Episode {esc(post.get('episode_number') or post.get('episode_no'))}")
    duration = audio.format_duration(episode.get('seconds'))
    if duration:
        facts.append(esc(duration))
    if episode.get('bytes'):
        facts.append(_format_bytes(episode['bytes']))
    meta_line = '<span class="sep">|</span>'.join(f'<span>{f}</span>' for f in facts)

    return (
        '<article class="episode-row">'
        f'<div class="episode-date"><span class="d">{day}</span>'
        f'<span class="m">{esc(month)}</span><span class="y">{year}</span></div>'
        f'{art}'
        '<div class="episode-body">'
        f'<h2><a href="{esc(post_href(post["slug"]))}">{esc(post["title"])}</a></h2>'
        f'<p>{esc(post.get("summary", ""))}</p>'
        f'<div class="episode-meta">{meta_line}</div>'
        '</div>'
        '</article>'
    )


def build_podcast_page(base_template, episodes, sitemap_urls):
    """Write public/podcast.html: the show header, the subscribe options, and
    every episode.

    Called before render_standalone_pages() so that a hand-written
    content_pipeline/pages/podcast.md overrides this one - the same precedence
    that already lets pages/index.md replace the generated homepage. A show with
    a real about-page to write should not have to fight the generator for its
    own URL.
    """
    if not PODCAST_ENABLED:
        return
    cfg = PODCAST
    pills = ''.join(
        f'<a href="{esc(url)}">{esc(name.replace("_", " ").title())}</a>'
        for name, url in cfg['links'].items()
    )
    rows = "".join(_episode_row_html(p) for p in episodes)
    empty = ('<p>No episodes yet. Link an audio file from a post and it '
             'appears here.</p>')
    cover = cfg['cover']

    main = (
        '<div class="article-content page-content">'
        '<section class="podcast-hero">'
        f'<img src="{esc(cover)}" alt="{esc(cfg["title"])} cover art">'
        '<div>'
        '<p class="podcast-eyebrow">Podcast</p>'
        f'<h1>{esc(cfg["title"])}</h1>'
        + (f'<p class="podcast-strapline">{esc(cfg["subtitle"])}</p>'
           if cfg['subtitle'] else '')
        + f'<p>{esc(cfg["description"])}</p>'
        '<div class="podcast-subscribe">'
        f'<a class="is-feed" href="{esc(podcast_feed_href())}">RSS feed</a>'
        f'{pills}'
        '</div>'
        f'<div class="podcast-button">{_podlove_button_html()}</div>'
        '</div>'
        '</section>'
        f'<div class="episode-list">{rows or empty}</div>'
        '</div>'
    )

    alt_feed = (f'<link rel="alternate" type="application/rss+xml" '
                f'title="{esc(cfg["title"])} podcast feed" '
                f'href="{SITE_URL}{podcast_feed_href()}" />')
    page = safe_render(base_template, {
        "%PAGE_TITLE%": f"{esc(cfg['title'])} — {esc(SITE_NAME)}",
        "%META_DESCRIPTION%": esc(cfg['description'])[:300],
        "%OG_TYPE%": "website",
        "%PAGE_SLUG%": PODCAST_PAGE_NAME,
        "%ALT_FEED_LINK%": alt_feed,
        "%STRUCTURED_DATA%": "",
        "%MAIN_CONTENT%": main,
    })
    write_file_if_changed(os.path.join(PUBLIC_DIR, PODCAST_PAGE_NAME), page)
    sitemap_urls.append(f"{SITE_URL}{podcast_href()}")


def render_standalone_pages(base_template, sitemap_urls):
    """Render content_pipeline/pages/*.md to /<name>.html at the site root.

    Standalone pages (about, colophon) are not posts: no date, no tags, no
    backlinking, no feed or archive entry. They go through the same base.html
    as everything else, so the header, footer, search and theme toggle come
    from one template instead of a copy that quietly stops matching the site.

    The filename is the URL: pages/about.md -> /about.html. Frontmatter takes
    'title' and 'description'; the body is plain Markdown.

    A page named index.md claims '/' and replaces the generated post feed there.
    That falls out of ordering - this runs after the homepage is written in
    build_site() - but it is deliberate and relied upon: it is how a site whose
    front page is a standalone landing page rather than a river of posts is
    built, without the engine needing a mode for it.

    Frontmatter also takes 'layout: panel', which sets the page's title as a
    heading in a narrow left column with the body beside it, instead of running
    the body full width under no heading at all. Anything else (including no
    value) renders the plain single-column page, which is what every page
    written before this key existed keeps."""
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

        # 'layout: panel' puts the page title in a left-hand column beside the
        # body. The heading is emitted here rather than written into the
        # Markdown so it cannot drift from the <title> and the og:title, which
        # come from the same frontmatter field.
        if str(meta.get('layout', '')).strip().lower() == 'panel':
            wrapper_class = "article-content page-content page-panel"
            body_html = f'<h1 class="panel-title">{esc(title)}</h1>{body_html}'
        else:
            wrapper_class = "article-content page-content"

        page_html = safe_render(base_template, {
            "%PAGE_TITLE%": f"{esc(title)} — {esc(SITE_NAME)}",
            "%META_DESCRIPTION%": esc(description),
            "%OG_TYPE%": "website",
            "%PAGE_SLUG%": f"{name}.html",
            "%STRUCTURED_DATA%": "",
            "%MAIN_CONTENT%": f'<div class="{wrapper_class}">{body_html}</div>',
        })
        write_file_if_changed(os.path.join(PUBLIC_DIR, f"{name}.html"), page_html)
        sitemap_urls.append(f"{SITE_URL}/{name}.html")
        print(f"📄 Rendered standalone page: /{name}.html")


# Theme files the template links by name. Their URLs carry a version query so
# they can be cached for a year and still update the moment they change.
_VERSIONED_ASSETS = ('style.css', 'fonts.css', 'search.js', 'comments.js',
                     'podcast.js', 'dompurify.min.js')

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


def _nav_link_html(entry, extra_class=''):
    """One <a> in the header menu, or a <span> for a section that only groups
    others. A submenu parent with no href of its own (site.yaml's `nav:` allows
    that) must not become a link to '#': that is a focusable control which does
    nothing, and screen readers announce it as a link to the top of the page."""
    label = esc(entry['label'])
    href = entry['href']
    cls = f' class="{extra_class}"' if extra_class else ''
    if not href:
        # tabindex so a keyboard can reach it: the submenu underneath opens on
        # :focus-within, which never fires if nothing in the branch can be
        # focused, leaving those links reachable by mouse only.
        return f'<span{cls} tabindex="0">{label}</span>'
    # Same rule the article bodies get in _externalize_links(): off-site links
    # open in a new tab, and keep sending a referrer so the sites we point at
    # can see where their traffic came from.
    target = ' target="_blank" rel="noopener"' if _is_external(href) else ''
    return f'<a href="{esc(href)}"{cls}{target}>{label}</a>'


def _header_nav_html():
    """The header's menu, built from site.yaml's `nav:` (see config._nav).

    With no `nav:` configured this returns the engine's built-in list - About,
    the two optional social links, Contact - so an existing site.yaml renders
    exactly as it did before this function existed. The RSS link, the search box
    and the theme toggle are not part of it either way: they are engine
    furniture that every site gets, not editorial navigation.

    Submenus are plain nested <ul>s revealed on hover and on :focus-within, with
    no JavaScript. On narrow screens style.css drops the positioning and lets
    them sit inline, permanently open - a hover target cannot be reached on a
    touchscreen, and a menu that needs a script to open is a menu that is
    missing whenever the script is."""
    if not NAV:
        return (f'<a href="{esc(LINK_ABOUT)}">About</a>'
                f'{_social_link_html(LINK_MASTODON, "Mastodon")}'
                f'{_social_link_html(LINK_BLUESKY, "Bluesky")}'
                f'<a href="mailto:{esc(AUTHOR_EMAIL)}">Contact</a>')

    items = []
    for entry in NAV:
        if not entry['items']:
            items.append(f'<li>{_nav_link_html(entry)}</li>')
            continue
        children = ''.join(f'<li>{_nav_link_html(child)}</li>'
                           for child in entry['items'])
        items.append(
            '<li class="has-submenu">'
            f'{_nav_link_html(entry, "submenu-parent")}'
            f'<ul class="submenu">{children}</ul>'
            '</li>'
        )
    return (f'<nav class="site-nav" aria-label="Main menu">'
            f'<ul class="nav-menu">{"".join(items)}</ul></nav>')


def _social_link_html(url, label):
    """One optional social link for the built-in header. rel="me" is what lets
    the profile on the other end verify this domain back, so it stays even
    though these are ordinary external links otherwise."""
    if not url:
        return ''
    return f'<a href="{esc(url)}" target="_blank" rel="me noopener">{label}</a>'


def _site_branding_html():
    """The header's branding band: the site name set large with its tagline
    underneath, above the nav row.

    Emitted only when site.yaml sets `site.tagline`. Without one there is
    nothing to put on a second line, and the compact single-row header - the
    engine's original shape, and the better one for reading a long post - is
    what every site.yaml written before this key existed keeps.

    The band scrolls away while the nav row below it stays stuck to the top;
    see the `header` rules in style.css."""
    if not SITE_TAGLINE:
        return ''
    return (
        '<div class="site-branding"><div class="branding-wrap">'
        f'<a href="/" class="site-title">{esc(SITE_NAME)}</a>'
        f'<p class="site-tagline">{esc(SITE_TAGLINE)}</p>'
        '</div></div>'
    )


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
    # The header menu, and the optional branding band above it. Both are whole
    # blocks rather than single values: the menu is either the engine's built-in
    # link list or the author's own `nav:` tree, and the branding band is absent
    # entirely on a site with no tagline. Escaping happens inside the builders,
    # element by element, since these are the one place author text becomes
    # markup rather than an attribute value.
    mappings.setdefault("%HEADER_NAV%", _header_nav_html())
    mappings.setdefault("%SUBSCRIBE_ITEM%", _subscribe_item_html())
    mappings.setdefault("%SITE_BRANDING%", _site_branding_html())
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
    # The player script also drives the header's Subscribe link, which is on
    # every page of a podcast site - so unlike the comments renderer this one is
    # not scoped to pages that carry a player. A site with no podcast still
    # never fetches it.
    if "%PODCAST_SCRIPT%" not in mappings:
        mappings["%PODCAST_SCRIPT%"] = (
            f'<script src="/podcast.js?v={_asset_version()}" defer></script>'
            if PODCAST_ENABLED else ''
        )
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

_PLAYER_OPEN = '<div class="episode-player">'
_DIV_TAG_RE = re.compile(r'<(/?)div\b', re.IGNORECASE)


def strip_player(html_body):
    """Remove the on-page episode player from a body bound for a feed.

    A podcast client has its own player and gets the audio from the
    <enclosure>; shipping ours inside the shownotes gives the listener a second,
    dead set of controls and drops the player's chrome ("Download", "All
    episodes", the duration) into <itunes:summary>, where it reads as part of
    the episode description.

    It also takes the player's root-relative URLs out of the feed. Only
    /assets/ and /posts/*.html are promoted to absolute for feed readers, so the
    player's /audio/ and cover links would arrive relative and resolve against
    whatever host the reader is on.

    Matched by counting <div> depth rather than with a regex. The player nests
    several levels, and a non-greedy pattern stops at the first </div> it meets:
    that removes the opening tag - and with it the class name that would have
    shown you the block was still there - while leaving most of the player in
    the feed.
    """
    while True:
        start = html_body.find(_PLAYER_OPEN)
        if start == -1:
            return html_body
        depth = 0
        end = None
        for match in _DIV_TAG_RE.finditer(html_body, start):
            depth += -1 if match.group(1) else 1
            if depth == 0:
                end = html_body.find('>', match.end()) + 1
                break
        if end is None:            # unbalanced markup: leave it alone rather
            return html_body       # than truncate the rest of the post
        html_body = html_body[:start] + html_body[end:]


def _cdata(text):
    """Wrap text in CDATA, splitting any literal ']]>' that would end it early.

    A post that quotes ']]>' - talking about XML, or about this very problem -
    would otherwise terminate the section mid-body and produce a feed no reader
    can parse. The standard fix is to break the sequence across two CDATA
    sections; the bytes a parser sees are identical.
    """
    return f"<![CDATA[{str(text).replace(']]>', ']]]]><![CDATA[>')}]]>"


def item_pubdate(post):
    """RFC-2822 publication date for a feed item.

    `published:` wins over `date:` when present, because `date:` is
    day-precision and two episodes released on one day would otherwise tie and
    be ordered arbitrarily. That matters for an imported archive, where the feed
    being replaced had real timestamps and subscribers would see the order
    change.
    """
    raw = post.get('published') or post.get('date')

    # Frontmatter carries wall-clock times with no zone, and they are read as
    # UTC. datetime.timestamp() on a naive value would read them as the *build
    # machine's* local time instead, which silently shifts every date in the
    # feed by the builder's offset - and makes a migrated episode disagree with
    # the pubDate its old feed published, which is the one thing an import must
    # reproduce exactly.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(str(raw).replace('Z', '').strip()[:19], fmt)
            return email.utils.format_datetime(dt.replace(tzinfo=timezone.utc),
                                               usegmt=True)
        except (ValueError, TypeError):
            continue
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        return email.utils.format_datetime(raw, usegmt=True)
    return email.utils.formatdate(usegmt=True)


def _episode_xml(post):
    """The <enclosure> and iTunes item tags, or '' for a post that is not an
    episode. Shared by both feeds - the main feed carries the enclosure too, so
    an ordinary RSS reader can play an episode without subscribing twice."""
    episode = post.get('episode')
    if not episode or post.get('podcast') is False:
        return ''
    parts = [
        f'<enclosure url="{html.escape(SITE_URL + episode["url"] if episode["url"].startswith("/") else episode["url"], quote=True)}" '
        f'length="{episode["bytes"]}" type="{episode["mime"]}" />'
    ]
    duration = audio.format_duration(episode.get('seconds'))
    if duration:
        parts.append(f'<itunes:duration>{duration}</itunes:duration>')
    return "\n            " + "\n            ".join(parts)


def rss_item_xml(post):
    title_escaped = html.escape(post['title'], quote=True)
    summary_escaped = html.escape(post.get('summary', ''), quote=True)

    # Grab the HTML body and promote its already root-absolute asset/link URLs
    # (e.g. /assets/x.png, /posts/y.html - see _absolutize_body) to fully
    # qualified ones so images and internal navigation work inside a feed reader.
    body_content = strip_player(post.get('html_body', ''))
    body_content = re.sub(r'src="/assets/', f'src="{SITE_URL}/assets/', body_content)
    body_content = re.sub(r'href="(/[^"]+\.html)"', f'href="{SITE_URL}\\1"', body_content)

    return f"""        <item>
            <title>{title_escaped}</title>
            <link>{SITE_URL}{post_href(post['slug'])}</link>
            <guid isPermaLink="true">{SITE_URL}{post_href(post['slug'])}</guid>
            <description>{summary_escaped}</description>
            <content:encoded>{_cdata(body_content)}</content:encoded>
            <pubDate>{item_pubdate(post)}</pubDate>{_episode_xml(post)}
        </item>"""

def rss_feed_xml(posts_list, feed_title, feed_desc, self_path, last_build_date_rfc):
    channels = "\n".join(rss_item_xml(p) for p in posts_list)
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
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

def podcast_item_xml(post):
    """One <item> in the podcast feed.

    Deliberately not rss_item_xml() with extra tags: the two feeds want
    different things from the same post. The blog feed's guid is the post URL,
    which is right for a reader that dedupes by link; this one's guid is the
    stored permanent identifier, which is the only thing standing between a slug
    edit and every subscriber re-downloading the archive.
    """
    episode = post['episode']
    title = html.escape(post['title'], quote=True)
    summary = html.escape(post.get('summary', ''), quote=True)

    body = strip_player(post.get('html_body', ''))
    body = re.sub(r'src="/assets/', f'src="{SITE_URL}/assets/', body)
    body = re.sub(r'href="(/[^"]+\.html)"', f'href="{SITE_URL}\\1"', body)

    # itunes:summary must be plain text - Apple strips or rejects markup there,
    # even though <description> accepts it. 4000 characters is Apple's cap.
    plain = plain_text(body)[:4000]

    optional = []
    duration = audio.format_duration(episode.get('seconds'))
    if duration:
        optional.append(f'<itunes:duration>{duration}</itunes:duration>')
    if post.get('episode_number') or post.get('episode_no'):
        optional.append(f'<itunes:episode>'
                        f'{int(post.get("episode_number") or post.get("episode_no"))}'
                        f'</itunes:episode>')
    if post.get('season'):
        optional.append(f'<itunes:season>{int(post["season"])}</itunes:season>')
    episode_type = str(post.get('episode_type', 'full')).strip().lower()
    if episode_type in ('full', 'trailer', 'bonus'):
        optional.append(f'<itunes:episodeType>{episode_type}</itunes:episodeType>')
    explicit = post.get('explicit')
    explicit = PODCAST['explicit'] if explicit is None else bool(explicit)
    optional.append(f'<itunes:explicit>{str(explicit).lower()}</itunes:explicit>')

    url = SITE_URL + episode['url'] if episode['url'].startswith('/') else episode['url']
    extras = "\n            ".join(optional)
    return f"""        <item>
            <title>{title}</title>
            <link>{SITE_URL}{post_href(post['slug'])}</link>
            <guid isPermaLink="false">{html.escape(str(post['guid']), quote=True)}</guid>
            <pubDate>{item_pubdate(post)}</pubDate>
            <description>{summary}</description>
            <itunes:summary>{html.escape(plain, quote=True)}</itunes:summary>
            <content:encoded>{_cdata(body)}</content:encoded>
            <enclosure url="{html.escape(url, quote=True)}" length="{episode['bytes']}" type="{episode['mime']}" />
            {extras}
        </item>"""


def podcast_feed_xml(episodes, last_build_date_rfc):
    """The iTunes/Podcasting 2.0 feed.

    A sibling of rss_feed_xml() rather than a parameterisation of it, because
    almost every channel element differs: the language and the <link> come from
    the podcast config instead of being hard-coded, and the whole iTunes block
    has no counterpart in a blog feed.

    Everything Apple requires is emitted unconditionally, since a feed missing
    one of them is rejected at submission with a message that names the tag but
    not the reason. The conditional tags below it are the ones that are wrong to
    guess at.
    """
    cfg = PODCAST
    cover = cfg['cover']
    cover_url = SITE_URL + cover if cover.startswith('/') else cover
    esc_attr = lambda v: html.escape(str(v), quote=True)

    optional = []
    if cfg['subtitle']:
        optional.append(f"    <itunes:subtitle>{esc_attr(cfg['subtitle'])}</itunes:subtitle>")
    if cfg['copyright']:
        optional.append(f"    <copyright>{esc_attr(cfg['copyright'])}</copyright>")
    # <podcast:locked> tells hosting platforms whether this feed may be imported
    # and the show claimed elsewhere. Default yes; the owner email is the proof.
    optional.append(
        f"    <podcast:locked owner=\"{esc_attr(cfg['owner_email'])}\">"
        f"{'yes' if cfg['locked'] else 'no'}</podcast:locked>")
    if cfg['new_feed_url']:
        # Only ever set while genuinely moving: every subscribed app follows it
        # permanently, so a stray value silently hands your audience away.
        optional.append(f"    <itunes:new-feed-url>{esc_attr(cfg['new_feed_url'])}"
                        f"</itunes:new-feed-url>")

    category = f'    <itunes:category text="{esc_attr(cfg["category"])}">'
    if cfg['subcategory']:
        category += (f'\n        <itunes:category text="{esc_attr(cfg["subcategory"])}" />'
                     f'\n    </itunes:category>')
    else:
        category = f'    <itunes:category text="{esc_attr(cfg["category"])}" />'

    items = "\n".join(podcast_item_xml(p) for p in episodes)
    extras = "\n".join(optional)
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel>
    <title>{esc_attr(cfg['title'])}</title>
    <link>{SITE_URL}{podcast_href()}</link>
    <description>{esc_attr(cfg['description'])}</description>
    <language>{esc_attr(cfg['language'])}</language>
    <lastBuildDate>{last_build_date_rfc}</lastBuildDate>
    <generator>Unprompted Blog Engine</generator>
    <atom:link href="{SITE_URL}{podcast_feed_href()}" rel="self" type="application/rss+xml" />
    <podcast:guid>{show_guid()}</podcast:guid>
    <itunes:image href="{esc_attr(cover_url)}" />
{category}
    <itunes:explicit>{str(cfg['explicit']).lower()}</itunes:explicit>
    <itunes:author>{esc_attr(cfg['author'])}</itunes:author>
    <itunes:type>{cfg['type']}</itunes:type>
    <itunes:owner>
        <itunes:name>{esc_attr(cfg['owner_name'])}</itunes:name>
        <itunes:email>{esc_attr(cfg['owner_email'])}</itunes:email>
    </itunes:owner>
{extras}
{items}
</channel>
</rss>"""


def feed_build_date(posts_list):
    # Same UTC rule as item_pubdate(): a frontmatter date is a wall-clock date,
    # not a moment in the builder's timezone. Reusing that function also means
    # <lastBuildDate> and the newest <pubDate> can never disagree.
    if posts_list:
        return item_pubdate(posts_list[0])
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
            # An episode page advertises the podcast feed as well as the blog's,
            # so a browser's feed discovery - and anything scraping for one -
            # finds the subscribable version rather than only the reading one.
            "%ALT_FEED_LINK%": (
                f'<link rel="alternate" type="application/rss+xml" '
                f'title="{esc(PODCAST["title"])} podcast feed" '
                f'href="{SITE_URL}{podcast_feed_href()}" />'
                if meta.get('episode') and PODCAST_ENABLED else ''
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
            
    # --- PODCAST ---
    # Episodes are ordinary posts that happen to carry audio; `podcast: false`
    # opts a post out of the show while keeping its player. GUIDs are minted
    # before anything is written, because both feeds depend on them.
    episodes = [p for p in posts
                if p.get('episode') and p.get('podcast') is not False]
    if PODCAST_ENABLED and episodes:
        ensure_episode_guids(episodes)
    build_podcast_page(base_template, episodes, sitemap_urls)

    # --- AUTOMATED SITEMAP OUT ---
    # Runs after build_podcast_page so a hand-written pages/podcast.md wins,
    # exactly as pages/index.md wins over the generated homepage.
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

    # Podcast feed: every episode, never capped at FEED_ITEMS. A blog feed is a
    # what's-new list, but a podcast feed is the show's whole catalogue - an
    # episode that falls out of it disappears from every directory and from the
    # back catalogue of every app.
    if PODCAST_ENABLED and episodes:
        write_file_if_changed(
            os.path.join(PUBLIC_DIR, PODCAST_FEED_NAME),
            podcast_feed_xml(episodes, feed_build_date(episodes)),
        )

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
    write_file_if_changed(os.path.join(PUBLIC_DIR, ".htaccess"), htaccess_content(REDIRECTS))

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
