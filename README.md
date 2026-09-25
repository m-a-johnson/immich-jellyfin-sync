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
- **Album page**: its videos; **Choose poster** picks one of the album's photos for a video (default: Jellyfin's own screen grab), and **Change folder image** picks the album folder's image (default: the album's Immich cover).
- **Sync now** runs a pass immediately; turning an album on or off also triggers one.
- Poster and folder thumbnails, and the picker's preview, show the **actual 16:9 crop** Jellyfin will get.

`GET /health` answers 200 while the app is running (the image has a Docker `HEALTHCHECK` using it);
Immich or Jellyfin problems are reported in its body rather than failing it, since restarting this
container wouldn't fix another server.

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

## Images

Confirmed on Jellyfin 12.1 (Home Videos and Photos library):

| Image | File |
|---|---|
| Album folder | `folder.jpg` |
| Video | `<video file name without extension>.jpg` |

| Title, date, description | `<video file name without extension>.nfo` |

The NFO also carries **people** (`<actor>`) and **tags** (`<tag>`), so Jellyfin shows a People row and
tag filters:

- People start from the named, visible people Immich has on the video (detected or added by hand in
  Immich). On the album page you can add names that exist only here, or remove an Immich name for
  one video (it stays removed even if Immich keeps listing it; restore it any time).
- Tags come from Immich's asset details (nested tags use their last part: `Places/Canada/BC` becomes `BC`).
  If tags can't be read, the video's existing NFO is kept as-is rather than rewritten without them.
- Each NFO has `<uniqueid type="immich">` with the video's Immich asset id (a future Jellyfin plugin
  could turn it into a link back to Immich).
- Names and tags are sorted, so Immich returning them in another order never rewrites an NFO.

- **Roles**: set once per person on the **People** page (e.g. "Nonno"); Jellyfin shows "as Nonno"
  under their name on every video instead of "Actor".
- **Faces and locking**: Jellyfin identifies people only by name, server-wide, and looks them up
  online, so a first name can pick up a stranger's biography and photo. After each sync, every person
  in your NFOs is cleaned (external ids, biography, birth details cleared), **locked** so Jellyfin
  won't look them up again, and given their face from Immich (`person.read`). Names added only here
  get the face of the Immich person with exactly that name, if there is one. An image you set in
  Jellyfin yourself is never replaced.
- **Titles**: each video's title is Immich's file name unless you set one on its album page.

Names, roles and titles you add live in this app's state DB (`/config/state.db`), not in Immich.

The NFO title is the video's original file name (e.g. `Tanis & Mark Wedding`), the date is when it
was recorded, and the description comes from Immich's info panel. Edit a description in Immich and
the NFO is rewritten on the next sync; edit an NFO by hand and it's left alone.

### Cropping

Jellyfin shows Home Videos folders and videos as landscape cards, so posters and folder images are
cropped to 16:9 (photos already within 5% are left alone):

1. **Faces first**: the crop is placed around the faces Immich detected (`GET /api/faces`, needs
   `face.read`), with headroom above; if a group is too tall to fit, the tops of heads win.
2. **Otherwise detail**: the strip with the most edge detail (subjects are busier than backgrounds).
3. **Otherwise** slightly above centre.

If the key lacks `face.read`, cropping continues without faces (one warning per sync). Turn it off
with `images: {crop: false}`. The crop method is stored with each image, so improving it later
re-renders existing images automatically.

Images are Immich's preview size, converted to JPEG if Immich serves WebP, and written atomically.
The sha256 of each image is recorded; if you replace one with your own file, it is never
overwritten or deleted. If a chosen photo leaves the album, the choice is cleared and the default
comes back.

## Jellyfin

With a `jellyfin:` section in `config.yaml`, each sync tells Jellyfin about new and removed videos
(`POST /Library/Media/Updated`) and refreshes the images of items whose image changed
(`POST /Items/{id}/Refresh` with `replaceAllImages=true`). Jellyfin 12 only accepts the
`Authorization: MediaBrowser Token="..."` header; the old `X-Emby-Token` / `api_key` are gone.

Test the connection without changing anything:

```bash
docker exec immich-jellyfin-sync python -m immich_jellyfin_sync jellyfin-check
```

## Immich compatibility

Requires Immich **v3+**. v3 removed `assets` from album responses, so album contents come from
`POST /api/search/metadata` with `albumIds`. Only `timeline` and `archive` visibility videos are
synced (v3 search returns all visibilities when none is given, which would include hidden
Live Photo motion clips).

## Requirements

- Immich API key with **album.read**, **asset.read**, **asset.view**, **face.read** and **person.read** only (asset.view: web UI thumbnails; face.read: face-aware cropping; person.read: faces for Jellyfin's people)
- Jellyfin API key (optional; Dashboard > API Keys) so changes show up without waiting for a library scan
- Jellyfin must mount Immich's storage **read-only** at `paths.jellyfin_prefix`, and this container's output folder as its library
- Put the web UI behind authentication (e.g. Traefik + Authentik); it holds API keys

See `config.example.yaml` and `docker-compose.example.yml`.

## License

MIT
