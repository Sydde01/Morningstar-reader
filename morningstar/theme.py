"""Mandatory dark theme (Fusion + palette + stylesheet)."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#e0303f"
ACCENT_HOVER = "#ff4655"
BG = "#141416"
BG_ALT = "#1b1b1f"
PANEL = "#202026"
BORDER = "#2e2e36"
TEXT = "#e6e6e6"
TEXT_DIM = "#9a9aa3"
CANVAS = "#0d0d0f"

QSS = f"""
* {{ outline: 0; }}
QWidget {{ color: {TEXT}; font-size: 10pt; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}

QTabWidget::pane {{ border: none; }}
QTabBar {{ background: {BG}; }}
QTabBar::tab {{
    background: {BG}; color: {TEXT_DIM}; padding: 7px 14px; border: none;
    border-bottom: 2px solid transparent; max-width: 260px;
}}
QTabBar::tab:selected {{ color: {TEXT}; background: {BG_ALT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; background: #19191d; }}
QTabBar::close-button {{ image: url(:/close.svg); subcontrol-position: right; }}

QToolBar {{ background: {BG_ALT}; border: none; border-bottom: 1px solid {BORDER}; spacing: 2px; padding: 3px; }}
QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 4px 6px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 5px; padding: 4px; }}
QToolButton:hover {{ background: #2a2a31; border-color: #34343d; }}
QToolButton:pressed {{ background: #33333b; }}
QToolButton:checked {{ background: #3a1b20; border-color: {ACCENT}; }}
QToolButton::menu-indicator {{ image: none; }}

QPushButton {{
    background: #2a2a31; border: 1px solid #383841; border-radius: 5px; padding: 6px 14px;
}}
QPushButton:hover {{ background: #33333b; border-color: #4a4a55; }}
QPushButton:pressed {{ background: #3b3b44; }}
QPushButton:default, QPushButton[accent="true"] {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton:default:hover, QPushButton[accent="true"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton:disabled {{ color: #666; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 5px; padding: 4px 6px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}

QTreeView, QListView, QListWidget, QTreeWidget {{
    background: {BG_ALT}; border: none; alternate-background-color: #1e1e23;
    selection-background-color: #3a1b20; selection-color: {TEXT};
}}
QTreeView::item, QListWidget::item, QTreeWidget::item {{ padding: 4px 2px; border-radius: 4px; }}
QTreeView::item:hover, QListWidget::item:hover, QTreeWidget::item:hover {{ background: #26262c; }}
QTreeView::item:selected, QListWidget::item:selected, QTreeWidget::item:selected {{
    background: #3a1b20; color: white;
}}
QHeaderView::section {{ background: {PANEL}; border: none; padding: 4px; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle {{ background: #3a3a44; border-radius: 4px; min-height: 30px; min-width: 30px; margin: 2px; }}
QScrollBar::handle:hover {{ background: #50505c; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: #3a1b20; }}
QMenu::item:disabled {{ color: #666; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
QMenu::icon {{ padding-left: 6px; }}

QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QCheckBox::indicator {{ border: 1px solid #555; border-radius: 3px; background: {BG}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url(:/check.svg); }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 6px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT_DIM}; }}
QProgressBar {{ background: {BG}; border: none; border-radius: 3px; height: 6px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QStatusBar {{ background: {BG}; color: {TEXT_DIM}; }}
QLabel#dim {{ color: {TEXT_DIM}; }}
QLabel#title {{ font-size: 13pt; font-weight: 600; }}
QFrame#sidebar {{ background: {BG_ALT}; }}
QListWidget#settingsNav {{ background: {BG_ALT}; font-size: 11pt; }}
QListWidget#settingsNav::item {{ padding: 9px 10px; }}
"""


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    c = QColor
    pal.setColor(QPalette.Window, c(BG))
    pal.setColor(QPalette.WindowText, c(TEXT))
    pal.setColor(QPalette.Base, c(BG))
    pal.setColor(QPalette.AlternateBase, c(BG_ALT))
    pal.setColor(QPalette.ToolTipBase, c(PANEL))
    pal.setColor(QPalette.ToolTipText, c(TEXT))
    pal.setColor(QPalette.PlaceholderText, c(TEXT_DIM))
    pal.setColor(QPalette.Text, c(TEXT))
    pal.setColor(QPalette.Button, c("#2a2a31"))
    pal.setColor(QPalette.ButtonText, c(TEXT))
    pal.setColor(QPalette.BrightText, c("white"))
    pal.setColor(QPalette.Highlight, c(ACCENT))
    pal.setColor(QPalette.HighlightedText, c("white"))
    pal.setColor(QPalette.Link, c(ACCENT_HOVER))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, c("#666"))
    app.setPalette(pal)
    from .icons import write_ui_svgs

    app.setStyleSheet(QSS.replace("url(:/", f"url({write_ui_svgs()}/"))
