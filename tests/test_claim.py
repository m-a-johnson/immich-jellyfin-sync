import copy
import itertools

import pytest

from immich_jellyfin_sync.jellyfin import JellyfinError
from immich_jellyfin_sync.people import claim_people
from immich_jellyfin_sync.state import State

_tags = itertools.count(1)


class FakeJellyfin:
    def __init__(self):
        self.people = {}
        self.uploads, self.deletes, self.updates = [], [], []

    def add(self, name, **fields):
        self.people[name] = {"Id": f"jf-{name}", "Name": name, "ProviderIds": {}, "ImageTags": {}, "LockData": False, **fields}

    def person(self, name):
        p = self.people.get(name)
        return copy.deepcopy(p) if p else None

    def update_item(self, item):
        self.updates.append(item["Name"])
        self.people[item["Name"]] = copy.deepcopy(item)

    def _by_id(self, item_id):
        return next(p for p in self.people.values() if p["Id"] == item_id)

    def upload_primary(self, item_id, jpeg):
        self.uploads.append((item_id, jpeg))
        self._by_id(item_id)["ImageTags"] = {"Primary": f"tag{next(_tags)}"}

    def delete_primary(self, item_id):
        self.deletes.append(item_id)
        self._by_id(item_id)["ImageTags"] = {}


class FakeImmich:
    def __init__(self):
        self.faces = {"imm-loris": b"loris-face", "imm-tanis": b"tanis-face"}
        self.by_name = {"Loris": "imm-loris", "Tanis": "imm-tanis"}

    def find_person(self, name):
        return self.by_name.get(name)

    def person_face(self, pid):
        return self.faces[pid]


@pytest.fixture
def env(tmp_path):
    state = State(tmp_path / "s.db")
    yield FakeJellyfin(), FakeImmich(), state
    state.close()


def test_online_match_is_cleaned_locked_and_given_immich_face(env):
    jf, im, st = env
    jf.add("Loris", ProviderIds={"Imdb": "nm10331257", "Tmdb": "2822593"}, Overview="drag performer",
           PremiereDate="1995-03-27T00:00:00Z", ProductionLocations=["Winterthur"], ImageTags={"Primary": "stranger"})
    r = claim_people(jf, im, st, {"Loris": "imm-loris"})
    p = jf.people["Loris"]
    assert p["LockData"] is True and p["ProviderIds"] == {} and p["Overview"] is None
    assert p["PremiereDate"] is None and p["ProductionLocations"] == []
    assert jf.uploads == [("jf-Loris", b"loris-face")] and r.faces == 1


def test_second_pass_changes_nothing(env):
    jf, im, st = env
    jf.add("Tanis")
    claim_people(jf, im, st, {"Tanis": "imm-tanis"})
    jf.uploads.clear(); jf.updates.clear()
    r = claim_people(jf, im, st, {"Tanis": "imm-tanis"})
    assert jf.uploads == [] and jf.updates == [] and (r.claimed, r.faces) == (0, 0)


def test_new_face_in_immich_is_uploaded_again(env):
    jf, im, st = env
    jf.add("Tanis")
    claim_people(jf, im, st, {"Tanis": "imm-tanis"})
    im.faces["imm-tanis"] = b"tanis-new-face"
    claim_people(jf, im, st, {"Tanis": "imm-tanis"})
    assert jf.uploads[-1] == ("jf-Tanis", b"tanis-new-face")


def test_image_you_set_in_jellyfin_is_never_replaced(env):
    jf, im, st = env
    jf.add("Tanis")
    claim_people(jf, im, st, {"Tanis": "imm-tanis"})
    jf.people["Tanis"]["ImageTags"] = {"Primary": "set-by-you"}          # you uploaded another photo
    im.faces["imm-tanis"] = b"tanis-new-face"
    r = claim_people(jf, im, st, {"Tanis": "imm-tanis"})
    assert jf.people["Tanis"]["ImageTags"]["Primary"] == "set-by-you" and r.kept_user_images == 1
    assert len(jf.uploads) == 1


def test_existing_own_image_before_claim_is_kept(env):
    jf, im, st = env
    jf.add("Tanis", ImageTags={"Primary": "yours"})                      # no online ids: not a stranger's
    claim_people(jf, im, st, {"Tanis": "imm-tanis"})
    assert jf.uploads == [] and jf.people["Tanis"]["LockData"] is True


def test_added_name_uses_immich_person_with_that_name(env):
    jf, im, st = env
    jf.add("Loris")
    claim_people(jf, im, st, {"Loris": None})
    assert jf.uploads == [("jf-Loris", b"loris-face")]


def test_stranger_photo_removed_when_no_immich_face(env):
    jf, im, st = env
    jf.add("Grandma Jo", ProviderIds={"Tmdb": "1"}, ImageTags={"Primary": "stranger"})
    r = claim_people(jf, im, st, {"Grandma Jo": None})
    assert jf.deletes == ["jf-Grandma Jo"] and r.removed_images == 1
    assert jf.people["Grandma Jo"]["LockData"] is True


def test_person_not_in_jellyfin_yet_is_retried_later(env):
    jf, im, st = env
    r = claim_people(jf, im, st, {"Evie": None})
    assert r.not_in_jellyfin == ["Evie"] and st.claimed("Evie") is None


def test_one_failure_does_not_stop_the_rest(env):
    jf, im, st = env
    jf.add("Loris"); jf.add("Tanis")
    orig = jf.upload_primary

    def flaky(item_id, jpeg):
        if item_id == "jf-Loris":
            raise JellyfinError("HTTP 500")
        orig(item_id, jpeg)
    jf.upload_primary = flaky
    r = claim_people(jf, im, st, {"Loris": "imm-loris", "Tanis": "imm-tanis"})
    assert r.errors == ["Loris"] and ("jf-Tanis", b"tanis-face") in jf.uploads
