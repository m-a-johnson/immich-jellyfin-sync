"""Minimal, read-only Immich v3 API client.

Immich v3 removed `assets` from album responses; album contents come from
POST /api/search/metadata with `albumIds`. In v3, omitting `visibility` returns
any visibility, so visibility is filtered here explicitly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

SYNCABLE_VISIBILITY = frozenset({"timeline", "archive"})


class ImmichError(Exception):
    pass


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    cover_asset_id: str | None
    asset_count: int


@dataclass(frozen=True)
class Asset:
    id: str
    type: str
    original_path: str          # exactly as stored on disk; never unescape
    original_file_name: str     # display name (e.g. '&' where the path has '&amp;')
    local_date_time: str
    visibility: str
    is_trashed: bool
    is_offline: bool
    duration_ms: int | None = None
    description: str = ""

    @property
    def visible(self) -> bool:
        """On the timeline or archived; not hidden (e.g. Live Photo motion), trashed or offline."""
        return self.visibility in SYNCABLE_VISIBILITY and not self.is_trashed and not self.is_offline

    @property
    def syncable(self) -> bool:
        return self.type == "VIDEO" and self.visible

    @property
    def title(self) -> str:
        stem = self.original_file_name.rsplit(".", 1)[0] if "." in self.original_file_name else self.original_file_name
        return stem or self.id


def _asset(d: dict) -> Asset:
    return Asset(
        id=d["id"],
        type=d.get("type", ""),
        original_path=d["originalPath"],
        original_file_name=d.get("originalFileName") or d["originalPath"].rsplit("/", 1)[-1],
        local_date_time=d.get("localDateTime") or d.get("fileCreatedAt") or "",
        visibility=d.get("visibility", "timeline"),
        is_trashed=bool(d.get("isTrashed")),
        is_offline=bool(d.get("isOffline")),
        duration_ms=d.get("duration") if isinstance(d.get("duration"), (int, float)) else None,
        description=((d.get("exifInfo") or {}).get("description") or "").strip(),
    )


class ImmichClient:
    PAGE_SIZE = 250
    MAX_PAGES = 400

    def __init__(self, url: str, api_key: str, timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self._with_exif = True      # ask search for exifInfo (descriptions); dropped if Immich rejects it
        self._http = httpx.Client(
            base_url=url,
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _raw(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ImmichError(f"{method} {path}: {e}") from e
        if r.status_code >= 400:
            raise ImmichError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def _request(self, method: str, path: str, **kw):
        r = self._raw(method, path, **kw)
        try:
            return r.json()
        except ValueError as e:
            raise ImmichError(f"{method} {path}: response is not JSON") from e

    def albums(self) -> list[Album]:
        data = self._request("GET", "/api/albums")
        if not isinstance(data, list):
            raise ImmichError("GET /api/albums: expected a list")
        return [
            Album(
                id=a["id"],
                name=a.get("albumName") or "",
                cover_asset_id=a.get("albumThumbnailAssetId"),
                asset_count=int(a.get("assetCount") or 0),
            )
            for a in data
        ]

    def album_assets(self, album_id: str, asset_type: str) -> list[Asset]:
        """All assets of one type ('VIDEO' or 'IMAGE') in an album, any visibility."""
        out: list[Asset] = []
        page = 1
        for _ in range(self.MAX_PAGES):
            body = {"albumIds": [album_id], "type": asset_type, "page": page, "size": self.PAGE_SIZE}
            if self._with_exif and asset_type == "VIDEO":
                body["withExif"] = True
            try:
                data = self._request("POST", "/api/search/metadata", json=body)
            except ImmichError as e:
                if "withExif" in body and "HTTP 400" in str(e):
                    log.warning("Immich rejected withExif; continuing without video descriptions (%s)", e)
                    self._with_exif = False
                    continue
                raise
            assets = (data or {}).get("assets")
            if not isinstance(assets, dict) or not isinstance(assets.get("items"), list):
                raise ImmichError("search/metadata: response has no assets.items")
            out.extend(_asset(i) for i in assets["items"])
            nxt = assets.get("nextPage")
            if nxt is None:
                if assets.get("nextCursor"):
                    # Refuse rather than silently return a partial list (which would
                    # look like assets were removed from the album).
                    raise ImmichError("search/metadata returned nextCursor; cursor paging not supported yet")
                return out
            page = int(nxt)
        raise ImmichError(f"album {album_id}: more than {self.MAX_PAGES} pages")

    def album_videos(self, album_id: str) -> list[Asset]:
        return self.album_assets(album_id, "VIDEO")

    def album_photos(self, album_id: str) -> list[Asset]:
        return self.album_assets(album_id, "IMAGE")

    def thumbnail(self, asset_id: str, size: str = "thumbnail") -> tuple[bytes, str]:
        """Image bytes and content type. Needs the asset.view permission."""
        if size not in ("thumbnail", "preview"):
            raise ValueError(size)
        r = self._raw("GET", f"/api/assets/{asset_id}/thumbnail", params={"size": size})
        return r.content, r.headers.get("content-type", "application/octet-stream")
