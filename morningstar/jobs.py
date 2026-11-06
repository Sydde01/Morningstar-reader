"""Run long conversions in a separate process, with progress and cancellation."""
from __future__ import annotations

import multiprocessing as mp
import queue as _queue

from PySide6.QtCore import QObject, QTimer, Signal

from . import convert


class Job(QObject):
    progress = Signal(float, int)       # overall fraction, item index
    itemDone = Signal(int, str)         # item index, output path
    itemError = Signal(int, str)        # item index, message
    finished = Signal(bool)             # True if completed, False if cancelled / crashed

    def __init__(self, kind: str, items: list, opts: dict, parent=None):
        super().__init__(parent)
        ctx = mp.get_context("spawn")
        self._queue = ctx.Queue()
        self._cancel = ctx.Event()
        self._proc = ctx.Process(target=convert.run_job, args=(kind, items, opts, self._queue, self._cancel),
                                 daemon=True)
        self._timer = QTimer(self, interval=80, timeout=self._poll)
        self._ended = False

    def start(self) -> None:
        self._proc.start()
        self._timer.start()

    def cancel(self) -> None:
        self._cancel.set()

    def is_running(self) -> bool:
        return not self._ended

    def _poll(self) -> None:
        while True:
            try:
                msg = self._queue.get_nowait()
            except _queue.Empty:
                break
            kind = msg[0]
            if kind == "progress":
                self.progress.emit(msg[1], msg[2])
            elif kind == "item_done":
                self.itemDone.emit(msg[1], msg[2])
            elif kind == "item_error":
                self.itemError.emit(msg[1], msg[2])
            elif kind in ("finished", "cancelled"):
                self._end(kind == "finished")
                return
        if not self._proc.is_alive() and not self._ended:
            self._end(False)

    def _end(self, ok: bool) -> None:
        self._ended = True
        self._timer.stop()
        self._proc.join(2)
        self.finished.emit(ok)

    def kill(self) -> None:
        if self._proc.is_alive():
            self._cancel.set()
            self._proc.join(3)
            if self._proc.is_alive():
                self._proc.terminate()
        self._ended = True
        self._timer.stop()
