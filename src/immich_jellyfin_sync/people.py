"""Make the Jellyfin people in our NFOs ours: no online match, locked, face from Immich.

Why: Jellyfin identifies people only by name, server-wide, and looks them up online.
A first name like "Loris" matched a stranger on TheMovieDb (biography, birthday, photo).
Clearing the external ids and locking the person stops that (confirmed on 12.1: a locked
person stays clean through a full "replace all" refresh of the video).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from .immich import ImmichError
from .jellyfin import JellyfinError

log = logging.getLogger(__name__)

ONLINE_FIELDS = ("Overview", "PremiereDate", "EndDate", "ProductionYear")


@dataclass
class ClaimResult:
    claimed: int = 0            # cleaned and/or locked this pass
    faces: int = 0              # faces uploaded
    removed_images: int = 0     # online-match photos removed (no Immich face to replace them)
    kept_user_images: int = 0   # someone set their own image in Jellyfin: left alone
    not_in_jellyfin: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _primary_tag(item: dict) -> str | None:
    return (item.get("ImageTags") or {}).get("Primary")


def _has_online_data(item: dict) -> bool:
    return (any(v for v in (item.get("ProviderIds") or {}).values())
            or any(item.get(f) for f in ONLINE_FIELDS) or bool(item.get("ProductionLocations")))


def claim_people(jf, immich, state, people: dict[str, str | None]) -> ClaimResult:
    """people: name -> Immich person id (None = look it up by exact name in Immich)."""
    res = ClaimResult()
    for name in sorted(people, key=str.casefold):
        try:
            _claim_one(jf, immich, state, name, people[name], res)
        except (JellyfinError, ImmichError) as e:
            log.warning("person %r: %s", name, e)
            res.errors.append(name)
    return res


def _claim_one(jf, immich, state, name: str, immich_id: str | None, res: ClaimResult) -> None:
    item = jf.person(name)
    if item is None:
        res.not_in_jellyfin.append(name)
        return
    rec = state.claimed(name)
    had_online = _has_online_data(item)

    if had_online or not item.get("LockData"):
        item["ProviderIds"] = {}
        for f in ONLINE_FIELDS:
            item[f] = None
        item["ProductionLocations"] = []
        item["LockData"] = True
        jf.update_item(item)
        res.claimed += 1
        log.info("person %r: %s", name, "cleared online details and locked" if had_online else "locked")

    if immich_id is None:
        immich_id = immich.find_person(name)
    face = immich.person_face(immich_id) if immich_id else None
    face_sha = hashlib.sha256(face).hexdigest() if face else None

    current = _primary_tag(jf.person(name) or item)
    ours = rec is not None and rec.get("image_tag") and current == rec["image_tag"]

    if current and not ours and not had_online:
        # an image we didn't upload and that didn't come from an online match: yours
        res.kept_user_images += 1
        state.set_claimed(name, item["Id"], rec.get("face_sha") if rec else None, rec.get("image_tag") if rec else None)
        return

    if face is not None:
        if ours and rec.get("face_sha") == face_sha:
            state.set_claimed(name, item["Id"], face_sha, current)
            return                                   # already showing this face
        jf.upload_primary(item["Id"], face)
        res.faces += 1
        log.info("person %r: face from Immich", name)
        state.set_claimed(name, item["Id"], face_sha, _primary_tag(jf.person(name) or {}))
        return

    if current and (had_online or ours):
        jf.delete_primary(item["Id"])                # stranger's photo, or our old face with nothing to replace it
        res.removed_images += 1
        log.info("person %r: removed image (no face in Immich)", name)
    state.set_claimed(name, item["Id"], None, None)
