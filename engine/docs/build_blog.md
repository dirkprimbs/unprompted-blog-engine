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

### `render_standalone_pages(base_template, sitemap_urls)`

Render `content_pipeline/pages/*.md` into the site root through the same
template as everything else - `pages/about.md` -> `/about.html`. No date, no
tags, no feed entry; the frontmatter needs only `title` and `description`.
Appends each page to the sitemap.

A page named `index.md` claims `/` and replaces the generated post feed there.
That falls out of ordering - this runs after `build_site()` has written the
homepage - but it is deliberate and relied upon: it is how a site fronted by a
landing page rather than a river of posts is built, without the engine needing
a mode for it.

`layout: panel` in the frontmatter adds `page-panel` to the wrapper and emits
the title as an `<h1 class="panel-title">`, which style.css sets in a narrow
left column with the body beside it. The heading is emitted here rather than
written into the Markdown so it cannot drift from the `<title>` and `og:title`,
which come from the same frontmatter field. Any other value, or none, renders
the plain single-column page.

Pages don't go through `_absolutize_body()` (no slugs to heal), so
`_enhance_images()` is called on them directly.

### `_nav_link_html(entry, extra_class='')`

One `<a>` in the header menu, or a `<span>` for a `nav:` entry that groups
others without linking anywhere itself. That case must not become a link to
`'#'`: a focusable control that does nothing, which screen readers announce as
a link to the top of the page. The `<span>` carries `tabindex="0"` instead, so
a keyboard can reach it and open the submenu underneath - which is revealed by
`:focus-within`, and so never opens if nothing in the branch can be focused.

Off-site hrefs get `target="_blank" rel="noopener"`, matching what article
bodies get from `_externalize_links()`.

### `_header_nav_html()`

The header's menu, built from `site.yaml`'s `nav:` (see `config._nav()`).

With no `nav:` configured this returns the engine's built-in list - About, the
two optional social links, Contact - so an existing `site.yaml` renders exactly
as it did before this function existed. The RSS link, the search box and the
theme toggle are not part of it either way: they are engine furniture that
every site gets, not editorial navigation.

Submenus are plain nested `<ul>`s revealed on hover and `:focus-within`, with
no JavaScript. Below 900px style.css drops the positioning and lets them sit
inline, permanently open - a hover target cannot be reached on a touchscreen,
and a menu that needs a script to open is a menu that is missing whenever the
script is.

### `_social_link_html(url, label)`

One optional social link for the built-in header, empty when the URL is unset.
`rel="me"` is what lets the profile on the other end verify this domain back,
so it stays even though these are ordinary external links otherwise.

### `_site_branding_html()`

The header's branding band: the site name set large with its tagline
underneath, above the nav row. Emitted only when `site.yaml` sets
`site.tagline`; without one there is nothing to put on a second line, and the
compact single-row header - the engine's original shape, and the better one for
reading a long post - is what every `site.yaml` predating the key keeps.

The band scrolls away while the nav row below stays stuck to the top. That
takes `header:has(.site-branding)` in style.css un-sticking the header itself
and moving the stickiness to `.nav-container`, since otherwise the whole
two-tier block would stick and eat the viewport on every scroll.

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

### `_standalone_link(line)`

The link target of a line that is *nothing but* a link, else None. Accepts a
bare URL alone on its line or a Markdown link that is the whole line.

This is the engine's rule for "the author meant to embed this, not to mention
it" - an inline link inside a sentence stays a link, because replacing it with a
player or a video would break the sentence around it. Shared by
`embed_youtube()` and `embed_audio()` so the two can never drift into
disagreeing about what counts, which an author would experience as the engine
being arbitrary.

### `_host_audio_links(text, filepath, filename, audio_claims)`

Copy any standalone *local* audio link into `content_pipeline/content/audio/`,
mirror it into `public/audio/`, and rewrite the Markdown to point at the hosted
copy. Returns `(text, changed)`. Called from `process_content_media()`.

**Audio keeps the author's filename; images do not.** `screenshot.png` collides
constantly, so images are namespaced `<post>_<name>`. An enclosure URL is quoted
in feeds, in directories and in other people's players and has to survive for
years, so it stays what the author called it - which is also what lets a show
migrating from another host redirect its whole back catalogue with one rewrite
rule instead of a per-episode table.

Remote URLs are left completely alone: that is what lets a migrated show keep
serving its old episodes from wherever they already live while new ones are
hosted here, in one feed, with no redirects.

### `_claim_audio(audio_claims, name, filename, disk_path)`

Record which post owns a hosted audio filename and fail loudly on a clash. Two
posts pointing at the *same* file is fine (a trailer and its episode); two
different files sharing one basename is fatal, because the second copy would
overwrite the first and one episode would quietly serve another's audio - a bug
no listener would report and everyone would hear.

### `_remote_audio_size(url)` / `_remote_audio_ledger()`

Byte length of a remote enclosure, asking the host at most once ever and caching
the answer in `content_pipeline/content/remote_audio.json`.

An `<enclosure>` must declare a length and a file on someone else's host cannot
be `stat`'d. For a migrated show these URLs are the normal case, so re-asking
every build would make an offline build impossible and a fifty-episode build
slow. Returns 0 when the host will not say - a legal value every client
tolerates, costing a progress bar rather than the feed.

### `_card_image(meta)`

The absolute URL of a post's social-card image (`og:image`/`twitter:image` and
the JSON-LD `image`), or `None`.

Prefers the post's own first hosted body image, then - **for episodes only** -
the episode's frontmatter artwork, then `podcast.cover`. It also reconciles
three URL shapes that do not agree: `first_image` is site-root-relative without
a leading slash, `_episode_artwork()` returns root-absolute, and either may
already be a remote URL that must pass through untouched.

A written post with no picture still gets no card image, which is the behaviour
that predates this function. The fallback exists because two migrated shows were
publishing every episode with none at all - their shownotes carry no images and
their episodes no artwork of their own - so every link to them rendered as a
bare text card in every scraper that shows one.

### `_player_html(episode, link_target=None)`

The episode player: cover, show name, one big play control, progress bar, and
the secondary actions under a dotted rule.

Progressive enhancement, which matters more here than anywhere else on the site:
the markup ships a real `<audio controls>` and `static/podcast.js` only hides it
once it has taken over. A page whose player fails to initialise is still a page
you can play the episode from - and an episode page that cannot play its episode
has no content at all.

`link_target` is set to `'_blank'` by the embed page and left alone everywhere
else. An embedded player sits inside someone else's page, so its Download / All
episodes / Subscribe links have to leave the frame; without it, tapping one
loads the whole site into a card two hundred pixels tall.

### `embed_audio(markdown_text, meta)`

Turn a standalone audio link into a player and describe the episode. The audio
counterpart of `embed_youtube()`, using the same `_standalone_link()` rule.

Returns `(markdown, episode_or_None)`; the episode dict (`url`, `bytes`,
`seconds`, `mime`) is put on the post's meta as `meta['episode']`, which is how
`build_site()` tells an episode from an ordinary post without re-parsing
anything.

**First link wins.** A post may render several players (an episode and its
trailer), but RSS allows one enclosure per item. First rather than largest or
last means the author decides by writing order, which is the only rule that is
obvious from reading the post.

### `show_guid()`

The channel-level `<podcast:guid>`: a UUIDv5 of the feed URL with protocol and
trailing slash removed, in the namespace the Podcasting 2.0 spec fixes.
Deterministic by definition, so unlike an episode GUID it is computed every
build rather than stored. It is what lets Podcast Index and the directories
following it keep tracking the show if the feed itself moves.

### `ensure_episode_guids(posts)`

Give every episode a permanent GUID, writing it into the post's frontmatter the
first time and never touching it again.

**The most consequential value the engine emits.** A podcast client decides
"have I already got this?" by GUID alone. If one changes, every subscriber's app
treats that episode as new and re-downloads it; if they all change, the entire
back catalogue is re-delivered with a notification each. Apple's rule is that it
must never change, for any reason.

Which is why it cannot be derived at render time from anything visible. A
URL-derived GUID breaks the day a slug is tidied or the site moves domain - both
things this engine makes easy, neither of which feels like it should have
consequences. So the value is minted once and *stored*, in the same
`content/*.md` the build already writes image paths and alt text into.

The minted value is a UUIDv5 of site URL plus slug, so a regenerated file
reproduces it. Once written it is read, never recomputed, and never validated: a
migrated episode carries its old host's GUID verbatim
(`podlove-2015-01-01t12:00:00+00:00-a1b2c3` is a perfectly good GUID, and
rewriting it as a tidy UUID would re-deliver the archive it was kept to
protect). A GUID that cannot be written is a **fatal** error, not a warning - it
produces a valid-looking feed today and changes the first time a slug does.

### `strip_player(html_body)`

Remove the on-page player from a body bound for a feed.

A podcast client has its own player and gets the audio from the `<enclosure>`;
shipping ours inside the shownotes gives a second, dead set of controls and
drops the player's chrome ("Download", "All episodes", the duration) into
`<itunes:summary>`, where it reads as part of the description. It also keeps the
player's root-relative `/audio/` and cover URLs out of the feed, since only
`/assets/` and `/posts/*.html` are promoted to absolute for feed readers.

Matched by counting `<div>` depth, not with a regex: the player nests several
levels and a non-greedy pattern stops at the first `</div>`, removing the class
name that would have shown you most of the block was still there.

### `_cdata(text)`

Wrap text in CDATA, splitting any literal `]]>` that would end the section
early. A post quoting that sequence - talking about XML, or about this very
problem - otherwise produces a feed no reader can parse.

### `item_pubdate(post)`

RFC-2822 publication date for a feed item. `published:` wins over `date:` when
present, because `date:` is day-precision and two episodes released on one day
would tie and be ordered arbitrarily. That matters for an imported archive,
where the feed being replaced had real timestamps and subscribers would see the
order change.

### `_episode_xml(post)`

The `<enclosure>` and `<itunes:duration>` for the **main** `feed.xml`, or `''`
for a post that is not an episode. The blog feed carries the enclosure too, so
an ordinary RSS reader can play an episode without subscribing twice.

### `podcast_item_xml(post)` / `podcast_feed_xml(episodes, last_build_date_rfc)`

The iTunes/Podcasting 2.0 feed. Deliberately siblings of `rss_item_xml()` and
`rss_feed_xml()` rather than parameterisations of them, because the two feeds
want different things from the same posts: this item's guid is the stored
permanent identifier rather than the post URL, and almost every channel element
differs - the language and `<link>` come from config instead of being
hard-coded, and the whole iTunes block has no counterpart in a blog feed.

Everything Apple requires is emitted unconditionally, since a feed missing one
is rejected at submission with a message naming the tag but not the reason.
`<itunes:summary>` is plain text via `plain_text()` capped at 4000 chars - Apple
rejects markup there even though `<description>` allows it. Not emitted:
`<itunes:block>` and `<itunes:complete>` (harmful unless meant),
`<managingEditor>`/`<webMaster>` (publish an email in plain text for no
benefit), `<itunes:keywords>` (deprecated).

The feed is never capped at `FEED_ITEMS`. A blog feed is a what's-new list; a
podcast feed is the show's whole catalogue, and an episode falling out of it
disappears from every directory and every app's back catalogue.

### `_subscribe_item_html()`

The Subscribe control in the header, on every page of a podcast site, beside the
RSS link and the theme toggle.

Podlove's widget normally renders itself as a large button wherever its script
tag sits, which is not a thing that fits in a row of nav links. It also supports
being hidden and driven from an element of the author's own (`data-hide` plus
`data-buttonid`, and a trigger carrying the matching class) - which is what this
uses: an ordinary link, styled like its neighbours, that opens Podlove's app
chooser.

The `href` is the feed itself, so the link is useful before the script loads and
stays useful if it never does. The button id is fixed rather than random, so the
markup is byte-identical between builds - a changing id would rewrite every page
in `public/` on every build and make the FTP mirror re-upload the whole site.

### `_podlove_button_html()`

The vendored Podlove subscribe button, configured inline.

Config goes in a global rather than through the widget's `data-json-url`,
because a failed fetch there is swallowed with a `console.debug` and no button
appears - exactly the failure nobody notices on their own site. Inline, the
config either renders with the page or does not exist.

**The `javascripts/` segment in the src is load-bearing**: the widget finds its
stylesheet, its iframe and its ninety-odd app logos by stripping that segment
off its own `src`. See `engine/fetch_podlove.py`.

### `_format_bytes(size)`

A file size a listener can act on - MB for an episode, KB for a clip. Integer
megabytes alone print "0 MB" for anything under one, and a download size of zero
reads as an error rather than as "small".

### `_embed_page_html(post)`

An episode's player alone in a minimal page, for framing inside a link preview.

Built as a literal rather than through `base.html`, because `base.html` is the
*site's* page - header, nav, search, comments, footer - and none of that belongs
in a card. Keeping it here is also what keeps "base.html is the single page
template" true, and leaves `load_template()`'s fork-override contract untouched.

The player markup is `_player_html()` verbatim, so it cannot drift from the one
on the episode page, and cannot drift from `strip_player()`'s `_PLAYER_OPEN`,
which strips the same block out of both feeds by matching its opening tag.

The theme is resolved from `prefers-color-scheme` and nothing else. The site's
pre-paint script reads `localStorage`, and inside the frame that storage belongs
to *this site's* origin - so a reader who once chose light here would get a white
player pasted into their dark timeline.

### `_oembed_json(post)`

The oEmbed payload an episode page advertises, as a JSON string.

Declared `"video"`, which is a fudge worth knowing about: oEmbed has no audio
type, and the consumers that embed anything embed `video` and leave `rich` as a
plain link. Every hosted podcast platform makes the same trade.

The thumbnail is whatever `_card_image()` gives the page itself, so the two
agree - a consumer shows the thumbnail first and only swaps in the frame when
someone clicks it, which is why an episode with no card image is worth fixing
before any of this is worth having.

### `build_episode_embeds(episodes)`

Writes `/embed/<slug>.html` and `/embed/<slug>.json` for every episode, and
nothing at all when the site has no `podcast:` section.

Both go through `write_file_if_changed()`, which is what registers them in
`GENERATED_FILES`; anything written straight to disk is deleted by the stale
sweep on the very same build. Its predicate must stay identical to the one
building the `episodes` list, `podcast is not False` included, or a post that
opted out of the show would advertise a payload that was never written.

Deliberately **not** added to `sitemap.xml`. These are machine surfaces: nothing
links to them, they carry no content the episode page does not, and offering a
search engine a second URL per episode is asking it to choose between them.

### `_episode_row_html(post)` / `build_podcast_page(base_template, episodes, sitemap_urls)`

The show header, the subscribe options and every episode, written to
`public/podcast.html`.

Called from `build_site()` **before** `render_standalone_pages()`, so a
hand-written `content_pipeline/pages/podcast.md` overrides it - the same
precedence that already lets `pages/index.md` replace the generated homepage. A
show with a real about-page to write should not have to fight the generator for
its own URL. Appends to `sitemap_urls` before the sitemap is frozen.

### `safe_render(template, mappings)`

Substitute %PLACEHOLDER% values into the base template.

The article body is popped out and injected LAST, after every other
placeholder has been filled. That ordering is the point: a post body can
legitimately contain a literal '%SOMETHING%', and injecting it first would
expose it to the remaining replacements. Defaults are supplied for
%SITE_URL% and for the two optional head blocks (%OG_IMAGE_TAGS%,
%ALT_FEED_LINK%) so callers only pass what applies to their page type.

The site.yaml-derived values are defaulted here too. Most are single strings
escaped on the way in, since they land inside `content="..."`. Three are whole
blocks instead, because each is optional and must be absent rather than empty:
%FEDIVERSE_CREATOR_META% (the entire `<meta>` tag), %HEADER_NAV%
(`_header_nav_html()`) and %SITE_BRANDING% (`_site_branding_html()`). The last
two are the one place author text becomes markup rather than an attribute
value, so they are escaped element by element inside their builders.

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

---

Migrating a podcast onto this engine is documented separately in
[`podlove-migration.md`](podlove-migration.md).
