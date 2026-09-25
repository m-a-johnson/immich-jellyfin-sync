import json
import os

import httpx
import pytest

from immich_jellyfin_sync.config import Paths
from immich_jellyfin_sync.immich import Album, Asset, ImmichClient, ImmichError
from immich_jellyfin_sync.state import State
from immich_jellyfin_sync.sync import Syncer, video_filename

WEDDING = dict(id="cf7586e2-3a66-4214-826e-2bef08451160",
               path="/data/library/mark/2018/07/Tanis &amp; Mark Wedding.mp4",
               name="Tanis & Mark Wedding.mp4", date="2018-07-28T19:51:01.114Z")


def asset(id, path, name, date="2020-01-02T03:04:05.000Z", visibility="timeline", trashed=False, type="VIDEO"):
    return Asset(id=id, type=type, original_path=path, original_file_name=name, local_date_time=date,
                 visibility=visibility, is_trashed=trashed, is_offline=False)


class FakeImmich:
    def __init__(self):
        self.albums_list = []
        self.videos = {}
        self.fail_albums = set()
        self.fail_list = False

    def albums(self):
        if self.fail_list:
            raise ImmichError("down")
        return list(self.albums_list)

    def album_videos(self, album_id):
        if album_id in self.fail_albums:
            raise ImmichError("boom")
        return list(self.videos.get(album_id, []))


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    paths = Paths(immich_prefix="/data", jellyfin_prefix="/immich", output=out)
    state = State(tmp_path / "state.db")
    fake = FakeImmich()
    fake.albums_list = [Album("A1", "Wedding", None, 1)]
    fake.videos["A1"] = [asset(WEDDING["id"], WEDDING["path"], WEDDING["name"], WEDDING["date"])]
    state.set_enabled("A1", "Wedding", True)

    def run(**kw):
        return Syncer(fake, state, paths, **kw).run()

    yield out, state, fake, run
    state.close()


WED_REL = "Wedding/2018-07-28 Tanis & Mark Wedding [cf7586e2].mp4"


def test_creates_symlink_with_verbatim_path_and_local_date(env):
    out, state, fake, run = env
    r = run()
    link = out / WED_REL
    assert r.created == 1
    assert os.path.islink(link)
    # &amp; kept exactly as on disk; prefix swapped /data -> /immich
    assert os.readlink(link) == "/immich/library/mark/2018/07/Tanis &amp; Mark Wedding.mp4"


def test_second_run_is_a_noop(env):
    out, state, fake, run = env
    run()
    r = run()
    assert (r.created, r.replaced, r.removed, r.unchanged) == (0, 0, 0, 1)


def test_video_removed_from_album_is_unlinked(env):
    out, state, fake, run = env
    run()
    fake.videos["A1"] = []
    r = run()
    assert r.removed == 1 and r.dirs_removed == 1
    assert not os.path.lexists(out / WED_REL)
    assert not (out / "Wedding").exists()


def test_disabling_album_removes_its_files(env):
    out, state, fake, run = env
    run()
    state.set_enabled("A1", "Wedding", False)
    run()
    assert not (out / "Wedding").exists()


def test_album_deleted_in_immich_removes_files(env):
    out, state, fake, run = env
    run()
    fake.albums_list = []
    run()
    assert not (out / "Wedding").exists()


def test_user_files_are_never_touched(env):
    out, state, fake, run = env
    run()
    mine = out / "Wedding" / "folder.jpg"
    mine.write_text("hand-picked")
    fake.videos["A1"] = []
    r = run()
    assert r.removed == 1
    assert mine.read_text() == "hand-picked"      # not deleted
    assert (out / "Wedding").is_dir()             # dir kept because it isn't empty


def test_existing_foreign_file_is_not_overwritten(env):
    out, state, fake, run = env
    (out / "Wedding").mkdir()
    (out / WED_REL).write_text("not ours")
    r = run()
    assert r.conflicts == 1 and r.created == 0
    assert (out / WED_REL).read_text() == "not ours"


def test_owned_symlink_replaced_by_real_file_is_left_alone(env):
    out, state, fake, run = env
    run()
    os.unlink(out / WED_REL)
    (out / WED_REL).write_text("user replaced it")
    fake.videos["A1"] = []
    r = run()
    assert r.conflicts == 1
    assert (out / WED_REL).read_text() == "user replaced it"


def test_album_read_failure_keeps_files(env):
    out, state, fake, run = env
    run()
    fake.fail_albums.add("A1")
    r = run()
    assert r.failed_albums == ["Wedding"] and r.removed == 0
    assert os.path.islink(out / WED_REL)


def test_album_list_failure_changes_nothing(env):
    out, state, fake, run = env
    run()
    fake.fail_list = True
    with pytest.raises(ImmichError):
        run()
    assert os.path.islink(out / WED_REL)


def test_hidden_trashed_photos_and_outside_prefix_are_skipped(env):
    out, state, fake, run = env
    fake.videos["A1"] = [
        asset("h1", "/data/x/hidden.mov", "hidden.mov", visibility="hidden"),
        asset("t1", "/data/x/trash.mov", "trash.mov", trashed=True),
        asset("p1", "/data/x/photo.jpg", "photo.jpg", type="IMAGE"),
        asset("o1", "/elsewhere/x.mov", "x.mov"),
        asset("a1", "/data/x/archived.mov", "archived.mov", visibility="archive"),
    ]
    r = run()
    assert r.created == 1 and r.skipped_assets == 4
    assert sorted(os.listdir(out / "Wedding")) == ["2020-01-02 archived [a1].mov", "2020-01-02 archived [a1].nfo"]


def test_dry_run_changes_nothing(env):
    out, state, fake, run = env
    r = run(dry_run=True)
    assert r.created == 1
    assert os.listdir(out) == []
    assert state.owned_files() == {}


def test_duplicate_album_names_get_distinct_folders(env):
    out, state, fake, run = env
    fake.albums_list.append(Album("B2", "Wedding", None, 1))
    fake.videos["B2"] = [asset("bbbbbbbb-1", "/data/y/b.mov", "b.mov")]
    state.set_enabled("B2", "Wedding", True)
    run()
    assert sorted(os.listdir(out)) == ["Wedding", "Wedding [B2]"]


def test_reserved_characters_are_cleaned():
    a = asset("12345678-x", "/data/a.mov", 'what: "is" this?.MOV')
    assert video_filename(a) == "2020-01-02 what_ _is_ this_ [12345678].mov"


# --- HTTP client ---------------------------------------------------------

def _client(handler):
    return ImmichClient("http://immich", "k", transport=httpx.MockTransport(handler))


def _item(i):
    return {"id": f"id{i}", "type": "VIDEO", "originalPath": f"/data/{i}.mov", "originalFileName": f"{i}.mov",
            "localDateTime": "2021-05-06T07:08:09.000Z", "visibility": "timeline", "isTrashed": False}


def test_client_paginates_search():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        assert req.headers["x-api-key"] == "k"
        assert body["albumIds"] == ["A"] and body["type"] == "VIDEO"
        nxt = "2" if body["page"] == 1 else None
        return httpx.Response(200, json={"assets": {"items": [_item(body["page"])], "nextPage": nxt, "nextCursor": None}})

    vids = _client(handler).album_videos("A")
    assert [v.id for v in vids] == ["id1", "id2"]
    assert [b["page"] for b in seen] == [1, 2]


def test_client_refuses_unsupported_cursor_paging():
    def handler(req):
        return httpx.Response(200, json={"assets": {"items": [], "nextPage": None, "nextCursor": "abc"}})
    with pytest.raises(ImmichError):
        _client(handler).album_videos("A")


def test_client_http_error_raises():
    with pytest.raises(ImmichError):
        _client(lambda req: httpx.Response(403, text="nope")).albums()
