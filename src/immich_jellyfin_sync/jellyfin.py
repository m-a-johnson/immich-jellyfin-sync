"""Minimal Jellyfin client for telling Jellyfin about changes.

Jellyfin 12 accepts only `Authorization: MediaBrowser Token="..."`; the legacy
X-Emby-Token header and ?api_key= query parameter were removed.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from . import __version__

log = logging.getLogger(__name__)


class JellyfinError(Exception):
    pass


class JellyfinClient:
    PAGE = 500

    def __init__(self, url: str, api_key: str, library_path: str, transport: httpx.BaseTransport | None = None):
        auth = (f'MediaBrowser Token="{api_key}", Client="immich-jellyfin-sync", Version="{__version__}", '
                f'Device="immich-jellyfin-sync", DeviceId="immich-jellyfin-sync"')
        self._http = httpx.Client(base_url=url, headers={"Authorization": auth, "Accept": "application/json"},
                                  timeout=30.0, transport=transport)
        self.library_path = library_path.rstrip("/")

    def close(self) -> None:
        self._http.close()

    def path(self, rel: str) -> str:
        """Path of one of our files as Jellyfin sees it."""
        return f"{self.library_path}/{rel}"

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise JellyfinError(f"{method} {path}: {e}") from e
        if r.status_code == 401:
            raise JellyfinError(f"{method} {path}: 401 Unauthorized (check the Jellyfin API key)")
        if r.status_code >= 400:
            raise JellyfinError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def server_info(self) -> dict:
        return self._req("GET", "/System/Info").json()

    def library_id(self) -> tuple[str, str]:
        """(item id, name) of the library whose folder is library_path."""
        for lib in self._req("GET", "/Library/VirtualFolders").json():
            for loc in lib.get("Locations") or []:
                loc = loc.rstrip("/")
                if self.library_path == loc or self.library_path.startswith(loc + "/"):
                    return lib["ItemId"], lib.get("Name", "")
        raise JellyfinError(f"no Jellyfin library uses the folder {self.library_path!r}")

    def item_ids_by_path(self, library_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        start = 0
        while True:
            data = self._req("GET", "/Items", params={
                "ParentId": library_id, "Recursive": "true", "Fields": "Path",
                "EnableImages": "false", "EnableUserData": "false",
                "StartIndex": start, "Limit": self.PAGE,
            }).json()
            items = data.get("Items") or []
            for it in items:
                if it.get("Path"):
                    out[it["Path"].rstrip("/")] = it["Id"]
            if len(items) < self.PAGE:
                return out
            start += self.PAGE

    def refresh(self, item_id: str, images: bool, metadata: bool) -> None:
        # Same as the web UI's "Refresh metadata" with "Replace all metadata" and/or
        # "Replace existing images". Neither resets watched state.
        self._req("POST", f"/Items/{item_id}/Refresh", params={
            "metadataRefreshMode": "FullRefresh" if metadata else "Default",
            "imageRefreshMode": "FullRefresh" if images else "Default",
            "replaceAllMetadata": str(metadata).lower(),
            "replaceAllImages": str(images).lower(),
        })

    # ---- people (confirmed on 12.1: GET /Persons/{name}, POST /Items/{id} keeps LockData,
    #      POST /Items/{id}/Images/Primary with a base64 body)
    def person(self, name: str) -> dict | None:
        try:
            r = self._http.request("GET", f"/Persons/{quote(name, safe='')}")
        except httpx.HTTPError as e:
            raise JellyfinError(f"GET /Persons/{name}: {e}") from e
        if r.status_code == 404:
            return None                        # Jellyfin hasn't read an NFO with this name yet
        if r.status_code >= 400:
            raise JellyfinError(f"GET /Persons/{name}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def update_item(self, item: dict) -> None:
        self._req("POST", f"/Items/{item['Id']}", json=item)

    def upload_primary(self, item_id: str, jpeg: bytes) -> None:
        self._req("POST", f"/Items/{item_id}/Images/Primary", content=base64.b64encode(jpeg),
                  headers={"Content-Type": "image/jpeg"})

    def delete_primary(self, item_id: str) -> None:
        self._req("DELETE", f"/Items/{item_id}/Images/Primary")

    def media_updated(self, updates: list[tuple[str, str]]) -> None:
        """updates: (jellyfin path, 'Created' | 'Deleted' | 'Modified')"""
        self._req("POST", "/Library/Media/Updated",
                  json={"Updates": [{"Path": p, "UpdateType": t} for p, t in updates]})


@dataclass
class NotifyResult:
    announced: int = 0
    refreshed: int = 0
    not_found: list[str] = field(default_factory=list)


def notify(jf: JellyfinClient, created: list[str], removed: list[str], images_changed: set[str],
           metadata_changed: set[str] = frozenset()) -> NotifyResult:
    """Tell Jellyfin what changed. `images_changed` / `metadata_changed` hold the rel paths of the
    items (videos or album folders) whose image / NFO changed; items Jellyfin already knows get one
    refresh covering both, the rest are new and pick everything up on their first scan."""
    res = NotifyResult()
    updates = [(jf.path(r), "Created") for r in created] + [(jf.path(r), "Deleted") for r in removed]
    to_refresh = (set(images_changed) | set(metadata_changed)) - set(created) - set(removed)
    if to_refresh:
        ids = jf.item_ids_by_path(jf.library_id()[0])
        for rel in sorted(to_refresh):
            item = ids.get(jf.path(rel))
            if item:
                jf.refresh(item, images=rel in images_changed, metadata=rel in metadata_changed)
                res.refreshed += 1
            else:
                res.not_found.append(rel)
                updates.append((jf.path(rel), "Modified"))
    if updates:
        jf.media_updated(updates)
        res.announced = len(updates)
    return res
