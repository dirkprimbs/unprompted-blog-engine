# Migrating a Podlove / WordPress podcast onto this engine

Written after moving `podcastprojekttagebuch.kopfstim.me` (38 episodes, 2018-2021)
off WordPress + Podlove Publisher. Everything here was learned by getting it
wrong first; the order below is the order that avoids that.

**The one thing that matters:** a podcast migration is judged by whether
subscribers notice. If GUIDs, pubDates and the old feed URL survive, nobody
notices. If any of the three breaks, every subscriber re-downloads the entire
back catalogue and gets a notification per episode. Everything else on this page
is cosmetic by comparison.

---

## 0. Before touching anything

Take a full static snapshot of the old site (`wget -mkEpnp` or similar) **while it
is still up**. You will need far more of it than you expect: the audio, the
shownote images, the site icon, the tag names, and the feeds. Once the domain is
repointed, none of it is fetchable, and the Wayback Machine will not have the
uploads directory.

Snapshot the *other* sites the shownotes link to as well, if they are yours and
also being migrated. Six images in this migration were lost precisely because
they lived on a sibling site that had already been migrated - 404 on the live
site, absent from every snapshot, and never archived.

---

## 1. The MP3 feed is the source of truth, not the HTML

`/feed/mp3/index.xml` in the snapshot carries everything an import needs, and
carries it in a form nothing else does:

| From the feed | Why nowhere else will do |
|---|---|
| `<guid>` | `podlove-2018-04-06t17:42:27+00:00-cfd9a8ebbeef9d3`. Not derivable. Not in the HTML. Lose it and the migration is a relaunch. |
| `<pubDate>` | Full timestamp. The HTML and the wp-json dump have day precision at best. |
| `<enclosure length>` | Byte length as the old feed declared it. |
| `<itunes:duration>`, `<itunes:episode>`, `<itunes:episodeType>` | |
| `<content:encoded>` | The full shownotes, links intact. |

**What the feed does not have: tags.** Those come from `wp-json/wp/v2/episodes/*`
(tag IDs per episode) joined against `wp-json/wp/v2/tags/*` (id → name). Without
that join every imported episode lands untagged, and tags drive the topic pages,
the per-tag feeds and backlink matching.

**The feed also does not have the posts that were never episodes.** WordPress
keeps them as a separate post type; look in `wp-json/wp/v2/posts/*` and
`wp-sitemap-posts-post-1.xml`. This migration had exactly one, and it was missed
on the first pass because the feed is such a complete-looking source.

---

## 2. Writing content/*.md

One file per episode, `YYYY-MM-DD_Title_With_Underscores.md`. Frontmatter:

```yaml
title: "…"
slug: "…"              # from the old permalink's last path segment
date: 2018-04-06
published: "2018-04-06 15:45:19"   # full timestamp; date: alone would tie same-day episodes
tags: ["Pod2go"]
summary: "…"           # first sentence of <description>, capped ~200 chars
themes: "…"
guid: "podlove-2018-04-06t17:42:27+00:00-cfd9a8ebbeef9d3"
episode_number: 1
```

Body: the audio link on its own line, then `<content:encoded>` as-is.

```markdown
[Diese Folge anhören](audio/pt01.mp3)

<p>…the original shownotes…</p>
```

Notes:

- **`guid` is written by hand here and never touched again.** The engine only
  mints one when the key is absent, and it never validates or rewrites an
  existing value - which is exactly what lets a Podlove GUID through verbatim.
- **Leave the shownotes as HTML.** Markdown passes raw HTML through, and an
  HTML→Markdown conversion mangles precisely the link lists that make shownotes
  worth keeping.
- **Strip WordPress furniture**: `<img>` tags pointing at `s.w.org` (those are
  emoji, rendered as images, one third-party request each), `class="wp-block-*"`,
  and empty `<p></p>`.
- **Do not add `announce: pending`.** It is absent by default; if you add it,
  deploying will post 38 archived episodes to Mastodon and Bluesky.

---

## 3. Audio

Copy every enclosure into `content_pipeline/content/audio/`, **keeping the
original filename**. The engine does not namespace audio the way it namespaces
images, and that is deliberate: predictable audio URLs turn the old-host
redirect into one rewrite rule instead of a table with a line per episode.

Two snags seen in practice:

- Filenames with spaces or an umlaut (`B%EF%BF%BDcher%20Podcasts.mp3`,
  `schla011_elchdämmerung.mp3`). Rename those - a broken byte should not live in
  a URL for a decade - and add an explicit redirect per renamed file, *before*
  the general directory rule, which would otherwise forward them to a name that
  no longer exists.

  **Diff the enclosure basenames against what landed in `content/audio/`** to
  find them. Both migrations had exactly one such file and neither was obvious;
  on the second it was found by comparing the two lists, not by reading them.

  The engine writes those redirects to match either encoding, because
  `%{REQUEST_URI}` is the URI as the client sent it and an umlaut arrives as
  `%C3%A4` - a rule written with the literal character silently never fires and
  the request falls through to the general rule instead.
- An enclosure URL whose file is missing from the snapshot. Match it by
  normalised basename against the directory listing before giving up.

**Alternative worth knowing:** you do not have to move the audio at all. A remote
enclosure URL is left alone by the engine, so a show can keep serving its back
catalogue from the old host and only host new episodes here - one feed, no
redirects, no 700 MB upload. Byte lengths then come from one HEAD request per
URL, cached in `content/remote_audio.json`.

---

## 4. site.yaml

Copy the channel block out of the old feed **verbatim**. Not "as a starting
point", not tidied, not condensed - verbatim. It is the show's own writing, and
an import is not the moment to improve it. Getting this wrong is easy and quiet:
on this project I paraphrased one show's summary and invented the other's
outright, and both read perfectly plausibly until they were held next to the
original.

The old feed carries `<description>`, `<itunes:subtitle>` and `<itunes:summary>`,
which map to `description`, `subtitle` and `summary`. Keep all three even when
two of them are the same string - condensing them is where the temptation to
rewrite comes from. Typos in them are the show's typos; leave them.

Verify it afterwards rather than trusting the copy-paste:

```python
# for description / itunes:subtitle / itunes:summary: old feed == new feed
```

The two load-bearing values:

```yaml
podcast:
  guid: "54e71306-4546-4161-892a-902dc00d0bd2"   # from <podcast:guid> - pin it
  cover: "/podcast-cover.jpg"                    # the original artwork, ≥1400px square
```

`podcast.guid` must be pinned: the spec says the show identifier does not change
when a feed moves, and a migration is a feed move. Derive a new one and every
directory following Podcast Index treats this as a different show.

`podcast.homepage: true` if the site is a podcast first - `/` becomes the episode
list and the written archive moves to `/articles.html` carrying only non-episode
posts. `display.archive_heading` names that page if the site is not in English.

---

## 5. Redirects - where the real work is

```yaml
redirects:
  - "/feed/mp3/": "/podcast.xml"     # THE critical one
  - "/feed/": "/feed.xml"
  - "/episoden/projekttagebuch/": "/audio/"
  - "/was-ist-das-hier/": "/about.html"
  - "/wp-sitemap.xml": "/sitemap.xml"
```

The engine adds the WordPress-shaped `/<slug>/` → `/posts/<slug>.html` rule
itself, existence-checked, so per-episode entries are unnecessary.

### The trap: MultiViews

Most shared hosting enables `MultiViews`. With it, Apache resolves an
extensionless request against real filenames **during map-to-storage, before
mod_rewrite's per-directory hook runs**. A request for `/feed/mp3/` is matched to
the file `feed.xml`, `/mp3/` becomes path-info, the handler rejects path-info, and
Apache returns 404 - and no rule in `.htaccess` ever sees the request.

The symptom is memorable: **every redirect on the site works except the one whose
prefix collides with a filename** - which, for a podcast migration, is exactly the
feed redirect. You will be certain the rule is wrong. It is not.

Diagnose it in one request:

```
curl -o /dev/null -w '%{http_code} %{content_type}\n' https://site/podcast
# 200 application/javascript  -> MultiViews is on; it just served podcast.js
```

Two fixes, and the first is not enough on its own:

1. Match `%{REQUEST_URI}` rather than the per-directory path. The engine now
   emits configured redirects this way. Necessary, but it only fixes *matching* -
   it cannot help a request that 404s before mod_rewrite runs.
2. **Ship a real directory at the colliding path.** A directory that exists wins
   over content negotiation, so Apache maps the request into it normally. Put
   `public_static/feed/.htaccess` there with the rules, plus `index.html`
   meta-refresh fallbacks. This is what actually fixed it.

`Options -MultiViews` would also work and is Apache's own advice, but `Options` in
`.htaccess` is a 500 on any host whose `AllowOverride` forbids it. Taking a live
site down to fix a redirect is a bad trade.

---

## 5b. Strip plugin scaffolding back to bare links

**A WordPress export contains plugin output that looks like content.** Carrying
the shownote HTML across verbatim preserves the old site's *workarounds* and
throws away the mechanism the engine would use instead.

The one that bit here was wp-youtube-lyte. Every trailer in the export looks
like this:

```html
<figure class="wp-block-embed-youtube ...">
  <a href="https://oldsite/episode-slug/">
    <img src="/wp-content/plugins/wp-youtube-lyte/...-hqdefault.jpg">
  </a>
</figure>
```

That is a *picture of a video* linking to a page. There is no YouTube URL in it
at all, so `embed_youtube()` never fires and the reader gets a static thumbnail
that goes nowhere useful. The import even helpfully copies the cached
thumbnails into `assets/`, which makes the result look deliberate.

**Reduce every embed to its bare link on its own line** and let the engine
re-derive the embedding - that is what produces a click-to-play facade with a
self-hosted thumbnail and no request to Google until someone clicks.

Grep the export before importing, not after:

```
wp-youtube-lyte | wp-block-embed | lyte-  | -hqdefault.jpg
wp-block-embed-twitter | wp-block-embed-vimeo | instagram-media | twitter-tweet
```

Same family as the emoji `<img>` tags WordPress serves from `s.w.org` - both are
the old site rendering something the engine renders itself. The emoji ones are
easy to spot because they are obviously wrong; the embeds are not, because they
look right until you click them.

## 5c. The other things a WordPress export smuggles in

5b is about markup that stands in for a mechanism. This is the rest: markup that
is simply broken, or that quietly keeps pointing at the host you are leaving.
All of it looks fine in a browser, which is why none of it turns up until you
grep for it.

**Anchors that lost their `href`.** The block editor drops the attribute name
when the editing session expires mid-paste, and writes the link it was bounced
to instead:

```html
<a "https://oldsite/buch" target="_blank">30x Fotogeschichte(n)</a>
<a rel="noreferrer noopener" "https://oldsite/wp-login.php?redirect_to=..." >Morse Camera</a>
```

Ninety-one of these in one export - ninety of them the same book promo, pasted
once into a template and repeated on every episode since. They render as plain
text, so nobody ever noticed. Repair the ones whose target is obvious, unwrap
the ones whose target is gone. Grep: `<a[^>]*\s"https\?://`.

**Block-editor bookkeeping.** Every link the editor made carries `type="URL"`
and `id="<the same URL again>"`; every image carries `srcset`, `sizes` and
assorted `data-*`. Strip all of it. `srcset` is the one that matters: it lists
width variants **on the old host**, and a browser prefers it over `src` - so an
image whose `src` you carefully rewrote still loads from the domain you are
leaving. It is invisible in a page that looks correct.

**Third-party iframes in shownotes.** Amazon's Kindle preview card
(`lesen.amazon.de/kp/card`) was the one here, 63 of them. Same reasoning as the
YouTube facade: an iframe hands every visitor to a third party on page load,
for a book they did not ask about. The card carries everything needed to replace
it - the title in the iframe's `title` attribute, the ASIN in its `src` - so it
becomes `https://www.amazon.de/dp/<ASIN>` under the book's own name. Drop the
`tag=kpembed-20` parameter; that is Amazon's embed tracking, not the author's.

**Tag archives.** WordPress transliterates an umlaut into a slug and this engine
does not, so `/tag/portrat/` and `/tags/porträt.html` are the same tag spelled
two ways - `redirects:` cannot bridge them, and it takes prefixes rather than
patterns anyway. Build the map from the export's own `wp-json/wp/v2/tags`
name→slug pairs and rewrite the links at import time, so the two never have to
agree on an algorithm. Inbound `/tag/` links from outside stay lost; say so
rather than assuming nobody had any.

**Off-site redirects need their own directory.** `redirects:` in site.yaml takes
root-absolute paths on both sides by design, so a vanity path that leaves the
site (`/buch` → a publisher) cannot go in it. Ship a real
`public_static/buch/.htaccess` instead. A real directory is also what keeps
MultiViews from resolving the path against some other file - the same trap
`/feed/` falls into.

## 6. Images and other leftovers

- Shownote images hosted at `/wp-content/uploads/` must be copied into
  `content/assets/` and the references rewritten. After the migration that host
  is *this* host, so a missed one is a 404 on your own domain.
- **Standalone pages are not processed by the build.** `process_content_media()`
  scans `content/*.md` only, so images in `pages/*.md` need copying by hand -
  `public_static/assets/` is the right home for them.
- Rewrite self-links (`https://oldsite/some-episode/` → `/posts/some-episode.html`)
  and drop `#comment-NN` fragments if comments are not migrated.
- Replace the favicon. The engine ships its own, and it will silently become the
  show's icon. The old one is usually the WordPress site icon in
  `wp-content/uploads/`. Override `favicon.ico`, `apple-touch-icon.png` **and
  `favicon.svg`** in `public_static/` - browsers prefer the SVG, so overriding
  only the `.ico` leaves the engine's mark showing on modern browsers.
- Write a `robots.txt` into `public_static/`. The old one pointed at
  `/wp-sitemap.xml`; the engine writes no robots.txt of its own.
- An embedded Podlove subscribe button in old shownotes can simply be repointed
  at `/subscribe-button/javascripts/app.js` - the engine vendors the same widget.
- **Re-read the privacy page before copying it from the last migration.** Its
  strongest sentence is that the site embeds nothing from foreign servers, and
  that sentence is a claim about *this* export. A show whose shownotes hotlink
  photographs from Wikimedia, the Library of Congress or a museum makes it false
  on the pages that do - as does one whose episodes embed video, even behind a
  facade. Name the exceptions in the text; the alternative is a legal page that
  is wrong, which is worse than one that is long.

---

## 5a. Per-episode artwork

Check `<itunes:image>` on the items, not just on the channel. A show where every
episode carries the same logo loses nothing by dropping it - the app falls back
to the channel art, which is the same picture. A show where each episode has its
own poster loses the posters, and nobody notices until an app shows sixteen
identical thumbnails where there used to be sixteen films.

```python
# unique per-episode <itunes:image> values in the old feed
# 1  -> drop it, the channel art covers it
# n  -> carry it: copy each into content/assets/ and set `image:` per post
```

---

## 6a. Alt text - check every image before calling the migration done

**Do this on the next project rather than after it.** A WordPress export is raw
`<img>` tags carrying `alt=""`, because the field was never filled in. The engine
now captions those (see `_caption_html_images`) - it did not until this migration
exposed the gap - but auto-captioning is a floor, not a finish:

- **It only covers images the build can see.** Hosted assets referenced from
  `content/*.md` get captioned. Three categories do not, and each has to be done
  by hand:
  - images in `pages/*.md`, because `process_content_media()` never scans that
    directory;
  - remote images, which are not fetched on every build to describe a picture
    that may 404;
  - anything whose file was lost with the old host.
- **A vision model describes what it sees, not what the image is for**, and it
  reads labels badly. Reviewing eleven captions on this migration caught, in
  order of seriousness: a Zoom H2n called "eine digitale Kamera"; a Focusrite
  Scarlett 18i20 called "Scarlett 20"; the Deutschlandfunk tower reduced to "ein
  hohes, dunkles Hochhaus"; and a Yellowtec microphone left unnamed in a post
  whose entire subject was that microphone. All eleven read fluently. **Model
  numbers in alt text are factual claims** - read them off the image yourself or
  leave them out.

  The review also caught one caption that was *right* where I assumed it was
  wrong: a show's cover art really does read "BEISEN & SAUGEN", one `s`. Check
  before correcting, in both directions.
- **Decorative images should keep `alt=""`.** Captioning a spacer or a
  repeated logo makes a screen reader worse, not better. Auto-captioning cannot
  tell the difference; you can.

**Alt text goes in the language of the page.** Set `site.language` in site.yaml
and the engine writes captions in it, sets `<html lang>` to it, and uses it for
the feed's `<language>`. This matters more than it looks: a screen reader
announces the document's language once and pronounces everything inside it
accordingly, so an English caption on a German page is read with German phonetics
and comes out as noise. Setting `site.language` is therefore part of the import,
not a polish step - do it before captioning, or every caption has to be redone.

Audit before deploying:

```python
# every <img> across content/ and pages/, bucketed by described / empty-local /
# empty-remote - anything not "described" is a decision you have not made yet
```

---

## 6b. Publishing a new episode afterwards

Write `content_pipeline/content/YYYY-MM-DD_Titel.md` with `title`, `slug`,
`date`, `summary` and `episode_number`, put the audio link alone on its own
line, and build. The engine copies the audio, mints the GUID, reads the
duration and adds it to the feed.

**The trap is copying the previous episode's file as a template.** That is the
obvious way to start one, and it carries two fields that must not be reused:

- `guid:` - delete the line. Two episodes sharing a GUID means apps treat the
  new one as a re-publication of the old: most ignore it entirely, some replace
  the old episode's metadata with the new one's. Either way the new episode does
  not reach subscribers, and the feed looks fine while it happens.
- `episode_number:` - increment it, or two episodes claim the same number.

Seen in the wild within a day of a migration going live, on exactly the file
that was copied. Check before deploying:

```python
# unique GUIDs == item count, and no episode_number appears twice
```

---

## 7. Deploy

- **Check `FTP_REMOTE_DIR` before the first deploy.** `lftp mirror --delete`
  removes everything at the target that is not in `public/`. Pointed at the wrong
  site, it replaces that site.
- Expect a large first upload (740 MB here) and a long one. Later deploys move
  only what changed.
- `set ftp:list-options -a` is now in `ftp_sync` so lftp can see remote dotfiles.
  Without it a stale `.htaccess` on the server is invisible to `--delete`, and a
  stale `.htaccess` in a subdirectory silently disables every inherited redirect
  underneath it.

---

## 8. Verify against the old feed, not against expectations

The check that matters, run after deploy against the live feed:

```python
# GUIDs, pubDates and enclosure lengths, old feed vs new
# All three must be 38/38 (or whatever the episode count is).
```

Then:

- Every configured redirect returns 301 to the right target - **test each one**,
  including `/feed/mp3/index.xml`, not just `/feed/mp3/`.
- `curl -r 0-1023` on an audio file returns **206**, not 200. Without Range the
  player's timeline silently stops working. (Locally, `./publish.sh --serve` uses
  `engine/preview_server.py` for the same reason - Python's `http.server` answers
  Range with the whole file.)
- Crawl the site for broken links and images. This is what surfaced the six dead
  sibling-site images and the unencoded sitemap entries.
- Feed validator (`podba.se/validate/`, `castfeedvalidator.com`).

---

## Checklist

- [ ] Snapshot taken while the old site is up, including sibling sites
- [ ] GUIDs copied verbatim into `guid:`
- [ ] `published:` carries the full timestamp
- [ ] `podcast.guid` pinned in site.yaml
- [ ] Tags joined from wp-json
- [ ] Non-episode posts imported
- [ ] Plugin embed scaffolding stripped back to bare links (wp-youtube-lyte and
      friends) - a static thumbnail linking to a page looks fine and plays nothing
- [ ] Per-episode `<itunes:image>` checked: one shared image is droppable,
      distinct ones are not
- [ ] Audio filenames preserved; renames found by diffing the enclosure list
      against content/audio/, and each given its own redirect first
- [ ] A real /feed/ directory shipped in public_static/ (the MultiViews trap -
      it will bite on this host every time)
- [ ] Every old URL shape simulated against the generated .htaccess BEFORE
      deploying, not just the ones you remember
- [ ] Cover art ≥1400px square
- [ ] Favicon replaced (`.ico`, `.png` **and `.svg`**)
- [ ] robots.txt written
- [ ] Show description, subtitle and summary carried over **verbatim**, and
      diffed against the old feed to prove it
- [ ] `site.language` set BEFORE captioning
- [ ] Every image audited: captions reviewed, pages/ and remote images done by
      hand, decorative ones left empty
- [ ] `/feed/mp3/` redirect verified **on the host**, not just in the file
- [ ] No `announce: pending` anywhere
- [ ] FTP target confirmed before the first `--deploy`
- [ ] Old feed vs new: GUIDs, pubDates, enclosure lengths all match
- [ ] No duplicate GUIDs and no duplicate episode numbers in the finished feed -
      check this on every later publish too, not only at migration
