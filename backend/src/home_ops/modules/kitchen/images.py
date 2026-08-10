"""Turning an upload into a stored image (SPEC §4.6).

This is the first place the application accepts a file, which makes it the
largest new attack surface in the project. Five rules, each closing something
specific:

1. **Trust the bytes, never the request.** The filename and `Content-Type` are
   attacker-chosen strings. What an upload *is* comes from decoding it.

2. **Re-encode; never store what arrived.** Passing bytes through preserves
   whatever else was in the file. Decoding to pixels and writing a fresh WebP
   discards EXIF — which routinely carries the GPS coordinates of somebody's
   kitchen — and defuses a polyglot file that is a valid image *and* a valid
   script, because the output is generated rather than copied.

3. **Cap pixels, not just bytes.** A 2 KB PNG can declare a 50000x50000 canvas
   and cost gigabytes to decode. `UPLOAD_MAX_BYTES` does nothing about that;
   `MAX_PIXELS` does, and it is checked before `load()` rather than after.

4. **The filename is generated here.** Nothing derived from user input reaches
   a path, so there is no traversal to get wrong. See `storage.py`.

5. **Only three input formats.** JPEG, PNG and WebP cover every camera and every
   recipe site. Accepting SVG would mean accepting a document that can carry
   script, and accepting the exotic formats Pillow supports means inheriting
   their decoders' bugs for no benefit.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Final

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError, DecompressionBombWarning

#: What Pillow may decode. A format outside this list is rejected even if Pillow
#: understands it perfectly well.
ALLOWED_FORMATS: Final[frozenset[str]] = frozenset({"JPEG", "PNG", "WEBP"})

#: Decoded pixel ceiling. 50 megapixels is far beyond any phone camera and far
#: below what it takes to exhaust the container.
MAX_PIXELS: Final[int] = 50_000_000

# Pillow carries its own bomb check with two thresholds of its own — a warning
# at MAX_IMAGE_PIXELS and an error at twice that. Left alone it would disagree
# with the limit above, and a warning is not a refusal. Pointing it at the same
# number keeps one answer to "how big is too big", and both of its outcomes are
# caught below and turned into the same clean rejection.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

#: Longest edge of the stored image, and of the thumbnail used in lists.
FULL_MAX_EDGE: Final[int] = 1600
THUMB_MAX_EDGE: Final[int] = 400

FULL_QUALITY: Final[int] = 82
THUMB_QUALITY: Final[int] = 72

#: Everything is stored as WebP regardless of what arrived.
OUTPUT_FORMAT: Final[str] = "WEBP"
OUTPUT_MEDIA_TYPE: Final[str] = "image/webp"


class NotAnImage(ValueError):
    """The upload could not be decoded, or is a format we do not accept."""


class ImageTooLarge(ValueError):
    """Decoded dimensions exceed MAX_PIXELS."""


@dataclass(frozen=True)
class RenderedImage:
    full: bytes
    thumb: bytes
    width: int
    height: int


def _decode(data: bytes) -> Image.Image:
    try:
        probe = Image.open(io.BytesIO(data))
    except (DecompressionBombError, DecompressionBombWarning) as exc:
        # Pillow got there first. Its message names the pixel count, which is
        # not something to hand back to a caller.
        raise ImageTooLarge("That image is too many pixels to process.") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise NotAnImage("That file is not an image we can read.") from exc

    # `format` is populated by the decoder that claimed the file, not by
    # anything the caller said.
    if probe.format not in ALLOWED_FORMATS:
        raise NotAnImage(f"{probe.format or 'That format'} is not accepted. Use JPEG, PNG or WebP.")

    # Checked from the header, before any pixel is decoded. Doing this after
    # `load()` would be checking whether the bomb went off.
    width, height = probe.size
    if width * height > MAX_PIXELS:
        raise ImageTooLarge("That image is too many pixels to process.")

    try:
        probe.load()
    except (OSError, ValueError) as exc:
        # A truncated or malformed file that got past `open`.
        raise NotAnImage("That image file is damaged.") from exc

    return probe


def _flatten(image: Image.Image) -> Image.Image:
    """To RGB, honouring the EXIF orientation before it is discarded.

    A phone photo is usually stored in the sensor's orientation with a tag
    saying which way up it is. Stripping EXIF without applying that tag first
    would leave every portrait photo on its side — the fix for a privacy problem
    quietly creating a correctness one.
    """
    from PIL import ImageOps

    upright = ImageOps.exif_transpose(image) or image
    if upright.mode in ("RGBA", "LA", "P"):
        # Composite onto white rather than dropping the alpha channel, or a
        # transparent PNG turns into a black rectangle.
        background = Image.new("RGB", upright.size, (255, 255, 255))
        converted = upright.convert("RGBA")
        background.paste(converted, mask=converted.split()[-1])
        return background
    return upright.convert("RGB")


def _encode(image: Image.Image, max_edge: int, quality: int) -> bytes:
    copy = image.copy()
    copy.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    # No `exif=` and no `icc_profile=`: this is a fresh file carrying only
    # pixels, which is the entire point.
    copy.save(buffer, format=OUTPUT_FORMAT, quality=quality, method=4)
    return buffer.getvalue()


def render(data: bytes) -> RenderedImage:
    """Decode an upload and produce the two WebPs that get stored.

    Raises `NotAnImage` or `ImageTooLarge`; both become a 422 at the route.
    """
    with _decode(data) as decoded:
        flattened = _flatten(decoded)
        return RenderedImage(
            full=_encode(flattened, FULL_MAX_EDGE, FULL_QUALITY),
            thumb=_encode(flattened, THUMB_MAX_EDGE, THUMB_QUALITY),
            width=flattened.width,
            height=flattened.height,
        )
