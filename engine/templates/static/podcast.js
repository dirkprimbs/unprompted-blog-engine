/* Episode player: the control the engine emits for a post that links audio.
 *
 * Progressive enhancement, the same bargain the rest of the theme makes: the
 * markup ships a real <audio controls> element, and this script only takes over
 * once it has successfully replaced it. With the script blocked, missing, or
 * broken, the page is still an audio page with working browser controls - which
 * matters more here than anywhere else on the site, because an episode page
 * whose player does not work has no content at all.
 */
(function () {
    'use strict';

    // The header's Subscribe link. Its href is the feed, so it works with no
    // JavaScript at all and keeps working if the Podlove widget fails to load.
    // But Podlove's own handler opens its app chooser WITHOUT cancelling the
    // click, so when the widget is present both happen: the popup opens and the
    // browser navigates away to the raw XML underneath it.
    //
    // The check happens at click time rather than at load: this file is
    // deferred and Podlove binds on DOMContentLoaded, so at load time its
    // instances do not exist yet. If they never appear - script blocked,
    // offline, CDN of one's own broken - nothing is cancelled and the link
    // still does the useful thing.
    document.querySelectorAll('.subscribe-link').forEach(function (link) {
        link.addEventListener('click', function (event) {
            if (window.subscribeButtons) event.preventDefault();
        });
    });

    function show(el, visible) {
        if (!el) return;
        if (visible) el.removeAttribute('hidden');
        else el.setAttribute('hidden', '');
    }

    function fmt(seconds) {
        if (!isFinite(seconds)) return '--:--';
        var s = Math.floor(seconds % 60);
        var m = Math.floor(seconds / 60) % 60;
        var h = Math.floor(seconds / 3600);
        var mm = (h && m < 10 ? '0' : '') + m;
        return (h ? h + ':' : '') + mm + ':' + (s < 10 ? '0' : '') + s;
    }

    document.querySelectorAll('.episode-player').forEach(function (card) {
        var audio = card.querySelector('audio');
        var play = card.querySelector('.episode-play');
        var label = card.querySelector('.episode-play .t');
        var track = card.querySelector('.episode-progress');
        var bar = card.querySelector('.episode-progress .bar');
        var elapsed = card.querySelector('.episode-elapsed');
        if (!audio || !play || !track) return;

        // Only now is the native control redundant. Removing the attribute
        // before this point would leave a player-less page if anything above
        // threw.
        audio.removeAttribute('controls');
        audio.style.display = 'none';
        card.querySelector('.episode-controls').hidden = false;

        // The duration in the markup is the feed's, which is what lets the page
        // show a length before any audio is fetched. Once metadata arrives the
        // real one wins, since a feed's duration is often rounded or wrong.
        var stated = play.getAttribute('data-duration') || '';
        if (label && stated) label.textContent = stated;

        function paint() {
            var d = audio.duration;
            var pct = d ? (audio.currentTime / d) * 100 : 0;
            bar.style.width = pct + '%';
            track.setAttribute('aria-valuenow', Math.round(pct));
            if (elapsed) {
                elapsed.textContent = fmt(audio.currentTime) + ' / ' + fmt(d);
            }
        }

        function setPlayingState(playing) {
            play.setAttribute('aria-pressed', playing ? 'true' : 'false');
            // setAttribute, not `.hidden = `. `hidden` is an IDL attribute of
            // HTMLElement and these are SVG elements, so assigning the property
            // sets a meaningless JS expando and never touches the attribute the
            // stylesheet matches on. The icons then never swap - the button
            // shows whatever the markup shipped, forever.
            show(play.querySelector('.icon-play'), !playing);
            show(play.querySelector('.icon-pause'), playing);
            if (label) {
                label.textContent = playing ? fmt(audio.currentTime) : (stated || fmt(audio.duration));
            }
        }

        play.addEventListener('click', function () {
            if (audio.paused) {
                // A play() rejection is normal, not exceptional: autoplay
                // policies and a still-loading source both land here, and the
                // button must not get stuck showing a pause icon for audio that
                // never started.
                var p = audio.play();
                if (p && p.catch) p.catch(function () { setPlayingState(false); });
            } else {
                audio.pause();
            }
        });

        audio.addEventListener('play', function () { setPlayingState(true); });
        audio.addEventListener('pause', function () { setPlayingState(false); });
        audio.addEventListener('ended', function () { setPlayingState(false); });
        audio.addEventListener('timeupdate', function () {
            paint();
            if (label && !audio.paused) label.textContent = fmt(audio.currentTime);
        });
        audio.addEventListener('loadedmetadata', function () {
            // The real duration replaces the feed's now that we have it. Doing
            // this only here, rather than up front, is what lets the page show a
            // length before any audio is fetched.
            if (isFinite(audio.duration)) stated = fmt(audio.duration);
            if (label && audio.paused) label.textContent = stated;
            paint();
        });

        function seekTo(clientX) {
            var box = track.getBoundingClientRect();
            var ratio = Math.min(1, Math.max(0, (clientX - box.left) / box.width));
            if (audio.duration) audio.currentTime = ratio * audio.duration;
        }

        // Click to jump, drag to scrub. Pointer events rather than mouse ones so
        // a touchscreen gets the same behaviour, and setPointerCapture so a drag
        // that wanders off the bar - which every drag does - keeps scrubbing
        // instead of stopping the moment the pointer leaves.
        track.addEventListener('pointerdown', function (e) {
            if (e.button && e.button !== 0) return;
            e.preventDefault();
            track.classList.add('is-scrubbing');
            try { track.setPointerCapture(e.pointerId); } catch (err) {}
            seekTo(e.clientX);
        });
        track.addEventListener('pointermove', function (e) {
            if (!track.classList.contains('is-scrubbing')) return;
            seekTo(e.clientX);
        });
        function endScrub(e) {
            if (!track.classList.contains('is-scrubbing')) return;
            track.classList.remove('is-scrubbing');
            try { track.releasePointerCapture(e.pointerId); } catch (err) {}
        }
        track.addEventListener('pointerup', endScrub);
        track.addEventListener('pointercancel', endScrub);
        track.addEventListener('keydown', function (e) {
            var duration = audio.duration || 0;
            var step = e.key === 'ArrowRight' ? 15 : e.key === 'ArrowLeft' ? -15 : 0;
            if (e.key === 'Home') { audio.currentTime = 0; e.preventDefault(); return; }
            if (e.key === 'End') { audio.currentTime = duration; e.preventDefault(); return; }
            if (!step) return;
            e.preventDefault();
            audio.currentTime = Math.max(0, Math.min(duration, audio.currentTime + step));
        });

        paint();
    });
}());
