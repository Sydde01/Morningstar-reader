"""DocumentTab: toolbar + side panel (contents, bookmarks, notes, search) + PageCanvas."""
from __future__ import annotations

import os
import time

import pymupdf
from PySide6.QtCore import QPoint, QPointF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QIntValidator, QKeySequence
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
                               QSplitter, QTabWidget, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import theme
from .canvas import PageCanvas
from .config import settings
from .db import Database
from .docbackend import is_comic, is_reflow, open_document
from .i18n import tr
from .icons import icon

ID_ROLE = Qt.UserRole + 1


class NoteDialog(QDialog):
    DELETE = 2

    def __init__(self, parent, text: str = "", page: int = 0, can_delete: bool = False):
        super().__init__(parent)
        self.setWindowTitle(tr("Note — page {n}", n=page + 1))
        self.resize(440, 280)
        lay = QVBoxLayout(self)
        self.edit = QPlainTextEdit(text)
        self.edit.setPlaceholderText(tr("Write your note…"))
        lay.addWidget(self.edit)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText(tr("Save"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        if can_delete:
            d = bb.addButton(tr("Delete"), QDialogButtonBox.DestructiveRole)
            d.clicked.connect(lambda: self.done(self.DELETE))
        lay.addWidget(bb)
        self.edit.setFocus()

    def text(self) -> str:
        return self.edit.toPlainText().strip()


class DocumentTab(QWidget):
    titleChanged = Signal(str)
    fullscreenRequested = Signal()
    favoritesChanged = Signal()
    stateSaved = Signal()
    toolRequested = Signal(str, str)  # tool name, path
    openRequested = Signal(str)
    fileSaved = Signal(str, str)      # old path, new path

    def __init__(self, path: str, db: Database, parent=None, password: str | None = None):
        super().__init__(parent)
        self.path = path
        self.password = password
        self.db = db
        self.title = os.path.splitext(os.path.basename(path))[0]
        self.comic = is_comic(path)
        self.reflow = is_reflow(path)
        st = db.doc_state(path)
        self.font_size = float(st.get("font_size") or settings.get("reading.epub_font_size", 12))
        self.doc = open_document(path, font_size=self.font_size if self.reflow else None,
                                 page_w=settings.get("reading.epub_page_width", 480),
                                 page_h=settings.get("reading.epub_page_height", 680), password=password)
        self.toc = []
        try:
            self.toc = self.doc.get_toc(simple=False)
        except Exception:  # noqa: BLE001
            pass
        self._search_query = ""
        self._search_page = 0
        self._search_results: list[tuple[int, int]] = []
        self._search_index = -1
        self._search_timer = QTimer(self, singleShot=True, interval=0, timeout=self._search_step)
        self._save_timer = QTimer(self, singleShot=True, interval=1500, timeout=self.save_state)

        self.canvas = PageCanvas(self)
        self._build_ui()
        from .editor import EditController

        self.editor = EditController(self)
        self.editor.dirtyChanged.connect(lambda _d: self.titleChanged.emit(self.display_title()))
        self.editor.saved.connect(self.fileSaved.emit)
        self._flash = QLabel(self.canvas)
        self._flash.setStyleSheet(f"background: rgba(20,20,24,235); color: {theme.TEXT}; padding: 8px 14px;"
                                  f"border: 1px solid {theme.ACCENT}; border-radius: 6px;")
        self._flash.hide()
        self._flash_timer = QTimer(self, singleShot=True, interval=3500, timeout=self._flash.hide)
        from .signing import has_signatures

        from .editing import is_dynamic_xfa

        if is_dynamic_xfa(self.doc):
            QTimer.singleShot(600, lambda: self.flash(tr("Dynamic XFA form: it can only be displayed and filled "
                                                          "in Adobe Reader.")))
        elif has_signatures(self.doc):
            QTimer.singleShot(600, lambda: self.flash(tr("This document is digitally signed — "
                                                          "Tools → Digital signatures")))
        self._apply_initial_state(st)
        self.reload_marks()
        db.update_doc(path, page_count=self.doc.page_count, last_opened=time.time())

    # ---------------------------------------------------------------- UI
    def _act(self, ic, text, slot, shortcut=None, checkable=False, tb=True) -> QAction:
        a = QAction(icon(ic) if isinstance(ic, str) else ic, text, self)
        a.setCheckable(checkable)
        if shortcut:
            seqs = shortcut if isinstance(shortcut, (list, tuple)) else [shortcut]
            a.setShortcuts([QKeySequence(s) for s in seqs])
            a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            a.setToolTip(f"{text}  ({QKeySequence(seqs[0]).toString(QKeySequence.NativeText)})")
        self.addAction(a)
        a.triggered.connect(slot)
        if tb:
            self.tb.addAction(a)
        return a

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.tb = QToolBar()
        self.tb.setIconSize(QSize(18, 18))
        self.tb.setMovable(False)
        lay.addWidget(self.tb)

        self.a_sidebar = self._act("sidebar", tr("Side panel"), self._toggle_sidebar, "F12", checkable=True)
        self.tb.addSeparator()
        self._act("first", tr("First page"), lambda: self.canvas.goto_page(0))
        self._act("prev", tr("Previous page"), self.canvas.prev_page)
        self.page_edit = QLineEdit()
        self.page_edit.setFixedWidth(52)
        self.page_edit.setAlignment(Qt.AlignCenter)
        self.page_edit.setValidator(QIntValidator(1, max(1, self.doc.page_count)))
        self.page_edit.returnPressed.connect(self._goto_entered)
        self.tb.addWidget(self.page_edit)
        self.page_total = QLabel(f" / {self.doc.page_count} ")
        self.page_total.setObjectName("dim")
        self.tb.addWidget(self.page_total)
        self._act("next", tr("Next page"), self.canvas.next_page)
        self._act("last", tr("Last page"), lambda: self.canvas.goto_page(self.doc.page_count - 1))
        self._act("goto", tr("Go to page"), self._focus_page, "Ctrl+G", tb=False)
        self.tb.addSeparator()

        self._act("zoom_out", tr("Zoom out"), lambda: self.canvas.zoom_step(-1), ["Ctrl+-"])
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.setFixedWidth(124)
        self.zoom_box.setInsertPolicy(QComboBox.NoInsert)
        for label, data in ((tr("Fit width"), "width"), (tr("Fit page"), "page")):
            self.zoom_box.addItem(label, data)
        for z in (25, 50, 75, 100, 125, 150, 200, 300, 400):
            self.zoom_box.addItem(f"{z}%", z / 100)
        self.zoom_box.activated.connect(self._zoom_chosen)
        self.zoom_box.lineEdit().returnPressed.connect(self._zoom_typed)
        self.tb.addWidget(self.zoom_box)
        self._act("zoom_in", tr("Zoom in"), lambda: self.canvas.zoom_step(1), ["Ctrl++", "Ctrl+="])
        self.a_fit_w = self._act("fit_width", tr("Fit width"), lambda: self._set_fit("width"), "Ctrl+2",
                                 checkable=True)
        self.a_fit_p = self._act("fit_page", tr("Fit page"), lambda: self._set_fit("page"), "Ctrl+0",
                                 checkable=True)
        self._act("zoom_in", tr("Actual size"), lambda: self.canvas.set_zoom(1.0), "Ctrl+1", tb=False)
        self.tb.addSeparator()

        self.a_cont = self._act("continuous", tr("Continuous scrolling"), lambda: self._set_mode("continuous"),
                                "Ctrl+8", checkable=True)
        self.a_single = self._act("single", tr("Single page"), lambda: self._set_mode("single"), "Ctrl+6",
                                  checkable=True)
        self.a_double = self._act("double", tr("Two pages"), lambda: self._set_mode("double"), "Ctrl+7",
                                  checkable=True)
        self.a_rtl = self._act("rtl", tr("Right-to-left (manga)"), self._toggle_rtl, checkable=True)
        self._act("rotate", tr("Rotate"), lambda: self.canvas.set_rotation(self.canvas.rotation + 90), "Ctrl+R")
        if self.reflow:
            self.tb.addSeparator()
            self._act("font_down", tr("Smaller text"), lambda: self._set_font(self.font_size - 1), "Ctrl+Shift+-")
            self._act("font_up", tr("Larger text"), lambda: self._set_font(self.font_size + 1), "Ctrl+Shift+=")
        self.tb.addSeparator()
        self.a_dark = self._act("moon", tr("Dark document (invert colors)"), self._toggle_dark, "Ctrl+D",
                                checkable=True)
        self.a_bm = self._act("bookmark_add", tr("Add bookmark"), self.add_bookmark, "Ctrl+B")
        self._act("note", tr("Add note"), self._add_note_toolbar, "Ctrl+Shift+N")
        self.a_fav = self._act("star", tr("Favorite"), self._toggle_favorite, checkable=True)
        self._act("fullscreen", tr("Full screen"), self.fullscreenRequested.emit, "F11")
        self.tb.addSeparator()
        self.a_edit = self._act("edit", tr("Edit (annotate, fill, sign, pages…)"), self.toggle_edit, "Ctrl+E",
                                checkable=True)
        tools_btn = QToolButton(icon=icon("tools"), toolTip=tr("Tools"))
        tools_btn.setPopupMode(QToolButton.InstantPopup)
        tm = QMenu(tools_btn)
        tm.addAction(icon("pages"), tr("Organize pages…"), self.open_pages)
        tm.addAction(icon("scissors"), tr("Split double pages…"), lambda: self.toolRequested.emit("split", self.path))
        tm.addAction(icon("convert"), tr("Convert…"), lambda: self.toolRequested.emit("convert", self.path))
        tm.addAction(icon("merge"), tr("Merge with other files…"), lambda: self.toolRequested.emit("merge", self.path))
        tm.addSeparator()
        tm.addAction(icon("search"), tr("Make searchable (OCR)…"), lambda: self.toolRequested.emit("ocr", self.path))
        tm.addAction(icon("signature"), tr("Sign digitally…"), self.start_signing)
        tm.addAction(icon("lock"), tr("Digital signatures…"), self.show_signatures)
        tools_btn.setMenu(tm)
        self.tb.addWidget(tools_btn)
        self._act("copy", tr("Copy"), self._copy_selection, "Ctrl+C", tb=False)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.tb.addWidget(spacer)
        self.search_edit = QLineEdit(placeholderText=tr("Search in document"), clearButtonEnabled=True)
        self.search_edit.addAction(icon("search", theme.TEXT_DIM), QLineEdit.LeadingPosition)
        self.search_edit.setFixedWidth(230)
        self.search_edit.returnPressed.connect(self._search_enter)
        self.search_edit.textChanged.connect(self._search_text_changed)
        self.tb.addWidget(self.search_edit)
        self._act("prev", tr("Previous result"), lambda: self._search_move(-1), "Shift+F3")
        self._act("next", tr("Next result"), lambda: self._search_move(1), "F3")
        self.search_count = QLabel("")
        self.search_count.setObjectName("dim")
        self.search_count.setMinimumWidth(70)
        self.tb.addWidget(self.search_count)
        self._act("search", tr("Search"), self._focus_search, "Ctrl+F", tb=False)
        esc = QAction(self)
        esc.setShortcut(QKeySequence("Escape"))
        esc.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        esc.triggered.connect(self._escape)
        self.addAction(esc)

        # side panel
        self.split = QSplitter(Qt.Horizontal)
        self.side = QTabWidget()
        self.side.setDocumentMode(True)
        self.side.setMinimumWidth(200)
        self.side.tabBar().setDrawBase(False)
        self.side.tabBar().setUsesScrollButtons(False)
        self.side.tabBar().setExpanding(True)
        self.side.tabBar().setElideMode(Qt.ElideNone)

        self.toc_tree = QTreeWidget()
        self.toc_tree.setHeaderHidden(True)
        self.toc_tree.itemActivated.connect(self._toc_go)
        self.toc_tree.itemClicked.connect(self._toc_go)
        self._fill_toc()
        self.side.addTab(self.toc_tree, icon("toc"), "")
        self.side.setTabToolTip(0, tr("Contents"))

        bm_w = QWidget()
        bl = QVBoxLayout(bm_w)
        bl.setContentsMargins(0, 4, 0, 0)
        self.bm_list = QListWidget()
        self.bm_list.itemActivated.connect(self._bm_go)
        self.bm_list.itemClicked.connect(self._bm_go)
        self.bm_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.bm_list.customContextMenuRequested.connect(self._bm_menu)
        bl.addWidget(self.bm_list)
        row = QHBoxLayout()
        b = QPushButton(icon("bookmark_add"), tr("Add"))
        b.clicked.connect(self.add_bookmark)
        row.addWidget(b)
        row.addStretch(1)
        bl.addLayout(row)
        self.side.addTab(bm_w, icon("bookmark"), "")
        self.side.setTabToolTip(1, tr("Bookmarks"))

        notes_w = QWidget()
        nl = QVBoxLayout(notes_w)
        nl.setContentsMargins(0, 4, 0, 0)
        self.note_list = QListWidget()
        self.note_list.setWordWrap(True)
        self.note_list.itemClicked.connect(self._note_go)
        self.note_list.itemDoubleClicked.connect(lambda it: self.edit_note(it.data(ID_ROLE)))
        self.note_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.note_list.customContextMenuRequested.connect(self._note_menu)
        nl.addWidget(self.note_list)
        row = QHBoxLayout()
        b = QPushButton(icon("note"), tr("Add"))
        b.clicked.connect(self._add_note_toolbar)
        row.addWidget(b)
        row.addStretch(1)
        nl.addLayout(row)
        self.side.addTab(notes_w, icon("note"), "")
        self.side.setTabToolTip(2, tr("Notes"))

        self.result_list = QListWidget()
        self.result_list.setWordWrap(True)
        self.result_list.itemClicked.connect(self._result_go)
        self.result_list.itemActivated.connect(self._result_go)
        self.side.addTab(self.result_list, icon("search"), "")
        self.side.setTabToolTip(3, tr("Search results"))

        self.split.addWidget(self.side)
        self.split.addWidget(self.canvas)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([260, 1200])
        lay.addWidget(self.split, 1)

        self.canvas.pageChanged.connect(self._on_page)
        self.canvas.zoomChanged.connect(self._on_zoom)
        self.canvas.linkActivated.connect(self._on_link)
        self.canvas.contextRequested.connect(self._context_menu)
        self.canvas.noteClicked.connect(self.edit_note)

    def _fill_toc(self) -> None:
        self.toc_tree.clear()
        stack: list[QTreeWidgetItem | None] = [None]
        for entry in self.toc:
            lvl, title, page = entry[0], entry[1], entry[2]
            dest = entry[3] if len(entry) > 3 else {}
            it = QTreeWidgetItem([title])
            it.setToolTip(0, f"{title} — p. {page}")
            it.setData(0, ID_ROLE, (page - 1, dest.get("to").y if isinstance(dest, dict) and dest.get("to") else None))
            while len(stack) > lvl:
                stack.pop()
            parent = stack[-1] if stack else None
            if parent is None:
                self.toc_tree.addTopLevelItem(it)
            else:
                parent.addChild(it)
            while len(stack) < lvl:
                stack.append(parent)
            stack.append(it)
        if self.toc_tree.topLevelItemCount() == 0:
            it = QTreeWidgetItem([tr("No table of contents")])
            it.setFlags(Qt.NoItemFlags)
            self.toc_tree.addTopLevelItem(it)
        elif self.toc_tree.topLevelItemCount() < 8:
            self.toc_tree.expandToDepth(0)

    # ------------------------------------------------------- state
    def _apply_initial_state(self, st: dict) -> None:
        c = self.canvas
        c.gap = int(settings.get("reading.page_gap", 12))
        c.cover_alone = bool(settings.get("reading.cover_alone", True))
        if self.comic:
            mode = st.get("view_mode") or settings.get("reading.comics_view_mode", "single")
            fit = st.get("fit") or settings.get("reading.comics_fit", "page")
        else:
            mode = st.get("view_mode") or settings.get("reading.view_mode", "continuous")
            fit = st.get("fit") or settings.get("reading.fit", "width")
        c.mode = mode
        c.fit = fit
        if fit == "none" and st.get("zoom"):
            c.zoom = float(st["zoom"])
        rtl = st.get("rtl")
        c.rtl = bool(rtl) if rtl is not None else (self.comic and settings.get("reading.comics_rtl", False))
        dark = st.get("dark")
        if dark is None:
            key = "default_comics" if self.comic else ("default_reflow" if self.reflow else "default_pdf")
            dark = settings.get(f"dark_doc.{key}", False)
        c.dark = bool(dark)
        c.dark_bg = settings.get("dark_doc.background")
        c.dark_fg = settings.get("dark_doc.text")
        c.keep_images = bool(settings.get("dark_doc.keep_images", True))
        c.set_document(self.doc)
        page = int(st.get("page") or 0)
        if self.reflow and st.get("location") is not None:
            page = self._page_from_location(st["location"], page)
        frac = float(st.get("offset") or 0)
        QTimer.singleShot(0, lambda: c.goto_page(page, frac))
        self.a_sidebar.setChecked(bool(settings.get("reading.show_sidebar", True)))
        self.side.setVisible(self.a_sidebar.isChecked())
        self._sync_actions()
        self.a_fav.setChecked(self.db.is_favorite(self.path))
        self.a_fav.setIcon(icon("star_filled", "#ffcc33") if self.a_fav.isChecked() else icon("star"))
        if not self.toc:
            self.side.setCurrentIndex(1)

    def _location(self, page: int) -> int | None:
        if not self.reflow:
            return None
        try:
            return int(self.doc.make_bookmark(self.doc.location_from_page_number(page)))
        except Exception:  # noqa: BLE001
            return None

    def _page_from_location(self, loc, fallback: int) -> int:
        try:
            return int(self.doc.page_number_from_location(self.doc.find_bookmark(int(loc))))
        except Exception:  # noqa: BLE001
            return fallback

    def save_state(self) -> None:
        c = self.canvas
        self.db.update_doc(
            self.path, page=c.current_page, offset=c.position_frac(), location=self._location(c.current_page),
            page_count=self.doc.page_count, zoom=c.zoom, fit=c.fit, view_mode=c.mode,
            dark=1 if c.dark else 0, rtl=1 if c.rtl else 0,
            font_size=self.font_size if self.reflow else None)
        self.stateSaved.emit()

    # ---------------------------------------------------------------- editing
    def display_title(self) -> str:
        return ("● " if self.editor.dirty else "") + self.title

    def flash(self, text: str) -> None:
        self._flash.setText(text)
        self._flash.adjustSize()
        self._flash.move((self.canvas.width() - self._flash.width()) // 2, self.canvas.height() - self._flash.height() - 24)
        self._flash.show()
        self._flash.raise_()
        self._flash_timer.start()

    def toggle_edit(self) -> None:
        ed = self.editor
        if ed.active:
            if not ed.stop():
                self.a_edit.setChecked(True)
                return
            self.a_edit.setChecked(False)
            return
        if not ed.can_edit():
            self.a_edit.setChecked(False)
            r = QMessageBox.question(
                self, tr("Edit"),
                tr("This format can't be edited directly. Convert it to a new PDF and edit that one?"))
            if r == QMessageBox.Yes:
                self.toolRequested.emit("edit_as_pdf", self.path)
            return
        if not ed.start():
            self.a_edit.setChecked(False)
            return
        self.a_edit.setChecked(True)
        if self.comic:
            self.flash(tr("Comic edit mode: pages are saved back as CBZ (a .bak copy is kept)."))

    # ------------------------------------------------------------ signatures
    def show_signatures(self) -> None:
        if not self.doc.is_pdf:
            QMessageBox.information(self, tr("Digital signatures"), tr("Only PDF files can be signed."))
            return
        from .signing_dialogs import SignaturesDialog

        SignaturesDialog(self.path, self, self.password, on_sign=self.start_signing).exec()

    def start_signing(self) -> None:
        if not self.doc.is_pdf or (self.editor.active and self.editor.kind == "comic"):
            QMessageBox.information(self, tr("Sign digitally"),
                                    tr("Only PDF files can be signed. Convert this document to PDF first."))
            return
        if self.editor.dirty and not self.editor.ask_save():
            return
        from .signing_dialogs import SignDialog

        dlg = SignDialog(self.path, self)
        if dlg.exec() != QDialog.Accepted:
            return
        opts = dlg.options()
        if opts["visible"]:
            self.editor.begin_pick_rect(lambda pno, rect: self._do_sign(opts, pno, rect),
                                        tr("Drag the area of the signature on the page (Esc to cancel)."))
        else:
            self._do_sign(opts, None, None)

    def _do_sign(self, opts: dict, pno, rect) -> None:
        from . import editing, signing

        unrot = None
        if rect is not None:
            page = self.doc.load_page(pno)
            unrot = rect * page.derotation_matrix
        replace = opts["replace"]
        dst = self.path + ".signing.pdf" if replace else signing.signed_output_path(self.path)
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            signing.sign_pdf(self.path, dst, opts["cert"], opts["password"], page=pno, rect=unrot,
                             image=opts["image"], reason=opts["reason"], location=opts["location"],
                             stamp_label=tr("Digitally signed by") + "\n%(signer)s\n%(ts)s",
                             doc_password=self.password)
        except Exception as e:  # noqa: BLE001
            QGuiApplication.restoreOverrideCursor()
            QMessageBox.warning(self, tr("Sign digitally"), tr("Could not sign:\n{e}", e=e))
            return
        QGuiApplication.restoreOverrideCursor()
        if replace:
            self.release_file()
            bak = editing.replace_with_backup(dst, self.path)
            if bak:
                self.db.add_backup(bak, self.path)
            self.reload_from(self.path)
            self.fileSaved.emit(self.path, self.path)
            self.flash(tr("Signed. Previous version kept as .bak"))
        else:
            self.openRequested.emit(dst)

    def open_pages(self) -> None:
        if not self.editor.active:
            if not self.editor.can_edit():
                self.toggle_edit()
                return
            self.editor.start()
            self.a_edit.setChecked(True)
        from .pages_dialog import PagesDialog

        dlg = PagesDialog(self.doc, self, rtl=self.canvas.rtl, title=self.title)
        if dlg.exec() == QDialog.Accepted and dlg.result_doc is not None:
            self.editor.replace_with(dlg.result_doc)

    def on_document_replaced(self) -> None:
        n = self.doc.page_count
        self.page_total.setText(f" / {n} ")
        self.page_edit.setValidator(QIntValidator(1, max(1, n)))
        try:
            self.toc = self.doc.get_toc(simple=False)
        except Exception:  # noqa: BLE001
            self.toc = []
        self._fill_toc()
        self._clear_search()
        self.page_edit.setText(str(self.canvas.current_page + 1))

    def release_file(self) -> None:
        """Close the file handle (needed on Windows before the file can be replaced)."""
        self.canvas.close_document()
        try:
            self.doc.close()
        except Exception:  # noqa: BLE001
            pass

    def reload_from(self, path: str) -> None:
        page, frac = self.canvas.current_page, self.canvas.position_frac()
        self.doc = open_document(path, font_size=self.font_size if self.reflow else None,
                                 page_w=settings.get("reading.epub_page_width", 480),
                                 page_h=settings.get("reading.epub_page_height", 680), password=self.password)
        self.canvas.set_document(self.doc)
        self.canvas.goto_page(min(page, self.doc.page_count - 1), frac)
        self.on_document_replaced()

    def reopen(self) -> None:
        """Back to the file on disk (leaving edit mode or discarding changes)."""
        if self.canvas.doc is not None:
            self.canvas.close_document()
        try:
            self.doc.close()
        except Exception:  # noqa: BLE001
            pass
        self.reload_from(self.path)
        self.reload_marks()

    def set_path(self, path: str) -> None:
        if path == self.path:
            self.titleChanged.emit(self.display_title())
            return
        self.path = path
        self.title = os.path.splitext(os.path.basename(path))[0]
        self.comic = is_comic(path)
        self.titleChanged.emit(self.display_title())

    def close_document(self) -> None:
        self._search_timer.stop()
        self.save_state()
        self.canvas.close_document()
        try:
            self.doc.close()
        except Exception:  # noqa: BLE001
            pass

    def _sync_actions(self) -> None:
        c = self.canvas
        self.a_fit_w.setChecked(c.fit == "width")
        self.a_fit_p.setChecked(c.fit == "page")
        self.a_cont.setChecked(c.mode == "continuous")
        self.a_single.setChecked(c.mode == "single")
        self.a_double.setChecked(c.mode == "double")
        self.a_rtl.setChecked(c.rtl)
        self.a_dark.setChecked(c.dark)

    # ------------------------------------------------------- reactions
    def _on_page(self, page: int) -> None:
        if not self.page_edit.hasFocus():
            self.page_edit.setText(str(page + 1))
        self._save_timer.start()

    def _on_zoom(self, z: float) -> None:
        c = self.canvas
        if c.fit == "width":
            self.zoom_box.setCurrentIndex(0)
        elif c.fit == "page":
            self.zoom_box.setCurrentIndex(1)
        else:
            self.zoom_box.setEditText(f"{round(z * 100)}%")
        self._sync_actions()
        self._save_timer.start()

    def _goto_entered(self) -> None:
        try:
            self.canvas.goto_page(int(self.page_edit.text()) - 1)
        except ValueError:
            pass
        self.canvas.setFocus()

    def _focus_page(self) -> None:
        self.page_edit.setFocus()
        self.page_edit.selectAll()

    def _zoom_chosen(self, idx: int) -> None:
        data = self.zoom_box.itemData(idx)
        if isinstance(data, str):
            self._set_fit(data)
        elif data:
            self.canvas.set_zoom(float(data))
        self.canvas.setFocus()

    def _zoom_typed(self) -> None:
        txt = self.zoom_box.currentText().strip().rstrip("%").replace(",", ".")
        try:
            self.canvas.set_zoom(float(txt) / 100)
        except ValueError:
            pass
        self.canvas.setFocus()

    def _set_fit(self, fit: str) -> None:
        self.canvas.set_fit(fit)
        self._sync_actions()

    def _set_mode(self, mode: str) -> None:
        self.canvas.set_mode(mode)
        self._sync_actions()
        self._save_timer.start()

    def _toggle_rtl(self) -> None:
        self.canvas.set_rtl(not self.canvas.rtl)
        self._sync_actions()

    def _toggle_dark(self) -> None:
        self.canvas.set_dark(not self.canvas.dark)
        self._sync_actions()
        self._save_timer.start()

    def apply_dark_settings(self) -> None:
        self.canvas.set_dark(self.canvas.dark, settings.get("dark_doc.background"), settings.get("dark_doc.text"),
                             bool(settings.get("dark_doc.keep_images", True)))

    def _toggle_sidebar(self) -> None:
        on = self.a_sidebar.isChecked()
        self.side.setVisible(on)
        settings.set("reading.show_sidebar", on)

    def _toggle_favorite(self) -> None:
        on = self.a_fav.isChecked()
        self.db.set_favorite(self.path, on)
        self.a_fav.setIcon(icon("star_filled", "#ffcc33") if on else icon("star"))
        self.favoritesChanged.emit()

    def refresh_favorite(self) -> None:
        on = self.db.is_favorite(self.path)
        self.a_fav.setChecked(on)
        self.a_fav.setIcon(icon("star_filled", "#ffcc33") if on else icon("star"))

    def set_chrome_visible(self, on: bool) -> None:
        self.tb.setVisible(on)
        if self.editor.toolbar is not None:
            self.editor.toolbar.setVisible(on and self.editor.active)
        self.side.setVisible(on and self.a_sidebar.isChecked())

    def _escape(self) -> None:
        if self.editor.active and self.editor.tool != "select":
            self.editor.set_tool("select")
        elif self.canvas.selected_text:
            self.canvas.clear_selection()
        elif self.search_edit.hasFocus():
            self.canvas.setFocus()
        elif self.window().isFullScreen():
            self.fullscreenRequested.emit()

    def _set_font(self, size: float) -> None:
        size = max(6.0, min(48.0, size))
        if size == self.font_size:
            return
        c = self.canvas
        loc = self._location(c.current_page)
        self.font_size = size
        c.clear_caches()
        self.doc.layout(width=settings.get("reading.epub_page_width", 480),
                        height=settings.get("reading.epub_page_height", 680), fontsize=size)
        c.set_document(self.doc)
        self.page_total.setText(f" / {self.doc.page_count} ")
        self.page_edit.setValidator(QIntValidator(1, max(1, self.doc.page_count)))
        try:
            self.toc = self.doc.get_toc(simple=False)
        except Exception:  # noqa: BLE001
            self.toc = []
        self._fill_toc()
        self.reload_marks()
        self._clear_search()
        page = self._page_from_location(loc, 0) if loc is not None else 0
        c.goto_page(page)
        self._save_timer.start()

    def _on_link(self, link: dict) -> None:
        kind = link.get("kind")
        page = link.get("page", -1)
        if kind == pymupdf.LINK_URI and link.get("uri"):
            QDesktopServices.openUrl(QUrl(link["uri"]))
            return
        if isinstance(page, int) and page >= 0:
            to = link.get("to")
            self.canvas.goto_page(page, y_pt=to.y if to is not None and kind == pymupdf.LINK_GOTO else None)
            return
        uri = link.get("uri") or link.get("file") or ""
        if uri.startswith(("http://", "https://", "mailto:")):
            QDesktopServices.openUrl(QUrl(uri))
            return
        try:
            loc, _x, y = self.doc.resolve_link(uri)
            if loc is not None:
                p = self.doc.page_number_from_location(loc) if isinstance(loc, tuple) else int(loc)
                if p >= 0:
                    self.canvas.goto_page(p, y_pt=y)
        except Exception:  # noqa: BLE001
            pass

    def _copy_selection(self) -> None:
        if self.canvas.selected_text:
            QGuiApplication.clipboard().setText(self.canvas.selected_text)
        elif self.search_edit.hasFocus():
            self.search_edit.copy()

    def _context_menu(self, page: int, pt: QPointF, gpos: QPoint) -> None:
        m = QMenu(self)
        sel = self.canvas.selected_text
        if sel:
            m.addAction(icon("copy"), tr("Copy"), self._copy_selection)
            short = sel if len(sel) < 30 else sel[:28] + "…"
            m.addAction(icon("search"), tr("Search “{t}”", t=short.replace("\n", " ")),
                        lambda: self._search_for(sel.replace("\n", " ")))
            m.addSeparator()
        m.addAction(icon("note"), tr("Add note here"), lambda: self.add_note(page, pt.x(), pt.y()))
        m.addAction(icon("bookmark_add"), tr("Add bookmark on this page"), lambda: self.add_bookmark(page))
        m.addSeparator()
        a = m.addAction(icon("moon"), tr("Dark document (invert colors)"), self._toggle_dark)
        a.setCheckable(True)
        a.setChecked(self.canvas.dark)
        m.addAction(icon("rotate"), tr("Rotate"), lambda: self.canvas.set_rotation(self.canvas.rotation + 90))
        m.addAction(icon("fullscreen"), tr("Full screen"), self.fullscreenRequested.emit)
        self.editor.extend_menu(m)
        m.exec(gpos)

    # -------------------------------------------------------- TOC
    def _toc_go(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        data = item.data(0, ID_ROLE)
        if not data:
            return
        page, y = data
        if page is not None and page >= 0:
            self.canvas.goto_page(page, y_pt=y)
            self.canvas.setFocus()

    def _chapter_for(self, page: int) -> str:
        title = ""
        for entry in self.toc:
            if entry[2] - 1 <= page:
                title = entry[1]
            else:
                break
        return title

    # -------------------------------------------------- bookmarks & notes
    def reload_marks(self) -> None:
        self.bookmarks = self.db.bookmarks(self.path)
        self.notes = self.db.notes(self.path)
        if self.reflow:
            for b in self.bookmarks:
                if b.get("location") is not None:
                    b["page"] = self._page_from_location(b["location"], b["page"])
            for n in self.notes:
                if n.get("location") is not None:
                    n["page"] = self._page_from_location(n["location"], n["page"])
        self.bookmarks.sort(key=lambda b: (b["page"], b["id"]))
        self.notes.sort(key=lambda n: (n["page"], n["y"] or 0))
        self.bm_list.clear()
        for b in self.bookmarks:
            it = QListWidgetItem(icon("bookmark", theme.ACCENT), f"{b['title']}")
            it.setToolTip(tr("Page {n}", n=b["page"] + 1))
            it.setData(ID_ROLE, b["id"])
            self.bm_list.addItem(it)
        self.note_list.clear()
        for n in self.notes:
            preview = (n["text"] or "").strip().replace("\n", " ")
            if len(preview) > 140:
                preview = preview[:138] + "…"
            it = QListWidgetItem(icon("note", "#ffd54a"), f"p. {n['page'] + 1} — {preview}")
            it.setToolTip(n["text"] or "")
            it.setData(ID_ROLE, n["id"])
            self.note_list.addItem(it)
        self.canvas.notes = self.notes
        self.canvas.bookmarked = {b["page"] for b in self.bookmarks}
        self.canvas.viewport().update()
        self.side.setTabText(1, str(len(self.bookmarks)) if self.bookmarks else "")
        self.side.setTabText(2, str(len(self.notes)) if self.notes else "")

    def add_bookmark(self, page: int | None = None) -> None:
        if page is None or isinstance(page, bool):
            page = self.canvas.current_page
        chapter = self._chapter_for(page)
        title = f"{chapter} — p. {page + 1}" if chapter else tr("Page {n}", n=page + 1)
        self.db.add_bookmark(self.path, page, title, self._location(page))
        self.reload_marks()
        self.a_sidebar.setChecked(True)
        self.side.setVisible(True)
        self.side.setCurrentIndex(1)

    def _bm_go(self, item: QListWidgetItem) -> None:
        bid = item.data(ID_ROLE)
        for b in self.bookmarks:
            if b["id"] == bid:
                self.canvas.goto_page(b["page"])
                break

    def _bm_menu(self, pos) -> None:
        item = self.bm_list.itemAt(pos)
        if item is None:
            return
        bid = item.data(ID_ROLE)
        m = QMenu(self)
        m.addAction(icon("edit"), tr("Rename"), lambda: self._bm_rename(bid, item.text()))
        m.addAction(icon("trash"), tr("Delete"), lambda: (self.db.delete_bookmark(bid), self.reload_marks()))
        m.exec(self.bm_list.viewport().mapToGlobal(pos))

    def _bm_rename(self, bid: int, current: str) -> None:
        text, ok = QInputDialog.getText(self, tr("Rename bookmark"), tr("Name:"), text=current)
        if ok and text.strip():
            self.db.rename_bookmark(bid, text.strip())
            self.reload_marks()

    def _add_note_toolbar(self) -> None:
        page = self.canvas.current_page
        w, _h = self.canvas.sizes[page] if self.canvas.sizes else (100, 100)
        self.add_note(page, w - 24, 24 + 22 * sum(1 for n in self.notes if n["page"] == page))

    def add_note(self, page: int, x: float, y: float) -> None:
        dlg = NoteDialog(self, page=page)
        if dlg.exec() == QDialog.Accepted and dlg.text():
            self.db.add_note(self.path, page, x, y, dlg.text(), self._location(page))
            self.reload_marks()
            self.a_sidebar.setChecked(True)
            self.side.setVisible(True)
            self.side.setCurrentIndex(2)

    def edit_note(self, nid: int) -> None:
        note = next((n for n in self.notes if n["id"] == nid), None)
        if note is None:
            return
        dlg = NoteDialog(self, note["text"] or "", note["page"], can_delete=True)
        res = dlg.exec()
        if res == NoteDialog.DELETE:
            self.db.delete_note(nid)
        elif res == QDialog.Accepted:
            if dlg.text():
                self.db.update_note(nid, dlg.text())
            else:
                self.db.delete_note(nid)
        self.reload_marks()

    def _note_go(self, item: QListWidgetItem) -> None:
        nid = item.data(ID_ROLE)
        note = next((n for n in self.notes if n["id"] == nid), None)
        if note is not None:
            self.canvas.goto_page(note["page"], y_pt=max(0, (note["y"] or 0) - 40))

    def _note_menu(self, pos) -> None:
        item = self.note_list.itemAt(pos)
        if item is None:
            return
        nid = item.data(ID_ROLE)
        m = QMenu(self)
        m.addAction(icon("edit"), tr("Edit"), lambda: self.edit_note(nid))
        m.addAction(icon("trash"), tr("Delete"), lambda: (self.db.delete_note(nid), self.reload_marks()))
        m.exec(self.note_list.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ search
    def _focus_search(self) -> None:
        sel = self.canvas.selected_text
        if sel and "\n" not in sel and len(sel) < 80:
            self.search_edit.setText(sel)
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _search_for(self, text: str) -> None:
        self.search_edit.setText(text)
        self._search_enter()

    def _search_text_changed(self, text: str) -> None:
        if not text:
            self._clear_search()

    def _clear_search(self) -> None:
        self._search_timer.stop()
        self._search_query = ""
        self._search_results = []
        self._search_index = -1
        self.canvas.search_hits = {}
        self.canvas.search_current = None
        self.result_list.clear()
        self.search_count.setText("")
        self.side.setTabText(3, "")
        self.canvas.viewport().update()

    def _search_enter(self) -> None:
        q = self.search_edit.text().strip()
        if not q:
            return
        if q != self._search_query:
            self._clear_search()
            self._search_query = q
            self._search_page = 0
            self._search_start_page = self.canvas.current_page
            self._jumped = False
            self.search_count.setText(tr("Searching…"))
            self._search_timer.start()
        else:
            if QGuiApplication.keyboardModifiers() & Qt.ShiftModifier:
                self._search_move(-1)
            else:
                self._search_move(1)

    def _search_step(self) -> None:
        q = self._search_query
        n = self.doc.page_count
        t0 = time.perf_counter()
        added = False
        while self._search_page < n and time.perf_counter() - t0 < 0.025:
            i = self._search_page
            self._search_page += 1
            try:
                page = self.doc.load_page(i)
                rects = page.search_for(q)
            except Exception:  # noqa: BLE001
                continue
            if not rects:
                continue
            rotm = page.rotation_matrix
            shown = [r * rotm for r in rects]
            self.canvas.search_hits[i] = shown
            for k, r in enumerate(rects):
                self._search_results.append((i, k))
                try:
                    line = page.get_textbox(pymupdf.Rect(0, r.y0, page.rect.width + 2000, r.y1)).strip()
                except Exception:  # noqa: BLE001
                    line = q
                line = " ".join(line.split())
                if len(line) > 110:
                    pos = line.casefold().find(q.casefold())
                    start = max(0, pos - 40)
                    line = ("…" if start else "") + line[start:start + 100] + "…"
                it = QListWidgetItem(f"p. {i + 1}   {line}")
                it.setData(ID_ROLE, len(self._search_results) - 1)
                self.result_list.addItem(it)
            added = True
            if not self._jumped and i >= self._search_start_page:
                self._jumped = True
                self._search_index = len(self._search_results) - len(rects) - 1
                self._search_move(1)
        if added:
            self.canvas.viewport().update()
        total = len(self._search_results)
        if self._search_page < n:
            pct = int(self._search_page * 100 / max(1, n))
            self.search_count.setText(f"{total} · {pct}%")
            self._search_timer.start()
        else:
            if not self._jumped and total:
                self._jumped = True
                self._search_index = -1
                self._search_move(1)
            self._update_count()
            if total:
                self.side.setTabText(3, str(total))
                if self.a_sidebar.isChecked():
                    self.side.setCurrentIndex(3)

    def _update_count(self) -> None:
        total = len(self._search_results)
        if not total:
            self.search_count.setText(tr("No results") if self._search_page >= self.doc.page_count else "")
            if self._search_page >= self.doc.page_count and not self.reflow and not any(
                    self.doc.load_page(i).get_text("text").strip()
                    for i in range(0, self.doc.page_count, max(1, self.doc.page_count // 8))):
                self.flash(tr("This document has no text layer (scan). Use Tools → Make searchable (OCR)."))
        else:
            self.search_count.setText(f"{self._search_index + 1} / {total}")

    def _search_move(self, d: int) -> None:
        if not self._search_results:
            if self.search_edit.text().strip() and self.search_edit.text().strip() != self._search_query:
                self._search_enter()
            return
        self._search_index = (self._search_index + d) % len(self._search_results)
        self._show_result()

    def _show_result(self) -> None:
        page, k = self._search_results[self._search_index]
        self.canvas.search_current = (page, k)
        rect = self.canvas.search_hits[page][k]
        self.canvas.ensure_visible(page, rect)
        self.result_list.blockSignals(True)
        self.result_list.setCurrentRow(self._search_index)
        self.result_list.blockSignals(False)
        if self._search_page >= self.doc.page_count:
            self._update_count()
        else:
            self.search_count.setText(f"{self._search_index + 1} / {len(self._search_results)}…")

    def _result_go(self, item: QListWidgetItem) -> None:
        self._search_index = item.data(ID_ROLE)
        self._show_result()
