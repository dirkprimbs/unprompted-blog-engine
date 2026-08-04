# `build_blog.py` — function reference

> Docstrings extracted from `build_blog.py` and kept here so the source
> stays compact. These are documentation only (nothing reads `__doc__`,
> no doctests, no argparse). When you change a function's behavior, update
> its entry here. Functional `f"""` string literals that generate HTML/RSS
> output stay in the source and are NOT listed here.

### `esc(value)`

HTML-escape AI/frontmatter-derived text before it goes into markup or
attribute values. Titles, summaries, and tags come from model output and
aren't guaranteed to be free of characters like " < > & — without this,
a stray double-quote in a summary can break out of a content="..."
attribute, and stray HTML/JS would be injected verbatim.

### `tag_emoji_html(tag)`

Return `<span class="tag-emoji" aria-hidden="true">…</span>` for a tag that has
an entry in `display.tag_emoji` (see `site.yaml`), or `''` for one that doesn't.

The emoji is decoration keyed off the tag, never part of it. The tag text stays
the identity used for slugs, feed categories, the search index and the tag
ledger, so adding or removing an entry moves no URLs and creates no duplicate
topics. Emitted in visible chrome only - the badges on post cards and article
meta lines, the homepage tag pills, and the tag-page heading - and deliberately
not in the page `<title>`, feeds, or structured data.

`aria-hidden` because it decorates a label already present in text: a screen
reader should announce "Photography", not "camera Photography".

### `reading_time_minutes(markdown_text)`

Estimate reading time in whole minutes from a post's Markdown body, at
`display.words_per_minute` from site.yaml.

Counts words after stripping the mechanics that aren't prose a reader
actually reads at speaking pace: fenced code blocks, inline code, image
markup, and link/URL syntax (keeping the visible link text). Always at
least 1 minute so no post reports '0 min read'.

### `plain_text(html_body)`

Strip a rendered HTML body down to readable plain text for the search
index: drop script/style blocks and all tags, unescape entities, and
collapse whitespace. Not for display - just a bag of words to match on.

### `load_template()`

Loads the base layout HTML file dynamically.

### `write_file_if_changed(filepath, content)`

Writes content to filepath only if it has changed, preserving mtime.

### `copy_asset_if_changed(src, dest)`

Copies file from src to dest only if size differs, preserving mtime.

### `_youtube_id(url)`

Extract the 11-character video ID from a YouTube URL, or return None.
Covers the shapes a link can arrive in - watch?v=, youtu.be/, /embed/,
/shorts/ - via YT_ID_RE, so a draft can paste whichever one the share
button produced.

### `_youtube_thumbnail_src(video_id)`

Return a site-relative src for a YouTube video's thumbnail, self-hosting
it so the reader's browser never contacts Google before they press play.

The thumbnail is fetched once at build time into content_pipeline/content/assets/ (the source
of truth, so it survives a public/ wipe and isn't re-downloaded every build)
and mirrored into public/assets/. Returns 'assets/yt_<id>.jpg'.

If the fetch fails (e.g. building offline), falls back to hotlinking
Google's CDN and warns loudly - because that fallback quietly reintroduces
the third-party request the self-hosting exists to prevent.

### `embed_youtube(markdown_text)`

Turn a standalone YouTube link into a click-to-play facade.

A "standalone" link is a whole paragraph that is just a YouTube URL - either
bare (https://youtu.be/ID) or a Markdown link ([text](https://youtu.be/ID)).
The facade shows a self-hosted copy of the video's thumbnail and only loads
the real iframe (and YouTube's cookies/JS) when the reader clicks. Because
the thumbnail is served from this site, not Google's CDN, opening a post
with a video in it sends nothing to YouTube until the reader presses play.
Inline links inside a sentence are left untouched.

Runs before markdown.markdown(); the injected HTML passes through the
'extra' extension unchanged because it's a block-level element.

### `load_comment_moderation()`

Read `content_pipeline/content/comment_moderation.json` once per build and
return its `posts` map (slug -> `{"blocked": [...], "approved": [...]}`, both
flat lists of status IDs from either network). A missing file means "nothing
moderated" and is normal. Malformed JSON, or a missing/non-object `posts` key,
aborts the build: this file is the control that keeps removed replies off the
site, so degrading to an empty blocklist would silently republish them.

### `_classify_moderation_id(raw)`

Which network one moderation ID names, from its shape: all digits is a Mastodon
status ID, a leading `at://` is a Bluesky post URI. Returns None for anything
else, which the caller treats as fatal. The two formats cannot be confused, so
the author's file stays one flat list per post and nothing already in it needed
rewriting when Bluesky was added.

A bsky.app permalink is deliberately **not** accepted: it carries a handle
rather than a DID, and resolving one to the other would need a network call at
build time. Every rendered comment carries its exact ID in a `data-comment-id`
attribute, so moderating one is a copy-paste from the page.

### `_moderation_ids(entry, key)`

Split one moderation list by network, returning
`{"mastodon": [...], "bluesky": [...]}` as strings. IDs are compared as strings
client-side and each network gets its own data attribute, so the split happens
here rather than in the browser. An ID matching no known format aborts the
build, on the same principle as the rest of this file: silently ignoring it
would quietly un-block a reply.

### `_author_acct()`

The site author's fediverse handle in the form Mastodon's `acct` field uses.
`site.yaml`'s `fediverse_creator` is `@user@instance`; the API reports
`user@instance` for remote accounts and bare `user` for accounts local to the
queried instance, so the leading `@` is stripped and the client normalises the
bare form. Empty when unset, which just disables author detection.

### `_author_bluesky()`

The same thing for Bluesky, from `site.yaml`'s `links.bluesky_creator`. Accepts
a handle or a DID; the client compares against both fields the API returns, so
which one is configured doesn't matter. Empty when unset.

### `bluesky_permalink(uri)`

The public bsky.app URL for an `at://` post URI. An AT-URI is
`at://<authority>/<collection>/<rkey>` and the web permalink is
`/profile/<authority>/post/<rkey>`. The authority (a DID) is used as-is rather
than resolved to a handle, because handles are not durable - a permalink built
from one breaks when its owner renames. A malformed URI aborts the build:
announce.py writes this key itself, so a bad value can only come from
hand-editing, and the alternative is a thread that silently renders empty.

### `build_comments_block(meta)`

Return the comments section for a post, or '' when the post is linked to
neither network / has comments disabled.

A post opts in by carrying `mastodon_host` + `mastodon_id` and/or `bluesky_uri`
in its frontmatter (written automatically by announce.py, or by hand for older
posts). **Either provider alone is enough** - a post announced only on Mastodon,
which is every post predating Bluesky support, is unchanged. When both are
present the two conversations are blended into one chronological thread
client-side (see `static/comments.js`). Setting 'comments: false' suppresses the
widget without removing the links, so it can be re-opened later. Here we only
emit the container carrying the thread coordinates and a `<noscript>` fallback
to the original posts.

`data-sanitizer-src` is emitted only alongside the Mastodon coordinates. Only
Mastodon bodies are HTML and need DOMPurify; Bluesky's are plain text rendered
as DOM nodes, so a Bluesky-only post never fetches the sanitizer. The versioned
URL has to travel as data because `comments.js` is copied to `public/` verbatim
and never passes through `safe_render()`.

**Moderation.** Two modes, both enforced client-side against IDs baked into
the section's data attributes:

- **open** (default) - every reply renders except those in this post's
  `blocked` list. Blocking a reply also drops its descendants, since a thread
  continuing a removed reply quotes it by implication.
- **curated** - set `comments: curated` in the post's frontmatter. Only replies
  in the `approved` list render, plus the author's own (matched by `acct` on
  Mastodon and by handle-or-DID on Bluesky, so answers in your own thread never
  need approving). The escape hatch for a thread that attracts a pile-on; the
  rest of the site stays default-open.

The blocked-subtree cascade runs per network - a reply can only ever descend
from one on its own network - but both networks' surviving replies are then
merged into a single list ordered by timestamp.

The intro line gains "This thread is moderated, so not every reply appears
here" only when moderation actually applies to that post (it has blocks, or
it's curated). On an untouched thread the notice would imply replies were
removed when none were.

Only the IDs belonging to *this* thread are emitted. Shipping the whole
blocklist to every page would advertise, on unrelated posts, what was
suppressed elsewhere. Note that blocked IDs are readable in the page source by
anyone - the mechanism declines to reproduce a reply, it does not conceal that
one was removed, and the reply itself remains public on its network.

### `_is_remote(src)`

True when an image src points off-site (http:// or https://). Remote
images are left where they are - only local ones get self-hosted into
assets/ - but both kinds can still be captioned.

### `_captioning_available()`

Alt-text generation needs the IMAGE role's provider to be reachable.
For OpenRouter that means a key; if it's absent we still copy/rewrite images
(fully offline) but skip captioning, so the build never hard-fails on it.

### `_guess_mime(path_or_url)`

Best-effort MIME type for an image, from its extension. Falls back to
image/jpeg, since the vision call needs *some* type declared and a wrong
guess on a real image is more recoverable than no guess at all.

### `_fetch_remote_bytes(url)`

Download a remote image for captioning only (not self-hosted). Returns
(bytes, mime) or (None, None) on any failure - captioning is best-effort.

### `process_content_media()`

Pre-build pass over content_pipeline/content/*.md that owns all image handling:

- Local images (referenced by any path on disk) are copied into
  content_pipeline/content/assets/ (the source of truth, so they survive a public/ wipe and
  re-ingestion) under a per-post namespaced name, and their Markdown path is
  rewritten to a content-relative 'assets/<name>'. The copy is also placed
  in public/assets/ for the compiled site.
- Remote https:// images are left in place (not self-hosted).
- Every hosted image is passed through `_optimize_content_asset()` (see
  below), which may downscale it and may replace a PNG photograph with a
  JPEG - in which case the Markdown is repointed at the new filename.
- Any image with empty/missing alt text gets alt text generated by the
  IMAGE model and written back into the .md - for both local and remote
  images. Existing author-written alt text is always preserved.

Optimisation runs in both the "new image" and the "already hosted" branch,
not just for newly ingested files. That is deliberate: it is what lets
`./publish.sh --rebuild` retrofit posts published before the step existed.
Once a post has been converted it points at the .jpg, and every later build
costs one image-header read per file.

Editing content_pipeline/content/*.md in place is intentional: the generated alt text and the
self-hosted paths become part of the source, so captioning happens once and
rebuilds stay cheap and offline. Files are only rewritten when something
actually changed (mtimes preserved otherwise).

### `_optimize_content_asset(disk_path)`

Runs one file in content_pipeline/content/assets/ through `images.optimize()`
with the site's `images:` settings, and returns
`(path_on_disk, 'assets/<name>')` - the reference the post should now use.

The returned name is not always the name that went in: a conversion writes a
new JPEG beside the original and leaves the original untouched, so callers
must use the returned reference rather than reconstructing it. Bytes saved
are accumulated in `IMAGE_BYTES_SAVED` for the build summary.

Because originals are kept, a post can end up referencing one again (an edit,
a revert, the same photo dropped into a second draft). `images.optimize()`
recognises its own earlier output and returns that instead of converting
again, so this stays idempotent rather than accumulating a duplicate JPEG per
rebuild - see `_existing_conversion()` in engine/images.py.

What is and isn't converted (and why originals are never deleted) is
documented in engine/images.py.

### `_enhance_images(html_body)`

Adds `width`/`height` and `loading="lazy" decoding="async"` to every
self-hosted `<img>` in a rendered body. Markdown emits a bare `<img alt src>`,
which leaves the browser no way to reserve space before the bytes arrive -
the text below jumps as each photo lands, which is what Cumulative Layout
Shift measures.

Dimensions come from `images.dimensions()` on the file in
content_pipeline/content/assets/, cached per path for the run (the same image
is rendered into a post page and both feeds). Author-written `width` or
`loading` attributes are always left alone, and remote images are skipped
since their size can't be known without fetching them.

Called from `_absolutize_body()` for posts, and directly for standalone pages,
which don't go through that function.

### `_asset_version()`

A short content hash of the theme files the template links by name
(style.css, fonts.css, search.js, comments.js, dompurify.min.js), injected by
`safe_render()` as `%ASSET_V%` and stamped onto every link to them as `?v=`.

This is what makes the year-long `immutable` caching in `htaccess_content()`
correct: those files' URLs otherwise never change, so any long max-age would
leave returning readers on a stale stylesheet. One version covers all five -
they change together rarely, and over-invalidating costs a few tens of KB on
the rare build that touched the theme. The last matching static source wins,
mirroring publish.sh's copy precedence.

Adding a theme file the template links by name means adding it here **and** to
`_VERSIONED_ASSETS`, plus a `FilesMatch` in `htaccess_content()`; miss the
tuple and the hash never moves when that file changes, stranding readers on the
old copy for a year.

### `parse_markdown_file(filepath, valid_slugs=None)`

Read one content_pipeline/content/*.md and return (frontmatter_dict, rendered_html_body).

Splits the YAML frontmatter (raising if it isn't a mapping, so the caller
can skip the file and report it rather than build a broken page) and fills
in defaults for title/date/tags/summary. Then, in order: heals dead
internal links in memory (a '[text](slug.html)' whose target isn't a known
slug collapses to its plain anchor text, leaving the source .md untouched),
mirrors any 'assets/' images into public/ and records the first as the
post's og:image, computes the reading-time estimate, normalises em/en
dashes to plain spaced hyphens, converts standalone YouTube links to
facades, renders Markdown, and finally runs _absolutize_body().

Note the returned body has NOT been through _externalize_links() - see
that entry for why.

### `_is_external(href)`

Decide whether a link leaves the site. Compares hostnames rather than
prefix-matching SITE_URL, so http:// and www. variants of our own domain
still count as internal - this matters because absolute self-links are
sometimes hand-written into drafts to survive the self-healing pass (which
exempts anything with an http(s) scheme), and those must keep behaving like
ordinary internal links. Anything without an http(s) scheme - mailto:, tel:,
'#anchor', root-relative paths - is internal by definition.

### `_externalize_links(html_body)`

Give off-site links `target="_blank" rel="noopener"` so they open in a new
tab. rel omits 'noreferrer' on purpose: sites we cite should still see us in
their referrers. Attributes Markdown emitted after href (title=, class=) are
preserved, and hand-written HTML that already sets target= is left alone.

Applied at page-render time only, wrapping meta['html_body'] where the
article body is injected - deliberately NOT in parse_markdown_file(). The
stored html_body feeds the RSS <content:encoded> body and the search index
as well as the page, and target/tab behavior is meaningless in both, so the
attributes stay out of them. It runs after _absolutize_body() either way,
so internal backlinks are already root-relative and read as internal by the
time we test them.

Note this only covers links in post bodies. The comment-section links are built
in build_comments_block()'s f-string and carry their own target/rel; replies
injected client-side are handled in engine/templates/static/comments.js.

### `_absolutize_body(html_body, valid_slugs)`

Rewrite in-body relative URLs to root-absolute paths for the sectioned
layout. Post bodies are stored in content_pipeline/content/*.md with bare 'assets/<img>'
image paths and bare '<slug>.html' backlinks (see the weave step and the
self-healing pass), both of which assume a page at the site root. Since a
post now renders one level deep at /posts/<slug>.html, those relatives would
resolve wrong, so we make them root-absolute here at render time - leaving
the Markdown source (and the healer, which keys off the bare slug) untouched.

Only backlinks whose target is a known post slug are rewritten, so author-
written links to anything else are left exactly as they are.

### `generate_post_feed_html(posts_list, title_text, current_page=1, total_pages=1, page_href=home_href)`

Render a list of posts as the stacked card feed used by both the homepage
and every tag page, under the given heading. Each card carries the title,
summary, date, reading time, and tag badges.

Pagination is appended only when there is more than one page. `page_href`
is the link builder for the surrounding section - home_href for the
homepage, a tag-bound lambda for a tag page - so the same feed markup
serves both without knowing which it is on.

### `safe_render(template, mappings)`

Substitute %PLACEHOLDER% values into the base template.

The article body is popped out and injected LAST, after every other
placeholder has been filled. That ordering is the point: a post body can
legitimately contain a literal '%SOMETHING%', and injecting it first would
expose it to the remaining replacements. Defaults are supplied for
%SITE_URL% and for the two optional head blocks (%OG_IMAGE_TAGS%,
%ALT_FEED_LINK%) so callers only pass what applies to their page type.

### `rss_item_xml(post)`

Render one post as an RSS <item> with full content. Shared by the main
feed and the per-tag feeds so their entries are byte-for-byte identical.

### `rss_feed_xml(posts_list, feed_title, feed_desc, self_path, last_build_date_rfc)`

Wrap a list of posts into a complete RSS 2.0 document. self_path is the
feed's own path relative to SITE_URL (e.g. 'feed.xml' or 'feeds/ai.xml')
for the atom:link self-reference.

### `feed_build_date(posts_list)`

RFC-822 <lastBuildDate> for a feed, taken from the newest post *in that
feed*. Using the feed's own newest post (not the site-wide newest) means a
per-tag feed's bytes only change when one of its own posts changes - so
editing a post no longer rewrites (and re-uploads) every unrelated tag feed.
Falls back to the current time only if the date can't be parsed.

### `build_site()`

The whole compile, in order. Each step exists where it does because a
later one depends on it:

1. Load engine/templates/base.html, and process_content_media() first, so every
   image is self-hosted and captioned before any post is parsed.
2. Collect valid_slugs from every post's frontmatter BEFORE parsing any of
   them, so the link healer in parse_markdown_file() can already see posts
   that come later in the directory listing.
3. Parse each post, sort newest-first, and render the article pages -
   prev/next navigation, the comments block, og:image and
   JSON-LD (both emitted only for posts that have their own image).
4. Write content_pipeline/content/link_manifest.json and content_pipeline/content/existing_tags.json, the
   two ledgers ingest.py reads on the next run.
5. Emit the paginated homepage and tag pages (`display.page_size` in
   site.yaml), the sitemap,
   the main feed and one feed per tag (`display.feed_items` each), search-index.json, and
   the generated .htaccess.
6. Sweep public/: anything not written by this run and not destined to be
   overwritten by publish.sh's static copies is stale, so it is deleted,
   then empty directories are removed.

A post that fails to parse is skipped and reported rather than aborting
the build - but note it then counts as stale, so its previously published
page is swept in step 6.

Never deletes public/ wholesale: every write goes through
write_file_if_changed / copy_asset_if_changed so unchanged files keep
their mtime and lftp's mirror skips re-uploading them.

