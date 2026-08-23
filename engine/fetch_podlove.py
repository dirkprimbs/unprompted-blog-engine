"""Vendor the Podlove subscribe button locally so no visitor's browser ever
contacts podlove.org.

The button is the one part of a podcast page that a reader cannot do without and
a site cannot easily build: it detects the platform, offers the podcast apps
available there, and hands the feed URL to the chosen one through its own URL
scheme (overcast://, antennapod-subscribe://, pktc://subscribe/ and about twenty
more). That last step is the whole point - a raw feed URL is close to useless to
a listener, because every app buries "add by URL" somewhere different.

Normally it is loaded from cdn.podlove.org. This script downloads it instead, on
the same reasoning as fetch_fonts.py: a page on this site should be servable
from this site. It is MIT licensed and explicitly documented as self-hostable.

Run once (and re-run only to update it):

    python3 engine/fetch_podlove.py

The downloaded tree is committed to the repo, like static/fonts/ and
static/dompurify.min.js, so a normal build and deploy needs no network access.

**The `javascripts/` path segment is load-bearing.** The widget locates its own
stylesheet, its iframe page and its ninety-odd app logos by taking its script
tag's `src` and stripping a trailing `javascripts/`. Served from
`/subscribe-button/javascripts/app.js` it finds `/subscribe-button/...`;
served from anywhere else it silently 404s everything except the JavaScript, and
the button renders as an empty box. Do not flatten this layout.

Source: github.com/podlove/cdn, which is what cdn.podlove.org actually serves -
the button's own repository does not commit its build output, and its GitHub
releases are years behind.
"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import THEME_STATIC_DIR

REPO_API = "https://api.github.com/repos/podlove/cdn"
RAW_BASE = "https://raw.githubusercontent.com/podlove/cdn/main/"
SUBTREE = "subscribe-button/"
DEST_DIR = os.path.join(THEME_STATIC_DIR, "subscribe-button")

# Pre-gzipped twins and the sourcemap are skipped. Apache compresses on the fly
# via mod_deflate (see htaccess_content), and a .map is a debugging aid for
# code we do not maintain - together they are about a third of the download for
# no benefit to any reader.
SKIP_SUFFIXES = ('.gz', '.map')


def _fetch(url, binary=True):
    req = urllib.request.Request(
        url, headers={'User-Agent': 'unprompted-blog/1.0'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode('utf-8')


def main():
    print("🎧 Vendoring the Podlove subscribe button (MIT, podlove/cdn)...")
    try:
        tree = json.loads(_fetch(f"{REPO_API}/git/trees/main?recursive=1",
                                 binary=False))
    except Exception as exc:
        print(f"❌ Could not list the repository tree: {exc}")
        print("💡 Needs network access. Nothing was written.")
        sys.exit(1)

    wanted = [
        node['path'] for node in tree.get('tree', [])
        if node.get('type') == 'blob'
        and node['path'].startswith(SUBTREE)
        and not node['path'].endswith(SKIP_SUFFIXES)
    ]
    if not wanted:
        print("❌ Found no subscribe-button files in podlove/cdn - the "
              "repository layout may have changed. Nothing was written.")
        sys.exit(1)

    written = skipped = total = 0
    for path in sorted(wanted):
        rel = path[len(SUBTREE):]
        dest = os.path.join(DEST_DIR, rel)
        try:
            data = _fetch(RAW_BASE + path)
        except Exception as exc:
            print(f"   ⚠️  {rel}: {exc}")
            continue
        total += len(data)
        # Byte-compare rather than overwrite: an unchanged file keeps its mtime,
        # so re-running this does not make the deploy re-upload 110 files.
        if os.path.exists(dest) and open(dest, 'rb').read() == data:
            skipped += 1
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as fh:
            fh.write(data)
        written += 1

    print(f"✅ {written} file(s) written, {skipped} already current "
          f"({total / 1024:.0f} KB total) in {DEST_DIR}")
    app_js = os.path.join(DEST_DIR, "javascripts", "app.js")
    if not os.path.exists(app_js):
        print("❌ javascripts/app.js is missing - the button will not load. "
              "Check the repository layout.")
        sys.exit(1)
    print("💡 The button is served from /subscribe-button/javascripts/app.js. "
          "Keep that path: the widget derives every other asset location from "
          "it.")


if __name__ == "__main__":
    main()
