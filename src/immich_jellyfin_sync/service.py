"""Background sync loop shared by the CLI and the web UI."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .config import Config
from .immich import ImmichError
from .jellyfin import JellyfinError, notify
from .people import claim_people
from .state import State
from .sync import Syncer

log = logging.getLogger(__name__)


class SyncService:
    def __init__(self, cfg: Config, client, state_path, jellyfin=None):
        self.cfg = cfg
        self.client = client
        self.jellyfin = jellyfin               # JellyfinClient or None
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
                r = Syncer(self.client, state, self.cfg.paths, crop=self.cfg.crop_images).run()
                log.info("sync done: %s", r)
                jf_error = self._tell_jellyfin(r)
                self._claim_people(state, r)
                self.last = {"ok": not r.failed_albums and not jf_error, "summary": str(r), "error": None,
                             "created": r.created, "removed": r.removed, "linked": r.linked,
                             "failed_albums": r.failed_albums, "jellyfin_error": jf_error}
            except ImmichError as e:
                log.error("sync skipped, Immich unavailable: %s", e)
                self.last = {"ok": False, "summary": "", "error": f"Couldn't reach Immich: {e}",
                             "created": 0, "removed": 0, "linked": None, "failed_albums": [], "jellyfin_error": None}
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                log.exception("sync failed")
                self.last = {"ok": False, "summary": "", "error": f"Sync failed: {e}",
                             "created": 0, "removed": 0, "linked": None, "failed_albums": [], "jellyfin_error": None}
            finally:
                self.last["finished"] = datetime.now(timezone.utc).isoformat()
                self.running = False
                state.close()
                if self.on_sync:
                    try:
                        self.on_sync()
                    except Exception:  # noqa: BLE001
                        log.exception("on_sync callback failed")

    def _tell_jellyfin(self, r) -> str | None:
        if self.jellyfin is None or not (r.created_links or r.removed_links or r.images_changed or r.metadata_changed):
            return None
        try:
            res = notify(self.jellyfin, r.created_links, r.removed_links, r.images_changed, r.metadata_changed)
            log.info("told Jellyfin: %d path update(s), %d item refresh(es)", res.announced, res.refreshed)
            if res.not_found:
                log.info("not in Jellyfin yet (will get images on first scan): %s", res.not_found)
            return None
        except JellyfinError as e:
            log.warning("couldn't tell Jellyfin about changes: %s", e)
            return f"Couldn't reach Jellyfin: {e}"

    def _claim_people(self, state, r) -> None:
        if self.jellyfin is None or not r.people:
            return
        try:
            res = claim_people(self.jellyfin, self.client, state, r.people)
        except Exception:  # noqa: BLE001 - never let this break syncing
            log.exception("claiming people in Jellyfin failed")
            return
        if res.claimed or res.faces or res.removed_images:
            log.info("people: %d cleaned/locked, %d face(s) from Immich, %d online photo(s) removed",
                     res.claimed, res.faces, res.removed_images)
        if res.not_in_jellyfin:
            log.info("people not in Jellyfin yet (next pass): %s", res.not_in_jellyfin)
        if res.errors:
            log.warning("people with errors: %s", res.errors)

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
