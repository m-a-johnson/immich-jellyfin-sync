"""Web UI + JSON API. No auth of its own: run it behind Traefik + Authentik."""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from . import images
from .immich import ImmichError
from .state import State, merge_people

log = logging.getLogger(__name__)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _check_id(value: str) -> str:
    if not _UUID.match(value):
        raise HTTPException(400, "invalid id")
    return value


def _same_origin(x_requested_with: str | None = Header(default=None)) -> None:
    # Cross-site forms can't set custom headers without a CORS preflight (which we never allow),
    # so requiring one on every change blocks CSRF through the Authentik session cookie.
    if x_requested_with != "fetch":
        raise HTTPException(403, "missing X-Requested-With header")


class VideoCounts:
    """Per-album count of videos that would sync (same rule as the sync: Asset.syncable).

    Fetching each album's video list is one Immich search per album, so results are
    cached for `ttl` seconds and dropped whenever a sync runs.
    """

    def __init__(self, client, ttl: float = 600, workers: int = 4):
        self.client = client
        self.ttl = ttl
        self.workers = workers
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, int]] = {}

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def _count(self, album_id: str) -> int | None:
        try:
            return sum(1 for v in self.client.album_videos(album_id) if v.syncable)
        except ImmichError as e:
            log.warning("couldn't count videos in album %s: %s", album_id, e)
            return None

    def get(self, album_ids: list[str]) -> dict[str, int | None]:
        now = time.monotonic()
        with self._lock:
            fresh = {i: c for i, (t, c) in self._cache.items() if now - t < self.ttl and i in album_ids}
        missing = [i for i in album_ids if i not in fresh]
        if missing:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                counted = dict(zip(missing, pool.map(self._count, missing)))
            with self._lock:
                for i, c in counted.items():
                    if c is not None:            # don't cache failures
                        self._cache[i] = (now, c)
            fresh.update(counted)
        return fresh


class EnabledBody(BaseModel):
    enabled: bool


class PosterBody(BaseModel):
    photoId: str | None = None


class CoverBody(BaseModel):
    photoId: str | None = None


class TitleBody(BaseModel):
    title: str | None = None


class RoleBody(BaseModel):
    name: str
    role: str | None = None


class PersonBody(BaseModel):
    action: str        # add | remove | restore
    name: str


def _clean_person(name: str) -> str:
    name = " ".join(_CONTROL.sub(" ", name).split())
    if not 1 <= len(name) <= 80:
        raise HTTPException(400, "Names must be 1 to 80 characters")
    return name


def _clean_text(value: str, what: str, max_len: int) -> str:
    value = " ".join(_CONTROL.sub(" ", value).split())
    if not 1 <= len(value) <= max_len:
        raise HTTPException(400, f"{what} must be 1 to {max_len} characters")
    return value


def people_view(immich_names, added: set[str], hidden: set[str]) -> dict:
    immich = set(immich_names)
    return {
        "people": [{"name": n, "source": "immich" if n in immich else "added"}
                   for n in merge_people(immich_names, added, hidden)],
        "hiddenPeople": sorted((hidden & immich), key=str.casefold),
    }


def create_app(client, state_path, service, crop: bool = True) -> FastAPI:
    app = FastAPI(title="immich-jellyfin-sync", docs_url=None, redoc_url=None, openapi_url=None)
    counts = VideoCounts(client)
    service.on_sync = counts.clear

    def db():
        s = State(state_path)
        try:
            yield s
        finally:
            s.close()

    def immich(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ImmichError as e:
            raise HTTPException(502, f"Couldn't read from Immich: {e}") from e

    def find_album(album_id: str):
        for a in immich(client.albums):
            if a.id == album_id:
                return a
        raise HTTPException(404, "Album not found in Immich")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return files("immich_jellyfin_sync").joinpath("static/index.html").read_text()

    @app.get("/health")
    def health():
        """For Docker's HEALTHCHECK: 200 while the web server and sync thread are up.
        Immich/Jellyfin trouble is reported in the body, not as a failure: restarting
        this container wouldn't fix another server being down."""
        last = service.last or {}
        return {"status": "ok", "syncing": service.running, "last_sync": last.get("finished"),
                "last_sync_ok": last.get("ok"), "last_error": last.get("error") or last.get("jellyfin_error")}

    @app.get("/api/status")
    def status():
        return {"running": service.running, "last": service.last,
                "jellyfin": getattr(service, "jellyfin", None) is not None}

    @app.post("/api/sync", dependencies=[Depends(_same_origin)])
    def sync_now():
        service.trigger()
        return {"queued": True}

    @app.get("/api/albums")
    def albums(state: State = Depends(db)):
        enabled = state.enabled_album_ids()
        rows = [{"id": a.id, "name": a.name, "assetCount": a.asset_count,
                 "coverId": a.cover_asset_id, "enabled": a.id in enabled} for a in immich(client.albums)]
        rows.sort(key=lambda r: (not r["enabled"], r["name"].casefold()))
        return rows

    @app.get("/api/video-counts")
    def video_counts():
        """{album id: number of videos that sync, or null if Immich couldn't be read}"""
        return counts.get([a.id for a in immich(client.albums)])

    @app.put("/api/albums/{album_id}", dependencies=[Depends(_same_origin)])
    def set_enabled(album_id: str, body: EnabledBody, state: State = Depends(db)):
        a = find_album(_check_id(album_id))
        state.set_enabled(a.id, a.name, body.enabled)
        service.trigger()
        return {"id": a.id, "enabled": body.enabled}

    @app.get("/api/albums/{album_id}")
    def album(album_id: str, state: State = Depends(db)):
        a = find_album(_check_id(album_id))
        videos = [v for v in immich(client.album_videos, a.id) if v.syncable]
        videos.sort(key=lambda v: v.local_date_time)
        posters = state.posters(a.id)
        override = state.cover(a.id)

        def tags_of(video_id):
            try:
                return client.tags(video_id)
            except ImmichError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            tags = dict(zip([v.id for v in videos], pool.map(tags_of, [v.id for v in videos])))
        people = {v.id: people_view(v.people, *state.people_changes(v.id)) for v in videos}
        known = sorted({p["name"] for pv in people.values() for p in pv["people"]}
                       | {n for pv in people.values() for n in pv["hiddenPeople"]}, key=str.casefold)
        return {
            "id": a.id, "name": a.name, "coverId": a.cover_asset_id,
            "folderImageId": override or a.cover_asset_id, "folderImageChosen": override is not None,
            "enabled": a.id in state.enabled_album_ids(),
            "knownPeople": known,
            "videos": [{"id": v.id, "title": state.title(v.id) or v.title, "immichTitle": v.title,
                        "titleChosen": state.title(v.id) is not None, "date": v.local_date_time[:10],
                        "durationMs": v.duration_ms, "posterId": posters.get(v.id),
                        "tags": tags[v.id], **people[v.id]} for v in videos],
        }

    @app.put("/api/albums/{album_id}/videos/{video_id}/title", dependencies=[Depends(_same_origin)])
    def set_title(album_id: str, video_id: str, body: TitleBody, state: State = Depends(db)):
        _check_id(album_id), _check_id(video_id)
        video = next((v for v in immich(client.album_videos, album_id) if v.id == video_id and v.syncable), None)
        if video is None:
            raise HTTPException(400, "That video isn't in this album")
        title = None if body.title is None else _clean_text(body.title, "Titles", 200)
        state.set_title(video_id, None if title == video.title else title)
        service.trigger()
        chosen = state.title(video_id)
        return {"videoId": video_id, "title": chosen or video.title, "titleChosen": chosen is not None}

    @app.get("/api/people")
    def people(state: State = Depends(db)):
        """Everyone who appears in a video that syncs, with their role and video count."""
        enabled = state.enabled_album_ids()
        counts: dict[str, int] = {}
        seen: set[str] = set()
        for a in immich(client.albums):
            if a.id not in enabled:
                continue
            for v in immich(client.album_videos, a.id):
                if not v.syncable or v.id in seen:
                    continue
                seen.add(v.id)
                for n in merge_people(v.people, *state.people_changes(v.id)):
                    counts[n] = counts.get(n, 0) + 1
        roles = state.roles()
        return [{"name": n, "role": roles.get(n), "videos": c}
                for n, c in sorted(counts.items(), key=lambda kv: kv[0].casefold())]

    @app.put("/api/people/role", dependencies=[Depends(_same_origin)])
    def set_role(body: RoleBody, state: State = Depends(db)):
        name = _clean_text(body.name, "Names", 80)
        role = None if body.role is None or not body.role.strip() else _clean_text(body.role, "Roles", 60)
        state.set_role(name, role)
        service.trigger()
        return {"name": name, "role": role}

    @app.post("/api/albums/{album_id}/videos/{video_id}/people", dependencies=[Depends(_same_origin)])
    def change_person(album_id: str, video_id: str, body: PersonBody, state: State = Depends(db)):
        _check_id(album_id), _check_id(video_id)
        video = next((v for v in immich(client.album_videos, album_id) if v.id == video_id and v.syncable), None)
        if video is None:
            raise HTTPException(400, "That video isn't in this album")
        name = _clean_person(body.name)
        if body.action == "add":
            state.add_person(video_id, name)
        elif body.action == "remove":
            state.remove_person(video_id, name, from_immich=name in video.people)
        elif body.action == "restore":
            state.restore_person(video_id, name)
        else:
            raise HTTPException(400, "action must be add, remove or restore")
        service.trigger()
        return {"videoId": video_id, **people_view(video.people, *state.people_changes(video_id))}

    @app.get("/api/albums/{album_id}/photos")
    def photos(album_id: str):
        _check_id(album_id)
        items = [p for p in immich(client.album_photos, album_id) if p.visible]
        items.sort(key=lambda p: p.local_date_time)
        return [{"id": p.id, "date": p.local_date_time[:10]} for p in items]

    @app.put("/api/albums/{album_id}/videos/{video_id}/poster", dependencies=[Depends(_same_origin)])
    def set_poster(album_id: str, video_id: str, body: PosterBody, state: State = Depends(db)):
        _check_id(album_id), _check_id(video_id)
        if not any(v.id == video_id and v.syncable for v in immich(client.album_videos, album_id)):
            raise HTTPException(400, "That video isn't in this album")
        if body.photoId is not None:
            _check_id(body.photoId)
            if not any(p.id == body.photoId and p.visible for p in immich(client.album_photos, album_id)):
                raise HTTPException(400, "Posters must be photos from the same album")
        state.set_poster(album_id, video_id, body.photoId)
        service.trigger()
        return {"videoId": video_id, "posterId": body.photoId}

    @app.put("/api/albums/{album_id}/cover", dependencies=[Depends(_same_origin)])
    def set_cover(album_id: str, body: CoverBody, state: State = Depends(db)):
        a = find_album(_check_id(album_id))
        if body.photoId is not None:
            _check_id(body.photoId)
            if not any(p.id == body.photoId and p.visible for p in immich(client.album_photos, a.id)):
                raise HTTPException(400, "The folder image must be a photo from the same album")
        state.set_cover(a.id, body.photoId)
        service.trigger()
        return {"albumId": a.id, "folderImageId": body.photoId or a.cover_asset_id,
                "folderImageChosen": body.photoId is not None}

    @app.get("/api/render/{photo_id}")
    def render(photo_id: str):
        """The photo exactly as it would be written for Jellyfin (same crop code as the sync)."""
        _check_id(photo_id)
        data, ctype = immich(client.thumbnail, photo_id, "preview")
        faces = []
        if crop:
            try:
                faces = client.faces(photo_id)
            except ImmichError:
                faces = []
        try:
            jpeg = images.prepare(data, ctype, faces, crop=crop)
        except (OSError, ValueError) as e:
            raise HTTPException(502, f"Couldn't read that image: {e}") from e
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/thumb/{asset_id}")
    def thumb(asset_id: str, size: str = "thumbnail"):
        if size not in ("thumbnail", "preview"):
            raise HTTPException(400, "size must be thumbnail or preview")
        data, ctype = immich(client.thumbnail, _check_id(asset_id), size)
        return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})

    @app.exception_handler(HTTPException)
    async def _errors(request: Request, exc: HTTPException):
        return Response(f'{{"error": {__import__("json").dumps(str(exc.detail))}}}',
                        status_code=exc.status_code, media_type="application/json")

    return app
