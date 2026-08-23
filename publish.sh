#!/bin/bash

# Exit instantly if any command fails
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_FILE="$SCRIPT_DIR/publish.local.sh"
SETUP_MARKER="$SCRIPT_DIR/.setup_done"

# Folders the pipeline reads from or writes to. Safe to (re)create empty.
REQUIRED_DIRS=(content_pipeline/sources content_pipeline/content content_pipeline/processed public public_static)

usage() {
    cat <<'USAGE'
Usage: ./publish.sh [option]

  (no option)   Full pipeline: ingest drafts from content_pipeline/sources/,
                build, ask for confirmation, then deploy.
  --rebuild     Skip ingestion. Rebuild from existing content, confirm, deploy.
  --ingest      Ingest drafts only. No build, no upload.
  --build       Build only. No confirmation, no upload. Safe to run anytime.
  --serve [port]
                Serve the built public/ on 127.0.0.1 (default port 8000) so you
                can look at it in a browser. Ctrl-C to stop. No build, no upload.
  --deploy      Upload the existing public/ as-is. No rebuild, no confirmation
                prompt - passing the flag IS the confirmation.
  --setup       Re-run dependency diagnostics and create missing folders.
  --help        Show this message.

The three pipeline stages, each runnable on its own:
  ./publish.sh --ingest    # drafts -> content, then read what the model wrote
  ./publish.sh --build     # content -> public
  ./publish.sh --serve     # look at what you just built, in a real browser
  ./publish.sh --deploy    # ship exactly what you just looked at
USAGE
}

# Create any missing project folders (idempotent, runs on every invocation).
ensure_dirs() {
    for d in "${REQUIRED_DIRS[@]}"; do
        if [ ! -d "$d" ]; then
            mkdir -p "$d"
            echo "📁 Created missing folder: $d/"
        fi
    done
}

# VOICE.md holds the personal writing voice and is gitignored, so a fresh clone
# won't have one. Seed it from the shipped example so ingestion has something to
# work with; the user is expected to replace the sample with their own writing.
ensure_voice() {
    if [ ! -f "content_pipeline/VOICE.md" ] && [ -f "content_pipeline/VOICE.example.md" ]; then
        cp content_pipeline/VOICE.example.md content_pipeline/VOICE.md
        echo "📝 Created VOICE.md from VOICE.example.md - edit it with your own writing sample."
    fi
}

# site.yaml holds this deployment's identity (name, URL, author, links) and is
# gitignored, so a fresh clone won't have one. Seed it from the tracked example
# exactly like VOICE.md; the user is expected to put their own details in.
ensure_site_config() {
    if [ ! -f "site.yaml" ] && [ -f "site.example.yaml" ]; then
        cp site.example.yaml site.yaml
        echo "🌐 Created site.yaml from site.example.yaml - edit it with your site name, URL, and links."
    fi
}

# True when publish.local.sh assigns the named variable itself, as opposed to
# the value merely being present in the environment already. Sourcing the config
# overwrites an inherited value, so this - not the variable's contents - is what
# says whether a secret belongs to THIS site.
#
# Matches an assignment at the start of a line, with or without `export`, so a
# commented-out example line in the shipped template does not count as set.
config_assigns() {
    [ -f "$CONFIG_FILE" ] || return 1
    grep -qE "^[[:space:]]*(export[[:space:]]+)?$1=" "$CONFIG_FILE"
}

# First-run diagnostic: verify key dependencies. Core deps (❌) block the
# build; optional deps (⚠️) only matter for ingestion or deploy.
run_diagnostics() {
    echo "🩺 Running diagnostics..."
    local core_ok=1

    # Load local config early (if present) so the OpenRouter key check below
    # reflects what an actual run would see. Safe: this file is user-owned.
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck source=publish.local.sh
        source "$CONFIG_FILE"
    fi

    # --- Core: needed to build the site ---
    if command -v python3 >/dev/null 2>&1; then
        echo "  ✅ python3 ($(python3 --version 2>&1))"
    else
        echo "  ❌ python3 not found — required to build the site."
        core_ok=0
    fi

    if python3 -c "import markdown, yaml" >/dev/null 2>&1; then
        echo "  ✅ Python packages: markdown, PyYAML"
    else
        echo "  ❌ Missing Python packages — run: pip install -r requirements.txt"
        core_ok=0
    fi

    # Optional, not part of core_ok: without Pillow the build still produces a
    # correct site, it just ships photographs at whatever size they were
    # exported at. Worth flagging because the difference is megabytes per page.
    if python3 -c "import PIL" >/dev/null 2>&1; then
        echo "  ✅ Pillow — build-time image optimisation enabled"
    else
        echo "  ⚠️  Pillow missing — images hosted as-is (no resize/convert)."
        echo "     Install it with: pip install -r requirements.txt"
    fi

    # --- Optional: ingestion (turning sources/ drafts into content/) ---
    # Ask ingest.py which (provider, model) pairs it actually uses, so this
    # check never drifts from the configured POLISH/UTILITY/TRIAGE roles.
    # Emits one "provider model" line per distinct role.
    ROLE_LINES=$(PYTHONPATH=engine python3 -c "import ingest; print('\n'.join(sorted({r['provider']+' '+r['model'] for r in ingest.ALL_ROLES})))" 2>/dev/null)

    OLLAMA_MODELS=$(echo "$ROLE_LINES" | grep '^ollama ' | cut -d' ' -f2)
    USES_OPENROUTER=$(echo "$ROLE_LINES" | grep -c '^openrouter ' || true)

    if [ -n "$OLLAMA_MODELS" ]; then
        if python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:11434/api/tags', timeout=3)" >/dev/null 2>&1; then
            echo "  ✅ Ollama reachable at localhost:11434"
            for model in $OLLAMA_MODELS; do
                if python3 -c "import urllib.request, json, sys; d=json.load(urllib.request.urlopen('http://localhost:11434/api/tags', timeout=3)); sys.exit(0 if any('$model' in m.get('name','') for m in d.get('models', [])) else 1)" >/dev/null 2>&1; then
                    echo "  ✅ Model '$model' available"
                else
                    echo "  ⚠️  Model '$model' not pulled — run: ollama pull $model (needed for ingestion)"
                fi
            done
        else
            echo "  ⚠️  Ollama not reachable — needed for draft ingestion (not for --rebuild)"
        fi
    fi

    if [ "${USES_OPENROUTER:-0}" -gt 0 ]; then
        if [ -n "${OPENROUTER_API_KEY:-}" ]; then
            # Name where the key came from. A key exported globally (~/.zshrc,
            # ~/.profile) satisfies this check without appearing in any file
            # here, so "present" alone would read as "this site is configured"
            # when in fact every instance of the engine on this machine is
            # quietly sharing - and billing - one key.
            if config_assigns OPENROUTER_API_KEY; then
                echo "  ✅ OPENROUTER_API_KEY present (from publish.local.sh)"
            else
                echo "  ✅ OPENROUTER_API_KEY present — inherited from your shell"
                echo "     environment, not publish.local.sh. Remote calls from this"
                echo "     site bill that key; set it here to give this site its own."
            fi
        else
            echo "  ⚠️  A model role uses OpenRouter but OPENROUTER_API_KEY is unset — export it in publish.local.sh"
        fi
    fi

    # --- Optional: deploy ---
    if command -v lftp >/dev/null 2>&1; then
        echo "  ✅ lftp installed"
    else
        echo "  ⚠️  lftp not found — needed to deploy via FTP (build/preview still works)"
    fi

    if [ -f "$CONFIG_FILE" ]; then
        echo "  ✅ FTP credentials present (publish.local.sh)"
    else
        echo "  ⚠️  publish.local.sh missing — copy publish.local.example.sh and add credentials to deploy"
    fi

    echo "--------------------------------------------------"
    if [ "$core_ok" -eq 1 ]; then
        touch "$SETUP_MARKER"
        echo "✅ Setup complete."
    else
        echo "❌ Core dependencies missing — fix the ❌ items above, then re-run."
        exit 1
    fi
    echo "--------------------------------------------------"
}

if [ "${1:-}" == "--help" ] || [ "${1:-}" == "-h" ]; then
    usage
    exit 0
fi

# Explicit re-run: ./publish.sh --setup
if [ "$1" == "--setup" ]; then
    ensure_dirs
    ensure_voice
    ensure_site_config
    run_diagnostics
    exit 0
fi

# Always keep folders in place; run diagnostics once on first use.
ensure_dirs
ensure_voice
ensure_site_config
if [ ! -f "$SETUP_MARKER" ]; then
    run_diagnostics
fi

# --build and --serve never touch the network, so they are the modes that work
# without credentials - useful on a fresh clone, or when you just want to look at
# the compiled site. Every other mode can end up uploading, so it needs the
# config.
if [ ! -f "$CONFIG_FILE" ] && [ "${1:-}" != "--build" ] && [ "${1:-}" != "--serve" ]; then
    echo "❌ Missing $CONFIG_FILE"
    echo "💡 Copy publish.local.example.sh to publish.local.sh and add your FTP credentials."
    echo "   (./publish.sh --build works without it.)"
    exit 1
fi

if [ -f "$CONFIG_FILE" ]; then
    # shellcheck source=publish.local.sh
    source "$CONFIG_FILE"
fi

# Export secrets so child processes (ingest.py, announce.py) can read them.
# Harmless if unset or already exported.
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
    export OPENROUTER_API_KEY
fi
# Announcement config for both comment providers (see publish.local.sh).
# announce.py degrades gracefully per network: each is skipped on its own when
# its credentials are missing, and skipped entirely when neither is set.
[ -n "${MASTODON_SERVER:-}" ] && export MASTODON_SERVER
[ -n "${MASTODON_TOKEN:-}" ] && export MASTODON_TOKEN
[ -n "${MASTODON_ID:-}" ] && export MASTODON_ID
[ -n "${BLUESKY_HANDLE:-}" ] && export BLUESKY_HANDLE
[ -n "${BLUESKY_APP_PASSWORD:-}" ] && export BLUESKY_APP_PASSWORD
[ -n "${BLUESKY_PDS:-}" ] && export BLUESKY_PDS

# Compile the static site and inject static/ assets into public/.
compile_site() {
    echo "🔨 Compiling Medium-style static pages..."
    python3 engine/build_blog.py

    # Copy manual static files into public before syncing.
    # --preserve=timestamps is important: a plain cp resets each file's mtime to
    # "now", which makes lftp's mirror (size + time comparison) re-upload every
    # static file on every publish even when the bytes are identical. Keeping
    # the source mtime lets unchanged static files be skipped, matching how
    # build_blog.py's own writes already preserve timestamps.
    # Two static sources are flattened into public/ root, which is why the
    # theme is served from /style.css and not /static/style.css. Order is
    # precedence: engine theme first, the site's own files second, so anything
    # in public_static/ deliberately shadows a same-named theme file.
    # Both dirs MUST also be listed in engine/paths.py STATIC_SOURCE_DIRS, or
    # build_blog.py's stale-file sweep deletes them from public/ on every build.
    if [ -d "engine/templates/static" ]; then
        echo "📁 Injecting engine theme assets..."
        cp -R --preserve=timestamps engine/templates/static/. public/
        # The vendored subscribe button and the episode player are theme files,
        # but only a podcast links them - and the button alone is 1.5 MB across
        # 106 files. A site with no podcast: section would otherwise upload all
        # of it, forever, to serve nothing. build_blog.py's sweep leaves the
        # same files out of its whitelist, so a site that used to have them
        # loses them on the next build rather than keeping them for good.
        if ! python3 -c "import sys; sys.path.insert(0, 'engine'); import config; sys.exit(0 if config.PODCAST_ENABLED else 1)" >/dev/null 2>&1; then
            rm -rf public/subscribe-button public/podcast.js
        fi
    fi
    if [ -d "public_static" ]; then
        echo "📁 Injecting site static assets..."
        cp -R --preserve=timestamps public_static/. public/
    fi
}

# Mirror public/ to the live web space over FTP.
ftp_sync() {
    echo "☁️  Syncing public/ folder to web space via FTP..."
    lftp -u "$FTP_USER","$FTP_PASS" "$FTP_HOST" <<EOF
# Encrypt the connection (FTPS / AUTH TLS). ssl-force refuses to fall back to
# plaintext, so credentials and files are never sent in the clear even if the
# server would allow it; ssl-protect-data encrypts the data channel too, not
# just the login. If a host genuinely can't do FTPS, ask it about SFTP rather
# than turning this off. verify-certificate stays on (the default) so a
# man-in-the-middle can't present a bogus cert.
set ftp:ssl-force yes
set ftp:ssl-protect-data yes
# List hidden files. FTP's LIST omits dotfiles by default, which means lftp
# cannot see them, cannot delete them, and cannot remove a directory that only
# still contains one. Migrating onto this engine is where that bites: the old
# site's per-directory .htaccess files survive the mirror invisibly, keeping
# their directories alive - and a leftover .htaccess gives mod_rewrite a fresh
# per-directory context, so the redirects in our own .htaccess never run for
# anything underneath it. The symptom is one old URL that stubbornly 404s while
# every other redirect works.
set ftp:list-options -a
mkdir -p "$FTP_REMOTE_DIR"
cd "$FTP_REMOTE_DIR"
mirror -R public/ . --delete --verbose
quit
EOF
}

preview_banner() {
    echo "--------------------------------------------------"
    echo "👀 Local compilation finished successfully."
    # Not "open public/index.html": every internal link is root-absolute
    # (/style.css, /posts/slug.html), which resolves against the filesystem root
    # under file:// and 404s. The site needs to be served from somewhere.
    echo "👉 Look at it with: ./publish.sh --serve"
    echo "--------------------------------------------------"
}

# Serve the built site locally. This is not a dev server - there is no watching
# and no reloading, because a build is one command and pretending otherwise would
# mean a second code path that can disagree with the real one.
#
# Two deliberate limits worth knowing before you trust what you see:
#   - It binds 127.0.0.1, NOT 0.0.0.0. An unpublished site is nobody else's
#     business, and a laptop on a cafe network should not be quietly hosting the
#     next post to everyone on it. Change this only on purpose.
#   - It does not read .htaccess, so the 301s from the old flat URLs, the gzip
#     and the immutable caching are all absent here. The pages and their links
#     are what this checks; the redirects need the real Apache host.
#
# It is engine/preview_server.py rather than `python3 -m http.server` for one
# reason: the stdlib server answers a Range request with the whole file, and a
# browser reads that as "cannot do partial content" and disables seeking in
# audio. The episode player then looks broken while being perfectly correct.
serve_site() {
    local port="${1:-8000}"
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        echo "❌ Not a usable port: $port"
        echo "💡 Usage: ./publish.sh --serve [port]   (default 8000)"
        exit 1
    fi
    if [ ! -f "public/index.html" ]; then
        echo "❌ No built site found at 'public/index.html' - nothing to serve."
        echo "💡 Run ./publish.sh --build first."
        exit 1
    fi
    echo "--------------------------------------------------"
    echo "🌐 Serving public/ at http://127.0.0.1:$port"
    echo "   $(find public -type f | wc -l) files, last built $(date -r public/index.html '+%Y-%m-%d %H:%M')"
    echo "   Local only, and .htaccess is not applied - old-URL redirects,"
    echo "   gzip and cache headers need the real host."
    echo "   Ctrl-C to stop."
    echo "--------------------------------------------------"
    # exec: this is the last thing the script does, and handing the process
    # straight to python means Ctrl-C stops the server instead of being caught
    # somewhere in between.
    exec python3 engine/preview_server.py "$port" public
}

# Refuse to upload when there is nothing built. ftp_sync mirrors with --delete,
# so syncing an empty or half-built public/ would strip the LIVE site rather
# than simply failing. Only relevant for --deploy, which skips the build that
# would otherwise have just produced these files.
require_built_site() {
    if [ ! -f "public/index.html" ]; then
        echo "❌ No built site found at 'public/index.html' - nothing to upload."
        echo "💡 Run ./publish.sh --build first."
        exit 1
    fi
}

# Upload, then announce, then - only if announcing rewrote frontmatter - rebuild
# and upload a second time so the new comment threads are baked into the pages.
deploy() {
    # First sync: publish the pages so their URLs resolve. This must happen
    # BEFORE announcing, so Mastodon can fetch a working link-preview card for
    # the toot (option b: perfect preview, at the cost of a possible second sync).
    # Bluesky builds its card from what announce.py sends rather than by
    # crawling, but the link in the post still has to resolve when read.
    ftp_sync

    # Announce any 'announce: pending' posts. announce.py exits 2 when it wrote
    # coordinates into frontmatter (-> the pages now need the comment thread
    # baked in), 0 when there was nothing to do or it was skipped.
    echo "📣 Checking for posts to announce..."
    set +e
    python3 engine/announce.py
    ANNOUNCE_RC=$?
    set -e

    if [ "$ANNOUNCE_RC" -eq 2 ]; then
        echo "🔁 New announcements written to frontmatter - rebuilding so comment threads appear..."
        compile_site
        ftp_sync
    fi

    echo "🎉 Success! Your blog has been updated and is live!"
}

# Build, show the preview banner, ask, then deploy. Used by the default
# (ingest) flow and by --rebuild; the standalone --build and --deploy modes
# call the pieces directly instead.
run_generation_and_deploy() {
    compile_site
    preview_banner

    # Interactive Review Prompt (Y/N Check)
    read -p "❓ Do you want to push these updates to your live web space? (y/N): " CONFIRMATION

    # Convert the input to lowercase
    CONFIRMATION=$(echo "$CONFIRMATION" | tr '[:upper:]' '[:lower:]')

    if [ "$CONFIRMATION" != "y" ] && [ "$CONFIRMATION" != "yes" ]; then
        echo "🛑 Upload aborted. Your changes are saved locally but have NOT been pushed live."
        exit 0
    fi

    deploy
}

# Ingest every draft in content_pipeline/sources/ into content_pipeline/content/.
# Reports its results through three globals rather than an exit status, because
# the three outcomes are genuinely different and each caller reacts differently:
#   DRAFT_TOTAL      how many drafts were queued (0 is normal, not an error)
#   PROCESSED_COUNT  how many became posts
#   FAILED_COUNT     how many did not
# A draft that fails is left in sources/ for retry and never aborts the batch.
run_ingestion() {
    shopt -s nullglob
    RAW_DRAFTS=(content_pipeline/sources/*)
    shopt -u nullglob

    DRAFT_TOTAL=${#RAW_DRAFTS[@]}
    PROCESSED_COUNT=0
    FAILED_COUNT=0

    if [ "$DRAFT_TOTAL" -eq 0 ]; then
        echo "ℹ️  No new drafts found in content_pipeline/sources/."
        return 0
    fi

    echo "📚 Found $DRAFT_TOTAL new draft(s) to process..."

    # Cache the voice guidelines, template configurations, and contextual manifestations outside the loop
    VOICE_CONTEXT=$(cat content_pipeline/VOICE.md)

    # Grab the current system date dynamically
    CURRENT_DATE=$(date +%Y-%m-%d)
    # Swap the placeholder in TEMPLATE.md with today's date
    TEMPLATE_CONTEXT=$(cat content_pipeline/TEMPLATE.md | sed "s/%DATE%/$CURRENT_DATE/g")
    EXISTING_TAGS=$(cat content_pipeline/content/existing_tags.json 2>/dev/null || echo "[]")
    LINK_MANIFEST=$(cat content_pipeline/content/link_manifest.json 2>/dev/null || echo "[]")

    # Loop through each draft file in the queue
    for DRAFT_FILE in "${RAW_DRAFTS[@]}"; do
        FILENAME=$(basename "$DRAFT_FILE")

        echo "--------------------------------------------------"
        echo "🚀 Processing: $FILENAME..."
        echo "--------------------------------------------------"

        # ingest.py decides the output filename (dated, derived from the title), so
        # we don't guess it here. On failure ingest.py removes its own partial
        # output before exiting non-zero, so there is nothing for us to clean up.
        if python3 engine/ingest.py "$DRAFT_FILE"; then
            mv "$DRAFT_FILE" "content_pipeline/processed/$FILENAME"
            echo "📦 Moved $FILENAME to processed/"
            PROCESSED_COUNT=$((PROCESSED_COUNT + 1))
        else
            echo "⚠️  Pipeline execution failed for $FILENAME"
            echo "↩️  Leaving $DRAFT_FILE in content_pipeline/sources/ for retry"
            FAILED_COUNT=$((FAILED_COUNT + 1))
        fi
    done

    echo "--------------------------------------------------"
    echo "✨ Ingestion complete: $PROCESSED_COUNT succeeded, $FAILED_COUNT failed"
    echo "--------------------------------------------------"
    return 0
}

# 1. MODE DISPATCH
# An empty $1 falls through to batch ingestion below; anything unrecognised is
# rejected rather than silently treated as "run the full pipeline", so a typo
# like --rebiuld can't ingest and deploy when you meant to skip ingestion.
case "${1:-}" in
    --ingest)
        echo "📥 Ingest Mode. Processing drafts only - nothing will be built or uploaded..."
        run_ingestion
        if [ "$DRAFT_TOTAL" -eq 0 ]; then
            echo "💡 Put drafts in content_pipeline/sources/ first."
            exit 0
        fi
        if [ "$PROCESSED_COUNT" -eq 0 ]; then
            echo "🛑 No posts were written to content_pipeline/content/."
            exit 1
        fi
        echo "💡 Review the new posts in content_pipeline/content/, then: ./publish.sh --build"
        exit 0
        ;;
    --build)
        echo "🔨 Build Mode. Compiling only - nothing will be uploaded..."
        compile_site
        preview_banner
        echo "💡 When it looks right, ship it with: ./publish.sh --deploy"
        exit 0
        ;;
    --serve)
        serve_site "${2:-}"
        ;;
    --deploy)
        echo "☁️  Deploy Mode. Uploading the existing public/ without rebuilding..."
        require_built_site
        echo "   $(find public -type f | wc -l) files, last built $(date -r public/index.html '+%Y-%m-%d %H:%M')"
        deploy
        exit 0
        ;;
    --rebuild)
        echo "🔄 Rebuild Mode Activated. Skipping AI ingestion..."
        run_generation_and_deploy
        exit 0
        ;;
    "")
        ;;  # no flag - continue into batch ingestion
    *)
        echo "❌ Unknown option: $1"
        echo
        usage
        exit 1
        ;;
esac

# 2. BATCH INGESTION MODE (no flag): ingest, then build, confirm, and deploy.
run_ingestion

if [ "$DRAFT_TOTAL" -eq 0 ]; then
    echo "💡 To rebuild your site using current content, use: ./publish.sh --rebuild"
    exit 0
fi

if [ "$PROCESSED_COUNT" -eq 0 ]; then
    echo "🛑 No posts were written to content_pipeline/content/. Skipping build and deploy."
    exit 1
fi

if [ "$FAILED_COUNT" -gt 0 ]; then
    echo "⚠️  Some drafts failed. Building site with the posts that succeeded."
fi

# Build, show the preview banner, then ask before anything goes live. That y/N
# gate is the whole reason the unflagged run is safe to fire and forget.
run_generation_and_deploy
