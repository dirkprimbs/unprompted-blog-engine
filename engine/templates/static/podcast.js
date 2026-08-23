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
            play.querySelector('.icon-play').hidden = playing;
            play.querySelector('.icon-pause').hidden = !playing;
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
        track.addEventListener('click', function (e) { seekTo(e.clientX); });
        track.addEventListener('keydown', function (e) {
            var step = e.key === 'ArrowRight' ? 15 : e.key === 'ArrowLeft' ? -15 : 0;
            if (!step) return;
            e.preventDefault();
            audio.currentTime = Math.max(0, Math.min(audio.duration || 0, audio.currentTime + step));
        });

        paint();
    });
}());
