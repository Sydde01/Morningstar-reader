"""Cover thumbnails: generated lazily in a process pool, cached as JPEG on disk."""
from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage, QPixmap

from .config import THUMB_DIR
from .docbackend import thumb_worker

THUMB_WIDTH = 360


def thumb_file(path: str, size: int | None = None, mtime: float | None = None) -> str:
    if size is None or mtime is None:
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0
    key = hashlib.sha1(f"{path}|{size}|{int(mtime or 0)}".encode("utf-8", "replace")).hexdigest()
    return str(THUMB_DIR / key[:2] / f"{key}.jpg")


class ThumbnailManager(QObject):
    ready = Signal(str)  # path
    pages_known = Signal(str, int)
    _done = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        workers = max(1, min(4, (os.cpu_count() or 2) - 1))
        self._pool: ProcessPoolExecutor | None = None
        self._workers = workers
        self._mem: OrderedDict[str, QPixmap] = OrderedDict()
        self._mem_limit = 900
        self._pending: OrderedDict[str, tuple[str, int, float]] = OrderedDict()
        self._inflight: set[str] = set()
        self._failed: set[str] = set()
        self._done.connect(self._on_done)

    def _ensure_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=self._workers)
        return self._pool

    def get(self, path: str, size: int | None = None, mtime: float | None = None) -> QPixmap | None:
        """Return the cover if available now, otherwise schedule it and return None."""
        pm = self._mem.get(path)
        if pm is not None:
            self._mem.move_to_end(path)
            return pm
        if path in self._failed:
            return None
        f = thumb_file(path, size, mtime)
        if os.path.exists(f):
            img = QImage(f)
            if not img.isNull():
                pm = QPixmap.fromImage(img)
                self._remember(path, pm)
                return pm
        if path in self._inflight:
            return None
        self._pending[path] = (f, size or 0, mtime or 0)
        self._pending.move_to_end(path)
        self._pump()
        return None

    def is_failed(self, path: str) -> bool:
        return path in self._failed

    def forget(self, path: str) -> None:
        self._mem.pop(path, None)
        self._failed.discard(path)

    def clear_memory(self) -> None:
        self._mem.clear()
        self._failed.clear()

    def _remember(self, path: str, pm: QPixmap) -> None:
        self._mem[path] = pm
        while len(self._mem) > self._mem_limit:
            self._mem.popitem(last=False)

    def _pump(self) -> None:
        while self._pending and len(self._inflight) < self._workers * 2:
            path, (f, _s, _m) = self._pending.popitem(last=True)  # newest request first
            os.makedirs(os.path.dirname(f), exist_ok=True)
            self._inflight.add(path)
            fut = self._ensure_pool().submit(thumb_worker, path, f, THUMB_WIDTH)
            fut.add_done_callback(lambda fu: self._done.emit(fu))

    def _on_done(self, fut) -> None:
        try:
            path, f, pages, err = fut.result()
        except Exception:  # noqa: BLE001 - broken pool etc.
            self._pool = None
            self._inflight.clear()
            self._pump()
            return
        self._inflight.discard(path)
        if err or not os.path.exists(f):
            self._failed.add(path)
        else:
            img = QImage(f)
            if not img.isNull():
                self._remember(path, QPixmap.fromImage(img))
            if pages:
                self.pages_known.emit(path, pages)
        self.ready.emit(path)
        self._pump()

    def drop_pending(self) -> None:
        self._pending.clear()

    def shutdown(self) -> None:
        self._pending.clear()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
