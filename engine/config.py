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
    'links': ('about', 'mastodon', 'fediverse_creator'),
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
LINK_MASTODON = str(_cfg['links']['mastodon'])
FEDIVERSE_CREATOR = str(_cfg['links']['fediverse_creator'])


def _optional_link(key):
    """One optional links.* string, empty when unset.

    Deliberately not in _REQUIRED: Bluesky arrived after this config existed, so
    every site.yaml already written predates these keys and must keep working
    untouched. Empty is the honest "not configured" value here - unlike the
    numbers in _int(), a blank link disables a feature rather than corrupting
    one, so there is nothing to fail loudly about."""
    value = _cfg['links'].get(key)
    return '' if value is None else str(value).strip()


# Bluesky, the second comment provider. LINK_BLUESKY is the header link (shown
# only when set); BLUESKY_CREATOR is the handle or DID used to badge the
# author's own replies, the counterpart of FEDIVERSE_CREATOR.
LINK_BLUESKY = _optional_link('bluesky')
BLUESKY_CREATOR = _optional_link('bluesky_creator')

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


# --- Images (all optional; see engine/images.py for what they gate) ---
# max_width 0 means "never downscale" - conversion still applies.
IMAGE_MAX_WIDTH = _image_setting('max_width', 1600, allow_zero=True)
IMAGE_JPEG_QUALITY = _image_setting('jpeg_quality', 82)
IMAGE_MIN_BYTES = _image_setting('min_bytes', 200_000)
