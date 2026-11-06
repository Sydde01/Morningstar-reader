"""Main window and application bootstrap."""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QMainWindow, QMenu, QMessageBox,
                               QTabBar, QTabWidget, QToolButton, QWidget)

from . import __app_name__, __version__, theme
from .config import DOC_EXTS, settings
from .db import Database, norm_path
from .docbackend import OpenError
from .i18n import tr
from .icons import icon
from .library import LibraryWidget
from .thumbs import ThumbnailManager
from .viewer import DocumentTab

SERVER_NAME = "MorningstarReader-single-instance"


def resource_path(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(__app_name__)
        self.setAcceptDrops(True)
        self.db = Database()
        self.thumbs = ThumbnailManager(self)
        self._fullscreen_was_max = False

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.ElideRight)
        self.tabs.setIconSize(QSize(16, 16))
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        self.library = LibraryWidget(self.db, self.thumbs)
        self.library.openRequested.connect(self.open_path)
        self.tabs.addTab(self.library, icon("library", theme.ACCENT), tr("Library"))
        self.tabs.tabBar().setTabButton(0, QTabBar.RightSide, None)
        self.tabs.tabBar().setTabButton(0, QTabBar.LeftSide, None)

        menu_btn = QToolButton()
        menu_btn.setIcon(icon("menu"))
        menu_btn.setPopupMode(QToolButton.InstantPopup)
        menu_btn.setAutoRaise(True)
        m = QMenu(self)
        self._add_action(m, "open", tr("Open a file…"), self.open_dialog, "Ctrl+O")
        self._add_action(m, "library", tr("Library"), lambda: self.tabs.setCurrentIndex(0), "Ctrl+L")
        self._add_action(m, "refresh", tr("Rescan library"), self.library.rescan, "F5")
        m.addSeparator()
        self._add_action(m, "settings", tr("Settings"), self.open_settings, "Ctrl+,")
        self._add_action(m, "info", tr("About"), self.about)
        m.addSeparator()
        self._add_action(m, "close", tr("Quit"), self.close, "Ctrl+Q")
        menu_btn.setMenu(m)
        open_btn = QToolButton(icon=icon("open"), toolTip=tr("Open a file…") + "  (Ctrl+O)")
        open_btn.setAutoRaise(True)
        open_btn.clicked.connect(self.open_dialog)
        cw = QWidget(objectName="tabCorner")
        cl = QHBoxLayout(cw)
        cl.setContentsMargins(0, 0, 4, 0)
        cl.setSpacing(0)
        cl.addWidget(open_btn)
        cl.addWidget(menu_btn)
        self.tabs.setCornerWidget(cw, Qt.TopRightCorner)

        self._add_action(self, None, "close tab", self.close_current_tab, "Ctrl+W")
        self._add_action(self, None, "next tab", lambda: self._cycle(1), "Ctrl+Tab")
        self._add_action(self, None, "prev tab", lambda: self._cycle(-1), "Ctrl+Shift+Tab")

        self._restore_geometry()
        self.library.reload()
        if settings.get("library.scan_on_startup", True):
            QTimer.singleShot(400, self.library.rescan)

    def _add_action(self, target, ic, text, slot, shortcut=None) -> QAction:
        a = QAction(icon(ic) if ic else QIcon(), text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
            a.setShortcutContext(Qt.WindowShortcut)
        a.triggered.connect(slot)
        target.addAction(a)
        if target is not self:
            self.addAction(a)
        return a

    # ------------------------------------------------------------- docs
    def tab_for(self, path: str) -> int:
        for i in range(1, self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, DocumentTab) and w.path == path:
                return i
        return -1

    def open_path(self, path: str, focus: bool = True) -> None:
        path = norm_path(path)
        i = self.tab_for(path)
        if i >= 0:
            if focus:
                self.tabs.setCurrentIndex(i)
            return
        if not os.path.exists(path):
            QMessageBox.warning(self, __app_name__, tr("File not found:\n{p}", p=path))
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            tab = DocumentTab(path, self.db)
        except OpenError as e:
            QApplication.restoreOverrideCursor()
            msg = str(e)
            if msg == "rar_tool_missing":
                text = tr("This CBR file is a RAR archive. Install 7-Zip (7-zip.org) to read it.")
            elif msg == "password_protected":
                text = tr("This document is password protected.")
            else:
                text = tr("Cannot open this document:\n{e}", e=msg)
            QMessageBox.warning(self, __app_name__, text)
            return
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, __app_name__, tr("Cannot open this document:\n{e}", e=e))
            return
        QApplication.restoreOverrideCursor()
        tab.fullscreenRequested.connect(self.toggle_fullscreen)
        tab.favoritesChanged.connect(self.library.refresh_state)
        tab.stateSaved.connect(self._schedule_library_refresh)
        ext = os.path.splitext(path)[1].lower()
        ic = {"pdf": "fit_page", "cbz": "tiles", "cbr": "tiles"}.get(ext[1:], "library")
        idx = self.tabs.addTab(tab, icon(ic, theme.TEXT_DIM), tab.title)
        self.tabs.setTabToolTip(idx, path)
        if focus:
            self.tabs.setCurrentIndex(idx)
            tab.canvas.setFocus()
        self.library.refresh_state()

    def _schedule_library_refresh(self) -> None:
        if not hasattr(self, "_lib_refresh"):
            self._lib_refresh = QTimer(self, singleShot=True, interval=2000, timeout=self.library.refresh_state)
        self._lib_refresh.start()

    def open_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(DOC_EXTS))
        start = (settings.get("library.roots") or [os.path.expanduser("~")])[0]
        files, _ = QFileDialog.getOpenFileNames(
            self, tr("Open a file…"), start, f"{tr('Documents')} ({exts});;{tr('All files')} (*.*)")
        for f in files:
            self.open_path(f)

    def close_tab(self, index: int) -> None:
        w = self.tabs.widget(index)
        if not isinstance(w, DocumentTab):
            return
        w.close_document()
        self.tabs.removeTab(index)
        w.deleteLater()
        self.library.refresh_state()

    def close_current_tab(self) -> None:
        if self.tabs.currentIndex() > 0:
            self.close_tab(self.tabs.currentIndex())

    def _cycle(self, d: int) -> None:
        n = self.tabs.count()
        if n > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + d) % n)

    def _on_tab_changed(self, idx: int) -> None:
        w = self.tabs.widget(idx)
        if isinstance(w, DocumentTab):
            self.setWindowTitle(f"{w.title} — {__app_name__}")
            w.refresh_favorite()
            w.canvas.setFocus()
        else:
            self.setWindowTitle(__app_name__)
            self.library.refresh_state()

    # --------------------------------------------------------- fullscreen
    def toggle_fullscreen(self) -> None:
        w = self.tabs.currentWidget()
        if self.isFullScreen():
            self.tabs.tabBar().show()
            self.tabs.cornerWidget(Qt.TopRightCorner).show()
            for i in range(1, self.tabs.count()):
                self.tabs.widget(i).set_chrome_visible(True)
            self.showMaximized() if self._fullscreen_was_max else self.showNormal()
        else:
            self._fullscreen_was_max = self.isMaximized()
            self.tabs.tabBar().hide()
            self.tabs.cornerWidget(Qt.TopRightCorner).hide()
            if isinstance(w, DocumentTab):
                w.set_chrome_visible(False)
            self.showFullScreen()
        if isinstance(w, DocumentTab):
            w.canvas.setFocus()

    # ----------------------------------------------------------- settings
    def open_settings(self) -> None:
        from .settings_dialog import SettingsDialog

        old_roots = [norm_path(r) for r in settings.get("library.roots", [])]
        dlg = SettingsDialog(self)
        if dlg.exec() != SettingsDialog.Accepted:
            return
        if getattr(dlg, "cache_cleared", False):
            self.thumbs.clear_memory()
        self.library.set_thumb_size(int(settings.get("library.thumb_size", 170)))
        new_roots = [norm_path(r) for r in dlg.new_roots]
        if new_roots != old_roots:
            self.library.set_roots(new_roots)
            settings.save()
        else:
            self.library.reload()
        for i in range(1, self.tabs.count()):
            t = self.tabs.widget(i)
            t.apply_dark_settings()
            t.canvas.gap = int(settings.get("reading.page_gap", 12))
            t.canvas.cover_alone = bool(settings.get("reading.cover_alone", True))
            t.canvas._relayout_keep()

    def about(self) -> None:
        QMessageBox.about(
            self, __app_name__,
            f"<h3>{__app_name__} {__version__}</h3>"
            + tr("Fast, dark, open-source PDF / EPUB / comics reader.")
            + "<br><br>AGPL-3.0 · MuPDF / PyMuPDF · Qt / PySide6<br>"
            '<a href="https://github.com/Sydde01/Morningstar-reader">github.com/Sydde01/Morningstar-reader</a>')

    # ------------------------------------------------------- persistence
    def _restore_geometry(self) -> None:
        geo = settings.get("window_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        else:
            self.resize(1400, 900)

    def show_initial(self) -> None:
        if settings.get("window_maximized", True):
            self.showMaximized()
        else:
            self.show()

    def restore_session(self) -> None:
        if not settings.get("restore_session", True):
            return
        tabs = settings.get("session_tabs", [])
        current = settings.get("session_current", 0)
        for p in tabs:
            if os.path.exists(p):
                self.open_path(p, focus=False)
        if 0 <= current < self.tabs.count():
            self.tabs.setCurrentIndex(current)

    def closeEvent(self, ev) -> None:
        paths = []
        for i in range(1, self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, DocumentTab):
                paths.append(w.path)
                w.close_document()
        settings.set("session_tabs", paths)
        settings.set("session_current", self.tabs.currentIndex())
        if self.isFullScreen():
            self.showNormal()
        settings.set("window_maximized", self.isMaximized())
        settings.set("window_geometry", bytes(self.saveGeometry().toBase64()).decode())
        settings.save()
        self.library.stop_scan()
        self.thumbs.shutdown()
        super().closeEvent(ev)

    # --------------------------------------------------------- drag & drop
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        for url in ev.mimeData().urls():
            p = url.toLocalFile()
            if p and os.path.splitext(p)[1].lower() in DOC_EXTS:
                self.open_path(p)

    def receive_paths(self, paths: list[str]) -> None:
        for p in paths:
            self.open_path(p)
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()


def _send_to_running(paths: list[str]) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(300):
        return False
    sock.write(("\n".join(paths) + "\n").encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def run(argv: list[str]) -> int:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Sydde01.MorningstarReader")
        except Exception:  # noqa: BLE001
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Morningstar")
    files = [os.path.abspath(a) for a in argv[1:] if os.path.isfile(a)]
    if _send_to_running(files or []):
        return 0
    theme.apply_dark_theme(app)
    ico = resource_path("assets", "icon.ico")
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))

    win = MainWindow()
    server = QLocalServer(win)
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)

    def on_conn():
        sock = server.nextPendingConnection()

        def read():
            data = bytes(sock.readAll()).decode("utf-8", "replace")
            win.receive_paths([p for p in data.splitlines() if p.strip()])

        sock.readyRead.connect(read)

    server.newConnection.connect(on_conn)
    win.show_initial()
    win.restore_session()
    for f in files:
        win.open_path(f)
    return app.exec()
