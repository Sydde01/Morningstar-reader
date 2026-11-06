"""Background library scanner (pure filesystem walk, no document parsing)."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QThread, Signal

from .config import DOC_EXTS
from .db import connect, norm_path

SKIP_DIRS = {"$recycle.bin", "system volume information", "__macosx"}

UPSERT = """
INSERT INTO library(path, root, rel_dir, name, ext, size, mtime, pages, scan_id)
VALUES (?,?,?,?,?,?,?,NULL,?)
ON CONFLICT(path) DO UPDATE SET
    root=excluded.root, rel_dir=excluded.rel_dir, name=excluded.name, ext=excluded.ext,
    pages=CASE WHEN library.size=excluded.size AND library.mtime=excluded.mtime
               THEN library.pages ELSE NULL END,
    size=excluded.size, mtime=excluded.mtime, scan_id=excluded.scan_id
"""


class LibraryScanner(QThread):
    progress = Signal(int)
    done = Signal(int)

    def __init__(self, roots: list[str], parent=None):
        super().__init__(parent)
        self.roots = [norm_path(r) for r in roots]
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        con = connect()
        scan_id = int(time.time() * 1000)
        total = 0
        batch: list[tuple] = []
        try:
            for root in self.roots:
                if not os.path.isdir(root):
                    continue  # offline drive: keep the previous index
                complete = True
                stack = [root]
                while stack:
                    if self._stop:
                        complete = False
                        break
                    folder = stack.pop()
                    rel = os.path.relpath(folder, root)
                    rel = "" if rel == "." else rel.replace(os.sep, "/")
                    try:
                        entries = list(os.scandir(folder))
                    except OSError:
                        continue
                    for e in entries:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                n = e.name.lower()
                                if not n.startswith(".") and n not in SKIP_DIRS:
                                    stack.append(e.path)
                                continue
                            ext = os.path.splitext(e.name)[1].lower()
                            if ext not in DOC_EXTS:
                                continue
                            st = e.stat()
                        except OSError:
                            continue
                        name = os.path.splitext(e.name)[0]
                        batch.append((e.path, root, rel, name, ext, st.st_size, st.st_mtime, scan_id))
                        total += 1
                        if len(batch) >= 500:
                            con.executemany(UPSERT, batch)
                            con.commit()
                            batch.clear()
                            self.progress.emit(total)
                if batch:
                    con.executemany(UPSERT, batch)
                    con.commit()
                    batch.clear()
                if complete:
                    con.execute("DELETE FROM library WHERE root=? AND scan_id<>?", (root, scan_id))
                    con.commit()
        finally:
            con.close()
        self.done.emit(total)
