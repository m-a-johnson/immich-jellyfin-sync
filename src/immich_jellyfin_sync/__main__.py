import logging
import sys

from . import __version__, config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("immich_jellyfin_sync")


def main() -> int:
    log.info("immich-jellyfin-sync %s starting", __version__)
    try:
        cfg = config.load()
    except config.ConfigError as e:
        log.error("config error: %s", e)
        return 2
    log.info("config ok: immich=%s output=%s jellyfin=%s",
             cfg.immich.url, cfg.paths.output, cfg.jellyfin.url if cfg.jellyfin else "disabled")
    # Step 1 (sync engine) goes here.
    return 0


if __name__ == "__main__":
    sys.exit(main())
