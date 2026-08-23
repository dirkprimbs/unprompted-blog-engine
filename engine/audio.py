"""Build-time audio inspection: what a podcast feed has to know about a file.

An `<enclosure>` needs three facts about the audio it points at - where it is,
how many bytes it is, and what type it is - and `<itunes:duration>` wants a
fourth. Three of those come from the filename and the filesystem. Only the
duration needs to look inside the file, and that is the one this module exists
for.

Why the byte length matters more than it looks: it is not decoration, it is what
a podcast client uses to size its progress bar and to resume a partial download.
A wrong `length` makes seeking behave strangely in several apps long before
anything visibly breaks, so it is read from disk every build rather than cached
or guessed.

Mutagen is an optional dependency, exactly like Pillow in `images.py`. Without
it `AVAILABLE` is False, `duration()` returns None, and the feed omits
`<itunes:duration>` rather than inventing one - a missing optional tag costs a
listener a number on a screen, while a wrong one desynchronises their player.
Everything else here is stdlib and always works, so a podcast still publishes
correctly with no third-party library installed at all.

Deliberately not here: transcoding, loudness normalisation, chapter extraction.
The engine ships the file the author encoded, byte for byte. An episode is
mastered once, by someone who listened to it, and a build step that silently
re-encoded audio would be a worse version of that decision.
"""

import os

try:
    import mutagen
    AVAILABLE = True
except ImportError:          # optional dependency - see module docstring
    mutagen = None
    AVAILABLE = False


# Extensions that make a link an episode. Kept deliberately short: these are the
# formats podcast clients actually accept. Adding one here is also adding it to
# what `_standalone_link()` in build_blog.py treats as audio, so a format nobody
# can subscribe to should not be on this list.
#
# The MIME types are the ones Apple documents. Note '.m4a' -> 'audio/mp4' and
# not 'audio/x-m4a': the x- form appears in older feeds and some clients still
# special-case it, but it was never registered and the modern readers all
# prefer the real type.
_MIME = {
    '.mp3':  'audio/mpeg',
    '.m4a':  'audio/mp4',
    '.mp4':  'audio/mp4',
    '.ogg':  'audio/ogg',
    '.opus': 'audio/opus',
    '.wav':  'audio/wav',
    '.aac':  'audio/aac',
}

EXTENSIONS = tuple(_MIME)


def is_audio(path_or_url):
    """True when this looks like an audio file by extension.

    Extension rather than content sniffing on purpose: this has to answer for
    remote URLs too, where there are no bytes to sniff without a network call,
    and a link that ends '.mp3' but is not one is a broken link either way.
    Query strings and fragments are ignored, since a hosted enclosure often
    carries a tracking suffix.
    """
    clean = str(path_or_url).split('?', 1)[0].split('#', 1)[0]
    return clean.lower().endswith(EXTENSIONS)


def mime_for(path_or_url):
    """The MIME type an <enclosure type=...> should carry, by extension.

    Falls back to 'audio/mpeg' for an unrecognised extension rather than
    returning None: the attribute is required, and mp3 is the format a client
    is likeliest to cope with when the type turns out to be a lie.
    """
    clean = str(path_or_url).split('?', 1)[0].split('#', 1)[0]
    _, ext = os.path.splitext(clean.lower())
    return _MIME.get(ext, 'audio/mpeg')


def duration(path):
    """Length in whole seconds, or None if it cannot be determined.

    None is a normal answer, not an error: mutagen may be absent, the format may
    be one it cannot parse, or the file may be truncated. Every caller has to
    handle it anyway, so a failure here returns None rather than raising and
    costing the whole build one unreadable file.
    """
    if not AVAILABLE:
        return None
    try:
        meta = mutagen.File(path)
        if meta is None or not getattr(meta, 'info', None):
            return None
        seconds = getattr(meta.info, 'length', None)
        if not seconds or seconds <= 0:
            return None
        return int(round(seconds))
    except Exception:
        return None


def probe(path):
    """(bytes, seconds_or_None, mime) for a local file, or None if unreadable.

    The one call the build makes per episode. Byte length comes first because it
    is the part that must never be missing - if `os.path.getsize` fails there is
    no usable enclosure and the caller needs to say so rather than emit one with
    a made-up length.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    return size, duration(path), mime_for(path)


def format_duration(seconds):
    """Seconds as H:MM:SS, or M:SS under an hour.

    Both are valid <itunes:duration> values and this is the shape every podcast
    app displays, so the same string serves the feed and the player button and
    the two can never disagree.
    """
    if seconds is None:
        return None
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
