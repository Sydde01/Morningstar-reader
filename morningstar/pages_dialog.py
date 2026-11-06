"""Page organizer: reorder, rotate, delete, duplicate, insert, split and extract pages."""
from __future__ import annotations

import os
from dataclasses import dataclass

import pymupdf
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout)

from . import theme
from .config import DOC_EXTS
from .docbackend import open_document
from .i18n import tr
from .icons import icon

THUMB = 130


@dataclass
class PageItem:
    src: int            # index in PagesDialog.sources (-1 = blank page)
    page: int = 0
    rot: int = 0        # extra rotation (degrees)
    half: str = ""      # "" | "L" | "R"  (split double page)
    size: tuple = (595.0, 842.0)


class PagesDialog(QDialog):
    def __init__(self, doc: pymupdf.Document, parent=None, rtl: bool = False, title: str = ""):
        super().__init__(parent)
        self.setWindowTitle(tr("Organize pages") + (f" — {title}" if title else ""))
        self.resize(1100, 760)
        self.sources: list[pymupdf.Document] = [doc]
        self._own: list[pymupdf.Document] = []
        self.result_doc: pymupdf.Document | None = None
        self._thumbs: dict[tuple, QPixmap] = {}
        self._queue: list[QListWidgetItem] = []

        lay = QVBoxLayout(self)
        tip = QLabel(tr("Drag pages to reorder them. Ctrl/Shift-click to select several."), objectName="dim")
        lay.addWidget(tip)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setIconSize(QSize(THUMB, int(THUMB * 1.42)))
        self.list.setGridSize(QSize(THUMB + 26, int(THUMB * 1.42) + 34))
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Snap)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setSpacing(6)
        self.list.setUniformItemSizes(True)
        self.list.setStyleSheet(f"QListWidget {{ background: {theme.CANVAS}; }}")
        self.list.model().rowsMoved.connect(lambda *_: self._renumber())
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()

        def btn(ic, text, slot):
            b = QPushButton(icon(ic), text)
            b.setAutoDefault(False)
            b.clicked.connect(slot)
            row.addWidget(b)
            return b

        btn("rotate", tr("Rotate left"), lambda: self._rotate(-90))
        btn("rotate", tr("Rotate right"), lambda: self._rotate(90))
        btn("trash", tr("Delete"), self._delete)
        btn("copy", tr("Duplicate"), self._duplicate)
        btn("fit_page", tr("Blank page"), self._blank)
        btn("open", tr("Insert a file…"), self._insert_file)
        btn("scissors", tr("Split in two"), self._split)
        btn("double", tr("Select double pages"), self._select_doubles)
        btn("save_as", tr("Extract selection…"), self._extract)
        row.addStretch(1)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        self.rtl = QCheckBox(tr("Right-to-left (manga): right half first when splitting"))
        self.rtl.setChecked(rtl)
        row2.addWidget(self.rtl)
        row2.addStretch(1)
        self.count = QLabel(objectName="dim")
        row2.addWidget(self.count)
        lay.addLayout(row2)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("Apply"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self._apply)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.list.keyPressEvent = self._list_key(self.list.keyPressEvent)

        for i in range(doc.page_count):
            r = doc.load_page(i).rect
            self._add_item(PageItem(0, i, size=(r.width, r.height)))
        self._renumber()
        self._timer = QTimer(self, interval=0, timeout=self._render_some)
        self._timer.start()

    # --------------------------------------------------------------- items
    def _list_key(self, orig):
        def handler(ev):
            if ev.key() == Qt.Key_Delete:
                self._delete()
            else:
                orig(ev)
        return handler

    def _add_item(self, pi: PageItem, row: int | None = None) -> QListWidgetItem:
        it = QListWidgetItem()
        it.setData(Qt.UserRole, pi)
        it.setFlags(it.flags() | Qt.ItemIsDragEnabled)
        it.setFlags(it.flags() & ~Qt.ItemIsDropEnabled)
        it.setIcon(self._placeholder(pi))
        if row is None:
            self.list.addItem(it)
        else:
            self.list.insertItem(row, it)
        self._queue.append(it)
        if hasattr(self, "_timer") and not self._timer.isActive():
            self._timer.start()
        return it

    def _items(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(self.list.count())]

    def _renumber(self) -> None:
        for i, it in enumerate(self._items()):
            pi: PageItem = it.data(Qt.UserRole)
            suffix = {"L": " ◧", "R": " ◨"}.get(pi.half, "")
            it.setText(f"{i + 1}{suffix}")
        self.count.setText(tr("{n} pages", n=self.list.count()))

    def _placeholder(self, pi: PageItem) -> QPixmap:
        w, h = self._disp_size(pi)
        s = THUMB / max(w, h) * (1.0 if h >= w else 1.0)
        pm = QPixmap(int(max(20, w * s * (1.42 if h > w else 1))), int(max(20, h * s * (1.42 if h > w else 1))))
        pm.fill(QColor("#ffffff" if pi.src == -1 else "#2a2a31"))
        return pm

    def _disp_size(self, pi: PageItem) -> tuple[float, float]:
        w, h = pi.size
        if pi.half:
            w = w / 2
        if pi.rot % 180:
            w, h = h, w
        return w, h

    def _render_some(self) -> None:
        for _ in range(3):
            if not self._queue:
                self._timer.stop()
                return
            it = self._queue.pop(0)
            try:
                pi: PageItem = it.data(Qt.UserRole)
            except RuntimeError:
                continue
            it.setIcon(self._thumb(pi))

    def _thumb(self, pi: PageItem) -> QPixmap:
        key = (pi.src, pi.page, pi.rot, pi.half)
        pm = self._thumbs.get(key)
        if pm is not None:
            return pm
        if pi.src == -1:
            w, h = self._disp_size(pi)
            s = THUMB * 1.42 / max(w, h)
            pm = QPixmap(int(w * s), int(h * s))
            pm.fill(QColor("white"))
        else:
            page = self.sources[pi.src].load_page(pi.page)
            r = page.rect
            clip = None
            if pi.half:
                mid = r.width / 2
                clip = pymupdf.Rect(0, 0, mid, r.height) if pi.half == "L" else pymupdf.Rect(mid, 0, r.width, r.height)
            area = clip or r
            s = THUMB * 1.42 / max(area.width, area.height)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(s, s), clip=clip, alpha=False)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
            if pi.rot:
                img = img.transformed(QTransform().rotate(pi.rot))
            pm = QPixmap.fromImage(img)
        self._thumbs[key] = pm
        return pm

    # -------------------------------------------------------------- actions
    def _selected(self) -> list[QListWidgetItem]:
        return sorted(self.list.selectedItems(), key=self.list.row)

    def _rotate(self, deg: int) -> None:
        for it in self._selected():
            pi: PageItem = it.data(Qt.UserRole)
            pi.rot = (pi.rot + deg) % 360
            it.setData(Qt.UserRole, pi)
            it.setIcon(self._thumb(pi))

    def _delete(self) -> None:
        for it in self._selected():
            self.list.takeItem(self.list.row(it))
        self._renumber()

    def _duplicate(self) -> None:
        for it in reversed(self._selected()):
            pi: PageItem = it.data(Qt.UserRole)
            self._add_item(PageItem(pi.src, pi.page, pi.rot, pi.half, pi.size), self.list.row(it) + 1)
        self._renumber()

    def _blank(self) -> None:
        sel = self._selected()
        row = self.list.row(sel[-1]) + 1 if sel else self.list.count()
        ref = sel[-1].data(Qt.UserRole) if sel else None
        size = self._disp_size(ref) if ref else (595.0, 842.0)
        self._add_item(PageItem(-1, 0, size=size), row)
        self._renumber()

    def _insert_file(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(DOC_EXTS)) + " *.png *.jpg *.jpeg *.webp"
        files, _ = QFileDialog.getOpenFileNames(self, tr("Insert a file…"), "", f"Documents ({exts})")
        sel = self._selected()
        row = self.list.row(sel[-1]) + 1 if sel else self.list.count()
        for f in files:
            try:
                d = open_document(f, font_size=11)
                if not d.is_pdf:
                    d = pymupdf.open("pdf", d.convert_to_pdf())
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, tr("Insert a file…"), f"{os.path.basename(f)}:\n{e}")
                continue
            self.sources.append(d)
            self._own.append(d)
            si = len(self.sources) - 1
            for i in range(d.page_count):
                r = d.load_page(i).rect
                self._add_item(PageItem(si, i, size=(r.width, r.height)), row)
                row += 1
        self._renumber()

    def _split(self) -> None:
        rtl = self.rtl.isChecked()
        for it in reversed(self._selected()):
            pi: PageItem = it.data(Qt.UserRole)
            if pi.half or pi.src == -1:
                continue
            row = self.list.row(it)
            self.list.takeItem(row)
            order = ("R", "L") if rtl else ("L", "R")
            for k, h in enumerate(order):
                self._add_item(PageItem(pi.src, pi.page, pi.rot, h, pi.size), row + k)
        self._renumber()

    def _select_doubles(self) -> None:
        self.list.clearSelection()
        for it in self._items():
            pi: PageItem = it.data(Qt.UserRole)
            w, h = self._disp_size(pi)
            if w > h * 1.15 and not pi.half:
                it.setSelected(True)

    def _extract(self) -> None:
        sel = self._selected()
        if not sel:
            return
        f, _ = QFileDialog.getSaveFileName(self, tr("Extract selection…"), "", "PDF (*.pdf)")
        if not f:
            return
        out = self._build([it.data(Qt.UserRole) for it in sel])
        out.save(f, garbage=3, deflate=True)
        out.close()
        QMessageBox.information(self, tr("Extract selection…"), tr("Saved: {f}", f=os.path.basename(f)))

    # -------------------------------------------------------------- build
    def _build(self, items: list[PageItem]) -> pymupdf.Document:
        out = pymupdf.open()
        base = self.sources[0]
        first_new: dict[int, int] = {}
        for pi in items:
            if pi.src == -1:
                w, h = pi.size
                p = out.new_page(width=w, height=h)
            elif pi.half:
                src = self.sources[pi.src]
                r = src.load_page(pi.page).rect
                mid = r.width / 2
                clip = pymupdf.Rect(0, 0, mid, r.height) if pi.half == "L" else pymupdf.Rect(mid, 0, r.width, r.height)
                p = out.new_page(width=clip.width, height=clip.height)
                p.show_pdf_page(p.rect, src, pi.page, clip=clip)
            else:
                out.insert_pdf(self.sources[pi.src], from_page=pi.page, to_page=pi.page)
                p = out[-1]
            if pi.src == 0 and pi.page not in first_new:
                first_new[pi.page] = out.page_count - 1
            if pi.rot:
                p.set_rotation((p.rotation + pi.rot) % 360)
        try:
            toc = [[lvl, t, first_new[pg - 1] + 1] for lvl, t, pg in base.get_toc(simple=True)
                   if pg - 1 in first_new]
            level = 0
            clean = []
            for lvl, t, pg in toc:  # keep a valid hierarchy
                lvl = min(lvl, level + 1)
                clean.append([lvl, t, pg])
                level = lvl
            if clean:
                out.set_toc(clean)
        except Exception:  # noqa: BLE001
            pass
        out.set_metadata(base.metadata or {})
        return out

    def _apply(self) -> None:
        items = [it.data(Qt.UserRole) for it in self._items()]
        if not items:
            QMessageBox.warning(self, tr("Organize pages"), tr("A document needs at least one page."))
            return
        self.result_doc = self._build(items)
        self.accept()

    def done(self, r: int) -> None:
        self._timer.stop()
        for d in self._own:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
        super().done(r)
