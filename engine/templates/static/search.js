/*
 * Local, in-browser full-text search for the blog.
 *
 * No third party and no library: it fetches the static /search-index.json
 * (built by build_blog.py from every post - title, summary, tags, and the
 * article body as plain text) and ranks matches client-side. The index is
 * fetched lazily on first interaction with the search box, so ordinary reads
 * never pay for it, and it's served gzipped with an hour of caching (.htaccess).
 *
 * Ranking is deliberately simple: each query term must appear somewhere in a
 * post (AND), and hits are weighted by field (title > tags > summary > body).
 * Good enough for a personal blog; no fuzzy/typo matching.
 */
(function () {
    var input = document.getElementById('site-search');
    var results = document.getElementById('search-results');
    if (!input || !results) return;

    var MAX_RESULTS = 8;
    var DEBOUNCE_MS = 120;

    // Fetch the index once, on first use. Cached as a promise so concurrent
    // callers share the single request; failures resolve to an empty index so
    // the box degrades quietly rather than throwing.
    var indexPromise = null;
    function loadIndex() {
        if (!indexPromise) {
            indexPromise = fetch('/search-index.json')
                .then(function (r) { return r.ok ? r.json() : []; })
                .catch(function () { return []; });
        }
        return indexPromise;
    }

    function tokenize(s) {
        return (s || '').toLowerCase().match(/[a-z0-9]+/g) || [];
    }

    // Every term must hit somewhere (AND). Score by where it hits.
    function scoreRecord(rec, terms) {
        var title = (rec.title || '').toLowerCase();
        var tags = (rec.tags || []).join(' ').toLowerCase();
        var summary = (rec.summary || '').toLowerCase();
        var text = (rec.text || '').toLowerCase();
        var score = 0;
        for (var i = 0; i < terms.length; i++) {
            var t = terms[i];
            var hit = false;
            if (title.indexOf(t) !== -1) { score += 8; hit = true; }
            if (tags.indexOf(t) !== -1) { score += 5; hit = true; }
            if (summary.indexOf(t) !== -1) { score += 3; hit = true; }
            if (text.indexOf(t) !== -1) { score += 1; hit = true; }
            if (!hit) return 0;
        }
        return score;
    }

    // A short body excerpt centred on the first matching term, for context.
    function snippet(rec, terms) {
        var text = rec.text || rec.summary || '';
        var low = text.toLowerCase();
        var pos = -1;
        for (var i = 0; i < terms.length && pos === -1; i++) {
            pos = low.indexOf(terms[i]);
        }
        if (pos === -1) return (rec.summary || '').slice(0, 140);
        var start = Math.max(0, pos - 40);
        var end = Math.min(text.length, pos + 110);
        return (start > 0 ? '…' : '') +
            text.slice(start, end).trim() +
            (end < text.length ? '…' : '');
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
    }

    function show() {
        results.hidden = false;
        input.setAttribute('aria-expanded', 'true');
    }
    function hide() {
        results.hidden = true;
        input.setAttribute('aria-expanded', 'false');
    }

    function render(matches, terms) {
        if (!matches.length) {
            results.innerHTML = '<p class="search-empty">No matches found.</p>';
        } else {
            results.innerHTML = matches.map(function (rec) {
                return '<a class="search-hit" href="' + escapeHtml(rec.url) + '" role="option">' +
                    '<span class="search-hit-title">' + escapeHtml(rec.title || 'Untitled') + '</span>' +
                    '<span class="search-hit-snippet">' + escapeHtml(snippet(rec, terms)) + '</span>' +
                    '</a>';
            }).join('');
        }
        show();
    }

    function run() {
        var terms = tokenize(input.value);
        if (!terms.length) { results.innerHTML = ''; hide(); return; }
        loadIndex().then(function (index) {
            // Ignore a stale async result if the box was cleared meanwhile.
            if (!tokenize(input.value).length) return;
            var scored = [];
            for (var i = 0; i < index.length; i++) {
                var s = scoreRecord(index[i], terms);
                if (s > 0) scored.push({ rec: index[i], score: s });
            }
            scored.sort(function (a, b) { return b.score - a.score; });
            render(scored.slice(0, MAX_RESULTS).map(function (x) { return x.rec; }), terms);
        });
    }

    var timer = null;
    input.addEventListener('input', function () {
        clearTimeout(timer);
        timer = setTimeout(run, DEBOUNCE_MS);
    });
    // Warm the index as soon as the reader shows intent to search.
    input.addEventListener('focus', function () {
        loadIndex();
        if (input.value) run();
    });
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { hide(); input.blur(); }
        else if (e.key === 'Enter') {
            var first = results.querySelector('.search-hit');
            if (first) { e.preventDefault(); window.location.href = first.getAttribute('href'); }
        }
    });
    // Close the dropdown when clicking away from the search box.
    document.addEventListener('click', function (e) {
        if (e.target !== input && !results.contains(e.target)) hide();
    });
})();
