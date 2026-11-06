"""Editing mode for a DocumentTab: tools, form filling, undo/redo and saving with .bak backups."""
from __future__ import annotations

import os

import pymupdf
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox,
                               QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import editing, theme
from .config import DATA_DIR
from .i18n import tr
from .icons import icon

SIGN_DIR = DATA_DIR / "signatures"
UNDO_BUDGET = 700 * 1024 * 1024
MOVABLE = {"FreeText", "Square", "Circle", "Stamp", "Text", "FileAttachment"}
DRAG_TOOLS = {"rect", "ellipse", "arrow", "whiteout", "redact", "place", "ink"}
TEXT_MARKUP = {"highlight", "underline", "strike"}

TOOLS = [
    ("select", "pointer", "Select / move"),
    ("text", "text", "Add text"),
    ("edittext", "edit_text", "Edit existing text"),
    ("highlight", "highlight", "Highlight"),
    ("underline", "underline", "Underline"),
    ("strike", "strike", "Strike out"),
    ("ink", "pen", "Draw"),
    ("rect", "square", "Rectangle"),
    ("ellipse", "circle", "Ellipse"),
    ("arrow", "arrow", "Arrow"),
    ("whiteout", "eraser", "White-out (erase an area)"),
    ("redact", "redact", "Redact (black out, permanently)"),
]


def _rgb(hex_color: str) -> tuple[float, float, float]:
    c = QColor(hex_color)
    return c.redF(), c.greenF(), c.blueF()


# ============================================================================ dialogs
class SignaturePad(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(520, 200)
        self.paths: list[QPainterPath] = []
        self.color = QColor("#0b2a7a")
        self.setCursor(Qt.CrossCursor)

    def clear(self) -> None:
        self.paths = []
        self.update()

    def mousePressEvent(self, e):
        path = QPainterPath(e.position())
        self.paths.append(path)

    def mouseMoveEvent(self, e):
        if self.paths and e.buttons() & Qt.LeftButton:
            self.paths[-1].lineTo(e.position())
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#ffffff"))
        p.setPen(QPen(QColor("#d0d0d0"), 1, Qt.DashLine))
        p.drawLine(20, self.height() - 40, self.width() - 20, self.height() - 40)
        p.setPen(QPen(self.color, 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        for path in self.paths:
            p.drawPath(path)

    def to_png(self) -> bytes | None:
        if not self.paths:
            return None
        bounds = QRectF()
        for path in self.paths:
            bounds = bounds.united(path.boundingRect())
        bounds = bounds.adjusted(-6, -6, 6, 6)
        scale = 3
        img = QImage(int(bounds.width() * scale), int(bounds.height() * scale), QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(scale, scale)
        p.translate(-bounds.topLeft())
        p.setPen(QPen(self.color, 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        for path in self.paths:
            p.drawPath(path)
        p.end()
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        return bytes(ba)


class SignatureDialog(QDialog):
    """Draw or load a signature, keep it for later, and return PNG bytes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Signature"))
        self.result_png: bytes | None = None
        SIGN_DIR.mkdir(parents=True, exist_ok=True)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Saved signatures"), objectName="dim"))
        self.saved = QListWidget()
        self.saved.setViewMode(QListWidget.IconMode)
        self.saved.setIconSize(QSize(160, 60))
        self.saved.setFixedHeight(96)
        self.saved.itemDoubleClicked.connect(lambda _it: self._use_saved())
        lay.addWidget(self.saved)
        lay.addWidget(QLabel(tr("Draw a new signature:"), objectName="dim"))
        self.pad = SignaturePad()
        lay.addWidget(self.pad)
        row = QHBoxLayout()
        b = QPushButton(icon("trash"), tr("Clear"))
        b.clicked.connect(self.pad.clear)
        row.addWidget(b)
        b = QPushButton(icon("image"), tr("Load an image…"))
        b.clicked.connect(self._load)
        row.addWidget(b)
        self.del_btn = QPushButton(icon("trash"), tr("Delete saved"))
        self.del_btn.clicked.connect(self._delete_saved)
        row.addWidget(self.del_btn)
        row.addStretch(1)
        self.keep = QCheckBox(tr("Keep for later"))
        self.keep.setChecked(True)
        row.addWidget(self.keep)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("Use"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill_saved()

    def _fill_saved(self) -> None:
        self.saved.clear()
        for f in sorted(SIGN_DIR.glob("*.png"), key=lambda p: -p.stat().st_mtime):
            pm = QPixmap(str(f))
            if pm.isNull():
                continue
            bg = QPixmap(160, 60)
            bg.fill(QColor("white"))
            p = QPainter(bg)
            s = pm.scaled(150, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap((160 - s.width()) // 2, (60 - s.height()) // 2, s)
            p.end()
            it = QListWidgetItem(bg, "")
            it.setData(Qt.UserRole, str(f))
            self.saved.addItem(it)
        self.del_btn.setEnabled(self.saved.count() > 0)

    def _load(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, tr("Load an image…"), "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if f:
            with open(f, "rb") as fh:
                self.result_png = fh.read()
            if self.keep.isChecked():
                self._store(self.result_png)
            self.accept()

    def _store(self, data: bytes) -> None:
        import time
        (SIGN_DIR / f"sig_{int(time.time() * 1000)}.png").write_bytes(data)

    def _use_saved(self) -> None:
        it = self.saved.currentItem()
        if it is not None:
            with open(it.data(Qt.UserRole), "rb") as fh:
                self.result_png = fh.read()
            self.accept()

    def _delete_saved(self) -> None:
        it = self.saved.currentItem()
        if it is not None:
            os.remove(it.data(Qt.UserRole))
            self._fill_saved()

    def _accept(self) -> None:
        data = self.pad.to_png()
        if data is None:
            if self.saved.currentItem() is not None:
                return self._use_saved()
            return
        self.result_png = data
        if self.keep.isChecked():
            self._store(data)
        self.accept()


class TextEditDialog(QDialog):
    """Edit one line of text: wording, font (any installed font or the document's own), size, style, color."""
    ORIGINAL = "__original__"

    def __init__(self, parent, text: str, size: float, color: str, family: str, bold: bool, italic: bool,
                 original: str | None = None):
        super().__init__(parent)
        from .fonts import installed_families

        self.setWindowTitle(tr("Edit text"))
        self.resize(640, 300)
        form = QFormLayout(self)
        self.text = QLineEdit(text)
        self.text.textChanged.connect(self._preview)
        form.addRow(tr("Text"), self.text)

        self.font = QComboBox()
        self.font.setEditable(True)
        self.font.setInsertPolicy(QComboBox.NoInsert)
        self.font.setMaxVisibleItems(18)
        if original:
            self.font.addItem(tr("Document font ({f})", f=original), self.ORIGINAL)
        for fam in installed_families():
            self.font.addItem(fam, fam)
            self.font.setItemData(self.font.count() - 1, QFont(fam, 10), Qt.FontRole)
        idx = 0 if original else max(0, self.font.findData(family))
        self.font.setCurrentIndex(idx)
        self.font.completer().setCompletionMode(self.font.completer().CompletionMode.PopupCompletion)
        self.font.completer().setFilterMode(Qt.MatchContains)
        self.font.currentIndexChanged.connect(self._preview)
        self.fallback_family = family
        form.addRow(tr("Font"), self.font)

        row = QHBoxLayout()
        self.size = QDoubleSpinBox()
        self.size.setRange(4, 120)
        self.size.setValue(size)
        self.size.setSuffix(" pt")
        self.size.valueChanged.connect(self._preview)
        row.addWidget(self.size)
        self.bold = QCheckBox(tr("Bold"))
        self.bold.setChecked(bold)
        self.italic = QCheckBox(tr("Italic"))
        self.italic.setChecked(italic)
        for cb in (self.bold, self.italic):
            cb.toggled.connect(self._preview)
            row.addWidget(cb)
        self.color = color
        self.color_btn = QPushButton()
        self.color_btn.setFixedWidth(60)
        self._paint_color()
        self.color_btn.clicked.connect(self._pick)
        row.addWidget(self.color_btn)
        row.addStretch(1)
        form.addRow(tr("Style"), row)

        self.preview = QLabel()
        self.preview.setMinimumHeight(56)
        self.preview.setStyleSheet("background: white; padding: 8px; border-radius: 4px;")
        form.addRow(tr("Preview"), self.preview)
        self.warn = QLabel(objectName="dim", wordWrap=True)
        form.addRow("", self.warn)
        form.addRow("", QLabel(tr("Leave empty to delete the line."), objectName="dim"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("Apply"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.text.selectAll()
        self._preview()

    def family(self) -> str:
        data = self.font.currentData()
        if data:
            return data
        typed = self.font.currentText().strip()
        idx = self.font.findText(typed, Qt.MatchFixedString)
        return self.font.itemData(idx) if idx >= 0 else self.fallback_family

    def _preview(self) -> None:
        fam = self.family()
        f = QFont(self.fallback_family if fam == self.ORIGINAL else fam)
        f.setPointSizeF(max(6.0, min(40.0, self.size.value())))
        f.setBold(self.bold.isChecked())
        f.setItalic(self.italic.isChecked())
        self.preview.setFont(f)
        self.preview.setText(self.text.text() or " ")
        pal = self.preview.palette()
        self.preview.setStyleSheet(f"background: white; color: {self.color}; padding: 8px; border-radius: 4px;")
        del pal
        self.warn.setText(tr("The document font is often a subset: if a character is missing, "
                             "the closest installed font is used instead.") if fam == self.ORIGINAL else "")

    def _paint_color(self) -> None:
        self.color_btn.setStyleSheet(f"QPushButton {{ background: {self.color}; border: 1px solid #555; }}")

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self.color), self)
        if c.isValid():
            self.color = c.name()
            self._paint_color()
            self._preview()


class MetadataDialog(QDialog):
    FIELDS = ("title", "author", "subject", "keywords")

    def __init__(self, parent, meta: dict):
        super().__init__(parent)
        self.setWindowTitle(tr("Document properties"))
        self.resize(460, 200)
        form = QFormLayout(self)
        self.edits = {}
        labels = {"title": tr("Title"), "author": tr("Author"), "subject": tr("Subject"), "keywords": tr("Keywords")}
        for k in self.FIELDS:
            e = QLineEdit((meta or {}).get(k) or "")
            form.addRow(labels[k], e)
            self.edits[k] = e
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self) -> dict:
        return {k: e.text().strip() for k, e in self.edits.items()}


class PasswordDialog(QDialog):
    def __init__(self, parent, has_password: bool):
        super().__init__(parent)
        self.setWindowTitle(tr("Password"))
        form = QFormLayout(self)
        self.pw = QLineEdit(echoMode=QLineEdit.Password)
        self.pw2 = QLineEdit(echoMode=QLineEdit.Password)
        form.addRow(tr("New password"), self.pw)
        form.addRow(tr("Confirm"), self.pw2)
        self.remove = QCheckBox(tr("Remove the password"))
        self.remove.setVisible(has_password)
        form.addRow("", self.remove)
        form.addRow("", QLabel(tr("Applied when you save (AES-256)."), objectName="dim"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _ok(self) -> None:
        if not self.remove.isChecked() and (not self.pw.text() or self.pw.text() != self.pw2.text()):
            QMessageBox.warning(self, tr("Password"), tr("The passwords are empty or different."))
            return
        self.accept()


# ======================================================================= controller
class _InlineFilter(QObject):
    def __init__(self, ctrl: "EditController"):
        super().__init__(ctrl)
        self.ctrl = ctrl

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.FocusOut:
            self.ctrl._commit_inline()
        elif ev.type() == QEvent.KeyPress:
            if ev.key() == Qt.Key_Escape:
                self.ctrl._cancel_inline()
                return True
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
                multiline = isinstance(obj, QPlainTextEdit)
                if not multiline or ev.modifiers() & Qt.ControlModifier:
                    self.ctrl._commit_inline()
                    return True
        return False


class EditController(QObject):
    dirtyChanged = Signal(bool)
    saved = Signal(str, str)  # old path, new path

    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self.canvas = tab.canvas
        self.canvas.interactor = self
        self.active = False
        self.kind: str | None = None
        self.dirty = False
        self.undo_stack: list[bytes] = []
        self.redo_stack: list[bytes] = []
        self._saved_len = 0
        self.tool = "select"
        self.colors = {"highlight": "#ffd400", "underline": "#e0303f", "strike": "#e0303f", "ink": "#e0303f",
                       "rect": "#e0303f", "ellipse": "#e0303f", "arrow": "#e0303f", "text": "#000000",
                       "select": "#e0303f"}
        self.width = 2.0
        self.font_size = 12
        self.save_opts = {"compress": False, "password": None, "remove_password": False}
        self.fonts_used = False
        self._drag_start: pymupdf.Point | None = None
        self._drag_cur: pymupdf.Point | None = None
        self._stroke: list[pymupdf.Point] = []
        self._sel: dict | None = None
        self._hover: tuple[int, pymupdf.Rect] | None = None
        self._pending_image: bytes | None = None
        self._inline: QWidget | None = None
        self._inline_info: dict | None = None
        self._inline_filter = _InlineFilter(self)
        self._fields: dict[int, list] = {}
        self._lines: dict[int, list] = {}
        self.toolbar: QToolBar | None = None

    # ------------------------------------------------------------ helpers
    @property
    def doc(self) -> pymupdf.Document:
        return self.tab.doc

    def _page(self, pno: int) -> pymupdf.Page:
        return self.doc.load_page(pno)

    @staticmethod
    def _to_unrot(page: pymupdf.Page, obj):
        return obj * page.derotation_matrix

    @staticmethod
    def _to_disp(page: pymupdf.Page, obj):
        return obj * page.rotation_matrix

    def has_fields(self) -> bool:
        doc = self.doc
        key = id(doc)
        if getattr(self, "_fields_key", None) != key:
            try:
                self._has_fields = bool(doc is not None and doc.is_pdf and doc.is_form_pdf)
            except Exception:  # noqa: BLE001
                self._has_fields = False
            self._fields_key = key
            self._fields.clear()
        return self._has_fields

    def _invalidate(self, pno: int) -> None:
        self._fields.pop(pno, None)
        self._lines.pop(pno, None)
        self.canvas.invalidate_page(pno)

    def _set_dirty(self, on: bool) -> None:
        if on != self.dirty:
            self.dirty = on
            self.dirtyChanged.emit(on)
        self._sync_undo_actions()

    # --------------------------------------------------------------- undo
    def checkpoint(self) -> None:
        """Snapshot the document before a modification (cheap enough for typical documents)."""
        data = self.doc.tobytes(garbage=0)
        self.undo_stack.append(data)
        self.redo_stack.clear()
        total = sum(len(d) for d in self.undo_stack)
        while total > UNDO_BUDGET and len(self.undo_stack) > 1:
            total -= len(self.undo_stack.pop(0))
            self._saved_len -= 1
        self._set_dirty(True)

    def _restore(self, data: bytes) -> None:
        new = pymupdf.open("pdf", data)
        old = self.doc
        self.tab.doc = new
        self._fields.clear()
        self._lines.clear()
        self._sel = None
        self.canvas.replace_document(new)
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass
        self.tab.on_document_replaced()

    def undo(self) -> None:
        if not self.undo_stack:
            return
        self.redo_stack.append(self.doc.tobytes(garbage=0))
        self._restore(self.undo_stack.pop())
        self._set_dirty(len(self.undo_stack) != self._saved_len)

    def redo(self) -> None:
        if not self.redo_stack:
            return
        self.undo_stack.append(self.doc.tobytes(garbage=0))
        self._restore(self.redo_stack.pop())
        self._set_dirty(len(self.undo_stack) != self._saved_len)

    def replace_with(self, new_doc: pymupdf.Document) -> None:
        """Used by the page organizer: the whole document is replaced (undoable)."""
        self.checkpoint()
        old = self.doc
        self.tab.doc = new_doc
        self._fields.clear()
        self._lines.clear()
        self._sel = None
        self.canvas.replace_document(new_doc)
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass
        self.tab.on_document_replaced()

    # ---------------------------------------------------------- edit mode
    def can_edit(self) -> bool:
        return self.doc.is_pdf or self.tab.comic

    def start(self) -> bool:
        if self.active:
            return True
        if self.doc.is_pdf:
            from .signing import has_signatures

            if has_signatures(self.doc) and QMessageBox.question(
                    self.tab, tr("Edit"),
                    tr("This document is digitally signed: saving changes will invalidate its signatures. "
                       "Continue?")) != QMessageBox.Yes:
                return False
            self.kind = "pdf"
        elif self.tab.comic:
            self.kind = "comic"
            work = pymupdf.open("pdf", self.doc.convert_to_pdf())
            old = self.doc
            self.tab.doc = work
            self.canvas.replace_document(work)
            old.close()
        else:
            return False
        self.active = True
        self._build_toolbar()
        self.toolbar.show()
        self.set_tool("select")
        return True

    def stop(self) -> bool:
        """Leave edit mode. Returns False if the user cancelled."""
        if not self.active:
            return True
        if self.dirty and not self.ask_save():
            return False
        self._commit_inline()
        self.active = False
        self.canvas.tool = None
        self._sel = None
        self._hover = None
        if self.toolbar is not None:
            self.toolbar.hide()
        if self.kind == "comic" or self.dirty:
            self.tab.reopen()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._saved_len = 0
        self._set_dirty(False)
        self.kind = None
        self.canvas.viewport().update()
        return True

    def ask_save(self) -> bool:
        """Unsaved changes prompt. True = continue (saved or discarded), False = cancel."""
        box = QMessageBox(self.tab)
        box.setWindowTitle(tr("Unsaved changes"))
        box.setText(tr("“{t}” has unsaved changes.", t=self.tab.title))
        save = box.addButton(tr("Save"), QMessageBox.AcceptRole)
        discard = box.addButton(tr("Don't save"), QMessageBox.DestructiveRole)
        box.addButton(tr("Cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(save)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save:
            return self.save()
        if clicked is discard:
            self.undo_stack.clear()
            self.redo_stack.clear()
            self._saved_len = 0
            self._set_dirty(False)
            self.tab.reopen()
            return True
        return False

    # ------------------------------------------------------------ toolbar
    def _build_toolbar(self) -> None:
        if self.toolbar is not None:
            self._sync_undo_actions()
            return
        tb = QToolBar()
        tb.setIconSize(QSize(18, 18))
        tb.setMovable(False)
        tb.setStyleSheet(f"QToolBar {{ background: #1f1416; border-bottom: 1px solid {theme.BORDER}; }}")
        self.toolbar = tb
        lbl = QLabel("  " + tr("Edit") + "  ")
        lbl.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 600;")
        tb.addWidget(lbl)
        self.tool_actions: dict[str, QAction] = {}
        for key, ic, text in TOOLS:
            if self.kind == "comic" and key in ("edittext", "highlight", "underline", "strike"):
                continue
            a = QAction(icon(ic), tr(text), self)
            a.setCheckable(True)
            a.triggered.connect(lambda _c=False, k=key: self.set_tool(k))
            tb.addAction(a)
            self.tool_actions[key] = a
        a = QAction(icon("image"), tr("Insert an image"), self)
        a.triggered.connect(self.pick_image)
        tb.addAction(a)
        if self.kind == "pdf":
            a = QAction(icon("signature"), tr("Signature"), self)
            a.triggered.connect(self.pick_signature)
            tb.addAction(a)
        tb.addSeparator()
        self.color_btn = QToolButton(toolTip=tr("Color"))
        self.color_btn.clicked.connect(self._pick_color)
        tb.addWidget(self.color_btn)
        self.width_spin = QDoubleSpinBox(toolTip=tr("Line width"))
        self.width_spin.setRange(0.5, 20)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setValue(self.width)
        self.width_spin.setFixedWidth(64)
        self.width_spin.valueChanged.connect(self._width_changed)
        tb.addWidget(self.width_spin)
        self.size_spin = QSpinBox(toolTip=tr("Text size"))
        self.size_spin.setRange(5, 96)
        self.size_spin.setValue(self.font_size)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setFixedWidth(70)
        self.size_spin.valueChanged.connect(self._size_changed)
        tb.addWidget(self.size_spin)
        tb.addSeparator()
        a = QAction(icon("pages"), tr("Organize pages…"), self)
        a.triggered.connect(self.tab.open_pages)
        tb.addAction(a)
        if self.kind == "pdf":
            doc_btn = QToolButton(icon=icon("tools"), toolTip=tr("Document"))
            doc_btn.setPopupMode(QToolButton.InstantPopup)
            m = QMenu(doc_btn)
            self.a_compress = m.addAction(icon("compress"), tr("Compress when saving"))
            self.a_compress.setCheckable(True)
            self.a_compress.toggled.connect(lambda on: self.save_opts.__setitem__("compress", on))
            m.addAction(icon("lock"), tr("Password…"), self.set_password)
            m.addAction(icon("tag"), tr("Properties (title, author)…"), self.edit_metadata)
            m.addAction(icon("layers"), tr("Flatten annotations and forms"), self.flatten)
            doc_btn.setMenu(m)
            tb.addWidget(doc_btn)
        tb.addSeparator()
        self.a_undo = QAction(icon("undo"), tr("Undo") + "  (Ctrl+Z)", self)
        self.a_undo.triggered.connect(self.undo)
        tb.addAction(self.a_undo)
        self.a_redo = QAction(icon("redo"), tr("Redo") + "  (Ctrl+Y)", self)
        self.a_redo.triggered.connect(self.redo)
        tb.addAction(self.a_redo)
        tb.addSeparator()
        self.a_save = QAction(icon("save", theme.ACCENT), tr("Save") + "  (Ctrl+S)", self)
        self.a_save.triggered.connect(lambda: self.save())
        tb.addAction(self.a_save)
        a = QAction(icon("save_as"), tr("Save as…") + "  (Ctrl+Shift+S)", self)
        a.triggered.connect(lambda: self.save(save_as=True))
        tb.addAction(a)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        done = QPushButton(icon("check"), tr("Close editing"))
        done.clicked.connect(self.tab.toggle_edit)
        tb.addWidget(done)
        for seq, slot in (("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo), ("Ctrl+Shift+Z", self.redo),
                          ("Ctrl+S", lambda: self.save()), ("Ctrl+Shift+S", lambda: self.save(save_as=True))):
            sc = QAction(self.tab)
            sc.setShortcut(QKeySequence(seq))
            sc.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            sc.triggered.connect(lambda _c=False, f=slot: f() if self.active else None)
            self.tab.addAction(sc)
        self.tab.layout().insertWidget(1, tb)
        self._sync_undo_actions()
        self._paint_color_btn()

    def _sync_undo_actions(self) -> None:
        if self.toolbar is None:
            return
        self.a_undo.setEnabled(bool(self.undo_stack))
        self.a_redo.setEnabled(bool(self.redo_stack))
        self.a_save.setEnabled(self.dirty)

    def begin_pick_rect(self, callback, hint: str) -> None:
        """Let the user drag an area on a page (used to place a digital signature)."""
        self._commit_inline()
        self._pick_cb = callback
        self._prev_tool = self.tool if self.active else None
        self.tool = "pickrect"
        self.canvas.tool = "pickrect"
        self.canvas.clear_selection()
        self.tab.flash(hint)

    def _end_pick(self) -> None:
        self._pick_cb = None
        if self.active:
            self.set_tool(self._prev_tool or "select")
        else:
            self.tool = "select"
            self.canvas.tool = None
        self.canvas.viewport().update()

    def set_tool(self, tool: str) -> None:
        self._commit_inline()
        self.tool = tool
        self.canvas.tool = tool
        self._sel = None if tool != "select" else self._sel
        self._hover = None
        for k, a in getattr(self, "tool_actions", {}).items():
            a.setChecked(k == tool)
        self._paint_color_btn()
        self.canvas.clear_selection()
        self.canvas.viewport().update()

    def _cur_color(self) -> str:
        return self.colors.get(self.tool, "#e0303f")

    def _paint_color_btn(self) -> None:
        if self.toolbar is None:
            return
        pm = QPixmap(18, 18)
        pm.fill(QColor(self._cur_color()))
        self.color_btn.setIcon(pm)

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self._cur_color()), self.tab, tr("Color"))
        if not c.isValid():
            return
        self.colors[self.tool] = c.name()
        self._paint_color_btn()
        if self._sel and self._sel["kind"] == "annot":
            self._restyle_selected(color=c.name())

    def _width_changed(self, v: float) -> None:
        self.width = v
        if self._sel and self._sel["kind"] == "annot":
            self._restyle_selected(width=v)

    def _size_changed(self, v: int) -> None:
        self.font_size = v
        if self._sel and self._sel["kind"] == "annot" and self._sel.get("type") == "FreeText":
            self._restyle_selected(size=v)

    # ---------------------------------------------------------- hit tests
    def _annot_at(self, pno: int, pt: pymupdf.Point) -> dict | None:
        page = self._page(pno)
        best = None
        for a in page.annots():
            r = self._to_disp(page, a.rect)
            if r.contains(pt) and a.type[1] not in ("Widget", "Link", "Popup"):
                if best is None or r.get_area() < best["rect"].get_area():
                    best = {"kind": "annot", "page": pno, "xref": a.xref, "rect": r, "type": a.type[1],
                            "movable": a.type[1] in MOVABLE}
        return best

    def _image_at(self, pno: int, pt: pymupdf.Point) -> dict | None:
        page = self._page(pno)
        best = None
        try:
            infos = page.get_image_info(xrefs=True)
        except Exception:  # noqa: BLE001
            return None
        for info in infos:
            r = self._to_disp(page, pymupdf.Rect(info["bbox"]))
            if r.contains(pt) and (best is None or r.get_area() < best["rect"].get_area()):
                best = {"kind": "image", "page": pno, "xref": info.get("xref", 0), "rect": r, "movable": False}
        return best

    def _line_at(self, pno: int, pt: pymupdf.Point) -> dict | None:
        lines = self._lines.get(pno)
        if lines is None:
            page = self._page(pno)
            lines = []
            for b in page.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    spans = [s for s in ln["spans"] if s["text"].strip()]
                    if spans:
                        lines.append((self._to_disp(page, pymupdf.Rect(ln["bbox"])), ln))
            self._lines[pno] = lines
        for r, ln in lines:
            if r.contains(pt):
                return {"rect": r, "line": ln}
        return None

    def _fields_on(self, pno: int) -> list:
        f = self._fields.get(pno)
        if f is None:
            page = self._page(pno)
            f = [(self._to_disp(page, w.rect), w.field_type, w.xref) for w in page.widgets()]
            self._fields[pno] = f
        return f

    # ------------------------------------------------------- canvas hooks
    def field_press(self, pno: int, pt: pymupdf.Point) -> bool:
        if not self.has_fields():
            return False
        for r, ftype, xref in self._fields_on(pno):
            if r.contains(pt):
                self._edit_field(pno, xref, r, ftype)
                return True
        return False

    def field_hover(self, pno: int, pt: pymupdf.Point):
        if not self.has_fields():
            return None
        for r, ftype, _x in self._fields_on(pno):
            if r.contains(pt):
                return Qt.IBeamCursor if ftype == pymupdf.PDF_WIDGET_TYPE_TEXT else Qt.PointingHandCursor
        return None

    def on_hover(self, pno: int, pt: pymupdf.Point):
        t = self.tool
        if t == "select":
            if self.field_hover(pno, pt) is not None:
                self._set_hover(None)
                return self.field_hover(pno, pt)
            hit = self._annot_at(pno, pt) or self._image_at(pno, pt)
            self._set_hover((pno, hit["rect"]) if hit else None)
            if hit:
                return Qt.SizeAllCursor if hit.get("movable") else Qt.PointingHandCursor
            return Qt.IBeamCursor if self.canvas._page(pno).words() else Qt.ArrowCursor
        if t == "edittext":
            hit = self._line_at(pno, pt)
            self._set_hover((pno, hit["rect"]) if hit else None)
            return Qt.PointingHandCursor if hit else Qt.ForbiddenCursor
        if t in TEXT_MARKUP:
            return Qt.IBeamCursor
        if t == "text":
            return Qt.IBeamCursor
        return Qt.CrossCursor

    def _set_hover(self, h) -> None:
        if h != self._hover:
            self._hover = h
            self.canvas.viewport().update()

    def on_press(self, pno: int, pt: pymupdf.Point, mods):
        self._commit_inline()
        t = self.tool
        if t == "select":
            if self.field_press(pno, pt):
                return "done"
            hit = self._annot_at(pno, pt) or self._image_at(pno, pt)
            self._sel = hit
            self.canvas.viewport().update()
            if hit is None:
                return "select"
            if hit.get("movable"):
                self._drag_start = pt
                self._drag_cur = pt
                return "drag"
            return "done"
        if t in TEXT_MARKUP:
            return "select"
        if t == "text":
            hit = self._annot_at(pno, pt)
            if hit and hit["type"] == "FreeText":
                self._edit_freetext(hit)
            else:
                self._new_freetext(pno, pt)
            return "done"
        if t == "edittext":
            hit = self._line_at(pno, pt)
            if hit:
                self._edit_line(pno, hit)
            return "done"
        if t in DRAG_TOOLS or t == "pickrect":
            self._drag_start = pt
            self._drag_cur = pt
            self._stroke = [pt]
            return "drag"
        return "pan"

    def on_drag(self, pno: int, pt: pymupdf.Point) -> None:
        self._drag_cur = pt
        if self.tool == "ink":
            self._stroke.append(pt)

    def on_release(self, pno: int, pt: pymupdf.Point, moved: bool) -> None:
        start, self._drag_start = self._drag_start, None
        self._drag_cur = None
        if start is None:
            return
        t = self.tool
        if t == "select" and self._sel and moved:
            d = pt - start
            self._move_selected(d)
            return
        if t == "ink":
            pts, self._stroke = self._stroke, []
            if len(pts) > 1:
                self._add_ink(pno, pts)
            return
        rect = pymupdf.Rect(start, pt).normalize()
        if t == "pickrect":
            if not moved or rect.width < 20 or rect.height < 10:
                rect = pymupdf.Rect(pt.x - 110, pt.y - 30, pt.x + 110, pt.y + 30)
            cb = self._pick_cb
            self._end_pick()
            if cb is not None:
                cb(pno, rect)
            return
        if t == "place":
            if not moved or rect.width < 8:
                rect = self._default_place_rect(pno, pt)
            self._place_image(pno, rect)
            return
        if not moved or rect.width < 3 and rect.height < 3:
            return
        if t == "arrow":
            self._add_arrow(pno, start, pt)
        elif t in ("rect", "ellipse"):
            self._add_shape(pno, rect, t)
        elif t in ("whiteout", "redact"):
            self._redact(pno, rect, black=(t == "redact"))

    def on_double(self, pno: int, pt: pymupdf.Point) -> bool:
        if self.tool in ("select", "text"):
            hit = self._annot_at(pno, pt)
            if hit and hit["type"] == "FreeText":
                self._edit_freetext(hit)
                return True
        return False

    def on_key(self, ev) -> bool:
        if self.tool == "pickrect" and ev.key() == Qt.Key_Escape:
            self._end_pick()
            return True
        if not self.active:
            return False
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self._sel:
            self.delete_selected()
            return True
        if ev.key() == Qt.Key_Escape:
            if self._sel:
                self._sel = None
                self.canvas.viewport().update()
                return True
            if self.tool != "select":
                self.set_tool("select")
                return True
        return False

    def on_text_selected(self, pno: int, rects: list) -> None:
        if self.tool in TEXT_MARKUP and rects:
            self._add_markup(pno, rects)
            self.canvas.clear_selection()

    # --------------------------------------------------------------- paint
    def paint(self, p: QPainter, pno: int, wr) -> None:
        c = self.canvas
        if self.has_fields():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(80, 140, 255, 38))
            for r, _t, _x in self._fields_on(pno):
                q = c.page_to_widget_rect(pno, r)
                if q is not None:
                    p.drawRect(q)
        if not self.active and self.tool != "pickrect":
            return
        if self._hover and self._hover[0] == pno and (not self._sel or self._sel["rect"] != self._hover[1]):
            q = c.page_to_widget_rect(pno, self._hover[1])
            if q is not None:
                p.setPen(QPen(QColor(theme.ACCENT), 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawRect(q.adjusted(-2, -2, 2, 2))
        if self._sel and self._sel["page"] == pno:
            r = self._sel["rect"]
            if self._drag_start is not None and self._drag_cur is not None and self.tool == "select":
                r = pymupdf.Rect(self._sel["rect"].x0 + (self._drag_cur.x - self._drag_start.x),
                                 self._sel["rect"].y0 + (self._drag_cur.y - self._drag_start.y),
                                 self._sel["rect"].x1 + (self._drag_cur.x - self._drag_start.x),
                                 self._sel["rect"].y1 + (self._drag_cur.y - self._drag_start.y))
            q = c.page_to_widget_rect(pno, r)
            if q is not None:
                p.setPen(QPen(QColor(theme.ACCENT), 2))
                p.setBrush(QColor(224, 48, 63, 25))
                p.drawRect(q.adjusted(-3, -3, 3, 3))
        if self._drag_start is not None and self._drag_cur is not None and c._tool_page == pno \
                and self.tool != "select":
            color = QColor(self._cur_color())
            a = c.page_to_widget(pno, self._drag_start)
            b = c.page_to_widget(pno, self._drag_cur)
            if a is None or b is None:
                return
            rect = QRectF(a, b).normalized()
            t = self.tool
            p.setRenderHint(QPainter.Antialiasing)
            if t == "ink" and len(self._stroke) > 1:
                p.setPen(QPen(color, max(1.0, self.width * c.scale), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                path = QPainterPath(c.page_to_widget(pno, self._stroke[0]))
                for s in self._stroke[1:]:
                    path.lineTo(c.page_to_widget(pno, s))
                p.drawPath(path)
            elif t == "rect":
                p.setPen(QPen(color, max(1.0, self.width * c.scale)))
                p.drawRect(rect)
            elif t == "ellipse":
                p.setPen(QPen(color, max(1.0, self.width * c.scale)))
                p.drawEllipse(rect)
            elif t == "arrow":
                p.setPen(QPen(color, max(1.0, self.width * c.scale)))
                p.drawLine(a, b)
            elif t == "whiteout":
                p.setPen(QPen(QColor("#888"), 1, Qt.DashLine))
                p.setBrush(QColor(255, 255, 255, 200))
                p.drawRect(rect)
            elif t == "redact":
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 200))
                p.drawRect(rect)
            elif t in ("place", "pickrect"):
                p.setPen(QPen(QColor(theme.ACCENT), 1, Qt.DashLine))
                p.setBrush(QColor(224, 48, 63, 30))
                p.drawRect(rect)

    # ----------------------------------------------------------- actions
    def _add_ink(self, pno: int, pts: list) -> None:
        self.checkpoint()
        page = self._page(pno)
        stroke = [tuple(self._to_unrot(page, q)) for q in pts]
        a = page.add_ink_annot([stroke])
        a.set_colors(stroke=_rgb(self._cur_color()))
        a.set_border(width=self.width)
        a.update()
        self._invalidate(pno)

    def _add_shape(self, pno: int, rect: pymupdf.Rect, kind: str) -> None:
        self.checkpoint()
        page = self._page(pno)
        r = self._to_unrot(page, rect)
        a = page.add_rect_annot(r) if kind == "rect" else page.add_circle_annot(r)
        a.set_colors(stroke=_rgb(self._cur_color()))
        a.set_border(width=self.width)
        a.update()
        self._invalidate(pno)

    def _add_arrow(self, pno: int, p1: pymupdf.Point, p2: pymupdf.Point) -> None:
        self.checkpoint()
        page = self._page(pno)
        a = page.add_line_annot(self._to_unrot(page, p1), self._to_unrot(page, p2))
        a.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
        a.set_colors(stroke=_rgb(self._cur_color()))
        a.set_border(width=self.width)
        a.update()
        self._invalidate(pno)

    def _add_markup(self, pno: int, rects: list) -> None:
        self.checkpoint()
        page = self._page(pno)
        quads = [self._to_unrot(page, r) for r in rects]
        t = self.tool
        if t == "highlight":
            a = page.add_highlight_annot(quads)
        elif t == "underline":
            a = page.add_underline_annot(quads)
        else:
            a = page.add_strikeout_annot(quads)
        a.set_colors(stroke=_rgb(self._cur_color()))
        a.update()
        self._invalidate(pno)

    def _redact(self, pno: int, rect: pymupdf.Rect, black: bool) -> None:
        self.checkpoint()
        page = self._page(pno)
        page.add_redact_annot(self._to_unrot(page, rect), fill=(0, 0, 0) if black else (1, 1, 1))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        self._invalidate(pno)

    # text annotations ----------------------------------------------------
    def _new_freetext(self, pno: int, pt: pymupdf.Point) -> None:
        r = pymupdf.Rect(pt.x, pt.y - self.font_size * 0.9, pt.x + 220, pt.y + self.font_size * 2.5)
        self._open_inline(pno, r, "", multiline=True, info={"what": "new_freetext", "page": pno, "pt": pt})

    def _edit_freetext(self, hit: dict) -> None:
        page = self._page(hit["page"])
        annot = page.load_annot(hit["xref"])
        text = annot.info.get("content", "")
        self._open_inline(hit["page"], hit["rect"], text, multiline=True,
                          info={"what": "edit_freetext", "page": hit["page"], "xref": hit["xref"]})

    def _freetext_rect(self, pt: pymupdf.Point, text: str, size: float) -> pymupdf.Rect:
        lines = text.split("\n") or [""]
        w = max(pymupdf.get_text_length(ln, fontname="helv", fontsize=size) for ln in lines) + size * 0.8 + 4
        h = len(lines) * size * 1.22 + size * 0.5
        top = pt.y - size * 0.9
        return pymupdf.Rect(pt.x, top, pt.x + w, top + h)

    def _restyle_selected(self, color: str | None = None, width: float | None = None,
                          size: float | None = None) -> None:
        sel = self._sel
        self.checkpoint()
        page = self._page(sel["page"])
        a = page.load_annot(sel["xref"])
        if a is None:
            return
        if a.type[1] == "FreeText":
            text = a.info.get("content", "")
            fs = size or self.font_size
            if size:
                r = self._freetext_rect(pymupdf.Point(a.rect.x0, a.rect.y0 + fs * 0.9), text, fs)
                a.set_rect(r)
            a.update(fontsize=fs, text_color=_rgb(color) if color else None)
        else:
            if color:
                if a.type[1] in ("Highlight", "Underline", "StrikeOut", "Squiggly", "Ink", "Line", "Square",
                                 "Circle", "Polygon", "PolyLine"):
                    a.set_colors(stroke=_rgb(color))
            if width:
                a.set_border(width=width)
            a.update()
        sel["rect"] = self._to_disp(page, a.rect)
        self._invalidate(sel["page"])

    def _move_selected(self, d: pymupdf.Point) -> None:
        sel = self._sel
        if not sel or sel["kind"] != "annot" or not sel.get("movable"):
            return
        self.checkpoint()
        page = self._page(sel["page"])
        a = page.load_annot(sel["xref"])
        new = pymupdf.Rect(sel["rect"].x0 + d.x, sel["rect"].y0 + d.y, sel["rect"].x1 + d.x, sel["rect"].y1 + d.y)
        a.set_rect(self._to_unrot(page, new))
        a.update()
        sel["rect"] = self._to_disp(page, a.rect)
        self._invalidate(sel["page"])

    def delete_selected(self) -> None:
        sel = self._sel
        if not sel:
            return
        if sel["kind"] == "annot":
            self.checkpoint()
            page = self._page(sel["page"])
            a = page.load_annot(sel["xref"])
            if a is not None:
                page.delete_annot(a)
        else:
            self._delete_image(sel)
        self._invalidate(sel["page"])
        self._sel = None

    # images ------------------------------------------------------------------
    def _image_pages(self, xref: int) -> int:
        if not xref:
            return 1
        return sum(1 for i in range(self.doc.page_count)
                   if any(img[0] == xref for img in self.doc.get_page_images(i)))

    def _delete_image(self, sel: dict) -> None:
        xref = sel.get("xref", 0)
        uses = self._image_pages(xref) if self.doc.is_pdf else 1
        everywhere = False
        if uses > 1:
            r = QMessageBox.question(
                self.tab, tr("Delete image"),
                tr("This image appears on {n} pages. Delete it everywhere?", n=uses),
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            everywhere = r == QMessageBox.Yes
        self.checkpoint()
        page = self._page(sel["page"])
        if everywhere and xref:
            page.delete_image(xref)
            for i in range(self.doc.page_count):
                self.canvas.invalidate_page(i)
        else:
            page.add_redact_annot(self._to_unrot(page, sel["rect"]))
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE if uses <= 1 and xref
                                  else pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=pymupdf.PDF_REDACT_TEXT_NONE)

    def replace_selected_image(self) -> None:
        sel = self._sel
        if not sel or sel["kind"] != "image":
            return
        f, _ = QFileDialog.getOpenFileName(self.tab, tr("Replace image"), "",
                                           "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if not f:
            return
        with open(f, "rb") as fh:
            data = fh.read()
        self.checkpoint()
        page = self._page(sel["page"])
        rect = self._to_unrot(page, sel["rect"])
        page.add_redact_annot(rect)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
        page.insert_image(rect, stream=data, keep_proportion=True)
        self._invalidate(sel["page"])
        self._sel = None

    def extract_selected_image(self) -> None:
        sel = self._sel
        if not sel or sel["kind"] != "image":
            return
        page = self._page(sel["page"])
        if sel.get("xref"):
            x = self.doc.extract_image(sel["xref"])
            data, ext = x["image"], x.get("ext", "png")
        else:
            pix = page.get_pixmap(clip=self._to_unrot(page, sel["rect"]), dpi=200)
            data, ext = pix.tobytes("png"), "png"
        f, _ = QFileDialog.getSaveFileName(self.tab, tr("Save the image"),
                                           os.path.join(os.path.dirname(self.tab.path), f"image.{ext}"),
                                           f"*.{ext}")
        if f:
            with open(f, "wb") as fh:
                fh.write(data)

    def pick_image(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self.tab, tr("Insert an image"), "",
                                           "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if f:
            with open(f, "rb") as fh:
                self._pending_image = fh.read()
            self.set_tool("place")
            self.tab.flash(tr("Click or drag on the page to place the image."))

    def pick_signature(self) -> None:
        dlg = SignatureDialog(self.tab)
        if dlg.exec() == QDialog.Accepted and dlg.result_png:
            self._pending_image = dlg.result_png
            self.set_tool("place")
            self.tab.flash(tr("Click or drag on the page to place the signature."))

    def _default_place_rect(self, pno: int, pt: pymupdf.Point) -> pymupdf.Rect:
        from .convert import image_size
        w, h = image_size(self._pending_image) or (300, 150)
        page_w = self.canvas.sizes[pno][0]
        target_w = min(w * 0.75, page_w * 0.35)
        target_h = target_w * h / max(1, w)
        return pymupdf.Rect(pt.x - target_w / 2, pt.y - target_h / 2, pt.x + target_w / 2, pt.y + target_h / 2)

    def _place_image(self, pno: int, rect: pymupdf.Rect) -> None:
        if not self._pending_image:
            return
        self.checkpoint()
        page = self._page(pno)
        page.insert_image(self._to_unrot(page, rect), stream=self._pending_image, keep_proportion=True)
        self._invalidate(pno)
        self._pending_image = None
        self.set_tool("select")

    # existing text -----------------------------------------------------------
    def _edit_line(self, pno: int, hit: dict) -> None:
        from . import fonts

        ln = hit["line"]
        spans = [s for s in ln["spans"] if s["text"].strip()] or ln["spans"]
        text = "".join(s["text"] for s in ln["spans"]).strip()
        s0 = spans[0]
        family, bold, italic = editing.span_style(s0)
        color = "#%06x" % s0.get("color", 0)
        page = self._page(pno)
        original = fonts.embedded_font(page, s0.get("font", ""))
        default_family = fonts.match_family(s0.get("font", "")) or \
            {"sans": "Arial", "serif": "Times New Roman", "mono": "Courier New"}[family]
        dlg = TextEditDialog(self.tab, text, round(s0["size"], 1), color, default_family, bold, italic,
                             original=fonts.clean_font_name(s0.get("font", "")) if original else None)
        if dlg.exec() != QDialog.Accepted:
            return
        new_text = dlg.text.text()
        choice = dlg.family()
        b, it_ = dlg.bold.isChecked(), dlg.italic.isChecked()
        self.checkpoint()
        page = self._page(pno)
        box = pymupdf.Rect(ln["bbox"])
        inset = box.height * 0.18
        page.add_redact_annot(pymupdf.Rect(box.x0, box.y0 + inset, box.x1, box.y1 - inset))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        if new_text.strip():
            size, rgb = dlg.size.value(), _rgb(dlg.color)
            origin = pymupdf.Point(s0["origin"])
            done = False
            if choice == TextEditDialog.ORIGINAL:
                if original and fonts.covers(original[1], new_text):
                    editing.insert_words(page, origin, new_text, size, rgb, fontbuffer=original[1])
                    done = True
                else:
                    choice = default_family
                    self.tab.flash(tr("The document font lacks some characters: {f} was used.", f=default_family))
            if not done:
                path = fonts.font_file(choice, b, it_)
                if path and not fonts.file_covers(path, new_text):
                    path = fonts.font_file("Arial", b, it_) or path
                    self.tab.flash(tr("“{f}” lacks some characters: Arial was used.", f=choice))
                if not path:
                    path = editing.font_for(family, b, it_)[1]
                editing.insert_words(page, origin, new_text, size, rgb, fontfile=path)
            self.fonts_used = True
        self._invalidate(pno)

    # forms ---------------------------------------------------------------------
    def _widget(self, page: pymupdf.Page, xref: int):
        for w in page.widgets():
            if w.xref == xref:
                return w
        return None

    def _edit_field(self, pno: int, xref: int, rect: pymupdf.Rect, ftype: int) -> None:
        page = self._page(pno)
        w = self._widget(page, xref)
        if w is None:
            return
        T = pymupdf
        if ftype == T.PDF_WIDGET_TYPE_TEXT:
            multiline = bool(w.field_flags & 4096)
            self._open_inline(pno, rect, w.field_value or "", multiline=multiline,
                              info={"what": "field", "page": pno, "xref": xref},
                              font_size=w.text_fontsize or 0)
        elif ftype == T.PDF_WIDGET_TYPE_CHECKBOX:
            on = w.on_state() or "Yes"
            cur = w.field_value
            self._set_field(pno, xref, "Off" if cur not in (False, "Off", "", None) else on)
        elif ftype == T.PDF_WIDGET_TYPE_RADIOBUTTON:
            self._set_field(pno, xref, w.on_state() or True)
        elif ftype in (T.PDF_WIDGET_TYPE_COMBOBOX, T.PDF_WIDGET_TYPE_LISTBOX):
            m = QMenu(self.tab)
            for ch in w.choice_values or []:
                label = ch[1] if isinstance(ch, (list, tuple)) else ch
                value = ch[0] if isinstance(ch, (list, tuple)) else ch
                a = m.addAction(str(label))
                a.setCheckable(True)
                a.setChecked(str(value) == str(w.field_value))
                a.triggered.connect(lambda _c=False, v=value: self._set_field(pno, xref, v))
            q = self.canvas.page_to_widget_rect(pno, rect)
            if q is not None:
                m.exec(self.canvas.viewport().mapToGlobal(q.bottomLeft().toPoint()))
        elif ftype == T.PDF_WIDGET_TYPE_SIGNATURE:
            self.tab.flash(tr("Digital signature fields are not supported; use the Signature tool."))

    def _set_field(self, pno: int, xref: int, value) -> None:
        self.checkpoint()
        page = self._page(pno)
        w = self._widget(page, xref)
        if w is None:
            return
        w.field_value = value
        w.update()
        self._invalidate(pno)

    # inline editor -------------------------------------------------------------
    def _open_inline(self, pno: int, rect: pymupdf.Rect, text: str, multiline: bool, info: dict,
                     font_size: float = 0) -> None:
        self._commit_inline()
        q = self.canvas.page_to_widget_rect(pno, rect)
        if q is None:
            return
        ed = QPlainTextEdit(self.canvas.viewport()) if multiline else QLineEdit(self.canvas.viewport())
        fs = font_size or (self.font_size if info["what"] != "field" else 0) or max(8.0, rect.height * 0.6)
        f = QFont("Arial")
        f.setPixelSize(max(9, int(fs * self.canvas.scale)))
        ed.setFont(f)
        ed.setStyleSheet("background: rgba(255,255,230,235); color: #000; border: 1px solid #e0303f;"
                         "border-radius: 2px; padding: 1px;")
        if multiline:
            ed.setPlainText(text)
            ed.setMinimumSize(int(max(q.width(), 180)), int(max(q.height(), 50)))
            ed.resize(int(max(q.width(), 240)), int(max(q.height(), 60)))
        else:
            ed.setText(text)
            ed.resize(int(max(q.width(), 60)), int(max(q.height(), f.pixelSize() + 8)))
        ed.move(int(q.x()), int(q.y()))
        ed.installEventFilter(self._inline_filter)
        ed.show()
        ed.setFocus()
        if not multiline:
            ed.selectAll()
        self._inline = ed
        self._inline_info = info
        self.canvas.verticalScrollBar().valueChanged.connect(self._commit_inline)

    def _inline_text(self) -> str:
        ed = self._inline
        return ed.toPlainText() if isinstance(ed, QPlainTextEdit) else ed.text()

    def _close_inline(self) -> None:
        ed, self._inline = self._inline, None
        try:
            self.canvas.verticalScrollBar().valueChanged.disconnect(self._commit_inline)
        except (RuntimeError, TypeError):
            pass
        if ed is not None:
            ed.removeEventFilter(self._inline_filter)
            ed.hide()
            ed.deleteLater()
        self.canvas.setFocus()

    def _cancel_inline(self) -> None:
        self._inline_info = None
        self._close_inline()

    def _commit_inline(self) -> None:
        if self._inline is None:
            return
        text = self._inline_text()
        info, self._inline_info = self._inline_info, None
        self._close_inline()
        if not info:
            return
        what = info["what"]
        pno = info["page"]
        if what == "field":
            self._set_field(pno, info["xref"], text)
        elif what == "new_freetext" and text.strip():
            self.checkpoint()
            page = self._page(pno)
            r = self._freetext_rect(info["pt"], text.rstrip(), self.font_size)
            a = page.add_freetext_annot(self._to_unrot(page, r), text.rstrip(), fontsize=self.font_size,
                                        fontname="helv", text_color=_rgb(self.colors["text"]))
            a.update()
            self._invalidate(pno)
        elif what == "edit_freetext":
            self.checkpoint()
            page = self._page(pno)
            a = page.load_annot(info["xref"])
            if a is None:
                return
            if not text.strip():
                page.delete_annot(a)
            else:
                fs = self.font_size
                r = self._freetext_rect(pymupdf.Point(a.rect.x0, a.rect.y0 + fs * 0.9), text.rstrip(), fs)
                a.set_info(content=text.rstrip())
                a.set_rect(r)
                a.update(fontsize=fs)
            self._invalidate(pno)

    # document-level --------------------------------------------------------------
    def set_password(self) -> None:
        dlg = PasswordDialog(self.tab, bool(self.doc.is_encrypted or self.tab.password))
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.remove.isChecked():
            self.save_opts.update(password=None, remove_password=True)
        else:
            self.save_opts.update(password=dlg.pw.text(), remove_password=False)
        self._set_dirty(True)
        self.tab.flash(tr("The password will be applied when you save."))

    def edit_metadata(self) -> None:
        dlg = MetadataDialog(self.tab, self.doc.metadata)
        if dlg.exec() == QDialog.Accepted:
            self.checkpoint()
            meta = dict(self.doc.metadata or {})
            meta.update(dlg.values())
            self.doc.set_metadata({k: v for k, v in meta.items() if k in (
                "title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate")})

    def flatten(self) -> None:
        self.checkpoint()
        self.doc.bake(annots=True, widgets=True)
        self._fields.clear()
        for i in range(self.doc.page_count):
            self.canvas.invalidate_page(i)
        self.tab.flash(tr("Annotations and form fields are now part of the pages."))

    # --------------------------------------------------------------------- save
    def save(self, save_as: bool = False) -> bool:
        self._commit_inline()
        tab = self.tab
        old_path = tab.path
        base, ext = os.path.splitext(old_path)
        if save_as:
            filters = "PDF (*.pdf)" if self.kind == "pdf" else "CBZ (*.cbz);;PDF (*.pdf)"
            start = base + (" (copy).pdf" if self.kind == "pdf" else " (copy).cbz")
            dst, chosen = QFileDialog.getSaveFileName(tab, tr("Save as…"), start, filters)
            if not dst:
                return False
            if not os.path.splitext(dst)[1]:
                dst += ".pdf" if "pdf" in chosen.lower() else ".cbz"
        else:
            dst = old_path if self.kind == "pdf" else (old_path if ext.lower() == ".cbz" else base + ".cbz")
        out_pdf = dst.lower().endswith(".pdf")
        tmp = dst + ".new"
        try:
            if out_pdf:
                editing.write_pdf(self.doc, tmp, compress=self.save_opts["compress"], subset=self.fonts_used,
                                  password=self.save_opts["password"],
                                  remove_password=self.save_opts["remove_password"])
            else:
                editing.write_cbz(self.doc, tmp)
        except Exception as e:  # noqa: BLE001
            if os.path.exists(tmp):
                os.remove(tmp)
            QMessageBox.warning(tab, tr("Save"), tr("Could not save:\n{e}", e=e))
            return False

        if save_as:
            os.replace(tmp, dst)
        else:
            if self.kind == "pdf":
                tab.release_file()  # the original PDF must be closed before it can be replaced on Windows
            bak = editing.replace_with_backup(tmp, old_path, dst)
            if bak:
                tab.db.add_backup(bak, old_path)
        if dst != old_path and not save_as:
            tab.db.move_doc_state(old_path, dst)
        new_pw = self.save_opts["password"]
        if new_pw:
            tab.password = new_pw
        elif self.save_opts["remove_password"]:
            tab.password = None
        if self.kind == "pdf" and not save_as:
            tab.reload_from(dst)
        tab.set_path(dst)
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._saved_len = 0
        self.save_opts.update(password=None, remove_password=False)
        self._set_dirty(False)
        self.saved.emit(old_path, dst)
        tab.flash(tr("Saved: {f}", f=os.path.basename(dst)) +
                  ("" if save_as else "  ·  " + tr("previous version kept as .bak")))
        return True

    # ------------------------------------------------------------ context menu
    def extend_menu(self, m: QMenu) -> None:
        if not self.active or not self._sel:
            return
        sel = self._sel
        m.addSeparator()
        if sel["kind"] == "annot" and sel["type"] == "FreeText":
            m.addAction(icon("edit"), tr("Edit text"), lambda: self._edit_freetext(sel))
        if sel["kind"] == "image":
            m.addAction(icon("image"), tr("Replace image…"), self.replace_selected_image)
            m.addAction(icon("save"), tr("Save the image…"), self.extract_selected_image)
        m.addAction(icon("trash"), tr("Delete") + "  (Del)", self.delete_selected)

