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
    kind     TEXT NOT NULL,              -- 'video' | 'poster' | 'cover'
    target   TEXT                        -- symlink target (video) or sha256 of what we wrote (images)
);
CREATE TABLE IF NOT EXISTS posters (   -- chosen in the web UI; written to disk in step 3
    album_id       TEXT NOT NULL,
    video_asset_id TEXT NOT NULL,
    photo_asset_id TEXT NOT NULL,
    PRIMARY KEY (album_id, video_asset_id)
);
CREATE TABLE IF NOT EXISTS covers (    -- folder image chosen in the web UI (else Immich's album cover)
    album_id       TEXT PRIMARY KEY,
    photo_asset_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people_added (    -- names you added to a video
    video_asset_id TEXT NOT NULL,
    name           TEXT NOT NULL,
    PRIMARY KEY (video_asset_id, name)
);
CREATE TABLE IF NOT EXISTS people_hidden (   -- Immich names you removed from a video
    video_asset_id TEXT NOT NULL,
    name           TEXT NOT NULL,
    PRIMARY KEY (video_asset_id, name)
);
CREATE TABLE IF NOT EXISTS titles (          -- title set in the app instead of Immich's file name
    video_asset_id TEXT PRIMARY KEY,
    title          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_roles (    -- shown under the name in Jellyfin ("as Nonno")
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL
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
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
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

    # posters
    def posters(self, album_id: str) -> dict[str, str]:
        """video asset id -> chosen photo asset id"""
        return dict(self.db.execute(
            "SELECT video_asset_id, photo_asset_id FROM posters WHERE album_id = ?", (album_id,)))

    def set_poster(self, album_id: str, video_id: str, photo_id: str | None) -> None:
        if photo_id is None:
            self.db.execute("DELETE FROM posters WHERE album_id = ? AND video_asset_id = ?", (album_id, video_id))
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO posters (album_id, video_asset_id, photo_asset_id) VALUES (?, ?, ?)",
                (album_id, video_id, photo_id))
        self.db.commit()

    # folder image override
    def cover(self, album_id: str) -> str | None:
        r = self.db.execute("SELECT photo_asset_id FROM covers WHERE album_id = ?", (album_id,)).fetchone()
        return r[0] if r else None

    def set_cover(self, album_id: str, photo_id: str | None) -> None:
        if photo_id is None:
            self.db.execute("DELETE FROM covers WHERE album_id = ?", (album_id,))
        else:
            self.db.execute("INSERT OR REPLACE INTO covers (album_id, photo_asset_id) VALUES (?, ?)",
                            (album_id, photo_id))
        self.db.commit()

    # people overrides (per video)
    def people_changes(self, video_id: str) -> tuple[set[str], set[str]]:
        """(added, hidden) for one video"""
        added = {r[0] for r in self.db.execute("SELECT name FROM people_added WHERE video_asset_id = ?", (video_id,))}
        hidden = {r[0] for r in self.db.execute("SELECT name FROM people_hidden WHERE video_asset_id = ?", (video_id,))}
        return added, hidden

    def add_person(self, video_id: str, name: str) -> None:
        self.db.execute("DELETE FROM people_hidden WHERE video_asset_id = ? AND name = ?", (video_id, name))
        self.db.execute("INSERT OR IGNORE INTO people_added (video_asset_id, name) VALUES (?, ?)", (video_id, name))
        self.db.commit()

    def remove_person(self, video_id: str, name: str, from_immich: bool) -> None:
        self.db.execute("DELETE FROM people_added WHERE video_asset_id = ? AND name = ?", (video_id, name))
        if from_immich:
            self.db.execute("INSERT OR IGNORE INTO people_hidden (video_asset_id, name) VALUES (?, ?)", (video_id, name))
        self.db.commit()

    def restore_person(self, video_id: str, name: str) -> None:
        self.db.execute("DELETE FROM people_hidden WHERE video_asset_id = ? AND name = ?", (video_id, name))
        self.db.commit()

    # titles
    def title(self, video_id: str) -> str | None:
        r = self.db.execute("SELECT title FROM titles WHERE video_asset_id = ?", (video_id,)).fetchone()
        return r[0] if r else None

    def set_title(self, video_id: str, title: str | None) -> None:
        if title is None:
            self.db.execute("DELETE FROM titles WHERE video_asset_id = ?", (video_id,))
        else:
            self.db.execute("INSERT OR REPLACE INTO titles (video_asset_id, title) VALUES (?, ?)", (video_id, title))
        self.db.commit()

    # roles
    def roles(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT name, role FROM person_roles"))

    def set_role(self, name: str, role: str | None) -> None:
        if role is None:
            self.db.execute("DELETE FROM person_roles WHERE name = ?", (name,))
        else:
            self.db.execute("INSERT OR REPLACE INTO person_roles (name, role) VALUES (?, ?)", (name, role))
        self.db.commit()


def merge_people(immich_names, added: set[str], hidden: set[str]) -> list[str]:
    """Immich's names minus the hidden ones, plus your own; sorted so the NFO doesn't
    change (and get rewritten) just because Immich returned people in another order."""
    names = {n for n in immich_names if n not in hidden} | set(added)
    return sorted(names, key=lambda n: (n.casefold(), n))
