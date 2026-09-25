# Development notes

Everything here was verified against **Immich 3.2.2** and **Jellyfin 12.1**. Where a fact came from
testing a real server rather than documentation, it says so; re-check those when either major
version changes.

## Layout

| Module | Job |
|---|---|
| `immich.py` | Read-only Immich v3 client: albums, album contents via search, tags, faces, people, thumbnails |
| `sync.py` | Plans the output folder (links, images, NFOs) and reconciles it; the safety rules live here |
| `images.py` | Immich preview to Jellyfin JPEG; 16:9 crop around faces, else edge detail |
| `jellyfin.py` | Jellyfin 12 client: path updates, item refresh, person read/update/image |
| `people.py` | Makes Jellyfin people ours: clear online matches, lock, Immich face |
| `service.py` | Background sync loop; tells Jellyfin and claims people after each pass |
| `web.py` + `static/index.html` | FastAPI JSON API and the single-page UI (no build step) |
| `state.py` | SQLite: album choices, posters, covers, titles, people changes, roles, owned files, claimed people |

## Safety rules (don't weaken these)

- Only files recorded in `state.db` as created by this tool are ever removed or rewritten.
- Videos: only symlinks are unlinked. Images and NFOs: only if their sha256 still matches what was
  written; a file someone edited or replaced is left alone.
- An Immich read failure never looks like an empty album: the album is "frozen" for that pass.
- If tags can't be read, the video's existing NFO is kept as-is.
- Jellyfin people: an image the tool didn't upload (tracked by Jellyfin's image tag) is never replaced,
  unless it came with an online match (external ids present).

## Immich v3 facts

- Albums no longer include `assets`; contents come from `POST /api/search/metadata` with `albumIds`.
- Search without `visibility` returns **all** visibilities (incl. hidden Live Photo motion clips):
  filter to `timeline`/`archive` yourself.
- `withExif: true` gives `exifInfo.description`; `withPeople: true` gives named people with ids.
  Both accepted together (tested). Tags are **not** in search results: `GET /api/assets/{id}` has
  `tags[].name` / `tags[].value` (`value` is the full nested path).
- `originalPath` is the real on-disk path and must be used verbatim. The storage template can write
  HTML-escaped names (`Tanis &amp; Mark Wedding.mp4` on disk, `&` in `originalFileName`). Tested.
- `localDateTime` is the local capture time; use it, not UTC `fileCreatedAt`, for dates.
- Pagination: `assets.nextPage` (and `nextCursor`, unsupported here, which fails safely).
- `GET /api/faces?id=` returns boxes plus the `imageWidth`/`imageHeight` they were measured on;
  normalise per face (sizes differ between ML and metadata faces).
- `GET /api/search/person?name=`, `GET /api/people/{id}/thumbnail` (JPEG) need `person.read`.
- Key permissions used: `album.read`, `asset.read`, `asset.view`, `face.read`, `person.read`.

## Jellyfin 12 facts

- Auth: only `Authorization: MediaBrowser Token="..."`. `X-Emby-Token` and `?api_key=` were removed.
- Home Videos and Photos library (tested on 12.1):
  - `folder.jpg` in a folder is its image; `<video name>.jpg` next to a video is the video's image.
  - `<video name>.nfo` with a `<movie>` root is read: `title`, `premiered`/`year`, `plot`, `tag`,
    `actor` (`name`, `role`, `type`), `uniqueid`.
  - `<role>` shows as "as Nonno" under the person instead of the type.
  - Unknown `uniqueid` types are kept: `<uniqueid type="immich">` becomes `ProviderIds.immich`.
  - Folder view (12.0+) shows albums as folders; set the library's default tab to Folders.
- Images/metadata only change on a refresh: `POST /Items/{id}/Refresh` with
  `imageRefreshMode`/`metadataRefreshMode=FullRefresh` and `replaceAll*=true`. Watched state survives.
- `POST /Library/Media/Updated` announces created/deleted paths; Jellyfin applies it after a short delay.
- Items by path: `GET /Library/VirtualFolders` (find the library by location) then
  `GET /Items?ParentId=&Recursive=true&Fields=Path`.
- People are global and matched **by name**, and Jellyfin looks them up online (a first name matched
  a stranger on TheMovieDb). `GET /Persons/{name}`; `POST /Items/{id}` with the edited DTO clears
  ids and sets `LockData` (lock survives a full replace-all refresh, tested);
  `POST /Items/{id}/Images/Primary` takes a **base64** body with `Content-Type: image/jpeg` (tested).
- Plugins built for 10.11 (.NET 9) don't load on 12.x (.NET 10).

## Tests

```bash
pip install -r requirements.txt pytest
pytest -q
```

Tests use fake Immich/Jellyfin clients (and `httpx.MockTransport` for the HTTP clients); no servers
needed. CI runs them before every image build.

Run the app locally against real servers:

```bash
IJS_CONFIG=./config.yaml IJS_STATE=./state.db PYTHONPATH=src python -m immich_jellyfin_sync
```

## Releases

- Every push to `main` builds `ghcr.io/m-a-johnson/immich-jellyfin-sync:main`.
- Pushing a `v*` tag builds `:<version>` and `:latest`. Bump `__version__` in `__init__.py` first.
- Image and crop changes: bump `images.VERSION` to re-render existing images on upgrade.
