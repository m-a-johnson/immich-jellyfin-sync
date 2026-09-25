"""Background sync loop shared by the CLI and the web UI."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .config import Config
from .immich import ImmichError
from .state import State
from .sync import Syncer

log = logging.getLogger(__name__)


class SyncService:
    def __init__(self, cfg: Config, client, state_path):
        self.cfg = cfg
        self.client = client
        self.state_path = state_path
        self._run_lock = threading.Lock()      # one sync at a time
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.running = False
        self.last: dict | None = None          # {finished, ok, summary, error, created, removed, failed_albums}
        self.on_sync = None                    # callback after every pass (e.g. drop cached counts)

    def run_once(self) -> None:
        with self._run_lock:
            self.running = True
            state = State(self.state_path)
            try:
                r = Syncer(self.client, state, self.cfg.paths).run()
                log.info("sync done: %s", r)
                self.last = {"ok": not r.failed_albums, "summary": str(r), "error": None,
                             "created": r.created, "removed": r.removed, "linked": r.created + r.unchanged + r.replaced,
                             "failed_albums": r.failed_albums}
            except ImmichError as e:
                log.error("sync skipped, Immich unavailable: %s", e)
                self.last = {"ok": False, "summary": "", "error": f"Couldn't reach Immich: {e}",
                             "created": 0, "removed": 0, "linked": None, "failed_albums": []}
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                log.exception("sync failed")
                self.last = {"ok": False, "summary": "", "error": f"Sync failed: {e}",
                             "created": 0, "removed": 0, "linked": None, "failed_albums": []}
            finally:
                self.last["finished"] = datetime.now(timezone.utc).isoformat()
                self.running = False
                state.close()
                if self.on_sync:
                    try:
                        self.on_sync()
                    except Exception:  # noqa: BLE001
                        log.exception("on_sync callback failed")

    def trigger(self) -> None:
        """Ask the loop to sync now (returns immediately)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def loop(self) -> None:
        interval = self.cfg.sync_interval_minutes * 60
        log.info("sync loop every %d min; output=%s", self.cfg.sync_interval_minutes, self.cfg.paths.output)
        while not self._stop.is_set():
            self.run_once()
            self._wake.wait(interval)
            self._wake.clear()
        log.info("sync loop stopped")

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.loop, name="sync-loop", daemon=True)
        t.start()
        return t
