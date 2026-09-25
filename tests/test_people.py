import xml.etree.ElementTree as ET

import pytest

from immich_jellyfin_sync.config import Paths
from immich_jellyfin_sync.immich import Album, Asset, ImmichError, _asset
from immich_jellyfin_sync.state import State, merge_people
from immich_jellyfin_sync.sync import Syncer

VID = "0dc71829-4482-4819-8957-9f9b838ad9aa"
NFO = "Vacations/2025-07-31 Fairmont 2025 [0dc71829].nfo"


def fairmont(people=("Tanis", "Evie", "Mark")):
    return Asset(VID, "VIDEO", "/data/library/mark/2025/07/Fairmont 2025.mp4", "Fairmont 2025.mp4",
                 "2025-07-31T10:00:00Z", "timeline", False, False, people=tuple(people))


class Fake:
    def __init__(self):
        self.video = fairmont()
        self.tag_list = ["BC"]
        self.tags_fail = False

    def albums(self):
        return [Album("A1", "Vacations", None, 2)]

    def album_videos(self, album_id):
        return [self.video]

    def album_photos(self, album_id):
        return []

    def tags(self, asset_id):
        if self.tags_fail:
            raise ImmichError("GET /api/assets: HTTP 500")
        return list(self.tag_list)

    def faces(self, asset_id):
        return []


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    state = State(tmp_path / "s.db")
    state.set_enabled("A1", "Vacations", True)
    fake = Fake()
    yield out, state, fake, (lambda: Syncer(fake, state, Paths("/data", "/immich", out)).run())
    state.close()


def nfo(out):
    root = ET.fromstring((out / NFO).read_bytes())
    return [a.findtext("name") for a in root.findall("actor")], [t.text for t in root.findall("tag")]


def test_search_people_parsed_named_and_visible_only():
    a = _asset({"id": "v", "type": "VIDEO", "originalPath": "/data/a.mov", "originalFileName": "a.mov",
                "people": [{"name": "Tanis"}, {"name": ""}, {"name": "Hidden", "isHidden": True}, {"name": " Evie "}]})
    assert a.people == ("Tanis", "Evie")


def test_people_and_tags_written_sorted(env):
    out, state, fake, run = env
    run()
    people, tags = nfo(out)
    assert people == ["Evie", "Mark", "Tanis"] and tags == ["BC"]
    root = ET.fromstring((out / NFO).read_bytes())
    assert root.find("actor").findtext("type") == "Actor"


def test_immich_order_change_does_not_rewrite(env):
    out, state, fake, run = env
    run()
    fake.video = fairmont(("Mark", "Tanis", "Evie"))
    assert run().nfos_written == 0


def test_added_and_hidden_people(env):
    out, state, fake, run = env
    run()
    state.add_person(VID, "Grandma")
    state.remove_person(VID, "Mark", from_immich=True)
    r = run()
    assert nfo(out)[0] == ["Evie", "Grandma", "Tanis"]
    assert r.metadata_changed == {"Vacations/2025-07-31 Fairmont 2025 [0dc71829].mp4"}
    state.restore_person(VID, "Mark")
    state.remove_person(VID, "Grandma", from_immich=False)
    run()
    assert nfo(out)[0] == ["Evie", "Mark", "Tanis"]


def test_hidden_name_stays_hidden_if_immich_keeps_finding_it(env):
    out, state, fake, run = env
    state.remove_person(VID, "Mark", from_immich=True)
    run()
    fake.video = fairmont(("Mark", "Tanis", "Evie", "Mark"))
    run()
    assert "Mark" not in nfo(out)[0]


def test_tag_read_failure_keeps_existing_nfo(env):
    out, state, fake, run = env
    run()
    before = (out / NFO).read_bytes()
    fake.tags_fail = True
    fake.video = fairmont(("Tanis",))             # even with other changes pending
    r = run()
    assert (out / NFO).read_bytes() == before and r.nfos_written == 0 and r.nfos_removed == 0
    fake.tags_fail = False
    run()
    assert nfo(out) == (["Tanis"], ["BC"])


def test_merge_people_sorting_and_rules():
    assert merge_people(["b", "A", "c"], {"d"}, {"c"}) == ["A", "b", "d"]


def test_tags_use_last_part_of_nested_tags():
    import httpx
    from immich_jellyfin_sync.immich import ImmichClient

    def handler(req):
        return httpx.Response(200, json={"tags": [{"name": "BC", "value": "Places/Canada/BC"},
                                                  {"value": "Events/Birthday"}, {"name": ""}]})
    c = ImmichClient("http://immich", "k", transport=httpx.MockTransport(handler))
    assert c.tags("v") == ["BC", "Birthday"]


def test_title_override_and_back(env):
    out, state, fake, run = env
    run()
    state.set_title(VID, "Fairmont Hot Springs")
    r = run()
    assert ET.fromstring((out / NFO).read_bytes()).findtext("title") == "Fairmont Hot Springs"
    assert r.metadata_changed == {"Vacations/2025-07-31 Fairmont 2025 [0dc71829].mp4"}
    state.set_title(VID, None)
    run()
    assert ET.fromstring((out / NFO).read_bytes()).findtext("title") == "Fairmont 2025"


def test_roles_written_once_per_person_across_videos(env):
    out, state, fake, run = env
    run()
    state.set_role("Tanis", "Mom")
    r = run()
    actors = {a.findtext("name"): a.findtext("role") for a in ET.fromstring((out / NFO).read_bytes()).findall("actor")}
    assert actors == {"Evie": None, "Mark": None, "Tanis": "Mom"}
    assert r.nfos_written == 1
    assert run().nfos_written == 0                                  # stable afterwards
