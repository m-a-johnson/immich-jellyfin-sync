# immich-jellyfin-sync

Keep Immich as the source of truth for home videos, and watch them in Jellyfin like any other library.

Pick Immich albums in a small web UI. The container mirrors each album's **videos** into a folder Jellyfin scans, as symlinks (no copies). Optionally pick a **photo from the same album** as a video's poster; otherwise Jellyfin's own screen grab is used.

## Status

Early development. Planned in three steps:

1. **Sync engine**: config, state DB, symlinks, album covers, owned-file cleanup
2. **Web UI**: album toggles, per-video poster picker (photos from the same album)
3. **Posters + Jellyfin refresh**: poster JPGs written from Immich previews, targeted Jellyfin image refresh

## How it works

```
Immich album "Camping 2019"
  ├── videos  ──► /output/Camping 2019/2019-07-04 Lake day [a1b2c3].mov  (symlink)
  └── photos  ──► poster picker only (never linked into Jellyfin)
```

- Symlink targets use the path **as Jellyfin sees Immich's storage** (`paths.jellyfin_prefix`), so this container never needs Immich's storage mounted.
- File names are derived only from the video itself (date, title, short asset id), so they never change when other videos are added or removed, and Jellyfin keeps watched state.

## Safety: owned-file cleanup

The container records every file it creates (symlink, poster, `folder.jpg`) and the Immich asset it came from. Cleanup only ever deletes files in that record. Files it didn't create are never touched, even inside its own output folder, and a misconfigured output path can't wipe unrelated media.

## Web UI

Open the container on port 8080 (put it behind Traefik + Authentik; it has no login of its own).

- **Albums**: every Immich album as a slide mount; turn on **In Jellyfin** to sync its videos. Albums in Jellyfin are circled.
- **Album page**: its videos, and **Choose poster** to pick one of the album's photos for a video. Poster choices are stored now and written for Jellyfin in step 3.
- **Sync now** runs a pass immediately; turning an album on or off also triggers one.

## CLI

Everything the UI does for albums is also available from the command line:

```bash
docker exec immich-jellyfin-sync python -m immich_jellyfin_sync albums          # list; [x] = enabled
docker exec immich-jellyfin-sync python -m immich_jellyfin_sync enable "Wedding" # by exact name or id
docker exec immich-jellyfin-sync python -m immich_jellyfin_sync sync --dry-run   # show what would change
docker exec immich-jellyfin-sync python -m immich_jellyfin_sync sync             # one pass now
```

The container's default command runs a sync every `sync_interval_minutes`.

Links are named `<local date> <original name> [<asset id prefix>].<ext>`, e.g.
`2018-07-28 Tanis & Mark Wedding [cf7586e2].mp4`. `originalPath` is used verbatim as the
link target (Immich's storage template can write names like `Tanis &amp; Mark Wedding.mp4`
to disk; that is the real file name and must not be decoded).

## Immich compatibility

Requires Immich **v3+**. v3 removed `assets` from album responses, so album contents come from
`POST /api/search/metadata` with `albumIds`. Only `timeline` and `archive` visibility videos are
synced (v3 search returns all visibilities when none is given, which would include hidden
Live Photo motion clips).

## Requirements

- Immich API key with **album.read**, **asset.read** and **asset.view** only (asset.view is for thumbnails in the web UI)
- Jellyfin API key (step 3, for poster refresh)
- Jellyfin must mount Immich's storage **read-only** at `paths.jellyfin_prefix`, and this container's output folder as its library
- Put the web UI behind authentication (e.g. Traefik + Authentik); it holds API keys

See `config.example.yaml` and `docker-compose.example.yml`.

## License

MIT
