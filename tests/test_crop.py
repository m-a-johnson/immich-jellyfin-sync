import io

import pytest
from PIL import Image

from immich_jellyfin_sync.config import Paths
from immich_jellyfin_sync.images import Face, TARGET, crop_box, prepare
from immich_jellyfin_sync.immich import Album, Asset, ImmichError
from immich_jellyfin_sync.state import State
from immich_jellyfin_sync.sync import Syncer


def ratio(box):
    l, t, r, b = box
    return (r - l) / (b - t)


def contains(box, f, w, h):
    l, t, r, b = box
    return l <= f.x1 * w and r >= f.x2 * w and t <= f.y1 * h and b >= f.y2 * h


def test_already_16_9_is_left_alone():
    assert crop_box(1920, 1080, []) is None
    assert crop_box(1900, 1080, []) is None           # within 5%


def test_portrait_without_faces_biased_upward():
    box = crop_box(1080, 1440, [])
    assert abs(ratio(box) - TARGET) < 0.01
    l, t, r, b = box
    assert (l, r) == (0, 1080)                         # full width kept
    centre = (t + b) / 2
    assert centre < 1440 / 2                           # above the middle


def test_portrait_crop_contains_face_near_bottom():
    face = Face(0.4, 0.70, 0.55, 0.82)                 # someone sitting low in the frame
    box = crop_box(1080, 1440, [face])
    assert contains(box, face, 1080, 1440)
    assert box[1] > 1440 * 0.4                         # moved down to find them, not the default strip


def test_portrait_group_too_tall_keeps_tops_of_heads():
    faces = [Face(0.1, 0.05, 0.25, 0.20), Face(0.6, 0.75, 0.75, 0.90)]   # one near the top, one near the bottom
    l, t, r, b = crop_box(1080, 1440, faces)
    assert t <= 0.05 * 1440                            # top face's head is in
    assert abs((r - l) / (b - t) - TARGET) < 0.01


def test_panorama_follows_face_horizontally():
    face = Face(0.85, 0.3, 0.92, 0.5)
    box = crop_box(4000, 1000, [face])
    assert contains(box, face, 4000, 1000)
    assert abs(ratio(box) - TARGET) < 0.01


def test_invalid_face_boxes_ignored():
    assert crop_box(1080, 1440, [Face(0.5, 0.5, 0.4, 0.4)]) == crop_box(1080, 1440, [])


def _jpeg(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 120, 150)).save(buf, "JPEG")
    return buf.getvalue()


def test_prepare_outputs_16_9_jpeg_and_converts_webp():
    buf = io.BytesIO()
    Image.new("RGB", (900, 1200), (1, 2, 3)).save(buf, "WEBP")
    out = prepare(buf.getvalue(), "image/webp", [])
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "JPEG" and abs(im.width / im.height - TARGET) < 0.01


def test_prepare_without_crop_passes_jpeg_through():
    data = _jpeg(900, 1200)
    assert prepare(data, "image/jpeg", None, crop=False) == data


# ---------------------------------------------------------------- in the sync

VID, PHOTO = "cf7586e2-aaaa", "p1p1p1p1-robe"
FOLDER = "Wedding"
POSTER = f"{FOLDER}/2018-07-28 Wedding [cf7586e2].jpg"


class Fake:
    def __init__(self):
        self.faces_error = False
        self.face_calls = 0

    def albums(self):
        return [Album("A1", FOLDER, None, 2)]

    def album_videos(self, album_id):
        return [Asset(VID, "VIDEO", "/data/w.mp4", "Wedding.mp4", "2018-07-28T19:51:01Z", "timeline", False, False)]

    def album_photos(self, album_id):
        return [Asset(PHOTO, "IMAGE", "/data/r.jpg", "r.jpg", "2018-07-28T10:00:00Z", "timeline", False, False)]

    def faces(self, asset_id):
        self.face_calls += 1
        if self.faces_error:
            raise ImmichError("GET /api/faces: HTTP 403 missing permission face.read")
        return [Face(0.4, 0.2, 0.6, 0.35)]

    def thumbnail(self, asset_id, size):
        return _jpeg(1080, 1440), "image/jpeg"


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    state = State(tmp_path / "s.db")
    state.set_enabled("A1", FOLDER, True)
    state.set_poster("A1", VID, PHOTO)
    fake = Fake()
    yield out, state, fake, (lambda **kw: Syncer(fake, state, Paths("/data", "/immich", out), **kw).run())
    state.close()


def test_poster_is_cropped_to_16_9(env):
    out, state, fake, run = env
    run()
    with Image.open(out / POSTER) as im:
        assert abs(im.width / im.height - TARGET) < 0.01


def test_images_from_before_cropping_are_re_rendered_once(env):
    out, state, fake, run = env
    Syncer(fake, state, Paths("/data", "/immich", out), crop=False).run()      # like v0.4.0
    with Image.open(out / POSTER) as im:
        assert im.size == (1080, 1440)
    r = run()                                                                    # upgrade: crop on
    assert r.images_written == 1 and r.images_changed == {f"{FOLDER}/2018-07-28 Wedding [cf7586e2].mp4"}
    with Image.open(out / POSTER) as im:
        assert abs(im.width / im.height - TARGET) < 0.01
    assert run().images_written == 0                                             # then stable


def test_missing_face_permission_still_writes_cropped_poster(env):
    out, state, fake, run = env
    fake.faces_error = True
    r = run()
    assert r.images_written == 1 and fake.face_calls == 1                        # asked once, then stopped
    with Image.open(out / POSTER) as im:
        assert abs(im.width / im.height - TARGET) < 0.01


def _subject(w, h, cx, cy):
    from PIL import ImageDraw
    im = Image.new("RGB", (w, h), (200, 210, 220))
    d = ImageDraw.Draw(im)
    for i in range(0, 12):
        d.rectangle([cx * w - 120 + i * 20, cy * h - 150, cx * w - 110 + i * 20, cy * h + 150], fill=(40, 50, 60))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def test_without_faces_crop_follows_detail_not_the_middle():
    out = Image.open(io.BytesIO(prepare(_subject(1080, 1440, 0.5, 0.8), "image/jpeg", [])))
    # the busy subject sits 80% down; the crop must include it (dark pixels present)
    assert out.convert("L").getextrema()[0] < 80


def test_uniform_image_uses_default_focus():
    buf = io.BytesIO()
    Image.new("RGB", (1080, 1440), (128, 128, 128)).save(buf, "JPEG")
    out = Image.open(io.BytesIO(prepare(buf.getvalue(), "image/jpeg", [])))
    assert abs(out.width / out.height - TARGET) < 0.01
