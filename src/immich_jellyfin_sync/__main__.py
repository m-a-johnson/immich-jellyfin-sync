"""Entry point.

  (no args)          web UI on :8080 plus the background sync loop (container default)
  sync [--dry-run]   one sync pass
  albums             list Immich albums and which are enabled
  enable ALBUM       enable an album by id or exact name
  disable ALBUM      disable an album (its files are removed on the next sync)
  jellyfin-check     test the Jellyfin connection, library and item lookup (changes nothing)
"""
import argparse
import logging
import os
import sys

from . import __version__, config
from .immich import ImmichClient, ImmichError
from .jellyfin import JellyfinClient, JellyfinError
from .state import State
from .sync import Syncer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("immich_jellyfin_sync")


def _find_album(client: ImmichClient, query: str):
    albums = client.albums()
    exact = [a for a in albums if a.id == query] or [a for a in albums if a.name.casefold() == query.casefold()]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        raise SystemExit(f"no album matches {query!r} (run 'albums' to list them)")
    ids = ", ".join(f"{a.name} [{a.id}]" for a in exact)
    raise SystemExit(f"{query!r} matches several albums, use the id: {ids}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="immich_jellyfin_sync")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("sync")
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("albums")
    for name in ("enable", "disable"):
        sub.add_parser(name).add_argument("album")
    sub.add_parser("jellyfin-check")
    args = ap.parse_args(argv)

    log.info("immich-jellyfin-sync %s", __version__)
    try:
        cfg = config.load()
    except config.ConfigError as e:
        log.error("config error: %s", e)
        return 2

    client = ImmichClient(cfg.immich.url, cfg.immich.api_key)
    state = State(config.state_path())
    try:
        if args.cmd == "albums":
            enabled = state.enabled_album_ids()
            for a in sorted(client.albums(), key=lambda a: a.name.casefold()):
                print(f"[{'x' if a.id in enabled else ' '}] {a.name}  ({a.asset_count} assets)  {a.id}")
            return 0
        if args.cmd in ("enable", "disable"):
            a = _find_album(client, args.album)
            state.set_enabled(a.id, a.name, args.cmd == "enable")
            print(f"{args.cmd}d {a.name!r}; run 'sync' or wait for the next pass")
            return 0
        if args.cmd == "jellyfin-check":
            return _jellyfin_check(cfg, state)
        if args.cmd == "sync":
            report = Syncer(client, state, cfg.paths, dry_run=args.dry_run, crop=cfg.crop_images).run()
            log.info("sync done: %s", report)
            return 1 if report.failed_albums else 0
        state.close()
        return _serve(client, cfg)
    except ImmichError as e:
        log.error("Immich error: %s", e)
        return 1
    finally:
        state.close()
        client.close()


def _jellyfin(cfg):
    if cfg.jellyfin is None:
        log.info("no jellyfin section in config.yaml: Jellyfin won't be told about changes")
        return None
    return JellyfinClient(cfg.jellyfin.url, cfg.jellyfin.api_key, cfg.jellyfin_library_path)


def _jellyfin_check(cfg, state) -> int:
    jf = _jellyfin(cfg)
    if jf is None:
        print("FAIL no jellyfin section in config.yaml")
        return 2
    try:
        info = jf.server_info()
        print(f"OK   connected to {info.get('ServerName')} (Jellyfin {info.get('Version')})")
        lib_id, name = jf.library_id()
        print(f"OK   library {name!r} uses {cfg.jellyfin_library_path}")
        ids = jf.item_ids_by_path(lib_id)
        print(f"OK   Jellyfin lists {len(ids)} item(s) in it")
        mine = [rel for rel, f in state.owned_files().items() if f.kind == "video"]
        found = [rel for rel in mine if jf.path(rel) in ids]
        folders = {rel.rsplit("/", 1)[0] for rel in mine}
        found_dirs = [d for d in folders if jf.path(d) in ids]
        print(f"{'OK  ' if len(found) == len(mine) else 'WARN'} {len(found)} of {len(mine)} synced video(s) found by path")
        print(f"{'OK  ' if len(found_dirs) == len(folders) else 'WARN'} {len(found_dirs)} of {len(folders)} album folder(s) found by path")
        missing = sorted(set(mine) - set(found))[:5]
        for rel in missing:
            print(f"     not found: {jf.path(rel)}")
        if missing and ids:
            print(f"     example path Jellyfin reports: {next(iter(ids))}")
        return 0 if len(found) == len(mine) else 1
    except JellyfinError as e:
        print(f"FAIL {e}")
        return 1
    finally:
        jf.close()


def _serve(client, cfg) -> int:
    import uvicorn

    from .service import SyncService
    from .web import create_app

    service = SyncService(cfg, client, config.state_path(), jellyfin=_jellyfin(cfg))
    service.start()
    port = int(os.environ.get("IJS_PORT", "8080"))
    log.info("web UI on :%d", port)
    try:
        uvicorn.run(create_app(client, config.state_path(), service, crop=cfg.crop_images), host="0.0.0.0", port=port,
                    log_level="warning", proxy_headers=True)
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
