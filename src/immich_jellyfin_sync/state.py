"""SQLite state: album selections and every file/dir this tool created."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (      -- paths relative to paths.output
    path     TEXT PRIMARY KEY,
    album_id TEXT NOT NULL,
    asset_id TEXT,
    kind     TEXT NOT NULL,              -- 'video' (step 1); 'poster'/'cover' later
    target   TEXT
);
CREATE TABLE IF NOT EXISTS dirs (
    path     TEXT PRIMARY KEY,
    album_id TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class OwnedFile:
    path: str
    album_id: str
    asset_id: str | None
    kind: str
    target: str | None


class State:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    # albums
    def remember_albums(self, albums) -> None:
        self.db.executemany(
            "INSERT INTO albums (id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
            [(a.id, a.name) for a in albums],
        )

    def set_enabled(self, album_id: str, name: str, enabled: bool) -> None:
        self.db.execute(
            "INSERT INTO albums (id, name, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, enabled = excluded.enabled",
            (album_id, name, int(enabled)),
        )
        self.db.commit()

    def enabled_album_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT id FROM albums WHERE enabled = 1")}

    # files
    def owned_files(self) -> dict[str, OwnedFile]:
        rows = self.db.execute("SELECT path, album_id, asset_id, kind, target FROM files")
        return {r[0]: OwnedFile(*r) for r in rows}

    def put_file(self, f: OwnedFile) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO files (path, album_id, asset_id, kind, target) VALUES (?, ?, ?, ?, ?)",
            (f.path, f.album_id, f.asset_id, f.kind, f.target),
        )

    def forget_file(self, path: str) -> None:
        self.db.execute("DELETE FROM files WHERE path = ?", (path,))

    # dirs
    def owned_dirs(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT path, album_id FROM dirs"))

    def put_dir(self, path: str, album_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO dirs (path, album_id) VALUES (?, ?)", (path, album_id))

    def forget_dir(self, path: str) -> None:
        self.db.execute("DELETE FROM dirs WHERE path = ?", (path,))
