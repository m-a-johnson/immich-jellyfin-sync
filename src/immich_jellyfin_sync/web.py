"""Web UI + JSON API. No auth of its own: run it behind Traefik + Authentik."""
from __future__ import annotations

import re
from importlib.resources import files

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .immich import ImmichError
from .state import State

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


class EnabledBody(BaseModel):
    enabled: bool


class PosterBody(BaseModel):
    photoId: str | None = None


def create_app(client, state_path, service) -> FastAPI:
    app = FastAPI(title="immich-jellyfin-sync", docs_url=None, redoc_url=None, openapi_url=None)

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

    @app.get("/api/status")
    def status():
        return {"running": service.running, "last": service.last}

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
        return {
            "id": a.id, "name": a.name, "coverId": a.cover_asset_id,
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
        return {"videoId": video_id, "posterId": body.photoId}

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
