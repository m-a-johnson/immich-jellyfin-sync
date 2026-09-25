import hashlib
import io
import json
import os

import httpx
import pytest
from PIL import Image

from immich_jellyfin_sync.config import Paths
from immich_jellyfin_sync.immich import Album, Asset, ImmichError
from immich_jellyfin_sync.jellyfin import JellyfinClient, notify
from immich_jellyfin_sync.state import State
from immich_jellyfin_sync.sync import Syncer

VID = "cf7586e2-3a66-4214-826e-2bef08451160"
COVER, P1, P2 = "c0c0c0c0-cover", "p1p1p1p1-robe", "p2p2p2p2-dress"
FOLDER = "Tanis and Mark Wedding"
LINK = f"{FOLDER}/2018-07-28 Tanis & Mark Wedding [cf7586e2].mp4"
POSTER = f"{FOLDER}/2018-07-28 Tanis & Mark Wedding [cf7586e2].jpg"
FOLDER_JPG = f"{FOLDER}/folder.jpg"
NFO = f"{FOLDER}/2018-07-28 Tanis & Mark Wedding [cf7586e2].nfo"


def asset(id, type, name, disk_name=None, description=""):
    # Immich's storage template can write '&amp;' into the file on disk while the display name keeps '&'
    return Asset(id=id, type=type, original_path=f"/data/library/mark/2018/07/{disk_name or name}", original_file_name=name,
                 local_date_time="2018-07-28T19:51:01.114Z", visibility="timeline", is_trashed=False, is_offline=False,
                 description=description)


def jpeg_of(tag):
    return b"\xff\xd8" + tag.encode() + b"\xff\xd9"


class Fake:
    def __init__(self):
        self.cover = COVER
        self.videos = [asset(VID, "VIDEO", "Tanis & Mark Wedding.mp4", disk_name="Tanis &amp; Mark Wedding.mp4")]
        self.photos = [asset(COVER, "IMAGE", "c.jpg"), asset(P1, "IMAGE", "robe.jpg"), asset(P2, "IMAGE", "dress.jpg")]
        self.fetched = []
        self.fail_thumbs = False
        self.webp = False

    def albums(self):
        return [Album("A1", FOLDER, self.cover, 4)]

    def album_videos(self, album_id):
        return list(self.videos)

    def album_photos(self, album_id):
        return list(self.photos)

    def tags(self, asset_id):
        return []

    def faces(self, asset_id):
        return []

    def thumbnail(self, asset_id, size):
        assert size == "preview"
        if self.fail_thumbs:
            raise ImmichError("down")
        self.fetched.append(asset_id)
        if self.webp:
            buf = io.BytesIO()
            Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, "WEBP")
            return buf.getvalue(), "image/webp"
        return jpeg_of(asset_id), "image/jpeg"


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    state = State(tmp_path / "state.db")
    state.set_enabled("A1", FOLDER, True)
    fake = Fake()
    paths = Paths("/data", "/immich", out)
    # byte-for-byte assertions below: run without cropping (JPEG passed through untouched)
    yield out, state, fake, (lambda **kw: Syncer(fake, state, paths, **{"crop": False, **kw}).run())
    state.close()


def test_folder_jpg_from_immich_cover_no_poster_by_default(env):
    out, state, fake, run = env
    r = run()
    assert (out / FOLDER_JPG).read_bytes() == jpeg_of(COVER)
    assert not (out / POSTER).exists()                 # no choice: Jellyfin's screen grab
    assert r.images_changed == {FOLDER}


def test_chosen_poster_is_written_and_replaced_when_changed(env):
    out, state, fake, run = env
    run()
    state.set_poster("A1", VID, P1)
    r = run()
    assert (out / POSTER).read_bytes() == jpeg_of(P1)
    assert r.images_changed == {LINK}                  # Jellyfin refreshes the video item
    state.set_poster("A1", VID, P2)
    r = run()
    assert (out / POSTER).read_bytes() == jpeg_of(P2)
    assert r.images_written == 1 and r.images_changed == {LINK}


def test_unchanged_images_are_not_refetched(env):
    out, state, fake, run = env
    state.set_poster("A1", VID, P1)
    run()
    fake.fetched.clear()
    r = run()
    assert fake.fetched == [] and r.images_written == 0 and not r.images_changed


def test_clearing_poster_removes_file_and_refreshes_video(env):
    out, state, fake, run = env
    state.set_poster("A1", VID, P1)
    run()
    state.set_poster("A1", VID, None)
    r = run()
    assert not (out / POSTER).exists()
    assert r.images_removed == 1 and r.images_changed == {LINK}


def test_poster_photo_removed_from_album_reverts_to_screen_grab(env):
    out, state, fake, run = env
    state.set_poster("A1", VID, P1)
    run()
    fake.photos = [p for p in fake.photos if p.id != P1]
    run()
    assert not (out / POSTER).exists()
    assert state.posters("A1") == {}                   # choice cleared, UI shows the default again


def test_folder_image_override_and_revert(env):
    out, state, fake, run = env
    state.set_cover("A1", P2)
    run()
    assert (out / FOLDER_JPG).read_bytes() == jpeg_of(P2)
    fake.photos = [p for p in fake.photos if p.id != P2]   # override leaves the album
    run()
    assert (out / FOLDER_JPG).read_bytes() == jpeg_of(COVER)
    assert state.cover("A1") is None


def test_immich_cover_change_is_followed(env):
    out, state, fake, run = env
    run()
    fake.cover = P1
    r = run()
    assert (out / FOLDER_JPG).read_bytes() == jpeg_of(P1) and r.images_changed == {FOLDER}


def test_user_replaced_image_is_never_overwritten_or_deleted(env):
    out, state, fake, run = env
    state.set_poster("A1", VID, P1)
    run()
    (out / POSTER).write_bytes(b"my own poster")
    state.set_poster("A1", VID, P2)
    r = run()
    assert (out / POSTER).read_bytes() == b"my own poster" and r.conflicts == 1
    state.set_enabled("A1", FOLDER, False)             # even disabling the album keeps it
    run()
    assert (out / POSTER).read_bytes() == b"my own poster"
    assert (out / FOLDER).is_dir()


def test_preexisting_folder_jpg_is_left_alone(env):
    out, state, fake, run = env
    (out / FOLDER).mkdir()
    (out / FOLDER_JPG).write_bytes(b"test image from earlier")
    r = run()
    assert (out / FOLDER_JPG).read_bytes() == b"test image from earlier" and r.conflicts == 1


def test_disabling_album_removes_images_and_folder(env):
    out, state, fake, run = env
    state.set_poster("A1", VID, P1)
    run()
    state.set_enabled("A1", FOLDER, False)
    r = run()
    assert r.images_removed == 2 and r.removed == 1
    assert not (out / FOLDER).exists()


def test_image_fetch_failure_keeps_existing_file(env):
    out, state, fake, run = env
    run()
    fake.cover = P1
    fake.fail_thumbs = True
    run()
    assert (out / FOLDER_JPG).read_bytes() == jpeg_of(COVER)


def test_webp_previews_are_converted_to_jpeg(env):
    out, state, fake, run = env
    fake.webp = True
    run()
    with Image.open(out / FOLDER_JPG) as im:
        assert im.format == "JPEG"


def test_no_videos_means_no_folder_or_image(env):
    out, state, fake, run = env
    fake.videos = []
    run()
    assert os.listdir(out) == []


def test_images_written_atomically_leave_no_temp_files(env):
    out, state, fake, run = env
    state.set_poster("A1", VID, P1)
    run()
    assert sorted(os.listdir(out / FOLDER)) == sorted([os.path.basename(LINK), os.path.basename(POSTER),
                                                       os.path.basename(NFO), "folder.jpg"])


# ------------------------------------------------------------- Jellyfin

def jf_client(handler):
    return JellyfinClient("http://jf", "KEY", "/immich-sync", transport=httpx.MockTransport(handler))


def test_jellyfin_uses_v12_authorization_header():
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, json={"ServerName": "Main", "Version": "12.1.0"})
    jf_client(handler).server_info()
    assert seen["authorization"].startswith('MediaBrowser Token="KEY"')
    assert "x-emby-token" not in seen


def test_notify_refreshes_known_items_and_announces_new_ones():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, dict(req.url.params), req.content))
        if req.url.path == "/Library/VirtualFolders":
            return httpx.Response(200, json=[{"Name": "Home Videos", "ItemId": "LIB", "Locations": ["/immich-sync"]}])
        if req.url.path == "/Items":
            return httpx.Response(200, json={"Items": [
                {"Id": "VID1", "Path": f"/immich-sync/{LINK}"},
                {"Id": "DIR1", "Path": f"/immich-sync/{FOLDER}"}]})
        return httpx.Response(204)

    new_link = f"{FOLDER}/2019-01-01 New [aaaaaaaa].mov"
    res = notify(jf_client(handler), created=[new_link], removed=[],
                 images_changed={LINK, FOLDER, new_link})
    refreshed = [c[1] for c in calls if c[1].endswith("/Refresh")]
    assert sorted(refreshed) == ["/Items/DIR1/Refresh", "/Items/VID1/Refresh"]
    assert all(c[2]["replaceAllImages"] == "true" for c in calls if c[1].endswith("/Refresh"))
    upd = json.loads(next(c[3] for c in calls if c[1] == "/Library/Media/Updated"))
    assert upd == {"Updates": [{"Path": f"/immich-sync/{new_link}", "UpdateType": "Created"}]}
    assert res.refreshed == 2


def test_notify_unknown_library_path_is_an_error():
    from immich_jellyfin_sync.jellyfin import JellyfinError

    def handler(req):
        if req.url.path == "/Library/VirtualFolders":
            return httpx.Response(200, json=[{"Name": "Movies", "ItemId": "M", "Locations": ["/movies"]}])
        return httpx.Response(204)
    with pytest.raises(JellyfinError):
        notify(jf_client(handler), [], [], {FOLDER})


# ------------------------------------------------------------------ NFO

import xml.etree.ElementTree as ET


def test_nfo_has_title_date_and_escapes_ampersand(env):
    out, state, fake, run = env
    r = run()
    raw = (out / NFO).read_bytes()
    assert b"Tanis &amp; Mark Wedding" in raw                  # valid XML
    root = ET.fromstring(raw)
    assert root.tag == "movie"
    assert root.findtext("title") == "Tanis & Mark Wedding"    # what Jellyfin displays
    assert root.findtext("premiered") == "2018-07-28" and root.findtext("year") == "2018"
    assert root.find("plot") is None                           # no description in Immich
    assert LINK in r.created_links                             # new video: notify() announces it rather than
                                                               # refreshing it; Jellyfin reads the NFO on first scan


def test_description_change_rewrites_nfo_and_refreshes_metadata(env):
    out, state, fake, run = env
    run()
    fake.videos = [asset(VID, "VIDEO", "Tanis & Mark Wedding.mp4", disk_name="Tanis &amp; Mark Wedding.mp4",
                         description="First dance & speeches")]
    r = run()
    assert ET.fromstring((out / NFO).read_bytes()).findtext("plot") == "First dance & speeches"
    assert r.nfos_written == 1 and r.metadata_changed == {LINK} and not r.images_changed


def test_unchanged_nfo_is_not_rewritten(env):
    out, state, fake, run = env
    run()
    r = run()
    assert r.nfos_written == 0 and not r.metadata_changed


def test_hand_edited_nfo_is_kept(env):
    out, state, fake, run = env
    run()
    (out / NFO).write_text("<movie><title>My own title</title></movie>")
    fake.videos = [asset(VID, "VIDEO", "Tanis & Mark Wedding.mp4", disk_name="Tanis &amp; Mark Wedding.mp4",
                         description="changed in Immich")]
    r = run()
    assert "My own title" in (out / NFO).read_text() and r.conflicts == 1


def test_nfo_removed_with_its_video(env):
    out, state, fake, run = env
    run()
    fake.videos = []
    r = run()
    assert not (out / NFO).exists() and r.nfos_removed == 1


def test_one_refresh_per_item_covering_both_changes():
    calls = []

    def handler(req):
        calls.append((req.url.path, dict(req.url.params)))
        if req.url.path == "/Library/VirtualFolders":
            return httpx.Response(200, json=[{"Name": "HV", "ItemId": "LIB", "Locations": ["/immich-sync"]}])
        if req.url.path == "/Items":
            return httpx.Response(200, json={"Items": [{"Id": "VID1", "Path": f"/immich-sync/{LINK}"},
                                                       {"Id": "DIR1", "Path": f"/immich-sync/{FOLDER}"}]})
        return httpx.Response(204)

    notify(jf_client(handler), [], [], images_changed={LINK, FOLDER}, metadata_changed={LINK})
    refreshes = {path: params for path, params in calls if path.endswith("/Refresh")}
    assert refreshes["/Items/VID1/Refresh"]["replaceAllMetadata"] == "true"
    assert refreshes["/Items/VID1/Refresh"]["replaceAllImages"] == "true"
    assert refreshes["/Items/DIR1/Refresh"]["replaceAllMetadata"] == "false"
    assert len(refreshes) == 2


def test_client_falls_back_when_immich_rejects_with_exif():
    from immich_jellyfin_sync.immich import ImmichClient
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if body.get("withExif"):
            return httpx.Response(400, json={"message": "property withExif should not exist"})
        return httpx.Response(200, json={"assets": {"items": [{
            "id": "v1", "type": "VIDEO", "originalPath": "/data/a.mov", "originalFileName": "a.mov",
            "localDateTime": "2020-01-01T00:00:00Z", "visibility": "timeline"}], "nextPage": None}})

    c = ImmichClient("http://immich", "k", transport=httpx.MockTransport(handler))
    assert [v.id for v in c.album_videos("A")] == ["v1"]
    assert [v.id for v in c.album_videos("A")] == ["v1"]
    assert [b.get("withExif") for b in bodies] == [True, None, None]   # asked once, then stopped


def test_description_read_from_exif_info():
    from immich_jellyfin_sync.immich import _asset
    a = _asset({"id": "v", "type": "VIDEO", "originalPath": "/data/a.mov", "originalFileName": "a.mov",
                "exifInfo": {"description": "  Lake day  "}})
    assert a.description == "Lake day"
