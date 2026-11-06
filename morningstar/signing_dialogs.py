"""Dialogs for certificate-based digital signatures."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QSpinBox,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from . import signing, theme
from .i18n import tr
from .icons import icon


class CreateCertDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Create a personal certificate"))
        self.path = ""
        form = QFormLayout(self)
        form.addRow(QLabel(tr("A personal certificate makes valid, tamper-evident signatures, but it is not issued "
                              "by a recognised authority: readers will show the signer as “unknown identity”. "
                              "For official procedures, use a certificate from an approved provider (.pfx / .p12)."),
                           wordWrap=True, objectName="dim"))
        self.name = QLineEdit()
        self.email = QLineEdit()
        self.org = QLineEdit()
        self.pw = QLineEdit(echoMode=QLineEdit.Password)
        self.pw2 = QLineEdit(echoMode=QLineEdit.Password)
        self.years = QSpinBox()
        self.years.setRange(1, 20)
        self.years.setValue(5)
        self.years.setSuffix(" " + tr("years"))
        form.addRow(tr("Full name"), self.name)
        form.addRow(tr("E-mail"), self.email)
        form.addRow(tr("Organization (optional)"), self.org)
        form.addRow(tr("Protect with a password"), self.pw)
        form.addRow(tr("Confirm"), self.pw2)
        form.addRow(tr("Valid for"), self.years)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("Create"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _ok(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, self.windowTitle(), tr("Enter a name."))
            return
        if len(self.pw.text()) < 4 or self.pw.text() != self.pw2.text():
            QMessageBox.warning(self, self.windowTitle(), tr("The passwords are empty or different."))
            return
        self.path = signing.create_self_signed(self.name.text().strip(), self.email.text().strip(),
                                               self.org.text().strip(), self.pw.text(), self.years.value())
        self.accept()


class SignDialog(QDialog):
    """Choose the certificate and options. The caller then asks for the signature area (if visible)."""

    def __init__(self, path: str, parent=None, can_replace: bool = True):
        super().__init__(parent)
        self.setWindowTitle(tr("Sign digitally") + " — " + os.path.basename(path))
        self.resize(560, 420)
        self.image: bytes | None = None
        lay = QVBoxLayout(self)
        form = QFormLayout()
        crow = QHBoxLayout()
        self.cert = QComboBox()
        crow.addWidget(self.cert, 1)
        b = QPushButton(icon("open"), tr("Import .pfx / .p12…"))
        b.clicked.connect(self._import)
        crow.addWidget(b)
        b = QPushButton(icon("lock"), tr("Create…"))
        b.clicked.connect(self._create)
        crow.addWidget(b)
        b = QPushButton(icon("refresh"), "")
        b.setToolTip(tr("Refresh (after inserting a smart card or USB key)"))
        b.clicked.connect(lambda: self._fill(self.cert.currentData()))
        crow.addWidget(b)
        form.addRow(tr("Certificate"), crow)
        self.pw = QLineEdit(echoMode=QLineEdit.Password)
        self.pw.textChanged.connect(lambda: self.info.setText(""))
        self.cert.currentIndexChanged.connect(self._cert_changed)
        form.addRow(tr("Certificate password"), self.pw)
        self.info = QLabel(objectName="dim", wordWrap=True)
        form.addRow("", self.info)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText(tr("e.g. I approve this document"))
        self.location = QLineEdit()
        form.addRow(tr("Reason"), self.reason)
        form.addRow(tr("Location"), self.location)
        lay.addLayout(form)
        self.visible = QCheckBox(tr("Visible signature (draw its area on the page)"))
        self.visible.setChecked(True)
        lay.addWidget(self.visible)
        irow = QHBoxLayout()
        self.with_image = QCheckBox(tr("Add my handwritten signature"))
        self.with_image.toggled.connect(self._pick_image)
        irow.addWidget(self.with_image)
        irow.addStretch(1)
        lay.addLayout(irow)
        self.visible.toggled.connect(self.with_image.setEnabled)
        orow = QHBoxLayout()
        self.new_file = QRadioButton(tr("Create a new “(signed)” file"))
        self.new_file.setChecked(True)
        self.replace = QRadioButton(tr("Sign this file (a .bak copy is kept)"))
        self.replace.setEnabled(can_replace)
        orow.addWidget(self.new_file)
        orow.addWidget(self.replace)
        lay.addLayout(orow)
        lay.addWidget(QLabel(tr("Once signed, any later change invalidates the signature: finish your edits first."),
                             objectName="dim", wordWrap=True))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("Sign"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill()

    def _fill(self, select: str | None = None) -> None:
        from .winstore import list_signing_certificates

        self.cert.blockSignals(True)
        self.cert.clear()
        for c in list_signing_certificates():
            self.cert.addItem(icon("form", theme.ACCENT), f"{c['name']} — {c['issuer']}  ({tr('Windows / smart card')})",
                              "win:" + c["thumbprint"])
            self.cert.setItemData(self.cert.count() - 1, tr("Expires {d}", d=c["expires"]), Qt.ToolTipRole)
        for p in signing.list_certificates():
            self.cert.addItem(icon("lock", theme.TEXT_DIM), os.path.splitext(os.path.basename(p))[0], p)
        if select:
            self.cert.setCurrentIndex(max(0, self.cert.findData(select)))
        self.cert.blockSignals(False)
        self._cert_changed()
        if self.cert.count() == 0:
            self.info.setText(tr("No certificate yet: import yours (.pfx / .p12) or create a personal one."))

    def _cert_changed(self, *_a) -> None:
        win = str(self.cert.currentData() or "").startswith("win:")
        self.pw.setEnabled(not win)
        self.pw.setPlaceholderText(tr("Windows will ask for the PIN if needed") if win else "")

    def _import(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, tr("Import .pfx / .p12…"), "", "Certificates (*.pfx *.p12)")
        if f:
            self._fill(signing.import_certificate(f))

    def _create(self) -> None:
        d = CreateCertDialog(self)
        if d.exec() == QDialog.Accepted:
            self._fill(d.path)
            self.pw.setText(d.pw.text())

    def _pick_image(self, on: bool) -> None:
        if not on:
            self.image = None
            return
        from .editor import SignatureDialog

        d = SignatureDialog(self)
        if d.exec() == QDialog.Accepted and d.result_png:
            self.image = d.result_png
        else:
            self.with_image.setChecked(False)

    def _ok(self) -> None:
        path = self.cert.currentData()
        if not path:
            QMessageBox.warning(self, self.windowTitle(), tr("Choose or create a certificate."))
            return
        if path.startswith("win:"):
            self.summary = {"name": self.cert.currentText()}
            self.accept()
            return
        try:
            summary = signing.certificate_summary(path, self.pw.text())
        except signing.SignError:
            self.info.setText(tr("Wrong certificate password."))
            self.info.setStyleSheet(f"color: {theme.ACCENT};")
            return
        except Exception as e:  # noqa: BLE001
            self.info.setText(str(e))
            return
        self.summary = summary
        self.accept()

    def options(self) -> dict:
        return {"cert": self.cert.currentData(), "password": self.pw.text(), "reason": self.reason.text().strip(),
                "location": self.location.text().strip(), "visible": self.visible.isChecked(),
                "image": self.image if self.visible.isChecked() else None, "replace": self.replace.isChecked()}


class SignaturesDialog(QDialog):
    """List and verify the digital signatures of a PDF."""

    def __init__(self, path: str, parent=None, password: str | None = None, on_sign=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Digital signatures") + " — " + os.path.basename(path))
        self.resize(760, 380)
        lay = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Signed by"), tr("Date"), tr("Integrity"), tr("Identity"), tr("Scope")])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 130)
        self.tree.setColumnWidth(2, 140)
        self.tree.setColumnWidth(3, 140)
        lay.addWidget(self.tree, 1)
        self.note = QLabel(objectName="dim", wordWrap=True)
        lay.addWidget(self.note)
        row = QHBoxLayout()
        if on_sign is not None:
            b = QPushButton(icon("signature"), tr("Sign this document…"))
            b.setProperty("accent", True)
            b.clicked.connect(lambda: (self.accept(), on_sign()))
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        try:
            sigs = signing.verify(path, password)
        except Exception as e:  # noqa: BLE001
            sigs = []
            self.note.setText(str(e))
        green, red, orange = "#3d9970", theme.ACCENT, "#e08e0b"
        for s in sigs:
            ok = s["intact"] and s["valid"]
            it = QTreeWidgetItem([
                s["signer"], s["time"] or "?",
                tr("Intact") if ok else tr("ALTERED / invalid"),
                tr("Trusted") if s["trusted"] else tr("Not verified"),
                tr("Whole document") if s["whole_file"] else tr("Changed after signing"),
            ])
            for col, color in ((2, green if ok else red), (3, green if s["trusted"] else orange),
                               (4, green if s["whole_file"] else orange)):
                it.setForeground(col, QColor(color))
            if s["error"]:
                it.setToolTip(2, s["error"])
            self.tree.addTopLevelItem(it)
        if not sigs and not self.note.text():
            self.note.setText(tr("This document has no digital signature."))
        elif sigs:
            self.note.setText(tr("“Not verified” identity: the certificate does not chain to an authority trusted "
                                 "by Windows (e.g. a personal certificate). “Changed after signing”: later "
                                 "revisions were added (another signature, annotations…)."))
