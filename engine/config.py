"""Site configuration: who this blog belongs to and how it presents itself.

Reads site.yaml from the repo root and exposes it as plain module constants, so
consumers just `from config import SITE_NAME` and never see the YAML. Nothing
reader-facing is hardcoded in the engine or the template - the site's name, the
author, the URL, the contact and social links, the footer, and the pagination
and feed sizes all come from here.

Why this is a file rather than constants in the code: the values a new owner
must change are not all visible in a browser. The JSON-LD `author.name` and the
`og:image:alt` text never render on screen, but they are exactly what search
engines and social scrapers attribute the writing to. Scattered across two
modules and a template, they get missed, and the fork silently keeps
publishing under someone else's name.

site.yaml is gitignored and seeded from the tracked site.example.yaml by
publish.sh, matching how VOICE.md and publish.local.sh already work.

Loading is eager and fails loudly: a missing or malformed config should stop
the build immediately with a message naming the problem, rather than letting a
page render with an empty title or the word "None" in a meta tag.
"""

import os
import sys

import yaml

from paths import REPO_ROOT, SITE_CONFIG_PATH, SITE_CONFIG_EXAMPLE_PATH


def _rel(path):
    """Path as the user would type it from the repo root - which is where they
    run publish.sh from. Relative to the current directory instead would print
    a stack of '../..' whenever a script is invoked from elsewhere."""
    try:
        return os.path.relpath(path, REPO_ROOT)
    except ValueError:      # different drive on Windows
        return path

# section -> keys the engine reads. Used both to validate and to describe what
# is missing, so a half-filled config names every gap at once instead of
# failing on one key per run.
_REQUIRED = {
    'site': ('name', 'url', 'description', 'feed_description'),
    'author': ('name', 'email'),
    'links': ('about',),
    'footer': ('ai_label', 'ai_explainer', 'ai_explainer_url'),
    'display': ('page_size', 'feed_items', 'visible_tags', 'words_per_minute'),
}


def _fail(*lines):
    for line in lines:
        print(line)
    sys.exit(1)


def _load():
    if not os.path.exists(SITE_CONFIG_PATH):
        _fail(f"❌ Site configuration not found at '{SITE_CONFIG_PATH}'.",
              f"💡 Copy the template and make it yours:",
              f"     cp {_rel(SITE_CONFIG_EXAMPLE_PATH)} {_rel(SITE_CONFIG_PATH)}",
              f"   ./publish.sh does this for you automatically.")
    try:
        with open(SITE_CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        _fail(f"❌ Could not parse '{SITE_CONFIG_PATH}': {exc}")

    if not isinstance(data, dict):
        _fail(f"❌ '{SITE_CONFIG_PATH}' did not parse to a mapping of settings.")

    missing = []
    for section, keys in _REQUIRED.items():
        block = data.get(section)
        if not isinstance(block, dict):
            missing.append(f"{section}: (whole section)")
            continue
        missing += [f"{section}.{k}" for k in keys if block.get(k) is None]
    if missing:
        _fail(f"❌ '{SITE_CONFIG_PATH}' is missing: {', '.join(missing)}",
              f"💡 Compare it against {_rel(SITE_CONFIG_EXAMPLE_PATH)}, "
              f"which lists every setting the engine reads.")
    return data


_cfg = _load()


def _int(section, key):
    """A display setting that must be a positive whole number. A page size of 0
    would divide by zero and a negative one would silently produce no pages, so
    both are rejected here rather than surfacing deep inside the build."""
    value = _cfg[section][key]
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number < 1:
        _fail(f"❌ '{SITE_CONFIG_PATH}': {section}.{key} must be a whole number "
              f"of at least 1 (got {value!r}).")
    return number


# --- Identity ---
SITE_NAME = str(_cfg['site']['name'])
# Trailing slashes are stripped: every URL in the engine is built as
# SITE_URL + "/path", so keeping one here would produce '//path' throughout the
# sitemap and both feeds.
SITE_URL = str(_cfg['site']['url']).rstrip('/')
SITE_DESCRIPTION = str(_cfg['site']['description'])
FEED_DESCRIPTION = str(_cfg['site']['feed_description'])

AUTHOR_NAME = str(_cfg['author']['name'])
AUTHOR_EMAIL = str(_cfg['author']['email'])

LINK_ABOUT = str(_cfg['links']['about'])

# Optional second line under the site name in the header. Setting it switches
# the header to its two-tier form (a branding band above the sticky nav row);
# leaving it out keeps the compact single-row header, which is what every
# site.yaml written before this key existed gets. See base.html's
# %SITE_BRANDING%.
SITE_TAGLINE = str(_cfg['site'].get('tagline') or '').strip()


def _optional_link(key):
    """One optional links.* string, empty when unset.

    Deliberately not in _REQUIRED: Bluesky arrived after this config existed, so
    every site.yaml already written predates these keys and must keep working
    untouched. Empty is the honest "not configured" value here - unlike the
    numbers in _int(), a blank link disables a feature rather than corrupting
    one, so there is nothing to fail loudly about.

    Mastodon is optional on the same terms, for the opposite reason: a site that
    announces nowhere and carries no comment thread would otherwise be forced to
    advertise a network it does not use. Empty means the header link and the
    fediverse:creator meta are not rendered at all - see build_blog.py's
    %LINK_MASTODON_ITEM%. Every site.yaml that already sets these is unaffected;
    a required key that becomes optional never invalidates an existing file."""
    value = _cfg['links'].get(key)
    return '' if value is None else str(value).strip()


# Mastodon, the first comment provider. LINK_MASTODON is the header link;
# FEDIVERSE_CREATOR is the handle used to badge the author's own replies and to
# set <meta name="fediverse:creator">.
LINK_MASTODON = _optional_link('mastodon')
FEDIVERSE_CREATOR = _optional_link('fediverse_creator')

# Bluesky, the second comment provider. LINK_BLUESKY is the header link (shown
# only when set); BLUESKY_CREATOR is the handle or DID used to badge the
# author's own replies, the counterpart of FEDIVERSE_CREATOR.
LINK_BLUESKY = _optional_link('bluesky')
BLUESKY_CREATOR = _optional_link('bluesky_creator')

def _nav():
    """The header's link list, as an ordered list of entries:

        [{'label': str, 'href': str, 'items': [{'label','href'}, ...]}, ...]

    Optional, and absent is the normal case: with no `nav:` section the engine
    keeps emitting its built-in header (About, the social links, Contact), so
    every site.yaml written before this key existed renders exactly as before.
    A site that sets one replaces that list wholesale - the RSS link, the search
    box and the theme toggle are engine furniture and stay either way.

    An entry may carry `href`, `items`, or both: a top-level link that also
    opens a submenu is the common shape for a section that has a landing page of
    its own. Submenus are one level deep on purpose; anything deeper wants a
    page, not a hover target that cannot be reached on a touchscreen.

    Validation is loud rather than lenient, matching the rest of this file. A
    mistyped key here does not corrupt anything, but it silently drops a link
    from every page of the site - and a missing menu entry is exactly the kind
    of thing an author does not notice on their own site, because they navigate
    it from memory."""
    raw = _cfg.get('nav')
    if raw is None:
        return []
    if not isinstance(raw, list):
        _fail(f"❌ '{SITE_CONFIG_PATH}': nav must be a list of menu entries "
              f"(got {type(raw).__name__}).")

    def _entry(item, where, allow_children):
        if not isinstance(item, dict):
            _fail(f"❌ '{SITE_CONFIG_PATH}': {where} must be a mapping with "
                  f"'label' and 'href' (got {type(item).__name__}).")
        label = str(item.get('label') or '').strip()
        if not label:
            _fail(f"❌ '{SITE_CONFIG_PATH}': {where} is missing 'label'.")
        href = str(item.get('href') or '').strip()
        children = item.get('items')
        if children is not None and not allow_children:
            _fail(f"❌ '{SITE_CONFIG_PATH}': {where} ({label!r}) has 'items', "
                  f"but submenus are only one level deep.")
        if children is None:
            children = []
        elif not isinstance(children, list):
            _fail(f"❌ '{SITE_CONFIG_PATH}': nav entry {label!r} has 'items' "
                  f"that is not a list (got {type(children).__name__}).")
        else:
            children = [_entry(c, f"nav entry {label!r} -> items[{i}]", False)
                        for i, c in enumerate(children)]
        if not href and not children:
            _fail(f"❌ '{SITE_CONFIG_PATH}': {where} ({label!r}) needs an "
                  f"'href', 'items', or both - it currently links nowhere.")
        return {'label': label, 'href': href, 'items': children}

    return [_entry(item, f"nav[{i}]", True) for i, item in enumerate(raw)]


NAV = _nav()

AI_LABEL = str(_cfg['footer']['ai_label'])
AI_EXPLAINER = str(_cfg['footer']['ai_explainer'])
AI_EXPLAINER_URL = str(_cfg['footer']['ai_explainer_url'])

# --- Presentation ---
PAGE_SIZE = _int('display', 'page_size')
FEED_ITEMS = _int('display', 'feed_items')
VISIBLE_TAGS = _int('display', 'visible_tags')
WORDS_PER_MINUTE = _int('display', 'words_per_minute')


def _tag_emoji():
    """Optional decoration: an emoji shown beside a tag wherever it is rendered.

    Deliberately a lookup table rather than part of the tag itself. The tag text
    is the identity used for slugs, feed categories, the search index and the
    tag ledger, so putting an emoji in it would change URLs and make 'Photography'
    and '📷 Photography' two different topics. Here it stays presentation, and
    removing an entry removes the decoration with nothing else moving.

    Absent or empty is normal - the whole feature is opt-in - so this is not in
    _REQUIRED. Matching is case-insensitive on the exact tag name."""
    raw = _cfg['display'].get('tag_emoji')
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail(f"❌ '{SITE_CONFIG_PATH}': display.tag_emoji must be a mapping of "
              f"tag name to emoji (got {type(raw).__name__}).")
    return {
        str(tag).strip().lower(): str(emoji).strip()
        for tag, emoji in raw.items()
        if str(emoji).strip()
    }


TAG_EMOJI = _tag_emoji()


def _image_setting(key, default, allow_zero=False):
    """One optional images.* number, with the engine's default when unset.

    The whole `images:` section is optional and not in _REQUIRED, so a site.yaml
    written before image optimisation existed keeps working untouched - it just
    gets the defaults. A present-but-nonsensical value is still fatal, on the
    same principle as _int(): a negative width would silently disable a step the
    author clearly meant to configure.
    """
    raw = (_cfg.get('images') or {})
    if not isinstance(raw, dict):
        _fail(f"❌ '{SITE_CONFIG_PATH}': images must be a mapping of settings "
              f"(got {type(raw).__name__}).")
    value = raw.get(key)
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = -1
    if number < (0 if allow_zero else 1):
        _fail(f"❌ '{SITE_CONFIG_PATH}': images.{key} must be a whole number "
              f"{'of at least 0' if allow_zero else 'of at least 1'} "
              f"(got {value!r}).")
    return number



# --- Podcast (wholly optional) -----------------------------------------------

# The iTunes categories Apple accepts. Validated against this list because a
# typo here is not a broken page you would notice - the feed still builds, and
# Apple rejects or silently miscategorises the show days later. Subcategories
# are not enumerated: they are numerous, they change, and a wrong one is
# survivable in a way a wrong top-level category is not.
_ITUNES_CATEGORIES = (
    'Arts', 'Business', 'Comedy', 'Education', 'Fiction', 'Government',
    'History', 'Health & Fitness', 'Kids & Family', 'Leisure', 'Music',
    'News', 'Religion & Spirituality', 'Science', 'Society & Culture',
    'Sports', 'Technology', 'True Crime', 'TV & Film',
)


def _podcast():
    """The `podcast:` section, or None when the site has no podcast.

    Absent is the normal case and costs nothing: with no section the engine
    emits no podcast feed, no /podcast.html and no player, and every site.yaml
    written before this key existed is untouched.

    Present means validated hard, and harder than the rest of this file. The
    reason is the failure mode: a blog with a bad config renders wrong and you
    see it. A podcast with a bad config builds a feed that looks fine, gets
    submitted to Apple, and is rejected - or worse, accepted with the wrong
    owner, at which point you cannot claim your own show. Everything Apple
    requires is therefore required here too, spelled out rather than defaulted
    to something plausible.

    The exceptions are the four keys the blog already knows - author, owner
    name, owner email, language - which default to the site's own values. Making
    someone write their own name twice in one file is not validation, it is
    friction.
    """
    raw = _cfg.get('podcast')
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _fail(f"❌ '{SITE_CONFIG_PATH}': podcast must be a mapping of settings "
              f"(got {type(raw).__name__}).")

    def _need(key):
        value = str(raw.get(key) or '').strip()
        if not value:
            _fail(f"❌ '{SITE_CONFIG_PATH}': podcast.{key} is required once a "
                  f"podcast: section exists.",
                  f"   Remove the whole section to publish without a podcast.")
        return value

    def _opt(key, default=''):
        value = raw.get(key)
        return default if value is None else str(value).strip()

    category = _need('category')
    if category not in _ITUNES_CATEGORIES:
        _fail(f"❌ '{SITE_CONFIG_PATH}': podcast.category must be one of "
              f"Apple's categories (got {category!r}).",
              f"   Valid: {', '.join(_ITUNES_CATEGORIES)}")

    cover = _need('cover')
    if not cover.startswith('/'):
        _fail(f"❌ '{SITE_CONFIG_PATH}': podcast.cover must be a root-absolute "
              f"path to a file the site serves, e.g. '/podcast-cover.png' "
              f"(got {cover!r}).")

    # Apple wants a literal 'true'/'false' string. A YAML boolean, the strings
    # 'yes'/'no', and the words themselves all mean the same thing to an author
    # and none of them are what the feed needs, so normalise all of them.
    explicit = raw.get('explicit')
    if explicit is None:
        _fail(f"❌ '{SITE_CONFIG_PATH}': podcast.explicit is required - Apple "
              f"rejects a feed without it. Use true or false.")
    explicit = str(explicit).strip().lower() in ('true', 'yes', '1', 'on')

    show_type = _opt('type', 'episodic').lower()
    if show_type not in ('episodic', 'serial'):
        _fail(f"❌ '{SITE_CONFIG_PATH}': podcast.type must be 'episodic' or "
              f"'serial' (got {show_type!r}).")

    links = raw.get('links') or {}
    if not isinstance(links, dict):
        _fail(f"❌ '{SITE_CONFIG_PATH}': podcast.links must be a mapping of "
              f"directory name to URL (got {type(links).__name__}).")

    return {
        'title': _need('title'),
        'subtitle': _opt('subtitle'),
        'description': _need('description'),
        'cover': cover,
        'category': category,
        'subcategory': _opt('subcategory'),
        'explicit': explicit,
        'type': show_type,
        # These four fall back to the blog's own identity rather than being
        # required again. owner_email is what Apple mails to verify ownership,
        # so it defaults to the address the site already publishes.
        'author': _opt('author') or AUTHOR_NAME,
        'owner_name': _opt('owner_name') or AUTHOR_NAME,
        'owner_email': _opt('owner_email') or AUTHOR_EMAIL,
        'language': _opt('language') or 'en',
        'copyright': _opt('copyright'),
        # Set this ONLY while moving the show to a different feed URL: it tells
        # every subscribed app to follow the new address permanently, and an
        # accidental value points your listeners somewhere you did not mean.
        'new_feed_url': _opt('new_feed_url'),
        # <podcast:locked> - 'yes' stops a hosting platform importing this feed
        # and claiming the show. Defaults to locked, since the safe answer is
        # the one that does nothing until you ask for it.
        'locked': str(raw.get('locked', True)).strip().lower()
                  in ('true', 'yes', '1', 'on'),
        'links': {str(k): str(v) for k, v in links.items() if v},
        # Fill colour for the Podlove subscribe button, which renders inside an
        # iframe and so cannot read the theme's CSS custom properties. The
        # default is the engine's own --accent; if you changed that in
        # style.css, set this to the same value or the button will be the one
        # element on the page in the old colour.
        'button_color': _opt('button_color') or '#0C7C74',
    }


PODCAST = _podcast()
PODCAST_ENABLED = PODCAST is not None

# --- Images (all optional; see engine/images.py for what they gate) ---
# max_width 0 means "never downscale" - conversion still applies.
IMAGE_MAX_WIDTH = _image_setting('max_width', 1600, allow_zero=True)
IMAGE_JPEG_QUALITY = _image_setting('jpeg_quality', 82)
IMAGE_MIN_BYTES = _image_setting('min_bytes', 200_000)
