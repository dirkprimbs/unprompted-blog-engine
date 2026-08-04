"""Build-time image optimisation: make photographs cheap to download.

Why this exists: the engine self-hosts every local image a post references, but
it used to host them in whatever format the author exported. A photograph saved
as PNG is lossless and therefore enormous - a single post here shipped seven of
them at 1.5-2.7 MB each, twelve megabytes for one page. The pixel dimensions
were fine; the format was the problem. Re-encoding those as JPEG cuts the page
to roughly a tenth with no visible difference.

The hard part is not the encoding, it is deciding *what not to touch*. JPEG is
wrong for screenshots, diagrams, logos and anything with transparency - it puts
ringing artefacts around text and sharp edges, and it cannot represent an alpha
channel at all. So `optimize()` is deliberately conservative and only converts
an image when every one of these holds:

  * it is not already a JPEG (those only get resized, never re-encoded, so a
    rebuild can't quietly recompress the same file over and over);
  * it is at least `min_bytes` on disk - small files are not worth the risk;
  * it is fully opaque - any real transparency and it stays as it is;
  * it looks photographic (see `_is_photographic`);
  * and the JPEG actually comes out meaningfully smaller.

Anything that fails a test is left exactly as the author exported it.

**A format conversion never deletes its original.** Re-encoding is lossy and
this runs unattended as part of every build, so the source stays on disk in
content_pipeline/content/assets/, just no longer referenced by the post. It
costs local disk and nothing else - unreferenced files are not copied into
public/ - and it means a bad conversion is undone by reverting one line of
Markdown rather than by re-exporting from the photo library.

**Downscaling a JPEG is the exception: it rewrites the file in place**, because
the output has the same name as the input and keeping both would mean inventing
a size-suffixed filename scheme for a case that has no format decision to
second-guess. Those pixels are gone from content/assets/ - which is a derived
cache, not an archive, so the real master is wherever the photo was exported
from. If that is not true for you, set `images.max_width: 0` and pre-size your
exports instead.

Pillow is an optional dependency. Without it the whole module no-ops (AVAILABLE
is False) and the build proceeds exactly as it did before, printing one warning
rather than failing - a fork that just wants to publish text should not need an
imaging library.
"""

import os

try:
    from PIL import Image, ImageOps
    AVAILABLE = True
except ImportError:          # optional dependency - see module docstring
    Image = ImageOps = None
    AVAILABLE = False


# Formats worth re-encoding. JPEG is excluded on purpose: it is already lossy,
# so converting it again would only lose quality, and doing it on every build
# would compound that loss. JPEGs still get resized by `optimize()`.
_CONVERTIBLE = {'PNG', 'TIFF', 'BMP', 'PPM'}

# The photographic test samples the image down to this many pixels per side
# before counting colours - a few thousand pixels is plenty to tell a photo
# from a screenshot, and it keeps the check fast on multi-megapixel files.
_SAMPLE_PX = 256

# Distinct colours in that sample, below which an image is flat artwork - a
# chart, a map, a logo. A photograph saturates the sample (tens of thousands of
# distinct values); line art lands in the hundreds.
_PHOTO_COLOR_THRESHOLD = 4096

# Share of the sample the sixteen commonest exact colours may cover before the
# image is treated as a screenshot rather than a photograph.
#
# The colour count alone is not enough, and the failure it misses is the one
# that matters: a screenshot of an image editor is mostly flat UI chrome, but
# the photo inside its preview pane contributes tens of thousands of colours
# and drags the whole file over the threshold. JPEG then puts ringing around
# every menu label. Dominant-colour share catches it, because no photograph has
# a handful of pixel values covering half the frame.
#
# Measured across this blog's own archive: photographs top out at 0.32, real
# screenshots start at 0.46. The gap is genuine but not enormous, so the
# threshold sits nearer the screenshot end - skipping a conversion only leaves
# a page heavy, while a wrong conversion visibly damages someone's screenshot.
_PHOTO_DOMINANCE_THRESHOLD = 0.38

# A conversion has to earn itself: anything less than this much smaller is not
# worth trading a lossless original for a lossy one.
_MIN_SAVING_RATIO = 0.25


def _open(path):
    """Open an image, applying any EXIF orientation. Returns (img, format, animated).

    The rotation has to be baked into the pixels here because the JPEG is
    written without an EXIF block - keeping the tag but dropping the metadata
    would silently turn every phone photo on its side.

    `format` and `is_animated` are read off the *source* object and passed back
    separately on purpose: Pillow does not carry either through
    `exif_transpose()` (the transposed image reports format None), so reading
    them from the returned image would make every file look unrecognised and
    silently disable the whole conversion step.
    """
    with Image.open(path) as src:
        src.load()
        fmt = (src.format or '').upper()
        animated = getattr(src, 'is_animated', False)
        return ImageOps.exif_transpose(src) or src.copy(), fmt, animated


def _is_opaque(img):
    """True when the image has no meaningful transparency.

    A mode with an alpha channel is not enough on its own - plenty of exports
    carry a fully-opaque alpha channel - so this looks at the actual extremes
    of the channel rather than at the mode.
    """
    if img.mode in ('RGBA', 'LA'):
        alpha = img.getchannel('A')
        return alpha.getextrema()[0] == 255
    if img.mode == 'P':
        return 'transparency' not in img.info
    return 'transparency' not in img.info


def _is_photographic(img):
    """Guess whether this is a photograph rather than a screenshot or diagram.

    Two signals, and the image has to pass both: it must use many distinct
    colours, *and* no small set of colours may dominate it. Neither works
    alone - see the notes on the two thresholds above.

    The sample is resized with NEAREST specifically so resampling cannot invent
    intermediate colours. A smooth resize would blur a screenshot's flat panels
    into gradients, inflating the colour count and erasing the dominance signal
    at the same time - i.e. it would defeat both tests at once.
    """
    sample = img.convert('RGB').resize((_SAMPLE_PX, _SAMPLE_PX), Image.NEAREST)
    total = _SAMPLE_PX * _SAMPLE_PX
    counts = sample.getcolors(maxcolors=total)
    if counts is None:          # more distinct colours than pixels is impossible
        return True
    if len(counts) <= _PHOTO_COLOR_THRESHOLD:
        return False
    counts.sort(reverse=True)
    dominance = sum(n for n, _ in counts[:16]) / total
    return dominance < _PHOTO_DOMINANCE_THRESHOLD


def _unique_path(path):
    """A free filename next to `path`, so converting foo.png can never clobber
    an unrelated foo.jpg the author put in the same post."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


def _existing_conversion(target, expected_size):
    """`target` if it already holds this image's conversion, else None.

    Originals are kept, so a post that goes back to referencing foo.png - by an
    edit, a revert, or the same photo being dropped into a second draft - would
    otherwise convert it again and, because foo.jpg is taken, land on
    foo-2.jpg. Repeat and you get foo-3.jpg: byte-identical duplicates, one per
    rebuild, each one uploaded.

    Identity is decided on the output dimensions rather than the name alone,
    which is what keeps _unique_path's original guarantee intact. A JPEG at
    exactly the size this conversion would produce, sitting under exactly this
    stem, is our earlier output; anything else is the author's own file and
    still gets a fresh name. Two genuinely different images sharing a stem
    *and* pixel dimensions would be misread here - vanishingly unlikely, and it
    shows up as an obviously wrong picture rather than as silent bloat.
    """
    if not os.path.exists(target):
        return None
    try:
        with Image.open(target) as existing:
            # Our own output carries no EXIF, so .size needs no orientation fix.
            if existing.format == 'JPEG' and existing.size == expected_size:
                return target
    except Exception:
        return None
    return None


def dimensions(path):
    """(width, height) of an image on disk, or None if it cannot be read.

    Used by the build to stamp width/height onto every <img> so the browser can
    reserve the space before the bytes arrive. Returns None rather than raising
    so a single unreadable file costs an attribute, not the build.
    """
    if not AVAILABLE:
        return None
    try:
        with Image.open(path) as img:
            width, height = img.size
            # Read the orientation tag rather than calling exif_transpose():
            # this runs for every image on every build and only needs the two
            # numbers, so decoding the pixels to rotate them would be waste.
            # Orientations 5-8 are the transposed ones, where the stored width
            # is the displayed height.
            try:
                orientation = img.getexif().get(274)
            except Exception:
                orientation = None
            if orientation in (5, 6, 7, 8):
                width, height = height, width
            return width, height
    except Exception:
        return None


def optimize(path, max_width=1600, jpeg_quality=82, min_bytes=200_000):
    """Re-encode and/or downscale one image in place-ish. Returns (path, saved).

    `path` is the file to consider; the returned path is either the same file
    (unchanged, `saved` == 0) or a **new** JPEG beside it, with the original
    left on disk untouched. Callers are expected to repoint their Markdown at
    the returned path.

    `max_width` caps the stored width - a post's text column is ~760px, so 1600
    still covers a 2x display - and 0 disables downscaling. `min_bytes` is the
    size below which a file is simply not worth touching.

    Every failure mode is non-fatal: an unreadable or exotic file returns
    unchanged, because a broken image should cost a page some bytes, not stop a
    publish.
    """
    if not AVAILABLE:
        return path, 0

    try:
        original_size = os.path.getsize(path)
    except OSError:
        return path, 0

    # Cheap gate first. This runs for every image on every build, and almost
    # every call is a no-op on a file optimised by an earlier build, so decide
    # from the header alone whether the expensive full decode is worth doing.
    # The size test uses the longer edge deliberately: it can only over-admit
    # (an image that turns out not to need resizing after EXIF rotation), which
    # costs one wasted decode, where under-admitting would skip real work.
    try:
        with Image.open(path) as probe:
            fmt = (probe.format or '').upper()
            # Animated images have frames this pipeline would silently flatten.
            if getattr(probe, 'is_animated', False):
                return path, 0
            maybe_wide = bool(max_width) and max(probe.size) > max_width
    except Exception as exc:
        print(f"   ⚠️  Could not read {os.path.basename(path)}: {exc}")
        return path, 0

    maybe_convertible = fmt in _CONVERTIBLE and original_size >= min_bytes
    if not maybe_convertible and not (maybe_wide and fmt == 'JPEG'):
        return path, 0

    try:
        img, fmt, _ = _open(path)
        with img:
            too_wide = bool(max_width) and img.width > max_width
            convertible = (maybe_convertible
                           and _is_opaque(img)
                           and _is_photographic(img))

            if not convertible and not (too_wide and fmt == 'JPEG'):
                return path, 0

            out = img
            if too_wide:
                height = round(img.height * max_width / img.width)
                out = img.convert('RGB').resize((max_width, height), Image.LANCZOS)

            # Keep the colour profile: these are photographs, and dropping the
            # profile is what turns a wide-gamut export dull in the browser.
            save_kwargs = {
                'quality': jpeg_quality,
                'optimize': True,
                'progressive': True,
            }
            profile = img.info.get('icc_profile')
            if profile:
                save_kwargs['icc_profile'] = profile

            if convertible:
                target = os.path.splitext(path)[0] + '.jpg'
                # Re-encoding an original we have already converted would just
                # mint a duplicate under a new name, once per rebuild.
                done = _existing_conversion(target, out.size)
                if done:
                    return done, 0
                target = _unique_path(target)
                out.convert('RGB').save(target, 'JPEG', **save_kwargs)
                new_size = os.path.getsize(target)
                # Only keep the conversion if it actually paid for itself.
                if new_size > original_size * (1 - _MIN_SAVING_RATIO):
                    os.remove(target)
                    return path, 0
                return target, original_size - new_size

            # A JPEG that was only too wide: rewrite it in place. Nothing is
            # lost that resizing had not already discarded.
            out.convert('RGB').save(path, 'JPEG', **save_kwargs)
            return path, max(0, original_size - os.path.getsize(path))

    except Exception as exc:
        print(f"   ⚠️  Could not optimise {os.path.basename(path)}: {exc}")
        return path, 0
