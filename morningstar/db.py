"""SQLite storage: library index, reading state, favorites, bookmarks and notes."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from .config import DB_FILE

SCHEMA = """
CREATE TABLE IF NOT EXISTS library (
    path    TEXT PRIMARY KEY,
    root    TEXT NOT NULL,
    rel_dir TEXT NOT NULL,
    name    TEXT NOT NULL,
    ext     TEXT NOT NULL,
    size    INTEGER,
    mtime   REAL,
    pages   INTEGER,
    scan_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_library_dir ON library(root, rel_dir);

CREATE TABLE IF NOT EXISTS docs (
    path        TEXT PRIMARY KEY,
    favorite    INTEGER DEFAULT 0,
    last_opened REAL,
    page        INTEGER DEFAULT 0,
    offset      REAL DEFAULT 0,
    location    INTEGER,
    page_count  INTEGER,
    zoom        REAL,
    fit         TEXT,
    view_mode   TEXT,
    dark        INTEGER,
    rtl         INTEGER,
    font_size   REAL
);
CREATE INDEX IF NOT EXISTS idx_docs_recent ON docs(last_opened);

CREATE TABLE IF NOT EXISTS bookmarks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT NOT NULL,
    page     INTEGER NOT NULL,
    location INTEGER,
    title    TEXT,
    created  REAL
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_path ON bookmarks(path);

CREATE TABLE IF NOT EXISTS notes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT NOT NULL,
    page     INTEGER NOT NULL,
    location INTEGER,
    x        REAL,
    y        REAL,
    text     TEXT,
    created  REAL,
    updated  REAL
);
CREATE INDEX IF NOT EXISTS idx_notes_path ON notes(path);
"""

DOC_FIELDS = (
    "favorite", "last_opened", "page", "offset", "location", "page_count",
    "zoom", "fit", "view_mode", "dark", "rtl", "font_size",
)


def norm_path(p: str | os.PathLike) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(p)))


def connect(path: Path = DB_FILE) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


class Database:
    """Main-thread database facade."""

    def __init__(self, path: Path = DB_FILE):
        self.con = connect(path)
        self.con.executescript(SCHEMA)
        self.con.commit()

    # ------------------------------------------------------------------ docs
    def doc_state(self, path: str) -> dict:
        row = self.con.execute("SELECT * FROM docs WHERE path=?", (path,)).fetchone()
        return dict(row) if row else {}

    def update_doc(self, path: str, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in DOC_FIELDS}
        if not fields:
            return
        self.con.execute("INSERT OR IGNORE INTO docs(path) VALUES (?)", (path,))
        cols = ", ".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE docs SET {cols} WHERE path=?", (*fields.values(), path))
        self.con.commit()

    def touch_opened(self, path: str) -> None:
        self.update_doc(path, last_opened=time.time())

    def is_favorite(self, path: str) -> bool:
        row = self.con.execute("SELECT favorite FROM docs WHERE path=?", (path,)).fetchone()
        return bool(row and row[0])

    def set_favorite(self, path: str, value: bool) -> None:
        self.update_doc(path, favorite=1 if value else 0)

    def favorite_set(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT path FROM docs WHERE favorite=1")}

    def progress_map(self) -> dict[str, tuple[int, int]]:
        rows = self.con.execute(
            "SELECT path, page, page_count FROM docs WHERE page_count > 0 AND last_opened IS NOT NULL")
        return {r[0]: (r[1] or 0, r[2]) for r in rows}

    def recent(self, limit: int = 60) -> list[str]:
        rows = self.con.execute(
            "SELECT path FROM docs WHERE last_opened IS NOT NULL ORDER BY last_opened DESC LIMIT ?",
            (limit,))
        return [r[0] for r in rows]

    def clear_recent(self) -> None:
        self.con.execute("UPDATE docs SET last_opened=NULL")
        self.con.commit()

    def remove_recent(self, path: str) -> None:
        self.con.execute("UPDATE docs SET last_opened=NULL WHERE path=?", (path,))
        self.con.commit()

    def favorites(self) -> list[str]:
        rows = self.con.execute(
            "SELECT path FROM docs WHERE favorite=1 ORDER BY last_opened DESC, path")
        return [r[0] for r in rows]

    # --------------------------------------------------------------- library
    def library_rows(self, roots: list[str]) -> list[sqlite3.Row]:
        if not roots:
            return []
        q = ",".join("?" * len(roots))
        return self.con.execute(
            f"SELECT path, root, rel_dir, name, ext, size, mtime, pages FROM library WHERE root IN ({q})",
            roots).fetchall()

    def library_info(self, path: str) -> sqlite3.Row | None:
        return self.con.execute("SELECT * FROM library WHERE path=?", (path,)).fetchone()

    def set_pages(self, path: str, pages: int) -> None:
        self.con.execute("UPDATE library SET pages=? WHERE path=?", (pages, path))
        self.con.commit()

    def drop_roots_except(self, roots: list[str]) -> None:
        if roots:
            q = ",".join("?" * len(roots))
            self.con.execute(f"DELETE FROM library WHERE root NOT IN ({q})", roots)
        else:
            self.con.execute("DELETE FROM library")
        self.con.commit()

    # ------------------------------------------------------------- bookmarks
    def bookmarks(self, path: str) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM bookmarks WHERE path=? ORDER BY page, id", (path,))
        return [dict(r) for r in rows]

    def add_bookmark(self, path: str, page: int, title: str, location: int | None = None) -> int:
        cur = self.con.execute(
            "INSERT INTO bookmarks(path, page, location, title, created) VALUES (?,?,?,?,?)",
            (path, page, location, title, time.time()))
        self.con.commit()
        return cur.lastrowid

    def rename_bookmark(self, bid: int, title: str) -> None:
        self.con.execute("UPDATE bookmarks SET title=? WHERE id=?", (title, bid))
        self.con.commit()

    def delete_bookmark(self, bid: int) -> None:
        self.con.execute("DELETE FROM bookmarks WHERE id=?", (bid,))
        self.con.commit()

    # ----------------------------------------------------------------- notes
    def notes(self, path: str) -> list[dict]:
        rows = self.con.execute("SELECT * FROM notes WHERE path=? ORDER BY page, y, id", (path,))
        return [dict(r) for r in rows]

    def add_note(self, path: str, page: int, x: float, y: float, text: str,
                 location: int | None = None) -> int:
        now = time.time()
        cur = self.con.execute(
            "INSERT INTO notes(path, page, location, x, y, text, created, updated) VALUES (?,?,?,?,?,?,?,?)",
            (path, page, location, x, y, text, now, now))
        self.con.commit()
        return cur.lastrowid

    def update_note(self, nid: int, text: str) -> None:
        self.con.execute("UPDATE notes SET text=?, updated=? WHERE id=?", (text, time.time(), nid))
        self.con.commit()

    def move_note_page(self, nid: int, page: int) -> None:
        self.con.execute("UPDATE notes SET page=? WHERE id=?", (page, nid))
        self.con.commit()

    def delete_note(self, nid: int) -> None:
        self.con.execute("DELETE FROM notes WHERE id=?", (nid,))
        self.con.commit()
