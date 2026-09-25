"""Prepare Immich previews as Jellyfin images: JPEG, cropped to 16:9 around faces.

Jellyfin shows Home Videos folders and videos as landscape cards, so a portrait photo
would otherwise be centre-cropped (often through someone's head).
"""
from __future__ import annotations

import io
from dataclasses import dataclass

TARGET = 16 / 9
TOLERANCE = 0.05          # already within 5% of 16:9: leave it alone
HEADROOM = 0.45           # face heights of space kept above the faces
BELOW = 0.9               # face heights kept below (chin/shoulders)
NO_FACE_FOCUS = 0.4       # last resort (no faces, no usable detail): centre the strip 40% down
VERSION = "crop16x9-v2"   # stored with each image; bump to re-render existing images


@dataclass(frozen=True)
class Face:
    """Bounding box as fractions (0..1) of the image it was detected on."""
    x1: float
    y1: float
    x2: float
    y2: float


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


def _place(span: float, size: float, lo: float | None, hi: float | None,
           pad_before: float, pad_after: float, default_focus: float) -> float:
    """Start offset of a window of length `span` along an axis of length `size`."""
    if lo is None:
        start = default_focus * size - span / 2
    else:
        a, b = lo - pad_before, hi + pad_after
        start = (a + b) / 2 - span / 2 if b - a <= span else a   # too big: keep the leading edge (tops of heads)
    return _clamp(start, 0, size - span)


def detail_focus(im, vertical: bool, window: float) -> float | None:
    """Without faces, centre the crop on the part of the image with the most edge detail
    (subjects are busier than their background). Returns the window centre as a fraction
    along the cropped axis, or None if the image is too uniform to tell."""
    from PIL import ImageFilter
    small = im.convert("L")
    small.thumbnail((256, 256))
    edges = small.filter(ImageFilter.FIND_EDGES)
    w, h = edges.size
    px = edges.load()
    n = h if vertical else w
    profile = [sum(px[x, i] if vertical else px[i, x] for x in range(w if vertical else h)) for i in range(n)]
    span = max(1, round(window * n))
    if n <= span or sum(profile) == 0:
        return None
    run = sum(profile[:span])
    best, best_at = run, 0
    for i in range(span, n):
        run += profile[i] - profile[i - span]
        if run > best:
            best, best_at = run, i - span + 1
    mean = sum(profile) / n * span
    if best < mean * 1.05:                     # nothing stands out: fall back to the default
        return None
    return (best_at + span / 2) / n


def crop_box(w: int, h: int, faces: list[Face], focus: float | None = None) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the largest 16:9 window, placed around the faces; None if no crop needed."""
    if w <= 0 or h <= 0 or abs((w / h) / TARGET - 1) < TOLERANCE:
        return None
    px = [Face(f.x1 * w, f.y1 * h, f.x2 * w, f.y2 * h) for f in faces
          if 0 <= f.x1 < f.x2 <= 1.001 and 0 <= f.y1 < f.y2 <= 1.001]
    if w / h < TARGET:                                   # too tall: keep full width, choose a strip
        ch = w / TARGET
        if px:
            fh = sum(f.y2 - f.y1 for f in px) / len(px)
            top = _place(ch, h, min(f.y1 for f in px), max(f.y2 for f in px), HEADROOM * fh, BELOW * fh, NO_FACE_FOCUS)
        else:
            top = _place(ch, h, None, None, 0, 0, NO_FACE_FOCUS if focus is None else focus)
        return 0, round(top), w, round(top + ch)
    cw = h * TARGET                                      # too wide: keep full height, choose a window
    if px:
        fw = sum(f.x2 - f.x1 for f in px) / len(px)
        left = _place(cw, w, min(f.x1 for f in px), max(f.x2 for f in px), 0.6 * fw, 0.6 * fw, 0.5)
    else:
        left = _place(cw, w, None, None, 0, 0, 0.5 if focus is None else focus)
    return round(left), 0, round(left + cw), h


def prepare(data: bytes, content_type: str, faces: list[Face] | None, crop: bool = True) -> bytes:
    """JPEG bytes for Jellyfin. Without cropping, JPEG input is passed through untouched."""
    is_jpeg = content_type.split(";")[0].strip().lower() in ("image/jpeg", "image/jpg")
    if not crop and is_jpeg:
        return data
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if crop:
            focus = None
            if not faces and abs((im.width / im.height) / TARGET - 1) >= TOLERANCE:
                tall = im.width / im.height < TARGET
                window = (im.width / TARGET) / im.height if tall else (im.height * TARGET) / im.width
                focus = detail_focus(im, vertical=tall, window=window)
            box = crop_box(im.width, im.height, faces or [], focus)
            if box:
                im = im.crop(box)
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=90)
        return out.getvalue()
