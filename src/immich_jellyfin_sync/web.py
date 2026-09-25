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
from .state import State

log = logging.getLogger(__name__)

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
        return {
            "id": a.id, "name": a.name, "coverId": a.cover_asset_id,
            "folderImageId": override or a.cover_asset_id, "folderImageChosen": override is not None,
            "enabled": a.id in state.enabled_album_ids(),
            "videos": [{"id": v.id, "title": v.title, "date": v.local_date_time[:10],
                        "durationMs": v.duration_ms, "posterId": posters.get(v.id)} for v in videos],
        }

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
