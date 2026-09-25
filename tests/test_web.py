import pytest
from fastapi.testclient import TestClient

from immich_jellyfin_sync.immich import Album, Asset, ImmichError
from immich_jellyfin_sync.state import State
from immich_jellyfin_sync.web import create_app

AID = "11111111-aaaa"
VID = "22222222-bbbb"
PID = "33333333-cccc"
H = {"X-Requested-With": "fetch"}


def mk(id, type, name="x.mov", visibility="timeline", people=()):
    return Asset(id=id, type=type, original_path=f"/data/{name}", original_file_name=name,
                 local_date_time="2018-07-28T19:51:01.114Z", visibility=visibility,
                 is_trashed=False, is_offline=False, duration_ms=369847, people=tuple(people))


class Fake:
    def __init__(self):
        self.down = False

    def albums(self):
        if self.down:
            raise ImmichError("down")
        return [Album(AID, "Wedding", PID, 3)]

    def album_videos(self, album_id):
        return [mk(VID, "VIDEO", "Tanis & Mark Wedding.mp4", people=("Tanis", "Mark")),
                mk("hidden-1", "VIDEO", visibility="hidden")]

    def tags(self, asset_id):
        if getattr(self, "tags_down", False):
            raise ImmichError("tags down")
        return ["Wedding"]

    def album_photos(self, album_id):
        return [mk(PID, "IMAGE", "cake.jpg")]

    def thumbnail(self, asset_id, size):
        return b"\xff\xd8jpeg", "image/jpeg"


class FakeService:
    running = False
    last = None
    triggered = 0

    def trigger(self):
        self.triggered += 1


@pytest.fixture
def ctx(tmp_path):
    fake, svc = Fake(), FakeService()
    db = tmp_path / "state.db"
    return TestClient(create_app(fake, db, svc)), fake, svc, db


def test_index_served(ctx):
    c, *_ = ctx
    r = c.get("/")
    assert r.status_code == 200 and "Home videos for Jellyfin" in r.text


def test_albums_list_and_toggle_triggers_sync(ctx):
    c, fake, svc, db = ctx
    assert c.get("/api/albums").json()[0] == {"id": AID, "name": "Wedding", "assetCount": 3,
                                              "coverId": PID, "enabled": False}
    r = c.put(f"/api/albums/{AID}", json={"enabled": True}, headers=H)
    assert r.status_code == 200 and svc.triggered == 1
    assert c.get("/api/albums").json()[0]["enabled"] is True
    assert AID in State(db).enabled_album_ids()


def test_changes_require_csrf_header(ctx):
    c, fake, svc, db = ctx
    assert c.put(f"/api/albums/{AID}", json={"enabled": True}).status_code == 403
    assert c.post("/api/sync").status_code == 403
    assert svc.triggered == 0


def test_unknown_album_is_404(ctx):
    c, *_ = ctx
    assert c.put("/api/albums/99999999-dead", json={"enabled": True}, headers=H).status_code == 404


def test_album_detail_hides_hidden_videos_and_shows_title(ctx):
    c, *_ = ctx
    a = c.get(f"/api/albums/{AID}").json()
    assert [v["id"] for v in a["videos"]] == [VID]
    assert a["videos"][0]["title"] == "Tanis & Mark Wedding"
    assert a["videos"][0]["date"] == "2018-07-28" and a["videos"][0]["durationMs"] == 369847


def test_poster_must_be_photo_from_same_album(ctx):
    c, *_ = ctx
    url = f"/api/albums/{AID}/videos/{VID}/poster"
    assert c.put(url, json={"photoId": "44444444-nope"}, headers=H).status_code == 400
    assert c.put(url, json={"photoId": PID}, headers=H).status_code == 200
    assert c.get(f"/api/albums/{AID}").json()["videos"][0]["posterId"] == PID
    assert c.put(url, json={"photoId": None}, headers=H).status_code == 200
    assert c.get(f"/api/albums/{AID}").json()["videos"][0]["posterId"] is None


def test_poster_for_video_not_in_album_rejected(ctx):
    c, *_ = ctx
    r = c.put(f"/api/albums/{AID}/videos/55555555-gone/poster", json={"photoId": PID}, headers=H)
    assert r.status_code == 400


def test_thumbnail_proxy_validates_input(ctx):
    c, *_ = ctx
    r = c.get(f"/api/thumb/{PID}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert c.get("/api/thumb/..%2Fetc").status_code in (400, 404)
    assert c.get(f"/api/thumb/{PID}?size=fullsize").status_code == 400


def test_immich_down_is_a_clear_502(ctx):
    c, fake, *_ = ctx
    fake.down = True
    r = c.get("/api/albums")
    assert r.status_code == 502 and "Immich" in r.json()["error"]


def test_video_counts_use_the_sync_rule_and_are_cached(ctx):
    c, fake, svc, db = ctx
    calls = []
    orig = fake.album_videos
    fake.album_videos = lambda album_id: calls.append(album_id) or orig(album_id)
    # the album has one visible video and one hidden one; only the visible one syncs
    assert c.get("/api/video-counts").json() == {AID: 1}
    assert c.get("/api/video-counts").json() == {AID: 1}
    assert calls == [AID]                     # second request served from cache
    svc.on_sync()                             # a sync clears the cache
    c.get("/api/video-counts")
    assert calls == [AID, AID]


def test_video_count_failure_is_null_and_not_cached(ctx):
    c, fake, svc, db = ctx
    fake.album_videos = lambda album_id: (_ for _ in ()).throw(ImmichError("boom"))
    assert c.get("/api/video-counts").json() == {AID: None}
    fake.album_videos = lambda album_id: [mk(VID, "VIDEO")]
    assert c.get("/api/video-counts").json() == {AID: 1}


def test_folder_image_choice_validated_and_triggers_sync(ctx):
    c, fake, svc, db = ctx
    url = f"/api/albums/{AID}/cover"
    assert c.put(url, json={"photoId": "44444444-nope"}, headers=H).status_code == 400
    r = c.put(url, json={"photoId": PID}, headers=H).json()
    assert r == {"albumId": AID, "folderImageId": PID, "folderImageChosen": True}
    assert svc.triggered == 1
    a = c.get(f"/api/albums/{AID}").json()
    assert a["folderImageChosen"] is True
    r = c.put(url, json={"photoId": None}, headers=H).json()
    assert r["folderImageChosen"] is False and r["folderImageId"] == PID   # falls back to Immich's cover


def test_poster_choice_triggers_sync(ctx):
    c, fake, svc, db = ctx
    c.put(f"/api/albums/{AID}/videos/{VID}/poster", json={"photoId": PID}, headers=H)
    assert svc.triggered == 1


def test_health_is_ok_and_reports_last_sync(ctx):
    c, fake, svc, db = ctx
    svc.last = {"finished": "2026-09-25T20:00:00+00:00", "ok": False, "error": "Couldn't reach Immich: down"}
    r = c.get("/health")
    assert r.status_code == 200                       # another server being down isn't a reason to restart us
    assert r.json()["last_error"] == "Couldn't reach Immich: down"


def test_render_returns_the_16_9_crop(ctx):
    import io
    from PIL import Image
    c, fake, svc, db = ctx
    buf = io.BytesIO()
    Image.new("RGB", (1080, 1440), (10, 20, 30)).save(buf, "JPEG")
    fake.thumbnail = lambda asset_id, size: (buf.getvalue(), "image/jpeg")
    fake.faces = lambda asset_id: []
    r = c.get(f"/api/render/{PID}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(r.content)) as im:
        assert abs(im.width / im.height - 16 / 9) < 0.01


def test_render_rejects_bad_ids(ctx):
    c, *_ = ctx
    assert c.get("/api/render/..%2F..%2Fetc").status_code in (400, 404)



def test_album_shows_people_and_tags(ctx):
    c, *_ = ctx
    v = c.get(f"/api/albums/{AID}").json()["videos"][0]
    assert v["people"] == [{"name": "Mark", "source": "immich"}, {"name": "Tanis", "source": "immich"}]
    assert v["hiddenPeople"] == [] and v["tags"] == ["Wedding"]


def test_add_remove_restore_people(ctx):
    c, fake, svc, db = ctx
    url = f"/api/albums/{AID}/videos/{VID}/people"
    r = c.post(url, json={"action": "add", "name": "  Grandma   Jo "}, headers=H).json()
    assert {"name": "Grandma Jo", "source": "added"} in r["people"]          # whitespace tidied
    r = c.post(url, json={"action": "remove", "name": "Mark"}, headers=H).json()
    assert [p["name"] for p in r["people"]] == ["Grandma Jo", "Tanis"] and r["hiddenPeople"] == ["Mark"]
    r = c.post(url, json={"action": "restore", "name": "Mark"}, headers=H).json()
    assert r["hiddenPeople"] == [] and "Mark" in [p["name"] for p in r["people"]]
    r = c.post(url, json={"action": "remove", "name": "Grandma Jo"}, headers=H).json()
    assert r["hiddenPeople"] == []                                         # your own name: just gone
    assert svc.triggered == 4


def test_people_validation(ctx):
    c, *_ = ctx
    url = f"/api/albums/{AID}/videos/{VID}/people"
    assert c.post(url, json={"action": "add", "name": "   "}, headers=H).status_code == 400
    assert c.post(url, json={"action": "add", "name": "x" * 81}, headers=H).status_code == 400
    assert c.post(url, json={"action": "rename", "name": "A"}, headers=H).status_code == 400
    assert c.post(url, json={"action": "add", "name": "A"}).status_code == 403              # CSRF header
    assert c.post(f"/api/albums/{AID}/videos/99999999-gone/people",
                  json={"action": "add", "name": "A"}, headers=H).status_code == 400


def test_tags_unavailable_is_null_not_an_error(ctx):
    c, fake, *_ = ctx
    fake.tags_down = True
    assert c.get(f"/api/albums/{AID}").json()["videos"][0]["tags"] is None


def test_title_endpoint(ctx):
    c, fake, svc, db = ctx
    url = f"/api/albums/{AID}/videos/{VID}/title"
    r = c.put(url, json={"title": "  Our   Wedding "}, headers=H).json()
    assert r == {"videoId": VID, "title": "Our Wedding", "titleChosen": True}
    v = c.get(f"/api/albums/{AID}").json()["videos"][0]
    assert v["title"] == "Our Wedding" and v["immichTitle"] == "Tanis & Mark Wedding"
    r = c.put(url, json={"title": "Tanis & Mark Wedding"}, headers=H).json()    # same as Immich: no override kept
    assert r["titleChosen"] is False
    assert c.put(url, json={"title": ""}, headers=H).status_code == 400


def test_people_page_and_roles(ctx):
    c, fake, svc, db = ctx
    c.put(f"/api/albums/{AID}", json={"enabled": True}, headers=H)
    assert c.get("/api/people").json() == [{"name": "Mark", "role": None, "videos": 1},
                                           {"name": "Tanis", "role": None, "videos": 1}]
    assert c.put("/api/people/role", json={"name": "Tanis", "role": " Mom "}, headers=H).json() == {"name": "Tanis", "role": "Mom"}
    assert c.get("/api/people").json()[1]["role"] == "Mom"
    c.put("/api/people/role", json={"name": "Tanis", "role": ""}, headers=H)           # empty clears it
    assert c.get("/api/people").json()[1]["role"] is None
    assert c.put("/api/people/role", json={"name": "Tanis", "role": "x" * 61}, headers=H).status_code == 400
