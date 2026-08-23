"""Filesystem layout: where every directory the pipeline touches actually lives
on disk. Imported by build_blog.py, ingest.py, announce.py, and fetch_fonts.py
so that no module carries a bare relative path like 'content' and none of them
silently depends on being launched with the repo root as the working directory.

Division of labour with urls.py: this module owns DISK layout, urls.py owns URL
space. They are deliberately separate. urls.py's POSTS_DIR / TAGS_DIR / FEEDS_DIR
are URL segments that build_blog.py happens to reuse as public/ subdirectory
names - they are not filesystem configuration and must not move here, or someone
will "fix" them into absolute paths and break every link on the site.

REPO_ROOT is derived from this file's own location (engine/paths.py, so two
dirname hops), never from os.getcwd() or sys.argv[0] - that is the whole point.

os.path.abspath, NOT os.path.realpath: abspath only normalises, while realpath
also resolves symlinks. If the repo is reached through a symlinked parent,
realpath would make these constants disagree with any path a caller built by a
different route - and build_blog.py's stale-file sweep compares exactly that way
(GENERATED_FILES vs. a walk of PUBLIC_DIR), so a mismatch would make every
generated file look stale and the sweep would empty public/. Leave it as abspath.

Plain strings rather than pathlib.Path, to match the os.path style already used
throughout the engine and to keep f-string interpolation behaving identically.
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- site configuration ---
# Identity and presentation for THIS deployment (see config.py). The live file
# is gitignored; the tracked template beside it is what a fresh clone copies.
SITE_CONFIG_PATH = os.path.join(REPO_ROOT, "site.yaml")
SITE_CONFIG_EXAMPLE_PATH = os.path.join(REPO_ROOT, "site.example.yaml")

# --- The engine itself ---
ENGINE_DIR = os.path.join(REPO_ROOT, "engine")
# Every prompt the models are given. Data, not code - see prompts.py.
PROMPTS_PATH = os.path.join(ENGINE_DIR, "prompts.yaml")
TEMPLATES_DIR = os.path.join(ENGINE_DIR, "templates")
TEMPLATE_PATH = os.path.join(TEMPLATES_DIR, "base.html")

# A site may keep its own base.html here and it wins over the engine's, the same
# way public_static/ shadows a same-named theme file. That is the escape hatch
# for markup the engine has no config key for - a landing page that wants no RSS
# link in its header, say - and it exists because the alternative was editing
# the tracked template inside a site folder, which is an engine change that then
# travels to every other site.
#
# The trade is real and the build says so out loud when the override is in use:
# base.html is not only presentation, it carries the placeholder contract with
# build_blog.py (%SITE_LANG%, %SUBSCRIBE_ITEM%, %THEME_OVERRIDES%, ...). A fork
# keeps working and quietly stops receiving anything new. Diff it against the
# engine's after an engine update.
SITE_TEMPLATE_PATH = os.path.join(REPO_ROOT, "templates", "base.html")

# The theme: CSS, client-side search, the vendored comment sanitizer, favicons,
# and the self-hosted fonts. Tracked in git - these are engine dependencies, not
# site content. Flattened into public/ at publish time, which is why the theme is
# served from /style.css rather than /static/style.css.
THEME_STATIC_DIR = os.path.join(TEMPLATES_DIR, "static")
FONTS_DIR = os.path.join(THEME_STATIC_DIR, "fonts")
FONTS_CSS = os.path.join(THEME_STATIC_DIR, "fonts.css")

# --- Site-owned static files ---
# The author's own uploads: PDFs, standalone pages, search-engine verification
# files. Untracked and personal. Copied into public/ verbatim, exactly like the
# theme, but kept separate so the engine stays reusable by someone else.
SITE_STATIC_DIR = os.path.join(REPO_ROOT, "public_static")

# Both static sources, in COPY ORDER - which is also PRECEDENCE order, since a
# later copy overwrites an earlier one in public/. Theme first, site second, so a
# file dropped in public_static/ deliberately shadows a same-named theme file.
#
# build_blog.py's stale-file sweep MUST whitelist every entry here. It deletes
# anything in public/ it did not generate itself, and these files are copied in
# by publish.sh AFTER the build, so without the whitelist they are deleted on
# every single build - silently, because the next copy restores them with their
# mtimes intact and the FTP mirror never notices.
STATIC_SOURCE_DIRS = (THEME_STATIC_DIR, SITE_STATIC_DIR)

# Theme files only a podcast uses. They are part of the theme - they belong in
# the repo and every clone gets them - but a site with no podcast: section never
# links them, and the vendored subscribe button alone is 1.5 MB across 106
# files. publish.sh skips copying these into public/ for such a site, and
# build_blog.py leaves them out of the sweep's whitelist so an earlier build's
# copies are cleaned up rather than sitting there being uploaded forever.
PODCAST_ONLY_ASSETS = ("subscribe-button", "podcast.js")

# --- Compiled output ---
PUBLIC_DIR = os.path.join(REPO_ROOT, "public")
PUBLIC_ASSETS_DIR = os.path.join(PUBLIC_DIR, "assets")
# Episode audio is kept out of assets/ deliberately. An enclosure URL is quoted
# in feeds, in podcast directories and in other people's players, and it has to
# keep working for years - so it lives at a short, stable, obviously-audio path
# rather than sharing a directory with images the build rewrites and re-encodes.
PUBLIC_AUDIO_DIR = os.path.join(PUBLIC_DIR, "audio")

# --- Content pipeline ---
# Everything about this author's writing, as opposed to the engine that processes
# it. All of it except VOICE.example.md and TEMPLATE.md is gitignored.
PIPELINE_DIR = os.path.join(REPO_ROOT, "content_pipeline")
CONTENT_DIR = os.path.join(PIPELINE_DIR, "content")
CONTENT_ASSETS_DIR = os.path.join(CONTENT_DIR, "assets")
# Episode masters, mirrored to PUBLIC_AUDIO_DIR at build time. Unlike
# CONTENT_ASSETS_DIR this really is an archive rather than a derived cache:
# nothing here is ever re-encoded or rewritten, because the file the author
# uploaded is the one listeners downloaded and it has to stay byte-identical.
CONTENT_AUDIO_DIR = os.path.join(CONTENT_DIR, "audio")
# content_pipeline/drafts/ deliberately has no constant here: it is the human
# drafting workspace and no engine code reads it, so giving it one would only
# create an unused name to keep in sync.
SOURCES_DIR = os.path.join(PIPELINE_DIR, "sources")
PROCESSED_DIR = os.path.join(PIPELINE_DIR, "processed")

# Standalone pages (about, colophon, ...) as opposed to dated posts: one .md
# here becomes one /<name>.html at the site root, rendered through the same
# base.html so it carries the real header, footer, theme toggle and search
# rather than a hand-copied imitation of them. Nothing generates these - they
# are written by hand and never touched by ingest.py.
PAGES_DIR = os.path.join(PIPELINE_DIR, "pages")

VOICE_PATH = os.path.join(PIPELINE_DIR, "VOICE.md")
VOICE_EXAMPLE_PATH = os.path.join(PIPELINE_DIR, "VOICE.example.md")
TEMPLATE_MD_PATH = os.path.join(PIPELINE_DIR, "TEMPLATE.md")

# Generated ledgers: the tag registry and the enriched article manifest that the
# backlinking step reads. Written by build_blog.py, consumed by ingest.py.
LINK_MANIFEST_PATH = os.path.join(CONTENT_DIR, "link_manifest.json")
EXISTING_TAGS_PATH = os.path.join(CONTENT_DIR, "existing_tags.json")

# Byte lengths of remote enclosures, keyed by URL. An <enclosure> must carry a
# length and a remote file cannot be stat'd, so the build asks the host once
# with a HEAD request and remembers the answer here - which is what keeps a
# migrated back catalogue building offline after the first run.
REMOTE_AUDIO_LEDGER_PATH = os.path.join(CONTENT_DIR, "remote_audio.json")

# Comment moderation: which Mastodon replies this site declines to reproduce
# (and, for threads switched to curated mode, which ones it will). Unlike the
# two ledgers above this file is hand-maintained - nothing generates it - but it
# lives here because it is per-post editorial state about this author's content,
# so the existing 'content/' gitignore rule already keeps it local.
COMMENT_MODERATION_PATH = os.path.join(CONTENT_DIR, "comment_moderation.json")
