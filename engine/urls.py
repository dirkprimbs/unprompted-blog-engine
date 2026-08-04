"""URL and output-layout layer. Owns the site's base URL, the sectioned-layout
directory scheme, the root-absolute link builders, the tag/post slug helpers,
and the generated .htaccess. Imported by build_blog.py (to build every link,
feed, and sitemap entry) and announce.py (for the tooted post URL). Change the
layout here and every consumer follows."""

import re
import yaml

# The site's base URL is configuration, not code - it lives in site.yaml (see
# config.py). Re-exported here because this module is the URL layer, so every
# consumer that already imports SITE_URL from urls keeps working unchanged.
from config import SITE_URL

# --- OUTPUT LAYOUT ---
# The compiled site is sectioned into subdirectories: posts, tag pages, and
# per-tag feeds each get their own folder, while index.html, feed.xml and
# sitemap.xml stay at the root. Every internal link is built ROOT-ABSOLUTE (a
# leading '/') through the helpers below, so a page that lives one level deep
# (e.g. /posts/foo.html) still links correctly and a future layout change only
# has to touch these few functions.
POSTS_DIR = "posts"
TAGS_DIR = "tags"
FEEDS_DIR = "feeds"

def post_href(slug):
    """Root-absolute URL path for a post page. `slug` already ends in '.html'."""
    return f"/{POSTS_DIR}/{slug}"

def tag_page_name(tag_slug, page=1):
    """Bare filename for a tag page (page 1) or a pagination page (page > 1)."""
    return f"{tag_slug}.html" if page == 1 else f"{tag_slug}-{page}.html"

def tag_href(tag_slug, page=1):
    """Root-absolute URL path for a tag page / its pagination."""
    return f"/{TAGS_DIR}/{tag_page_name(tag_slug, page)}"

def tag_feed_name(tag_slug):
    """Bare filename for a per-tag RSS feed."""
    return f"{tag_slug}.xml"

def tag_feed_href(tag_slug):
    """Root-absolute URL path for a per-tag RSS feed."""
    return f"/{FEEDS_DIR}/{tag_feed_name(tag_slug)}"

def home_href(page=1):
    """Root-absolute URL path for the homepage / its pagination."""
    return "/index.html" if page == 1 else f"/index-{page}.html"

def slugify_tag(tag):
    """URL/filename-safe slug for a tag. Only affects links and filenames -
    the human-readable tag (e.g. 'hm...') is shown via esc(tag) elsewhere and
    keeps its punctuation. Lowercases, drops characters that aren't letters,
    digits, or spaces (Unicode letters like German umlauts are kept), then
    collapses whitespace/underscores to single hyphens. Falls back to 'tag'
    if a tag is nothing but punctuation, so we never emit an empty filename."""
    s = tag.lower()
    # Strip anything that isn't a Unicode word char (letters/digits/_) or space.
    s = re.sub(r'[^\w\s-]', '', s, flags=re.UNICODE)
    # Collapse runs of spaces/underscores/hyphens into a single hyphen.
    s = re.sub(r'[\s_-]+', '-', s)
    s = s.strip('-')
    return s or 'tag'

def slug_for(filename, frontmatter):
    """A post's public URL (with .html). Prefer the explicit 'slug' frontmatter
    field so the on-disk filename (dated) can differ from the URL (short/clean);
    fall back to the filename for older posts that predate the field."""
    slug = (frontmatter or {}).get('slug')
    if slug:
        slug = str(slug).strip()
        return slug if slug.endswith('.html') else f"{slug}.html"
    return filename.replace('.md', '.html')

def read_slug(filepath, filename):
    """Cheap slug lookup for the link-validity pre-pass: read only the
    frontmatter block (not the whole post) and apply slug_for. Falls back to the
    filename if the file can't be read or has no frontmatter."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
        m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
        fm = yaml.safe_load(m.group(1)) if m else None
        if not isinstance(fm, dict):
            fm = None
        return slug_for(filename, fm)
    except Exception:
        return filename.replace('.md', '.html')

def htaccess_content():
    """The generated public/.htaccess: legacy-URL redirects plus text
    compression and cache headers. Every block is wrapped in an <IfModule> guard
    so a missing module is a no-op rather than a 500. Needs an Apache host that
    honors .htaccess.

    Redirects: 301 the old flat-root URLs to the sectioned layout so historical
    links (bookmarks, search results, the URLs in already-published Mastodon
    toots, and feed subscriptions) keep working - one file instead of a stub per
    URL. The post rule is existence-checked (RewriteCond -f) so it covers every
    post without enumeration and never touches a real root file like index.html.

    Compression: gzip text responses, which matters most for the lazy-loaded
    full-text search index (see the search-index.json build step).

    Caching: three tiers, and the tier depends entirely on whether a file's URL
    changes when its bytes do. Fonts and the theme's CSS/JS are immutable for a
    year because they are versioned - the fonts by fetch_fonts.py, the CSS/JS by
    the ?v= content hash the template stamps on every link to them. Images get a
    long but revalidating month, because re-exporting a photo under the same
    name reuses its URL. HTML, feeds and the search index stay short, because
    they change in place. Block order matters here; see the note inline."""
    return (
        "# Generated by build_blog.py - do not edit by hand.\n"
        "\n"
        "# --- Legacy URL redirects (flat root -> sectioned layout) ---\n"
        "<IfModule mod_rewrite.c>\n"
        "    RewriteEngine On\n"
        "\n"
        "    # Old per-tag pages: /tag-<slug>.html -> /tags/<slug>.html\n"
        "    RewriteRule ^tag-(.+)\\.html$ /tags/$1.html [R=301,L]\n"
        "\n"
        "    # Old per-tag feeds: /feed-tag-<slug>.xml -> /feeds/<slug>.xml\n"
        "    RewriteRule ^feed-tag-(.+)\\.xml$ /feeds/$1.xml [R=301,L]\n"
        "\n"
        "    # Old flat post URLs: /<slug>.html -> /posts/<slug>.html, but only\n"
        "    # when that post exists, so real root files (index.html, any hand-\n"
        "    # uploaded pages) are never redirected.\n"
        "    RewriteCond %{DOCUMENT_ROOT}/posts/$1.html -f\n"
        "    RewriteRule ^([^/]+)\\.html$ /posts/$1.html [R=301,L]\n"
        "</IfModule>\n"
        "\n"
        "# --- Compression ---\n"
        "<IfModule mod_mime.c>\n"
        "    AddType application/json .json\n"
        "</IfModule>\n"
        "<IfModule mod_deflate.c>\n"
        "    AddOutputFilterByType DEFLATE text/html text/css text/xml \\\n"
        "        application/xml application/rss+xml application/json \\\n"
        "        application/javascript text/javascript image/svg+xml\n"
        "</IfModule>\n"
        "\n"
        "# --- Caching ---\n"
        "<IfModule mod_headers.c>\n"
        "    # Fonts and the theme's CSS/JS never change under a given URL: the\n"
        "    # font files are vendored and versioned by fetch_fonts.py, and every\n"
        "    # link to the CSS/JS carries a ?v= content hash (see _asset_version\n"
        "    # in build_blog.py), so a change ships as a new URL. That is what\n"
        "    # makes 'immutable' honest here rather than a way to strand readers\n"
        "    # on last month's stylesheet.\n"
        '    <FilesMatch "\\.(woff2|ttf|otf)$">\n'
        '        Header set Cache-Control "public, max-age=31536000, immutable"\n'
        "    </FilesMatch>\n"
        '    <FilesMatch "^(style|fonts)\\.css$">\n'
        '        Header set Cache-Control "public, max-age=31536000, immutable"\n'
        "    </FilesMatch>\n"
        '    <FilesMatch "^(comments|dompurify\\.min)\\.js$">\n'
        '        Header set Cache-Control "public, max-age=31536000, immutable"\n'
        "    </FilesMatch>\n"
        "\n"
        "    # Images and icons are not content-addressed - re-exporting a photo\n"
        "    # under the same name reuses its URL - so they get a long cache but\n"
        "    # not an immutable one. Thirty days, then revalidate.\n"
        '    <FilesMatch "\\.(jpg|jpeg|png|gif|webp|avif|svg|ico)$">\n'
        '        Header set Cache-Control "public, max-age=2592000"\n'
        "    </FilesMatch>\n"
        "\n"
        "    # HTML itself must not be cached hard: a post's page changes when a\n"
        "    # comment thread or a backlink does, under the same URL.\n"
        '    <FilesMatch "\\.(html|xml|json)$">\n'
        '        Header set Cache-Control "max-age=600, must-revalidate"\n'
        "    </FilesMatch>\n"
        "\n"
        "    # Deliberately last. Apache applies matching FilesMatch blocks in\n"
        "    # order and a later 'Header set' replaces an earlier one, so this\n"
        "    # has to follow the .json rule above or search-index.json would be\n"
        "    # caught by it and silently get the shorter HTML lifetime.\n"
        "    #\n"
        "    # An hour, then revalidate: long enough that searching twice does\n"
        "    # not re-download the index, short enough that a new post becomes\n"
        "    # findable the same day. search.js keeps the same lifetime despite\n"
        "    # carrying a ?v= - it is fetched together with the index it drives,\n"
        "    # and matching their lifetimes keeps the pair consistent.\n"
        '    <FilesMatch "^(search-index\\.json|search\\.js)$">\n'
        '        Header set Cache-Control "max-age=3600, must-revalidate"\n'
        "    </FilesMatch>\n"
        "</IfModule>\n"
    )
