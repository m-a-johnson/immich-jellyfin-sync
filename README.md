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

## Requirements

- Immich API key with **album read** and **asset read** only
- Jellyfin API key (step 3, for poster refresh)
- Jellyfin must mount Immich's storage **read-only** at `paths.jellyfin_prefix`, and this container's output folder as its library
- Put the web UI behind authentication (e.g. Traefik + Authentik); it holds API keys

See `config.example.yaml` and `docker-compose.example.yml`.

## License

MIT
