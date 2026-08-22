# Unprompted Blog Engine

A personal static site generator with a privacy-first, open-source mindset and an optional, LLM-powered editorial pipeline.

## What this is

At heart this is a small Python program that turns a folder of Markdown files into a complete static website - posts, tag pages, RSS feeds, a sitemap, and an in-browser search index - and uploads it to a web server. No database, no runtime, no JavaScript framework. The output is plain files you could host anywhere.

Two ideas shape it:

- **Privacy-first.** Nothing about the finished site phones home. Fonts are self-hosted (no Google Fonts request), search runs entirely in the reader's browser (no hosted search service), YouTube embeds don't load until clicked, and comments come from Mastodon and Bluesky rather than a tracking widget. A reader can visit without any third party learning they did.
- **Open-source and local by default.** The whole engine is here and readable. The AI help is *optional* and runs locally through [Ollama](https://ollama.com) unless you deliberately opt into a hosted model. If you never want a model involved, you can write posts by hand and it is still a perfectly good static site generator.

The AI part is an editorial pipeline: throw a rough draft at it and it polishes the prose, translates stray German passages, suggests and reconciles tags, writes a title and summary, and weaves in links to your earlier posts, the way an editor would. It is a convenience, not a requirement, and you review everything it produces before it ships.

This repository is the reusable *engine*. Your drafts, published posts, credentials, and site identity are all gitignored and live only on your machine, so the engine can be shared or forked without dragging one person's blog along with it.

## Setup and configuration

### Requirements

- Python 3 (the only packages are `Markdown` and `PyYAML`, in `requirements.txt`)
- `lftp`, for deployment
- Bash
- [Ollama](https://ollama.com) - **only** if you use the AI editorial pipeline
- An [OpenRouter](https://openrouter.ai) key - **only** if you point a model role at a hosted model (the defaults use one for polishing outline-style drafts)

The plain build-and-deploy path needs just Python and `lftp`. Ollama and any API key are for the optional pipeline.

### Install

```bash
git clone YOUR_REPOSITORY_URL
cd YOUR_REPOSITORY_DIRECTORY

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt

chmod +x publish.sh
./publish.sh --setup      # creates the working directories and runs diagnostics
```

### Configure who you are (`site.yaml`)

Everything that names you or your site - title, URL, author, contact, social links, footer text - lives in `site.yaml` at the repo root. `publish.sh` seeds it from `site.example.yaml` on first run; you can also copy it by hand:

```bash
cp site.example.yaml site.yaml
```

At minimum set `site.name`, `site.url`, `author.name`, `author.email`, and the entries under `links`. The same file holds presentation knobs (posts per page, feed length, reading-speed estimate, an optional tag-to-emoji map).

This matters more than the visible fields suggest: the invisible ones - the JSON-LD `author` block, the `og:image:alt` text - are exactly what search engines and social scrapers read to decide who wrote a post. Keeping all of them in one gitignored file is the point, so a fork starts neutral instead of publishing under your name.

To change the footer's *markup* or the header's link list (as opposed to their wording), edit `engine/templates/base.html`.

### Configure the AI pipeline (optional)

Model roles are defined in `engine/llm.py`, each a `{"provider", "model"}` pair where `provider` is `"ollama"` (local) or `"openrouter"` (hosted):

```python
UTILITY        = {"provider": "ollama",     "model": "gemma4:e4b"}   # tags, title, summary
POLISH_PROSE   = {"provider": "ollama",     "model": "gemma4:e4b"}   # tidy already-flowing prose
POLISH_OUTLINE = {"provider": "openrouter", "model": "google/gemini-3.5-flash"}  # use stronger remote model
TRIAGE         = {"provider": "ollama",     "model": "gemma4:e4b"}   # shortlist backlink candidates
WEAVE          = {"provider": "ollama",     "model": "gemma4:e4b"}   # add inline backlinks
IMAGE          = {"provider": "ollama",     "model": "gemma4:e4b"}   # alt text (needs a vision model)
```

Polishing is split by how finished the draft already is. A draft that is mostly bullet points needs a strong model to *write* it into prose, so it defaults to a hosted one; a draft that is already flowing sentences only needs a light copy-edit, which a small local model does about as well while keeping the text on your machine.

**To run entirely locally**, point `POLISH_OUTLINE` at a capable local model too, e.g. `{"provider": "ollama", "model": "gemma4:12b"}`. Then nothing leaves your machine and no API key is needed. Install whichever Ollama models you configure.

**To use a hosted model**, put your key in `publish.local.sh` (gitignored):

```bash
export OPENROUTER_API_KEY="sk-or-..."
```

Ingestion checks every role up front and fails fast with a clear message if a role needs a key that isn't set, so you never get halfway through a batch before finding out.

Prompt wording lives in `engine/prompts.yaml` (one entry per step, with its temperature) - edit it there to change what a model is told, no code change needed. Your writing voice lives in `content_pipeline/VOICE.md`; copy it from `VOICE.example.md` and paste in a few paragraphs of your own real writing so the polish step learns your voice.

### Configure deployment (`publish.local.sh`)

```bash
cp publish.local.example.sh publish.local.sh
```

Fill in the FTP details (the connection is upgraded to **FTPS** - TLS - at deploy time):

```bash
FTP_HOST="ftp.example.com"
FTP_USER="your-username"
FTP_PASS="your-password"
FTP_REMOTE_DIR="/"
```

`publish.local.sh` holds credentials and is gitignored. Never commit it. It is also where the optional `OPENROUTER_*`, `MASTODON_*` and `BLUESKY_*` variables go.

## Writing flow

There are two ways to get a post onto the site. They meet in the same place - `content_pipeline/content/`, the folder of finished Markdown the build reads - so you can mix them freely.

### a) Let the pipeline do the editorial work

Drop a rough Markdown draft into `content_pipeline/sources/`:

```
content_pipeline/sources/my-idea.md
```

Run the pipeline and it turns that draft into a polished post:

```bash
./publish.sh --ingest
```

The model polishes the prose, translates what is not already in English, picks and reconciles tags, writes a title and summary, and weaves in links to related earlier posts. The result lands as a dated `.md` file in `content_pipeline/content/`, and the original draft moves to `content_pipeline/processed/`. **Nothing is published yet** - this is a candidate for you to read and adjust.

### b) Write the finished post yourself (or with any assistant)

If you'd rather write the whole thing - by hand, or by pointing any LLM you like at the task - skip ingestion entirely and put a finished Markdown file straight into `content_pipeline/content/`. It needs valid frontmatter; `content_pipeline/TEMPLATE.md` is the reference:

```yaml
---
title: "My Post Title"
slug: "my-post-title"
date: 2026-07-21
tags: [Topic1, Topic2]
summary: "A crisp one-sentence summary."
themes: "Editorial coverage line used to match backlinks - never shown to readers."
---

Your post body in Markdown...
```

No model ever touches a file written this way (bar the best-effort image captioning at build time, which you can ignore). This is the path for posts you want full control over, or for content an assistant of your choice has already finished.

**Standalone pages** (an about page, a colophon) work the same way but live in `content_pipeline/pages/`. A file `pages/about.md` renders to `/about.html` at the site root - no date, no tags, no feed entry - and only needs `title` and `description` in its frontmatter.

## Interacting with the system

Everything runs through `publish.sh`. The three stages - ingest, build, deploy - each run on their own so you can stop and check between them:

```bash
./publish.sh              # full pipeline: ingest drafts -> build -> confirm -> deploy
./publish.sh --ingest     # drafts -> content/, then stop
./publish.sh --build      # content/ -> public/, nothing uploaded (works on a fresh clone)
./publish.sh --rebuild    # skip AI, rebuild public/ from current content, then deploy
./publish.sh --deploy     # upload the existing public/ as-is
./publish.sh --help       # full list of modes
```

**Drafting and editorial writing.** Put ideas in `content_pipeline/drafts/` while you work them out; move one to `content_pipeline/sources/` when it's ready for the pipeline.

**Reviewing what the pipeline produced.** After `--ingest`, open the new file in `content_pipeline/content/` and read it. The title, summary, tags, and inline backlinks are all the model's suggestions - treat them as a starting point and fix any that miss: drop an off-topic tag, add one it didn't think of, remove a backlink that doesn't fit. Edit the Markdown directly - it's yours now, and nothing is public until you build and deploy.

**Publishing directly.** Write or generate a finished post into `content_pipeline/content/` (flow b above), then `./publish.sh --rebuild` to build and deploy it. `--rebuild` skips the AI entirely.

**Editing a post.** Edit its `.md` file in `content_pipeline/content/` and rebuild. The URL comes from the `slug` field, so you can rename the file freely; change the `slug` only if you actually want the URL to change (old links will then 404 unless you add a redirect).

**Deleting a post.** Delete its `.md` file from `content_pipeline/content/` and rebuild. The build sweeps `public/` of anything it no longer generates, so the page, its feed entries, and its search-index entry all disappear on the next deploy.

> The build edits your source files in `content_pipeline/content/` - it self-hosts local images into `assets/` and writes back generated alt text. Commit `content/` before building so those changes are easy to review.

## Comments

### How commenting works

[Mastodon](https://joinmastodon.org) is a free, open-source social network built on an open standard (ActivityPub) - part of the "fediverse", a network of independent servers that talk to each other, with no single company owning it. [Bluesky](https://bsky.app) is built on a different open standard, the AT Protocol, with the same basic property: its posts are public and readable without an account.

This blog has no comment database of its own. Instead, when a post is published it can be announced on both networks, and the *replies to those announcements become the comment thread* on the article page. A reader clicks "Load comments" at the foot of a post, and the replies are fetched from each network's public API and shown inline as **one blended thread**, ordered by time. Each comment carries a small badge saying where it came from, and its permalink leads back to the original. Discussion happens in the open, and anyone can join in by replying from their own account on either network.

Nothing is fetched until the reader clicks, no account is needed to read, and there's no third-party widget: Mastodon replies are HTML and are sanitized in the browser with a self-hosted copy of DOMPurify, while Bluesky replies are plain text rendered as DOM nodes and need no sanitizer at all.

### Why these two, and not a comment service

Partly principle: both lean on an open standard rather than a proprietary widget, the site self-hosts the one script it needs, and neither adds tracking. Replies live on the poster's own account, on a network they chose, rather than in a database I own. And partly personal - this blog isn't a promotional exercise. Routing conversation through platforms I actually use fits better than bolting on a general-purpose comment service I don't believe in. Bluesky is here because a chunk of the conversation moved there; blending rather than tabbing the two keeps it one discussion rather than two half-empty ones.

Each network is independent and entirely optional - enable either, both, or neither in `publish.local.sh`:

```bash
MASTODON_SERVER="mastodon.social"    # your instance host, no https://
MASTODON_TOKEN="..."                 # scopes: write:statuses, write:bookmarks
MASTODON_ID="yourhandle"             # your @handle without the @

BLUESKY_HANDLE="you.bsky.social"
BLUESKY_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"   # an App Password, NOT your account password
```

Create the Bluesky app password at **bsky.app → Settings → App Passwords**. It can post on your behalf but cannot delete or migrate the account.

Each network also has two optional entries in `site.yaml` under `links:` - `mastodon` / `bluesky` for a header link, and `fediverse_creator` / `bluesky_creator` (handle or DID) to badge your own replies as OP. All four may be omitted: a link you leave out is not rendered at all, rather than pointing nowhere.

**Running a site with no social presence at all** is a supported configuration: leave the credentials out of `publish.local.sh` and the four `links:` entries out of `site.yaml`. Nothing is announced, no header link or `fediverse:creator` meta is emitted, and since the comment thread is driven by the coordinates that announcing writes, no post gets a comment section - `comments.js` is not even loaded. One caveat if you might change your mind later: every ingested post is stamped `announce: pending`, and with no network configured that marker never clears, so enabling one afterwards would announce the whole archive at once. Strip the `announce: pending` lines from older posts first if you only want new ones to go out.

With credentials set, deploying a new post announces it, bookmarks the announcement, and records its coordinates in the post's frontmatter so the thread appears. Re-runs never re-announce. Each network is skipped on its own when its credentials are missing, and if one network fails the other still goes out - the post stays pending and the next run posts only the missing half.

Posts published before you enable a network keep whatever threads they already have; enabling Bluesky does not retro-announce your archive.

### Moderating comments

Threads are **open by default** - every reply shows, because it lives publicly on its own network anyway. Moderation is a hand-maintained file, `content_pipeline/content/comment_moderation.json`, keyed by post slug:

```json
{ "posts": { "my-slug": {
    "blocked": ["12345", "at://did:plc:xxxx/app.bsky.feed.post/3lxyz"],
    "approved": []
} } }
```

Both networks share one flat list - a Mastodon status ID is all digits and a Bluesky post is an `at://` URI, so the build tells them apart on sight. Every rendered comment carries its exact ID in a `data-comment-id` attribute, so the easiest way to block one is to inspect it on your own page and copy the value.

- **Hide a reply**: add its ID to that post's `blocked` list. Its replies drop with it.
- **Approve-only mode**: set `comments: curated` in a post's frontmatter, then list the IDs you want shown in `approved` (your own replies always show). This is the escape hatch for a thread that attracts a pile-on; the rest of the site stays open.
- **Turn comments off** for a post: add `comments: false` to its frontmatter.

Blocking declines to *reproduce* a reply on your page; it does not conceal that one was removed, and the reply stays public on its network. Because this is how removed replies are kept off the site, any problem with the file (bad JSON, a slug that matches no post, an ID in neither format) fails the build on purpose rather than silently un-blocking anything.

## Testing and deploying

### Preview locally before you ship

Build the site without touching the network or needing any credentials:

```bash
./publish.sh --build
```

Then open the result in a browser:

```
public/index.html
```

Click around - the homepage, a post, a tag page, the search box, the theme toggle. A healthy build prints no `🗑️ Cleaned up stale file` lines; treat any as a sign something is misconfigured. (You can also run `python3 engine/build_blog.py` directly, but that skips the static-file copy that `publish.sh` does, so prefer `--build`.)

### Deploy to the web server

When you're happy with `public/`, ship it:

```bash
./publish.sh --deploy
```

This mirrors `public/` to your server over FTPS with `lftp`, deleting remote files that are no longer present locally so the live site matches your build exactly. Two safeguards: passing `--deploy` *is* the confirmation (no prompt), and it refuses to run if `public/index.html` is missing, since the mirror would otherwise strip the live site.

Or do the whole thing at once - ingest any drafts, build, pause for a `y/N` confirmation, then deploy:

```bash
./publish.sh
```

That confirmation gate is deliberate: it's the last chance to look at `public/` before anything goes live.

## Local-only files

These are gitignored - the engine stays separate from your credentials, drafts, writing, and generated output:

- `site.yaml`, `publish.local.sh`
- `content_pipeline/content/`, `drafts/`, `processed/`, `sources/`, `pages/`
- `content_pipeline/VOICE.md`
- `public/`, `public_static/`

The one static directory that *is* tracked is `engine/templates/static/` - the theme (CSS, search script, fonts) - because it's a dependency of the engine, not site-specific content.

## A note on deeper documentation

This README is the tour. The details of how the engine is put together - the module boundaries, the build steps, the conventions - live in `engine/docs/`. Start there if you want to change how the engine itself works.
