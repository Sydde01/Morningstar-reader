"""Tool dialogs: convert, split double pages (single / batch), merge, and .bak backups."""
from __future__ import annotations

import datetime
import json
import os
import zipfile

import pymupdf
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QRadioButton, QSpinBox, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import convert, editing, theme
from .config import DOC_EXTS, settings
from .db import Database
from .i18n import tr
from .icons import icon
from .jobs import Job
from .utils import human_size, natural_key, show_in_explorer

DOC_FILTER = "Documents (" + " ".join(f"*{e}" for e in sorted(DOC_EXTS)) + ")"
TARGET_LABELS = {"pdf": "PDF", "epub": "EPUB", "cbz": "CBZ", "cbr": "CBR"}


class JobPanel(QWidget):
    """Progress bar + status + cancel button for a background Job."""
    cancelRequested = Signal()

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.label = QLabel(objectName="dim")
        lay.addWidget(self.label)
        lay.addWidget(self.bar, 1)
        self.hide()

    def set_progress(self, frac: float) -> None:
        self.bar.setValue(int(frac * 1000))


class _JobDialog(QDialog):
    """Base: runs one Job, shows progress, can be cancelled before the end."""
    filesCreated = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.job: Job | None = None
        self.created: list[str] = []
        self.errors: list[str] = []

    def _bottom(self, lay, ok_text: str) -> None:
        self.panel = JobPanel()
        lay.addWidget(self.panel)
        row = QHBoxLayout()
        self.open_btn = QPushButton(icon("open"), tr("Open"))
        self.open_btn.hide()
        self.folder_btn = QPushButton(icon("explorer"), tr("Show in Explorer"))
        self.folder_btn.hide()
        self.folder_btn.clicked.connect(lambda: self.created and show_in_explorer(self.created[0]))
        row.addWidget(self.open_btn)
        row.addWidget(self.folder_btn)
        row.addStretch(1)
        self.cancel_btn = QPushButton(tr("Cancel"))
        self.cancel_btn.clicked.connect(self._cancel)
        self.ok_btn = QPushButton(ok_text)
        self.ok_btn.setProperty("accent", True)
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self._start)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.ok_btn)
        lay.addLayout(row)

    def showEvent(self, ev) -> None:
        # only the main action button is highlighted / triggered by Enter
        for b in self.findChildren(QPushButton):
            if b is not getattr(self, "ok_btn", None):
                b.setAutoDefault(False)
                b.setDefault(False)
        super().showEvent(ev)

    def _run(self, kind: str, items: list, opts: dict) -> None:
        self.created, self.errors = [], []
        self.job = Job(kind, items, opts, self)
        self.job.progress.connect(lambda f, _i: self.panel.set_progress(f))
        self.job.itemDone.connect(self._item_done)
        self.job.itemError.connect(self._item_error)
        self.job.finished.connect(self._finished)
        self.panel.show()
        self.panel.label.setText(tr("Working…"))
        self.ok_btn.setEnabled(False)
        self.cancel_btn.setText(tr("Stop"))
        self._set_inputs_enabled(False)
        self.job.start()

    def _set_inputs_enabled(self, on: bool) -> None:
        pass

    def _item_done(self, idx: int, out: str) -> None:
        self.created.append(out)

    def _item_error(self, idx: int, msg: str) -> None:
        self.errors.append(msg)

    def _finished(self, ok: bool) -> None:
        self.job = None
        self.cancel_btn.setText(tr("Close"))
        self._set_inputs_enabled(True)
        self.ok_btn.setEnabled(True)
        if ok:
            self.panel.set_progress(1)
            msg = tr("Done: {n} file(s) created.", n=len(self.created))
        else:
            msg = tr("Stopped. Nothing was kept from the unfinished file.")
        if self.errors:
            msg += "  " + tr("{n} error(s).", n=len(self.errors))
            self.panel.label.setToolTip("\n".join(self.errors))
        self.panel.label.setText(msg)
        if self.created:
            self.folder_btn.show()
            self.filesCreated.emit(list(self.created))
        if self.errors and not self.created:
            QMessageBox.warning(self, self.windowTitle(), "\n".join(self.errors[:8]))

    def _cancel(self) -> None:
        if self.job is not None and self.job.is_running():
            self.job.cancel()
            self.panel.label.setText(tr("Stopping…"))
        else:
            self.reject()

    def done(self, r: int) -> None:
        if self.job is not None and self.job.is_running():
            self.job.kill()
        super().done(r)


def _file_list(files: list[str]) -> QListWidget:
    lw = QListWidget()
    lw.setSelectionMode(QAbstractItemView.ExtendedSelection)
    for f in files:
        it = QListWidgetItem(icon("fit_page", theme.TEXT_DIM), os.path.basename(f))
        it.setToolTip(f)
        it.setData(Qt.UserRole, f)
        lw.addItem(it)
    return lw


def _paths(lw: QListWidget) -> list[str]:
    return [lw.item(i).data(Qt.UserRole) for i in range(lw.count())]


# =========================================================================== convert
class ConvertDialog(_JobDialog):
    def __init__(self, files: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Convert"))
        self.resize(640, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Files to convert"), objectName="dim"))
        self.files = _file_list(files)
        lay.addWidget(self.files, 1)
        row = QHBoxLayout()
        b = QPushButton(icon("open"), tr("Add files…"))
        b.clicked.connect(self._add)
        row.addWidget(b)
        b = QPushButton(icon("trash"), tr("Remove"))
        b.clicked.connect(lambda: [self.files.takeItem(self.files.row(i)) for i in self.files.selectedItems()])
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        form = QFormLayout()
        self.target = QComboBox()
        rar = convert.find_rar_writer()
        for key, label in TARGET_LABELS.items():
            self.target.addItem(label, key)
        if not rar:
            idx = self.target.findData("cbr")
            self.target.model().item(idx).setEnabled(False)
            self.target.setItemData(idx, tr("Creating RAR archives needs WinRAR (rar.exe)."), Qt.ToolTipRole)
            self.target.setItemText(idx, "CBR — " + tr("needs WinRAR"))
        self.target.currentIndexChanged.connect(self._sync)
        form.addRow(tr("Convert to"), self.target)
        self.epub_mode = QComboBox()
        self.epub_mode.addItem(tr("Automatic"), "auto")
        self.epub_mode.addItem(tr("Text (reflowable, for novels)"), "text")
        self.epub_mode.addItem(tr("Images (fixed layout, for comics / scans)"), "images")
        form.addRow(tr("EPUB mode"), self.epub_mode)
        qrow = QHBoxLayout()
        self.dpi = QSpinBox()
        self.dpi.setRange(72, 400)
        self.dpi.setValue(150)
        self.dpi.setSuffix(" dpi")
        self.quality = QSpinBox()
        self.quality.setRange(40, 100)
        self.quality.setValue(88)
        self.quality.setSuffix(" %")
        qrow.addWidget(self.dpi)
        qrow.addWidget(QLabel(tr("JPEG quality")))
        qrow.addWidget(self.quality)
        qrow.addStretch(1)
        self.quality_row = QWidget()
        self.quality_row.setLayout(qrow)
        form.addRow(tr("Page images"), self.quality_row)
        orow = QHBoxLayout()
        self.next_to = QRadioButton(tr("Next to each file"))
        self.next_to.setChecked(True)
        self.other = QRadioButton(tr("In:"))
        self.out_dir = QLineEdit()
        self.out_dir.setPlaceholderText(tr("Choose a folder…"))
        browse = QPushButton(icon("folder"), "")
        browse.clicked.connect(self._browse)
        orow.addWidget(self.next_to)
        orow.addWidget(self.other)
        orow.addWidget(self.out_dir, 1)
        orow.addWidget(browse)
        form.addRow(tr("Output"), orow)
        lay.addLayout(form)
        lay.addWidget(QLabel(tr("The original files are never modified."), objectName="dim"))
        self._bottom(lay, tr("Convert"))
        self.open_btn.clicked.connect(lambda: self.created and self.parent() and
                                      getattr(self.parent(), "open_path", lambda *_: None)(self.created[0]))
        self._sync()

    def _sync(self) -> None:
        t = self.target.currentData()
        self.epub_mode.setEnabled(t == "epub")
        self.quality_row.setEnabled(t in ("epub", "cbz", "cbr"))

    def _add(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, tr("Add files…"), "", DOC_FILTER)
        for f in files:
            it = QListWidgetItem(icon("fit_page", theme.TEXT_DIM), os.path.basename(f))
            it.setData(Qt.UserRole, f)
            it.setToolTip(f)
            self.files.addItem(it)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("Choose a folder…"))
        if d:
            self.out_dir.setText(d)
            self.other.setChecked(True)

    def _set_inputs_enabled(self, on: bool) -> None:
        for w in (self.files, self.target, self.epub_mode, self.quality_row):
            w.setEnabled(on)
        if on:
            self._sync()

    def _start(self) -> None:
        files = _paths(self.files)
        if not files:
            return
        target = self.target.currentData()
        out_dir = self.out_dir.text().strip() if self.other.isChecked() else None
        if self.other.isChecked() and not (out_dir and os.path.isdir(out_dir)):
            QMessageBox.warning(self, tr("Convert"), tr("Choose an existing output folder."))
            return
        opts = {"target": target, "out_dir": out_dir, "epub_mode": self.epub_mode.currentData(),
                "dpi": self.dpi.value(), "quality": self.quality.value(),
                "lang": settings.get("language", "fr"),
                "font_size": settings.get("reading.epub_font_size", 12) * 0.9}
        self._run("convert", files, opts)

    def _item_done(self, idx: int, out: str) -> None:
        super()._item_done(idx, out)
        it = self.files.item(idx)
        if it is not None:
            it.setIcon(icon("check", "#3d9970"))
            it.setText(f"{os.path.basename(it.data(Qt.UserRole))}  →  {os.path.basename(out)}")

    def _item_error(self, idx: int, msg: str) -> None:
        super()._item_error(idx, msg)
        it = self.files.item(idx)
        if it is not None:
            it.setIcon(icon("close", theme.ACCENT))
            if msg == "winrar_missing":
                msg = tr("Creating RAR archives needs WinRAR (rar.exe).")
            it.setToolTip(msg)
            it.setText(f"{os.path.basename(it.data(Qt.UserRole))}  —  {msg[:80]}")

    def _finished(self, ok: bool) -> None:
        super()._finished(ok)
        self.open_btn.setVisible(len(self.created) == 1)


# ============================================================================== OCR
OCR_LANGS = {
    "fra": "French", "eng": "English", "deu": "German", "spa": "Spanish", "ita": "Italian",
    "por": "Portuguese", "nld": "Dutch", "pol": "Polish", "rus": "Russian", "ukr": "Ukrainian",
    "ell": "Greek", "tur": "Turkish", "ara": "Arabic", "heb": "Hebrew", "lat": "Latin",
    "jpn": "Japanese", "jpn_vert": "Japanese (vertical)", "chi_sim": "Chinese (simplified)",
    "chi_tra": "Chinese (traditional)", "chi_tra_vert": "Chinese (traditional, vertical)",
    "kor": "Korean", "kor_vert": "Korean (vertical)", "vie": "Vietnamese", "hin": "Hindi",
    "swe": "Swedish", "dan": "Danish", "nor": "Norwegian", "fin": "Finnish", "ces": "Czech", "ron": "Romanian",
    "hun": "Hungarian", "cat": "Catalan", "bre": "Breton", "eus": "Basque", "cos": "Corsican",
}


class _Downloader(QObject):
    """Sequential downloads with Qt's own network stack (asynchronous, no Python thread)."""
    progress = Signal(str, int)  # language code, percent
    done = Signal(str, str)      # language code, error ("" = ok)
    finished = Signal()

    def __init__(self, codes: list[str], parent=None):
        super().__init__(parent)
        from PySide6.QtNetwork import QNetworkAccessManager

        self.codes = list(codes)
        self.nam = QNetworkAccessManager(self)
        self.reply = None
        self.folder = convert.user_tessdata()
        os.makedirs(self.folder, exist_ok=True)
        self._running = False

    def start(self) -> None:
        self._running = True
        self._next()

    def isRunning(self) -> bool:  # noqa: N802 - mirrors QThread
        return self._running

    def stop(self) -> None:
        self.codes.clear()
        if self.reply is not None:
            self.reply.abort()

    def wait(self, _ms: int = 0) -> None:
        pass

    def _next(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtNetwork import QNetworkRequest

        if not self.codes:
            self._running = False
            convert.tessdata_dir()  # copies the bundled French / English next to the downloads
            self.finished.emit()
            return
        code = self.codes.pop(0)
        req = QNetworkRequest(QUrl(convert.OCR_DOWNLOAD_URL.format(code=code)))
        req.setHeader(QNetworkRequest.UserAgentHeader, "MorningstarReader")
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
        self.reply = self.nam.get(req)
        self.reply.downloadProgress.connect(
            lambda got, total, c=code: total > 0 and self.progress.emit(c, int(got * 100 / total)))
        self.reply.finished.connect(lambda c=code: self._finished(c))

    def _finished(self, code: str) -> None:
        from PySide6.QtNetwork import QNetworkReply

        reply, self.reply = self.reply, None
        err = ""
        if reply.error() != QNetworkReply.NoError:
            err = reply.errorString()
        else:
            data = bytes(reply.readAll())
            if len(data) < 1024 or data.lstrip().startswith((b"<", b"{")):
                err = "not a language file"
            else:
                dst = os.path.join(self.folder, f"{code}.traineddata")
                with open(dst + ".part", "wb") as fh:
                    fh.write(data)
                os.replace(dst + ".part", dst)
        reply.deleteLater()
        self.done.emit(code, err)
        self._next()


class OcrLanguagesDialog(QDialog):
    """Download extra OCR languages (Tesseract “fast” models, Apache-2.0)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("OCR languages"))
        self.resize(460, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Check the languages to download (about 1 to 10 MB each). "
                                "Installed languages are already checked."), objectName="dim", wordWrap=True))
        self.list = QListWidget()
        installed = set(convert.ocr_languages())
        for code, name in sorted(OCR_LANGS.items(), key=lambda kv: tr(kv[1])):
            it = QListWidgetItem(f"{tr(name)}  ({code})")
            it.setData(Qt.UserRole, code)
            if code in installed:
                it.setFlags(Qt.ItemIsEnabled)
                it.setCheckState(Qt.Checked)
                it.setForeground(QColor(theme.TEXT_DIM))
            else:
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list, 1)
        self.status = QLabel(objectName="dim")
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.clicked.connect(self._close)
        self.go = QPushButton(icon("down"), tr("Download"))
        self.go.setProperty("accent", True)
        self.go.clicked.connect(self._start)
        row.addWidget(self.close_btn)
        row.addWidget(self.go)
        lay.addLayout(row)
        self.thread: _Downloader | None = None
        self.changed = False

    def _start(self) -> None:
        codes = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                 if self.list.item(i).flags() & Qt.ItemIsUserCheckable
                 and self.list.item(i).checkState() == Qt.Checked]
        if not codes:
            return
        self.go.setEnabled(False)
        self.thread = _Downloader(codes, self)
        self.thread.progress.connect(lambda c, p: self.status.setText(tr("Downloading {l}… {p}%", l=c, p=p)))
        self.thread.done.connect(self._one_done)
        self.thread.finished.connect(self._all_done)
        self.thread.start()

    def _one_done(self, code: str, err: str) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.UserRole) == code:
                if err:
                    it.setToolTip(err)
                    it.setForeground(QColor(theme.ACCENT))
                else:
                    it.setFlags(Qt.ItemIsEnabled)
                    it.setForeground(QColor("#3d9970"))
                    self.changed = True

    def _all_done(self) -> None:
        self.go.setEnabled(True)
        self.status.setText(tr("Done.") if self.changed else tr("Download failed (check your connection)."))
        self.thread = None

    def _close(self) -> None:
        if self.thread is not None:
            self.thread.stop()
            self.thread.wait(3000)
        self.done(1 if self.changed else 0)

    def reject(self) -> None:
        self._close()


class OcrDialog(_JobDialog):
    """Add a searchable text layer to scanned PDFs / comics (new file, or replace the PDF keeping a .bak)."""

    def __init__(self, files: list[str], db: Database, before_replace=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.before_replace = before_replace
        self.setWindowTitle(tr("Make searchable (OCR)"))
        self.resize(640, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Text recognition adds an invisible text layer to scanned pages: you can then "
                                "search, select and copy the text. The page images are not changed."),
                             objectName="dim", wordWrap=True))
        self.files = _file_list(files)
        lay.addWidget(self.files, 1)
        row = QHBoxLayout()
        b = QPushButton(icon("open"), tr("Add files…"))
        b.clicked.connect(self._add)
        row.addWidget(b)
        b = QPushButton(icon("trash"), tr("Remove"))
        b.clicked.connect(lambda: [self.files.takeItem(self.files.row(i)) for i in self.files.selectedItems()])
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        form = QFormLayout()
        self.lang = QComboBox()
        langs = convert.ocr_languages()
        self._fill_langs()
        lrow = QHBoxLayout()
        lrow.addWidget(self.lang, 1)
        more = QPushButton(icon("down"), tr("More languages…"))
        more.clicked.connect(self._more_langs)
        lrow.addWidget(more)
        form.addRow(tr("Language"), lrow)
        self.quality = QComboBox()
        self.quality.addItem(tr("Accurate (300 dpi)"), 300)
        self.quality.addItem(tr("Fast (200 dpi)"), 200)
        form.addRow(tr("Recognition"), self.quality)
        self.force = QCheckBox(tr("Also process pages that already contain text"))
        form.addRow("", self.force)
        orow = QHBoxLayout()
        self.new_file = QRadioButton(tr("Create a new “(OCR)” PDF"))
        self.new_file.setChecked(True)
        self.replace = QRadioButton(tr("Replace the PDF (a .bak copy is kept)"))
        orow.addWidget(self.new_file)
        orow.addWidget(self.replace)
        form.addRow(tr("Output"), orow)
        lay.addLayout(form)
        if not langs:
            lay.addWidget(QLabel(tr("OCR language data not found."), objectName="dim"))
        self._bottom(lay, tr("Start"))
        self.ok_btn.setEnabled(bool(langs))
        self.open_btn.clicked.connect(lambda: self.created and self.parent() and
                                      getattr(self.parent(), "open_path", lambda *_: None)(self.created[0]))

    def _fill_langs(self) -> None:
        cur = self.lang.currentData()
        self.lang.clear()
        langs = convert.ocr_languages()
        if "fra" in langs and "eng" in langs:
            self.lang.addItem(tr("French + English"), "fra+eng")
        if "jpn" in langs and "jpn_vert" in langs:
            self.lang.addItem(tr(OCR_LANGS["jpn"]) + " + " + tr(OCR_LANGS["jpn_vert"]), "jpn+jpn_vert")
        for code in langs:
            self.lang.addItem(tr(OCR_LANGS.get(code, code)), code)
        if cur:
            self.lang.setCurrentIndex(max(0, self.lang.findData(cur)))

    def _more_langs(self) -> None:
        if OcrLanguagesDialog(self).exec():
            self._fill_langs()
            self.ok_btn.setEnabled(self.lang.count() > 0)

    def _add(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, tr("Add files…"), "", "PDF / Comics (*.pdf *.cbz *.cbr)")
        for f in files:
            it = QListWidgetItem(icon("fit_page", theme.TEXT_DIM), os.path.basename(f))
            it.setData(Qt.UserRole, f)
            it.setToolTip(f)
            self.files.addItem(it)

    def _set_inputs_enabled(self, on: bool) -> None:
        for w in (self.files, self.lang, self.quality, self.force, self.new_file, self.replace):
            w.setEnabled(on)

    def _start(self) -> None:
        files = _paths(self.files)
        if not files:
            return
        self._files = files
        self._replace = self.replace.isChecked()
        dst_for = {}
        if self._replace:
            dst_for = {f: f + ".ocr-new.pdf" for f in files if f.lower().endswith(".pdf")}
        self._run("ocr", files, {"ocr_lang": self.lang.currentData(), "ocr_dpi": self.quality.currentData(),
                                 "ocr_force": self.force.isChecked(), "dst_for": dst_for})

    def _item_done(self, idx: int, out: str) -> None:
        src = self._files[idx]
        if out.endswith(".ocr-new.pdf"):
            if self.before_replace is None or self.before_replace(src):
                bak = editing.replace_with_backup(out, src)
                if bak:
                    self.db.add_backup(bak, src)
                out = src
            else:
                final = convert.ocr_output_path(src)
                os.replace(out, final)
                out = final
        super()._item_done(idx, out)
        it = self.files.item(idx)
        if it is not None:
            it.setIcon(icon("check", "#3d9970"))
            it.setText(f"{os.path.basename(src)}  →  {os.path.basename(out)}")

    def _item_error(self, idx: int, msg: str) -> None:
        super()._item_error(idx, msg)
        it = self.files.item(idx)
        if it is not None:
            it.setIcon(icon("close", theme.ACCENT))
            it.setToolTip(msg)

    def _finished(self, ok: bool) -> None:
        super()._finished(ok)
        self.open_btn.setVisible(len(self.created) == 1)


# =========================================================================== split
class SplitDialog(_JobDialog):
    """Preview double pages of one file, choose which ones to split, then create a new file."""

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle(tr("Split double pages") + " — " + os.path.basename(path))
        self.resize(1000, 720)
        lay = QVBoxLayout(self)
        self.info = QLabel(objectName="dim", wordWrap=True)
        lay.addWidget(self.info)
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(QSize(150, 150))
        self.grid.setGridSize(QSize(176, 196))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setStyleSheet(f"QListWidget {{ background: {theme.CANVAS}; }}")
        self.grid.itemChanged.connect(lambda *_: self._summary())
        self.grid.itemDoubleClicked.connect(
            lambda it: it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked))
        lay.addWidget(self.grid, 1)
        row = QHBoxLayout()
        self.rtl = QCheckBox(tr("Right-to-left (manga): right half first"))
        self.rtl.setChecked(bool(settings.get("reading.comics_rtl", False)))
        self.skip = QCheckBox(tr("Never split the first page (cover)"))
        self.skip.setChecked(True)
        self.skip.toggled.connect(self._redetect)
        row.addWidget(self.rtl)
        row.addWidget(self.skip)
        row.addStretch(1)
        b = QPushButton(tr("Check none"))
        b.clicked.connect(lambda: self._check_all(False))
        row.addWidget(b)
        b = QPushButton(tr("Auto-detect"))
        b.clicked.connect(self._redetect)
        row.addWidget(b)
        lay.addLayout(row)
        self.summary = QLabel()
        lay.addWidget(self.summary)
        self._bottom(lay, tr("Create the file"))
        self.open_btn.clicked.connect(lambda: self.created and self.parent() and
                                      getattr(self.parent(), "open_path", lambda *_: None)(self.created[0]))
        self._queue: list[int] = []
        self._timer = QTimer(self, interval=0, timeout=self._thumb_some)
        QTimer.singleShot(0, self._load)

    def _load(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.sizes = convert.page_sizes(self.path)
            self.is_comic = convert.is_comic_path(self.path)
            if self.is_comic:
                self.zip_path = convert.comic_zip(self.path)
                self.names = convert.comic_entries(self.zip_path)
                self.zip = zipfile.ZipFile(self.zip_path)
            else:
                self.pdf = pymupdf.open(self.path)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, self.windowTitle(), str(e))
            self.reject()
            return
        QApplication.restoreOverrideCursor()
        doubles = set(convert.detect_doubles(self.sizes, skip_first=self.skip.isChecked()))
        self.grid.blockSignals(True)
        for i, (w, h) in enumerate(self.sizes):
            it = QListWidgetItem(f"{i + 1}  ·  {int(w)}×{int(h)}")
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            it.setCheckState(Qt.Checked if i in doubles else Qt.Unchecked)
            pm = QPixmap(150, 150)
            pm.fill(QColor("#22222a"))
            it.setIcon(pm)
            self.grid.addItem(it)
        self.grid.blockSignals(False)
        self.info.setText(tr("Detected double pages are checked. Double-click a page to change it. "
                             "A new file is created next to the original, which is not modified."))
        self._queue = list(range(len(self.sizes)))
        self._timer.start()
        self._summary()

    def _thumb_some(self) -> None:
        for _ in range(4):
            if not self._queue:
                self._timer.stop()
                return
            i = self._queue.pop(0)
            it = self.grid.item(i)
            if it is None:
                continue
            try:
                if self.is_comic:
                    data = self.zip.read(self.names[i])
                    buf = QBuffer()
                    buf.setData(QByteArray(data))
                    buf.open(QIODevice.ReadOnly)
                    reader = QImageReader(buf)
                    sz = reader.size()
                    if sz.isValid():
                        reader.setScaledSize(sz.scaled(150, 150, Qt.KeepAspectRatio))
                    img = reader.read()
                else:
                    page = self.pdf.load_page(i)
                    s = 150 / max(page.rect.width, page.rect.height)
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
                    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
                if not img.isNull():
                    it.setIcon(QPixmap.fromImage(img))
            except Exception:  # noqa: BLE001
                pass

    def _checked(self) -> list[int]:
        return [i for i in range(self.grid.count()) if self.grid.item(i).checkState() == Qt.Checked]

    def _check_all(self, on: bool) -> None:
        for i in range(self.grid.count()):
            self.grid.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _redetect(self) -> None:
        doubles = set(convert.detect_doubles(self.sizes, skip_first=self.skip.isChecked()))
        for i in range(self.grid.count()):
            self.grid.item(i).setCheckState(Qt.Checked if i in doubles else Qt.Unchecked)

    def _summary(self) -> None:
        n = len(self._checked())
        total = self.grid.count() + n
        out = os.path.basename(convert.split_output_path(self.path))
        self.summary.setText(tr("{d} page(s) to split → {n} pages in “{f}”", d=n, n=total, f=out))
        self.ok_btn.setEnabled(n > 0 and self.job is None)

    def _set_inputs_enabled(self, on: bool) -> None:
        for w in (self.grid, self.rtl, self.skip):
            w.setEnabled(on)

    def _start(self) -> None:
        pages = self._checked()
        if not pages:
            return
        self._run("split", [(self.path, pages)], {"rtl": self.rtl.isChecked()})

    def _finished(self, ok: bool) -> None:
        super()._finished(ok)
        self.open_btn.setVisible(bool(self.created))
        if self.created:
            self.ok_btn.setEnabled(False)

    def done(self, r: int) -> None:
        self._timer.stop()
        for attr in ("zip", "pdf"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
        super().done(r)


class BatchSplitDialog(_JobDialog):
    """Analyse many files, then split the double pages of the checked ones (new files)."""

    def __init__(self, files: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Split double pages (batch)"))
        self.resize(760, 560)
        self.files = files
        self.analysis: dict[int, dict] = {}
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Each checked file gets a new “(single pages)” copy. Originals are not "
                                "modified."), objectName="dim", wordWrap=True))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("File"), tr("Double pages")])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 520)
        for f in files:
            it = QTreeWidgetItem([os.path.basename(f), "…"])
            it.setToolTip(0, f)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Unchecked)
            self.tree.addTopLevelItem(it)
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.rtl = QCheckBox(tr("Right-to-left (manga): right half first"))
        self.rtl.setChecked(bool(settings.get("reading.comics_rtl", False)))
        row.addWidget(self.rtl)
        row.addStretch(1)
        lay.addLayout(row)
        self._bottom(lay, tr("Split the checked files"))
        self.ok_btn.setEnabled(False)
        self.phase = "analyze"
        QTimer.singleShot(0, lambda: self._run("analyze", list(files), {"skip_first": True}))

    def _item_done(self, idx: int, out: str) -> None:
        if self.phase == "analyze":
            data = json.loads(out)
            self.analysis[idx] = data
            it = self.tree.topLevelItem(idx)
            n = len(data["doubles"])
            it.setText(1, f"{n} / {data['pages']}")
            it.setCheckState(0, Qt.Checked if n else Qt.Unchecked)
        else:
            super()._item_done(idx, out)
            it = self.tree.topLevelItem(self._split_rows[idx])
            it.setIcon(0, icon("check", "#3d9970"))

    def _item_error(self, idx: int, msg: str) -> None:
        super()._item_error(idx, msg)
        row = idx if self.phase == "analyze" else self._split_rows[idx]
        it = self.tree.topLevelItem(row)
        it.setText(1, tr("error"))
        it.setToolTip(1, msg)

    def _finished(self, ok: bool) -> None:
        if self.phase == "analyze":
            self.job = None
            self.phase = "ready"
            self.panel.hide()
            self.errors = []
            self.cancel_btn.setText(tr("Cancel"))
            self.ok_btn.setEnabled(True)
            if not ok:
                self.panel.show()
                self.panel.label.setText(tr("Analysis stopped."))
            return
        super()._finished(ok)
        self.ok_btn.setEnabled(False)

    def _set_inputs_enabled(self, on: bool) -> None:
        self.tree.setEnabled(on)
        self.rtl.setEnabled(on)

    def _start(self) -> None:
        items, self._split_rows = [], []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            a = self.analysis.get(i)
            if it.checkState(0) == Qt.Checked and a and a["doubles"]:
                items.append((self.files[i], a["doubles"]))
                self._split_rows.append(i)
        if not items:
            return
        self.phase = "split"
        self._run("split", items, {"rtl": self.rtl.isChecked()})


# =========================================================================== merge
class MergeDialog(_JobDialog):
    def __init__(self, files: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Merge files"))
        self.resize(640, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Files, in order"), objectName="dim"))
        self.files = _file_list(files)
        self.files.setDragDropMode(QAbstractItemView.InternalMove)
        lay.addWidget(self.files, 1)
        row = QHBoxLayout()
        for ic, text, slot in (("open", tr("Add files…"), self._add), ("up", tr("Up"), lambda: self._move(-1)),
                               ("down", tr("Down"), lambda: self._move(1)),
                               ("trash", tr("Remove"), self._remove)):
            b = QPushButton(icon(ic), text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        form = QFormLayout()
        self.fmt = QComboBox()
        self.fmt.addItem("PDF", "pdf")
        self.fmt.addItem(tr("CBZ (comics: pages as images)"), "cbz")
        self.fmt.currentIndexChanged.connect(self._fix_ext)
        form.addRow(tr("Format"), self.fmt)
        orow = QHBoxLayout()
        first = files[0] if files else os.path.expanduser("~")
        self.out = QLineEdit(os.path.join(os.path.dirname(first) if files else first, tr("Merged") + ".pdf"))
        b = QPushButton(icon("folder"), "")
        b.clicked.connect(self._browse)
        orow.addWidget(self.out, 1)
        orow.addWidget(b)
        form.addRow(tr("New file"), orow)
        lay.addLayout(form)
        self._bottom(lay, tr("Merge"))
        self.open_btn.clicked.connect(lambda: self.created and self.parent() and
                                      getattr(self.parent(), "open_path", lambda *_: None)(self.created[0]))

    def _fix_ext(self) -> None:
        base = os.path.splitext(self.out.text())[0]
        self.out.setText(base + "." + self.fmt.currentData())

    def _add(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, tr("Add files…"), "", DOC_FILTER)
        for f in files:
            it = QListWidgetItem(icon("fit_page", theme.TEXT_DIM), os.path.basename(f))
            it.setData(Qt.UserRole, f)
            it.setToolTip(f)
            self.files.addItem(it)

    def _move(self, d: int) -> None:
        row = self.files.currentRow()
        if row < 0 or not (0 <= row + d < self.files.count()):
            return
        it = self.files.takeItem(row)
        self.files.insertItem(row + d, it)
        self.files.setCurrentRow(row + d)

    def _remove(self) -> None:
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))

    def _browse(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, tr("New file"), self.out.text(),
                                           "PDF (*.pdf)" if self.fmt.currentData() == "pdf" else "CBZ (*.cbz)")
        if f:
            self.out.setText(f)

    def _set_inputs_enabled(self, on: bool) -> None:
        for w in (self.files, self.fmt, self.out):
            w.setEnabled(on)

    def _start(self) -> None:
        files = _paths(self.files)
        if len(files) < 2:
            QMessageBox.information(self, tr("Merge files"), tr("Add at least two files."))
            return
        dst = self.out.text().strip()
        if not dst.lower().endswith("." + self.fmt.currentData()):
            dst += "." + self.fmt.currentData()
        dst = convert.unique_path(dst)
        self._run("merge", [(files, dst)], {"font_size": 11})

    def _finished(self, ok: bool) -> None:
        super()._finished(ok)
        self.open_btn.setVisible(bool(self.created))


# ========================================================================= backups
class BackupsDialog(QDialog):
    """List, restore or purge the .bak copies created when saving edited documents."""

    def __init__(self, db: Database, before_restore=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.before_restore = before_restore
        self.setWindowTitle(tr("Backups (.bak)"))
        self.resize(820, 480)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("When you save an edited document, the original is kept next to it as "
                                "“name.ext.bak”. Restore it or delete the copies you no longer need."),
                             objectName="dim", wordWrap=True))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Original file"), tr("Date"), tr("Size"), tr("Folder")])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 130)
        self.tree.setColumnWidth(2, 80)
        lay.addWidget(self.tree, 1)
        self.total = QLabel(objectName="dim")
        lay.addWidget(self.total)
        row = QHBoxLayout()
        for ic, text, slot in (("backup", tr("Restore"), self._restore),
                               ("trash", tr("Delete"), self._delete),
                               ("trash", tr("Delete those older than 30 days"), lambda: self._purge(30)),
                               ("trash", tr("Delete all"), lambda: self._purge(0))):
            b = QPushButton(icon(ic), text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self._fill()

    def _fill(self) -> None:
        self.tree.clear()
        rows = self.db.backups()
        total = 0
        for r in rows:
            total += r["size"] or 0
            d = datetime.datetime.fromtimestamp(r["created"] or 0).strftime("%Y-%m-%d %H:%M")
            it = QTreeWidgetItem([os.path.basename(r["original"]), d, human_size(r["size"] or 0),
                                  os.path.dirname(r["original"])])
            it.setData(0, Qt.UserRole, r)
            it.setToolTip(0, r["bak"])
            self.tree.addTopLevelItem(it)
        self.total.setText(tr("{n} backup(s) · {s}", n=len(rows), s=human_size(total)))

    def _selected(self) -> list[dict]:
        return [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()]

    def _restore(self) -> None:
        sel = self._selected()
        if not sel:
            return
        if QMessageBox.question(self, tr("Restore"), tr("Replace the current file(s) by the backup?")) \
                != QMessageBox.Yes:
            return
        for r in sel:
            if self.before_restore and not self.before_restore(r["original"]):
                continue
            try:
                editing.restore_backup(r["bak"], r["original"])
                self.db.forget_backup(r["bak"])
            except OSError as e:
                QMessageBox.warning(self, tr("Restore"), str(e))
        self._fill()

    def _delete(self) -> None:
        sel = self._selected()
        if sel and QMessageBox.question(self, tr("Delete"), tr("Delete {n} backup(s) permanently?", n=len(sel))) \
                == QMessageBox.Yes:
            self._remove(sel)

    def _purge(self, days: int) -> None:
        import time
        rows = self.db.backups()
        if days:
            rows = [r for r in rows if (r["created"] or 0) < time.time() - days * 86400]
        if not rows:
            return
        if QMessageBox.question(self, tr("Delete"), tr("Delete {n} backup(s) permanently?", n=len(rows))) \
                == QMessageBox.Yes:
            self._remove(rows)

    def _remove(self, rows: list[dict]) -> None:
        for r in rows:
            try:
                os.remove(r["bak"])
            except OSError:
                pass
            self.db.forget_backup(r["bak"])
        self._fill()


def collect_docs(folder: str, comics_only: bool = False) -> list[str]:
    out = []
    for root, _d, files in os.walk(folder):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in DOC_EXTS and (not comics_only or ext in (".cbz", ".cbr", ".pdf")):
                out.append(os.path.join(root, f))
    return sorted(out, key=natural_key)
