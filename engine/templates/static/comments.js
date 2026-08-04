/*
 * Comments, blended from Mastodon and Bluesky into one thread.
 *
 * This blog has no comment database. Each post is announced on both networks
 * (engine/announce.py), the announcement's coordinates are written back into
 * the post's frontmatter, and build_blog.py bakes them into the page as
 * data-* attributes. On click, this script fetches the replies to both
 * announcements and renders them as a single chronological list.
 *
 * Both reads are unauthenticated public API calls, which is the whole point:
 * the thread is always live, and moderating it never requires re-publishing.
 * A post announced on only one network (every post predating Bluesky support)
 * works unchanged - whichever coordinates are present get fetched.
 *
 * Two things differ sharply between the networks, and most of the code below
 * exists because of them:
 *
 *   1. Mastodon returns a FLAT list of descendants and HTML bodies. The HTML is
 *      third-party, so it goes through DOMPurify (self-hosted, lazily loaded).
 *   2. Bluesky returns a RECURSIVE TREE and PLAIN TEXT bodies. Links exist only
 *      as "facets" carrying UTF-8 BYTE offsets, so the text has to be sliced as
 *      bytes, not as a JS string. Because the body is built from text nodes and
 *      anchors rather than parsed as markup, it needs no sanitizer at all -
 *      a Bluesky-only post never fetches DOMPurify.
 *
 * Both are normalised to one internal record shape so the renderer below never
 * branches on network except to draw the origin badge.
 */
(function () {
    var section = document.querySelector('.comments');
    if (!section) return;
    var button = section.querySelector('.comments-load');
    var list = section.querySelector('.comments-list');
    if (!button || !list) return;

    var mastodonHost = section.getAttribute('data-mastodon-host');
    var mastodonId = section.getAttribute('data-mastodon-id');
    var blueskyUri = section.getAttribute('data-bluesky-uri');
    if (!mastodonHost && !blueskyUri) return;

    // How deep a Bluesky reply tree to request. The API allows up to 1000, but
    // the renderer only indents one level, so anything past a handful is fetched
    // purely to keep deep sub-threads from vanishing.
    var BLUESKY_DEPTH = 20;

    // Moderation, baked in at build time from comment_moderation.json. 'open'
    // shows every reply except the blocklist; 'curated' shows only the approve
    // list plus the author's own replies. Kept per network: an ID only ever
    // means something on the network it came from.
    var curated = section.getAttribute('data-comments-mode') === 'curated';
    var authorAcct = (section.getAttribute('data-author-acct') || '').toLowerCase();
    var authorBluesky = (section.getAttribute('data-author-bluesky') || '').toLowerCase();

    function idSet(attr) {
        var out = {};
        (section.getAttribute(attr) || '').split(',').forEach(function (raw) {
            var id = raw.trim();
            if (id) out[id] = true;
        });
        return out;
    }
    var blocked = {
        mastodon: idSet('data-blocked-mastodon'),
        bluesky: idSet('data-blocked-bluesky')
    };
    var approved = {
        mastodon: idSet('data-approved-mastodon'),
        bluesky: idSet('data-approved-bluesky')
    };

    function loadScript(src) {
        return new Promise(function (resolve, reject) {
            var s = document.createElement('script');
            s.src = src;
            s.onload = resolve;
            s.onerror = function () { reject(new Error('failed to load ' + src)); };
            document.head.appendChild(s);
        });
    }

    // The sanitizer's URL carries a ?v= content hash and is cached for a year as
    // immutable, so the version has to come from the build. This file is copied
    // to public/ verbatim - it is never run through the template renderer - so
    // the URL arrives as a data attribute rather than a placeholder.
    function ensureSanitizer() {
        if (window.DOMPurify) return Promise.resolve();
        var src = section.getAttribute('data-sanitizer-src');
        if (!src) return Promise.reject(new Error('no sanitizer configured'));
        return loadScript(src);
    }

    function setStatus(msg) {
        list.innerHTML = '';
        var p = document.createElement('p');
        p.className = 'comments-status';
        p.textContent = msg;
        list.appendChild(p);
    }

    /* ---------------------------------------------------------------- *
     * Mastodon
     * ---------------------------------------------------------------- */

    // Emoji shortcodes -> <img> (Mastodon custom emoji). Runs on already
    // sanitized text; escapes the URL to be safe. Bluesky has no equivalent -
    // emoji there are plain Unicode - so this is Mastodon-only.
    function emojify(html, emojis) {
        if (!emojis || !emojis.length) return html;
        emojis.forEach(function (e) {
            var url = encodeURI(e.static_url || e.url || '');
            if (!url) return;
            var tag = '<img class="emoji" draggable="false" alt=":' +
                e.shortcode + ':" src="' + url + '">';
            html = html.split(':' + e.shortcode + ':').join(tag);
        });
        return html;
    }

    function mastodonInstance(acct) {
        // Local accounts have no '@domain'; fall back to the toot's host.
        var parts = acct.split('@');
        return parts.length > 1 ? parts[parts.length - 1] : mastodonHost;
    }

    function isMastodonAuthor(acct) {
        if (!authorAcct) return false;
        // Accounts local to this instance have a bare 'user' acct; remote ones
        // carry 'user@instance'. Normalise to the latter.
        var full = acct.indexOf('@') === -1 ? acct + '@' + mastodonHost : acct;
        return full.toLowerCase() === authorAcct;
    }

    function fetchMastodon() {
        return fetch('https://' + mastodonHost + '/api/v1/statuses/' +
                encodeURIComponent(mastodonId) + '/context')
            .then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function (context) {
                var all = (context && context.descendants) || [];
                if (!all.length) return [];
                // Sanitizing needs DOMPurify; only load it now that we know
                // there is third-party HTML to clean.
                return ensureSanitizer().then(function () {
                    return all.map(function (s) {
                        var acct = s.account;
                        var parentId = String(s.in_reply_to_id);
                        return {
                            network: 'mastodon',
                            id: String(s.id),
                            // The announcement itself is not a parent worth
                            // indenting under - it is the article.
                            parentId: parentId === String(mastodonId) ? null : parentId,
                            authorName: window.DOMPurify.sanitize(
                                emojify(acct.display_name || acct.username, acct.emojis),
                                { ALLOWED_TAGS: ['img'], ALLOWED_ATTR: ['class', 'src', 'alt', 'draggable'] }
                            ),
                            authorHandle: '@' + acct.acct.split('@')[0] + '@' +
                                mastodonInstance(acct.acct),
                            authorUrl: acct.url,
                            avatar: acct.avatar_static || acct.avatar || '',
                            isAuthor: isMastodonAuthor(acct.acct),
                            bodyHtml: window.DOMPurify.sanitize(
                                emojify(s.content || '', s.emojis)),
                            createdAt: s.created_at,
                            permalink: s.url,
                            likes: s.favourites_count || 0
                        };
                    });
                });
            });
    }

    /* ---------------------------------------------------------------- *
     * Bluesky
     * ---------------------------------------------------------------- */

    // at://<authority>/<collection>/<rkey> -> https://bsky.app/profile/<authority>/post/<rkey>
    // The authority is the author's DID and is used as-is: handles are not
    // durable, so a permalink built from one breaks when its owner renames.
    function blueskyPermalink(uri) {
        var parts = String(uri).split('/');
        if (parts.length !== 5) return '';
        return 'https://bsky.app/profile/' + parts[2] + '/post/' + parts[4];
    }

    function isBlueskyAuthor(account) {
        if (!authorBluesky) return false;
        return String(account.handle || '').toLowerCase() === authorBluesky ||
            String(account.did || '').toLowerCase() === authorBluesky;
    }

    // Build a post body from Bluesky's plain text plus its rich-text facets.
    //
    // Facet offsets are UTF-8 BYTE offsets, and JS strings are UTF-16 - slicing
    // the string directly silently corrupts every post containing an emoji or
    // non-Latin text, which is common enough to be the normal case rather than
    // an edge one. So the text is encoded to bytes once and every slice is
    // taken there, then decoded back.
    //
    // Facets arrive from arbitrary clients: they are not guaranteed sorted, may
    // overlap, and may point outside the text. Anything malformed is skipped
    // rather than trusted.
    function renderBlueskyText(text, facets) {
        var div = document.createElement('div');
        var encoder = new TextEncoder();
        var decoder = new TextDecoder();
        var bytes = encoder.encode(text || '');

        var ranges = (facets || [])
            .filter(function (f) {
                return f && f.index &&
                    Number.isInteger(f.index.byteStart) &&
                    Number.isInteger(f.index.byteEnd) &&
                    f.index.byteStart >= 0 &&
                    f.index.byteEnd > f.index.byteStart &&
                    f.index.byteEnd <= bytes.length &&
                    Array.isArray(f.features) && f.features.length;
            })
            .sort(function (a, b) { return a.index.byteStart - b.index.byteStart; });

        var cursor = 0;
        ranges.forEach(function (facet) {
            // Skip a facet overlapping one already drawn.
            if (facet.index.byteStart < cursor) return;
            if (facet.index.byteStart > cursor) {
                div.appendChild(document.createTextNode(
                    decoder.decode(bytes.slice(cursor, facet.index.byteStart))));
            }
            var label = decoder.decode(
                bytes.slice(facet.index.byteStart, facet.index.byteEnd));
            // A range can carry several features; the first recognised one wins.
            var feature = facet.features.find(function (f) {
                return f && (f.$type === 'app.bsky.richtext.facet#link' ||
                    f.$type === 'app.bsky.richtext.facet#tag' ||
                    f.$type === 'app.bsky.richtext.facet#mention');
            });
            var href = '';
            if (feature) {
                if (feature.$type === 'app.bsky.richtext.facet#link') {
                    href = feature.uri || '';
                    // Only http(s). A facet is third-party data and could name
                    // a javascript: URL.
                    if (!/^https?:\/\//i.test(href)) href = '';
                } else if (feature.$type === 'app.bsky.richtext.facet#tag') {
                    href = 'https://bsky.app/hashtag/' +
                        encodeURIComponent(feature.tag || '');
                } else if (feature.$type === 'app.bsky.richtext.facet#mention') {
                    href = 'https://bsky.app/profile/' +
                        encodeURIComponent(feature.did || '');
                }
            }
            if (href) {
                var a = document.createElement('a');
                a.href = href;
                a.rel = 'nofollow noopener';
                a.target = '_blank';
                a.textContent = label;
                div.appendChild(a);
            } else {
                div.appendChild(document.createTextNode(label));
            }
            cursor = facet.index.byteEnd;
        });
        if (cursor < bytes.length) {
            div.appendChild(document.createTextNode(
                decoder.decode(bytes.slice(cursor))));
        }
        return div;
    }

    // Bluesky returns a tree; the renderer wants a flat list. Walk it depth
    // first, skipping the union members that carry no post (#notFoundPost for a
    // deleted reply, #blockedPost for one from a blocked account). Dispatch on
    // $type, falling back to the const-true marker keys.
    function flattenBluesky(node, rootUri, out) {
        if (!node || node.notFound || node.blocked) return;
        if (node.$type && node.$type !== 'app.bsky.feed.defs#threadViewPost') return;
        var post = node.post;
        if (post && post.uri && post.uri !== rootUri) {
            var record = post.record || {};
            var account = post.author || {};
            var parentUri = record.reply && record.reply.parent &&
                record.reply.parent.uri;
            out.push({
                network: 'bluesky',
                id: String(post.uri),
                parentId: (parentUri && parentUri !== rootUri) ? String(parentUri) : null,
                // textContent, not innerHTML: display names are user-supplied.
                authorName: account.displayName || account.handle || '',
                authorHandle: '@' + (account.handle || ''),
                authorUrl: 'https://bsky.app/profile/' + (account.did || account.handle || ''),
                avatar: account.avatar || '',
                isAuthor: isBlueskyAuthor(account),
                // Plain text plus facets - built as DOM, never parsed as markup.
                bodyNode: renderBlueskyText(record.text || '', record.facets),
                createdAt: record.createdAt || post.indexedAt,
                permalink: blueskyPermalink(post.uri),
                // Every count field is optional in the lexicon, so absent
                // means "none", not "unknown".
                likes: post.likeCount || 0
            });
        }
        (node.replies || []).forEach(function (child) {
            flattenBluesky(child, rootUri, out);
        });
    }

    function fetchBluesky() {
        var url = 'https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread' +
            '?uri=' + encodeURIComponent(blueskyUri) +
            '&depth=' + BLUESKY_DEPTH + '&parentHeight=0';
        return fetch(url)
            .then(function (r) {
                // A missing post answers 400 with {"error":"NotFound"}, not 404.
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function (data) {
                var out = [];
                flattenBluesky(data && data.thread, blueskyUri, out);
                return out;
            });
    }

    /* ---------------------------------------------------------------- *
     * Moderation, merge and render
     * ---------------------------------------------------------------- */

    // Blocking a reply also drops everything hanging off it - a thread
    // continuing a removed reply quotes it by implication. Iterate to a fixed
    // point since replies aren't guaranteed parents-first. Runs per network:
    // a reply can only ever descend from one on its own network.
    function blockedSubtree(comments) {
        var out = {};
        ['mastodon', 'bluesky'].forEach(function (network) {
            Object.keys(blocked[network]).forEach(function (id) { out[id] = true; });
        });
        var changed = true;
        while (changed) {
            changed = false;
            comments.forEach(function (c) {
                if (!out[c.id] && c.parentId && out[c.parentId]) {
                    out[c.id] = true;
                    changed = true;
                }
            });
        }
        return out;
    }

    function moderate(comments) {
        var dropped = blockedSubtree(comments);
        return comments.filter(function (c) {
            if (dropped[c.id]) return false;
            if (!curated) return true;
            // In curated mode an approved reply still shows even when the reply
            // it answers wasn't approved - it just renders flat, via the parent
            // check in render().
            return approved[c.network][c.id] || c.isAuthor;
        });
    }

    function renderComment(comment, visible) {
        // Indent only when the reply being answered is itself shown; otherwise
        // it would sit under nothing.
        var isReply = comment.parentId && visible[comment.parentId];

        var article = document.createElement('article');
        article.className = 'comment' + (isReply ? ' comment-reply' : '') +
            (comment.isAuthor ? ' comment-op' : '');
        // The exact ID this comment is moderated by, so blocking one is a
        // copy-paste from the page rather than a hunt through an API.
        article.setAttribute('data-comment-id', comment.id);

        if (comment.avatar) {
            var avatar = document.createElement('img');
            avatar.className = 'comment-avatar';
            avatar.loading = 'lazy';
            avatar.src = comment.avatar;
            avatar.alt = '';
            article.appendChild(avatar);
        }

        var bodyDiv = document.createElement('div');
        bodyDiv.className = 'comment-body';

        var head = document.createElement('div');
        head.className = 'comment-head';

        var author = document.createElement('a');
        author.className = 'comment-author';
        author.href = encodeURI(comment.authorUrl || '');
        author.rel = 'nofollow noopener';
        author.target = '_blank';
        // Mastodon display names may legitimately contain custom-emoji <img>
        // tags, already sanitized above. Bluesky's are plain text and carry no
        // markup, so they are assigned as text.
        if (comment.network === 'mastodon') {
            author.innerHTML = comment.authorName;
        } else {
            author.textContent = comment.authorName;
        }
        head.appendChild(author);

        var handle = document.createElement('span');
        handle.className = 'comment-instance';
        handle.textContent = ' ' + comment.authorHandle;
        head.appendChild(handle);

        // The origin badge. Comments blend into one list, so this is what keeps
        // two different handle formats from reading as a bug.
        var badge = document.createElement('span');
        badge.className = 'comment-network comment-network-' + comment.network;
        badge.textContent = comment.network === 'mastodon' ? 'Mastodon' : 'Bluesky';
        badge.title = 'Posted on ' + badge.textContent;
        head.appendChild(badge);

        if (comment.isAuthor) {
            var op = document.createElement('span');
            op.className = 'comment-op-badge';
            op.title = 'Original poster';
            op.textContent = 'OP';
            head.appendChild(op);
        }

        var when = comment.createdAt ? new Date(comment.createdAt) : null;
        if (when && !isNaN(when)) {
            var sep = document.createElement('span');
            sep.textContent = ' · ';
            head.appendChild(sep);
            var date = document.createElement('a');
            date.className = 'comment-date';
            date.href = encodeURI(comment.permalink || '');
            date.rel = 'nofollow noopener';
            date.target = '_blank';
            date.textContent = when.toLocaleDateString(undefined, {
                year: 'numeric', month: 'short', day: 'numeric'
            });
            head.appendChild(date);
        }
        bodyDiv.appendChild(head);

        var content = document.createElement('div');
        content.className = 'comment-content';
        if (comment.bodyNode) {
            content.appendChild(comment.bodyNode);
        } else {
            content.innerHTML = comment.bodyHtml || '';
        }
        bodyDiv.appendChild(content);

        if (comment.likes > 0) {
            var favs = document.createElement('a');
            favs.className = 'comment-favs';
            favs.href = encodeURI(comment.permalink || '');
            favs.rel = 'nofollow noopener';
            favs.target = '_blank';
            favs.textContent = '★ ' + comment.likes;
            bodyDiv.appendChild(favs);
        }

        article.appendChild(bodyDiv);
        return article;
    }

    function render(comments, failedNetworks) {
        if (!comments.length) {
            setStatus(failedNetworks.length
                ? 'Could not load comments right now.'
                : 'No comments yet. Be the first to reply.');
            return;
        }

        var visible = moderate(comments);
        if (!visible.length) {
            // Replies exist but none survived moderation. Don't claim the
            // thread is empty - that reads as a bug to anyone who can see the
            // discussion on either network.
            setStatus('No replies have been published for this post yet.');
            return;
        }

        // One thread: both networks interleaved oldest-first, which is how each
        // reads on its own and how a conversation actually happened.
        visible.sort(function (a, b) {
            return new Date(a.createdAt) - new Date(b.createdAt);
        });
        var shown = {};
        visible.forEach(function (c) { shown[c.id] = true; });

        list.innerHTML = '';
        visible.forEach(function (c) {
            list.appendChild(renderComment(c, shown));
        });

        // One network down is not the same as no comments. Say so rather than
        // presenting a half thread as the whole thread.
        if (failedNetworks.length) {
            var note = document.createElement('p');
            note.className = 'comments-status';
            note.textContent = 'Replies from ' + failedNetworks.join(' and ') +
                ' could not be loaded.';
            list.appendChild(note);
        }
    }

    button.addEventListener('click', function () {
        button.disabled = true;
        button.textContent = 'Loading comments…';

        var jobs = [];
        if (mastodonHost && mastodonId) {
            jobs.push({ name: 'Mastodon', run: fetchMastodon });
        }
        if (blueskyUri) {
            jobs.push({ name: 'Bluesky', run: fetchBluesky });
        }

        // allSettled, not all: one network being unreachable must not take the
        // other's replies down with it.
        Promise.allSettled(jobs.map(function (job) { return job.run(); }))
            .then(function (results) {
                var comments = [];
                var failed = [];
                results.forEach(function (result, i) {
                    if (result.status === 'fulfilled') {
                        comments = comments.concat(result.value);
                    } else {
                        failed.push(jobs[i].name);
                    }
                });
                if (failed.length === jobs.length) {
                    button.disabled = false;
                    button.textContent = '💬 Load comments';
                    setStatus('Could not load comments. You can still view the ' +
                        'discussion on ' + failed.join(' or ') + '.');
                    return;
                }
                button.remove();
                render(comments, failed);
            });
    });
})();
