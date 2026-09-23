"""Reconcile enabled Immich albums into symlinks under paths.output.

Safety rules:
  * Only files/dirs recorded in the state DB (i.e. created by this tool) are ever removed.
  * Only symlinks are unlinked; a real file at an owned path is left alone and forgotten.
  * Existing files not created by this tool are never overwritten.
  * If Immich can't be read (album list or one album's contents), nothing is removed
    for the affected album(s) - an API failure never looks like an empty album.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from .config import Paths
from .immich import Album, Asset, ImmichError
from .state import OwnedFile, State

log = logging.getLogger(__name__)

_RESERVED = re.compile(r'[<>:"/\\|?*\x00-\x1f]')   # Jellyfin/Windows-reserved + control chars
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def clean_name(name: str, fallback: str = "untitled", max_len: int = 150) -> str:
    s = _RESERVED.sub("_", name).strip().strip(". ")
    return s[:max_len].rstrip(". ") or fallback


def video_filename(a: Asset) -> str:
    """Stable name derived only from this asset: '<local date> <title> [<id8>]<ext>'."""
    stem, ext = os.path.splitext(a.original_file_name)
    date = a.local_date_time[:10] if _DATE.match(a.local_date_time) else "0000-00-00"
    ext = "." + clean_name(ext[1:].lower(), fallback="") if len(ext) > 1 else ""
    return f"{date} {clean_name(stem)} [{a.id[:8]}]{ext}"


def jellyfin_target(original_path: str, paths: Paths) -> str | None:
    if not original_path.startswith(paths.immich_prefix + "/"):
        return None
    return paths.jellyfin_prefix + original_path[len(paths.immich_prefix):]


def album_folders(albums: list[Album], owned_dirs: dict[str, str]) -> dict[str, str]:
    """Folder per album; duplicate names get ' [id8]'. An album keeps a folder it already owns."""
    folders: dict[str, str] = {}
    used: set[str] = set()
    owns = {(path, album_id) for path, album_id in owned_dirs.items()}
    for a in sorted(albums, key=lambda a: ((clean_name(a.name, f"album {a.id[:8]}"), a.id) not in owns, a.id)):
        base = clean_name(a.name, fallback=f"album {a.id[:8]}")
        name = base if base.casefold() not in used else f"{base} [{a.id[:8]}]"
        used.add(name.casefold())
        folders[a.id] = name
    return folders


@dataclass(frozen=True)
class Desired:
    album_id: str
    asset_id: str
    target: str


@dataclass
class Report:
    created: int = 0
    replaced: int = 0
    removed: int = 0
    unchanged: int = 0
    conflicts: int = 0
    skipped_assets: int = 0
    dirs_removed: int = 0
    failed_albums: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        s = (f"created={self.created} replaced={self.replaced} removed={self.removed} "
             f"unchanged={self.unchanged} conflicts={self.conflicts} "
             f"skipped_assets={self.skipped_assets} dirs_removed={self.dirs_removed}")
        if self.failed_albums:
            s += f" failed_albums={self.failed_albums}"
        return s


class Syncer:
    def __init__(self, client, state: State, paths: Paths, dry_run: bool = False):
        self.client = client
        self.state = state
        self.paths = paths
        self.dry_run = dry_run
        self.root = os.path.normpath(str(paths.output))
        if self.root == os.sep:
            raise ValueError("refusing to use / as the output directory")

    def _abs(self, rel: str) -> str:
        p = os.path.normpath(os.path.join(self.root, rel))
        if os.path.commonpath([p, self.root]) != self.root or p == self.root:
            raise ValueError(f"path escapes output directory: {rel!r}")
        return p

    def _act(self, verb: str, rel: str, extra: str = "") -> None:
        log.info("%s%s %s%s", "[dry-run] " if self.dry_run else "", verb, rel, f" -> {extra}" if extra else "")

    def run(self) -> Report:
        report = Report()
        albums = self.client.albums()          # ImmichError propagates: no changes at all
        self.state.remember_albums(albums)
        present = {a.id: a for a in albums}
        enabled = self.state.enabled_album_ids()
        for gone in sorted(enabled - present.keys()):
            log.warning("enabled album %s no longer exists in Immich; its files will be removed", gone)
        active = [present[i] for i in sorted(enabled & present.keys())]
        folders = album_folders(active, self.state.owned_dirs())

        desired: dict[str, Desired] = {}
        frozen: set[str] = set()
        for album in active:
            try:
                videos = self.client.album_videos(album.id)
            except ImmichError as e:
                log.error("album %r: %s (keeping its existing files)", album.name, e)
                frozen.add(album.id)
                report.failed_albums.append(album.name)
                continue
            for a in videos:
                if not a.syncable:
                    report.skipped_assets += 1
                    continue
                target = jellyfin_target(a.original_path, self.paths)
                if target is None:
                    log.warning("asset %s: %r is not under immich_prefix %r; skipped",
                                a.id, a.original_path, self.paths.immich_prefix)
                    report.skipped_assets += 1
                    continue
                desired[f"{folders[album.id]}/{video_filename(a)}"] = Desired(album.id, a.id, target)

        owned = self.state.owned_files()
        for rel, row in sorted(owned.items()):
            if row.album_id in frozen:
                continue
            d = desired.get(rel)
            if d is not None and d.album_id == row.album_id:
                continue
            self._remove(rel, report)
            owned.pop(rel)

        for rel, d in sorted(desired.items()):
            self._ensure(rel, d, owned.get(rel), report)

        self._cleanup_dirs(desired, frozen, report)
        if not self.dry_run:
            self.state.commit()
        return report

    def _remove(self, rel: str, report: Report) -> None:
        p = self._abs(rel)
        if os.path.islink(p):
            self._act("remove", rel)
            if not self.dry_run:
                os.unlink(p)
            report.removed += 1
        elif os.path.lexists(p):
            log.warning("%s is no longer a symlink (replaced by a real file?); leaving it untouched", rel)
            report.conflicts += 1
        if not self.dry_run:
            self.state.forget_file(rel)

    def _ensure(self, rel: str, d: Desired, row: OwnedFile | None, report: Report) -> None:
        p = self._abs(rel)
        if os.path.islink(p):
            if row is None:
                log.warning("%s exists but was not created by this tool; not touching it", rel)
                report.conflicts += 1
                return
            if os.readlink(p) == d.target:
                report.unchanged += 1
                if row.album_id != d.album_id or row.asset_id != d.asset_id:
                    self._record(rel, d)
                return
            self._act("relink", rel, d.target)
            if not self.dry_run:
                os.unlink(p)
                os.symlink(d.target, p)
            report.replaced += 1
        elif os.path.lexists(p):
            log.warning("%s exists and is not a symlink; not touching it", rel)
            report.conflicts += 1
            return
        else:
            self._make_dirs(os.path.dirname(rel), d.album_id)
            self._act("link", rel, d.target)
            if not self.dry_run:
                os.symlink(d.target, p)
            report.created += 1
        self._record(rel, d)

    def _record(self, rel: str, d: Desired) -> None:
        if not self.dry_run:
            self.state.put_file(OwnedFile(rel, d.album_id, d.asset_id, "video", d.target))

    def _make_dirs(self, rel_dir: str, album_id: str) -> None:
        parts = [x for x in rel_dir.split("/") if x]
        for i in range(1, len(parts) + 1):
            rel = "/".join(parts[:i])
            p = self._abs(rel)
            if not os.path.lexists(p):
                self._act("mkdir", rel)
                if not self.dry_run:
                    os.mkdir(p)
                    self.state.put_dir(rel, album_id)

    def _cleanup_dirs(self, desired: dict[str, Desired], frozen: set[str], report: Report) -> None:
        needed = set()
        for rel in desired:
            parts = rel.split("/")[:-1]
            needed.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
        for rel, album_id in sorted(self.state.owned_dirs().items(), key=lambda kv: -kv[0].count("/")):
            if album_id in frozen or rel in needed:
                continue
            p = self._abs(rel)
            if not os.path.lexists(p):
                if not self.dry_run:
                    self.state.forget_dir(rel)
                continue
            if os.path.islink(p) or not os.path.isdir(p):
                continue
            if os.listdir(p):
                log.info("%s kept: it contains files this tool didn't create", rel)
                continue
            self._act("rmdir", rel)
            if not self.dry_run:
                os.rmdir(p)
                self.state.forget_dir(rel)
            report.dirs_removed += 1
