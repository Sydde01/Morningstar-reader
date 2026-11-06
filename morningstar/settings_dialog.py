"""Settings dialog."""
from __future__ import annotations

import os
import shutil

from PySide6.QtCore import QRect, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QSlider, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from . import __version__, theme
from .config import THUMB_DIR, CONVERT_DIR, settings
from .db import norm_path
from .i18n import LANGUAGES, tr
from .icons import icon
from .utils import human_size

PRESETS = [
    ("Soft dark", "#121212", "#e8e6e3"),
    ("Pure black (OLED)", "#000000", "#ffffff"),
    ("Dim grey", "#1e1e1e", "#d4d4d4"),
    ("Sepia night", "#1b1611", "#e3d3b8"),
    ("Hellfire", "#0f0506", "#ffb3b3"),
]

SHORTCUTS = [
    ("Ctrl+O", "Open a file"), ("Ctrl+W", "Close tab"), ("Ctrl+Tab", "Next tab"),
    ("Ctrl+L", "Library"), ("Ctrl+F", "Search in document"), ("F3 / Shift+F3", "Next / previous result"),
    ("Ctrl+G", "Go to page"), ("Ctrl+ + / Ctrl+ -", "Zoom in / out"), ("Ctrl+wheel", "Zoom"),
    ("Ctrl+0", "Fit page"), ("Ctrl+1", "Actual size"), ("Ctrl+2", "Fit width"),
    ("Ctrl+6 / 7 / 8", "Single page / two pages / continuous"), ("Ctrl+R", "Rotate"),
    ("Ctrl+D", "Dark document on / off"), ("Ctrl+B", "Add bookmark"), ("Ctrl+Shift+N", "Add note"),
    ("Ctrl+C", "Copy selected text"), ("F11", "Full screen"), ("F12", "Side panel"),
    ("←/→, PgUp/PgDn, Space", "Navigate"), ("Home / End", "First / last page"),
    ("Alt+drag, middle-drag", "Pan the page"),
]


def _dir_size(path) -> int:
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class ColorButton(QPushButton):
    def __init__(self, color: str, on_change):
        super().__init__()
        self.color = color
        self.on_change = on_change
        self.setFixedSize(QSize(90, 28))
        self.clicked.connect(self._pick)
        self._style()

    def _style(self) -> None:
        self.setText(self.color.upper())
        fg = "#000" if QColor(self.color).lightness() > 140 else "#fff"
        self.setStyleSheet(f"QPushButton {{ background: {self.color}; color: {fg}; border: 1px solid #555; }}")

    def set_color(self, c: str) -> None:
        self.color = c
        self._style()

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self.color), self, tr("Choose a color"))
        if c.isValid():
            self.set_color(c.name())
            self.on_change()


class DarkPreview(QWidget):
    def __init__(self):
        super().__init__()
        self.bg, self.fg = "#121212", "#e8e6e3"
        self.setMinimumSize(200, 150)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(10, 6, -10, -6)
        p.fillRect(self.rect(), QColor(theme.CANVAS))
        p.fillRect(r, QColor(self.bg))
        f = QFont(self.font())
        f.setPointSize(12)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(self.fg))
        p.drawText(QRect(r.x() + 14, r.y() + 10, r.width() - 28, 24), Qt.AlignLeft, "Chapter I")
        f.setPointSize(9)
        f.setBold(False)
        p.setFont(f)
        txt = ("The most merciful thing in the world, I think, is the inability of the human mind "
               "to correlate all its contents. We live on a placid island of ignorance in the midst "
               "of black seas of infinity…")
        p.drawText(QRect(r.x() + 14, r.y() + 40, r.width() - 28, r.height() - 46), Qt.TextWordWrap, txt)


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Settings"))
        self.resize(820, 560)
        outer = QVBoxLayout(self)
        body = QHBoxLayout()
        outer.addLayout(body, 1)
        self.nav = QListWidget(objectName="settingsNav")
        self.nav.setFixedWidth(190)
        self.nav.setIconSize(QSize(20, 20))
        self.pages = QStackedWidget()
        body.addWidget(self.nav)
        body.addWidget(self.pages, 1)
        for ic, name, builder in (("settings", tr("General"), self._page_general),
                                  ("library", tr("Library"), self._page_library),
                                  ("single", tr("Reading"), self._page_reading),
                                  ("moon", tr("Dark document"), self._page_dark),
                                  ("info", tr("About"), self._page_about)):
            self.nav.addItem(QListWidgetItem(icon(ic), name))
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(16, 6, 6, 6)
            title = QLabel(name, objectName="title")
            lay.addWidget(title)
            builder(lay)
            lay.addStretch(1)
            self.pages.addWidget(w)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("Save"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    # ---------------------------------------------------------------- pages
    def _page_general(self, lay) -> None:
        form = QFormLayout()
        self.lang = QComboBox()
        for code, name in LANGUAGES.items():
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(settings.get("language"))))
        form.addRow(tr("Language"), self.lang)
        lay.addLayout(form)
        hint = QLabel(tr("Language changes apply after a restart."), objectName="dim")
        lay.addWidget(hint)
        self.restore = QCheckBox(tr("Reopen the documents of the previous session"))
        self.restore.setChecked(bool(settings.get("restore_session", True)))
        lay.addWidget(self.restore)

    def _page_library(self, lay) -> None:
        box = QGroupBox(tr("Library folders"))
        bl = QVBoxLayout(box)
        self.roots = QListWidget()
        self.roots.setMaximumHeight(140)
        for r in settings.get("library.roots", []):
            self.roots.addItem(QListWidgetItem(icon("folder"), r))
        bl.addWidget(self.roots)
        row = QHBoxLayout()
        add = QPushButton(icon("folder"), tr("Add folder"))
        add.clicked.connect(self._add_root)
        rem = QPushButton(icon("trash"), tr("Remove"))
        rem.clicked.connect(lambda: [self.roots.takeItem(self.roots.row(i)) for i in self.roots.selectedItems()])
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        bl.addLayout(row)
        bl.addWidget(QLabel(tr("Each sub-folder becomes a category, e.g. “Novels / Lovecraft”."), objectName="dim"))
        lay.addWidget(box)

        self.strip = QCheckBox(tr("Hide numeric prefixes in category names (“00_Marvel” → “Marvel”)"))
        self.strip.setChecked(bool(settings.get("library.strip_prefixes", True)))
        lay.addWidget(self.strip)
        self.merge = QCheckBox(tr("Merge folders that contain a single document into their parent"))
        self.merge.setChecked(bool(settings.get("library.merge_single_doc_folders", True)))
        lay.addWidget(self.merge)
        self.scan_start = QCheckBox(tr("Scan for new files at startup"))
        self.scan_start.setChecked(bool(settings.get("library.scan_on_startup", True)))
        lay.addWidget(self.scan_start)

        form = QFormLayout()
        self.thumb = QSlider(Qt.Horizontal)
        self.thumb.setRange(110, 280)
        self.thumb.setValue(int(settings.get("library.thumb_size", 170)))
        self.thumb_lbl = QLabel()
        self.thumb.valueChanged.connect(lambda v: self.thumb_lbl.setText(f"{v}px"))
        self.thumb_lbl.setText(f"{self.thumb.value()}px")
        trow = QHBoxLayout()
        trow.addWidget(self.thumb, 1)
        trow.addWidget(self.thumb_lbl)
        form.addRow(tr("Cover size"), trow)
        lay.addLayout(form)
        crow = QHBoxLayout()
        self.cache_lbl = QLabel(objectName="dim")
        clear = QPushButton(icon("trash"), tr("Clear cover cache"))
        clear.clicked.connect(self._clear_cache)
        crow.addWidget(clear)
        crow.addWidget(self.cache_lbl, 1)
        lay.addLayout(crow)
        self._update_cache_label()

    def _page_reading(self, lay) -> None:
        modes = [("continuous", tr("Continuous scrolling")), ("single", tr("Single page")),
                 ("double", tr("Two pages"))]
        fits = [("width", tr("Fit width")), ("page", tr("Fit page")), ("none", tr("Actual size"))]

        def combo(items, value):
            c = QComboBox()
            for k, label in items:
                c.addItem(label, k)
            c.setCurrentIndex(max(0, c.findData(value)))
            return c

        g1 = QGroupBox(tr("Books and documents (PDF, EPUB…)"))
        f1 = QFormLayout(g1)
        self.mode = combo(modes, settings.get("reading.view_mode"))
        self.fit = combo(fits, settings.get("reading.fit"))
        f1.addRow(tr("Layout"), self.mode)
        f1.addRow(tr("Zoom"), self.fit)
        lay.addWidget(g1)

        g2 = QGroupBox(tr("Comics (CBZ, CBR)"))
        f2 = QFormLayout(g2)
        self.cmode = combo(modes, settings.get("reading.comics_view_mode"))
        self.cfit = combo(fits, settings.get("reading.comics_fit"))
        self.rtl = QCheckBox(tr("Right-to-left reading (manga)"))
        self.rtl.setChecked(bool(settings.get("reading.comics_rtl")))
        f2.addRow(tr("Layout"), self.cmode)
        f2.addRow(tr("Zoom"), self.cfit)
        f2.addRow("", self.rtl)
        lay.addWidget(g2)

        g3 = QGroupBox(tr("General"))
        f3 = QFormLayout(g3)
        self.cover = QCheckBox(tr("Show the cover alone in two-page mode"))
        self.cover.setChecked(bool(settings.get("reading.cover_alone", True)))
        f3.addRow("", self.cover)
        self.gap = QSpinBox()
        self.gap.setRange(0, 60)
        self.gap.setSuffix(" px")
        self.gap.setValue(int(settings.get("reading.page_gap", 12)))
        f3.addRow(tr("Space between pages"), self.gap)
        self.font_size = QSpinBox()
        self.font_size.setRange(6, 48)
        self.font_size.setValue(int(settings.get("reading.epub_font_size", 12)))
        f3.addRow(tr("EPUB default text size"), self.font_size)
        lay.addWidget(g3)

    def _page_dark(self, lay) -> None:
        lay.addWidget(QLabel(tr("The app is always dark. This option also turns document pages dark: "
                                "white background → dark, black text → light. Toggle it per document "
                                "with the moon button (Ctrl+D)."), wordWrap=True, objectName="dim"))
        g = QGroupBox(tr("Enabled by default for"))
        gl = QVBoxLayout(g)
        self.d_pdf = QCheckBox(tr("PDF / XPS documents"))
        self.d_pdf.setChecked(bool(settings.get("dark_doc.default_pdf")))
        self.d_reflow = QCheckBox(tr("E-books (EPUB, MOBI, FB2)"))
        self.d_reflow.setChecked(bool(settings.get("dark_doc.default_reflow")))
        self.d_comics = QCheckBox(tr("Comics (CBZ, CBR)"))
        self.d_comics.setChecked(bool(settings.get("dark_doc.default_comics")))
        for w in (self.d_pdf, self.d_reflow, self.d_comics):
            gl.addWidget(w)
        lay.addWidget(g)

        g2 = QGroupBox(tr("Colors"))
        row = QHBoxLayout(g2)
        form = QFormLayout()
        self.preset = QComboBox()
        for name, bg, fg in PRESETS:
            self.preset.addItem(tr(name), (bg, fg))
        self.preset.addItem(tr("Custom"), None)
        self.preset.activated.connect(self._apply_preset)
        form.addRow(tr("Preset"), self.preset)
        self.bg_btn = ColorButton(settings.get("dark_doc.background"), self._colors_changed)
        self.fg_btn = ColorButton(settings.get("dark_doc.text"), self._colors_changed)
        form.addRow(tr("Background"), self.bg_btn)
        form.addRow(tr("Text"), self.fg_btn)
        self.keep_img = QCheckBox(tr("Keep pictures in their original colors"))
        self.keep_img.setChecked(bool(settings.get("dark_doc.keep_images", True)))
        form.addRow("", self.keep_img)
        row.addLayout(form, 1)
        self.preview = DarkPreview()
        row.addWidget(self.preview, 1)
        lay.addWidget(g2)
        self._colors_changed()

    def _page_about(self, lay) -> None:
        lay.addWidget(QLabel(f"<b>Morningstar Reader</b> {__version__}<br>"
                             + tr("Fast, dark, open-source PDF / EPUB / comics reader.")
                             + "<br>License: AGPL-3.0 · Powered by MuPDF (PyMuPDF) and Qt (PySide6)"))
        gh = QPushButton(icon("explorer"), "GitHub")
        gh.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/Sydde01/Morningstar-reader")))
        row = QHBoxLayout()
        row.addWidget(gh)
        row.addStretch(1)
        lay.addLayout(row)
        box = QGroupBox(tr("Keyboard shortcuts"))
        form = QFormLayout(box)
        for keys, what in SHORTCUTS:
            k = QLabel(f"<code>{keys}</code>")
            form.addRow(k, QLabel(tr(what)))
        lay.addWidget(box)

    # --------------------------------------------------------------- helpers
    def _add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("Choose a library folder"))
        if folder:
            folder = norm_path(folder)
            existing = [self.roots.item(i).text() for i in range(self.roots.count())]
            if folder not in existing:
                self.roots.addItem(QListWidgetItem(icon("folder"), folder))

    def _update_cache_label(self) -> None:
        self.cache_lbl.setText(tr("Cache: {s}", s=human_size(_dir_size(THUMB_DIR) + _dir_size(CONVERT_DIR))))

    def _clear_cache(self) -> None:
        for d in (THUMB_DIR, CONVERT_DIR):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
        self.cache_cleared = True
        self._update_cache_label()

    def _apply_preset(self, idx: int) -> None:
        data = self.preset.itemData(idx)
        if data:
            self.bg_btn.set_color(data[0])
            self.fg_btn.set_color(data[1])
            self._colors_changed()

    def _colors_changed(self) -> None:
        bg, fg = self.bg_btn.color, self.fg_btn.color
        self.preview.bg, self.preview.fg = bg, fg
        self.preview.update()
        idx = next((i for i, (_n, b, f) in enumerate(PRESETS) if b.lower() == bg.lower() and f.lower() == fg.lower()),
                   len(PRESETS))
        self.preset.setCurrentIndex(idx)

    def _save(self) -> None:
        s = settings
        s.set("language", self.lang.currentData())
        s.set("restore_session", self.restore.isChecked())
        self.new_roots = [self.roots.item(i).text() for i in range(self.roots.count())]
        s.set("library.strip_prefixes", self.strip.isChecked())
        s.set("library.merge_single_doc_folders", self.merge.isChecked())
        s.set("library.scan_on_startup", self.scan_start.isChecked())
        s.set("library.thumb_size", self.thumb.value())
        s.set("reading.view_mode", self.mode.currentData())
        s.set("reading.fit", self.fit.currentData())
        s.set("reading.comics_view_mode", self.cmode.currentData())
        s.set("reading.comics_fit", self.cfit.currentData())
        s.set("reading.comics_rtl", self.rtl.isChecked())
        s.set("reading.cover_alone", self.cover.isChecked())
        s.set("reading.page_gap", self.gap.value())
        s.set("reading.epub_font_size", self.font_size.value())
        s.set("dark_doc.default_pdf", self.d_pdf.isChecked())
        s.set("dark_doc.default_reflow", self.d_reflow.isChecked())
        s.set("dark_doc.default_comics", self.d_comics.isChecked())
        s.set("dark_doc.background", self.bg_btn.color)
        s.set("dark_doc.text", self.fg_btn.color)
        s.set("dark_doc.keep_images", self.keep_img.isChecked())
        s.save()
        self.accept()
