"""Library view: Recent / Favorites / folder-structured categories with a cover grid."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QPoint, QRect, QRectF, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListView, QMenu, QPushButton, QSplitter, QStackedWidget,
                               QStyle, QStyledItemDelegate, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import theme
from .config import settings
from .db import Database, norm_path
from .i18n import tr
from .icons import icon, render
from .scanner import LibraryScanner
from .thumbs import ThumbnailManager
from .utils import human_size, natural_key, show_in_explorer, strip_prefix

KEY_ROLE = Qt.UserRole + 1
EXT_COLORS = {".pdf": "#d9534f", ".epub": "#3d9970", ".cbz": "#e08e0b", ".cbr": "#e08e0b",
              ".mobi": "#6f5bd6", ".fb2": "#2f8fd8", ".xps": "#888", ".oxps": "#888"}


@dataclass
class Entry:
    path: str
    root: str
    rel_dir: str
    name: str
    ext: str
    size: int = 0
    mtime: float = 0
    pages: int | None = None
    eff_dir: str = ""


@dataclass
class Item:
    kind: str  # "doc" | "folder"
    name: str
    path: str = ""  # doc path, or cover path for folders
    key: tuple = ()
    count: int = 0
    entry: Entry | None = None
    sub: str = ""
    extra: dict = field(default_factory=dict)


def _parent(d: str) -> str:
    return d.rsplit("/", 1)[0] if "/" in d else ""


# --------------------------------------------------------------------------- model
class GridModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[Item] = []
        self._rows_by_path: dict[str, list[int]] = {}

    def set_items(self, items: list[Item]) -> None:
        self.beginResetModel()
        self.items = items
        self._rows_by_path = {}
        for i, it in enumerate(items):
            if it.path:
                self._rows_by_path.setdefault(it.path, []).append(i)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        if role == Qt.UserRole:
            return it
        if role == Qt.DisplayRole:
            return it.name
        if role == Qt.ToolTipRole:
            if it.kind == "doc" and it.entry:
                e = it.entry
                lines = [f"<b>{it.name}</b>", e.path]
                meta = [e.ext[1:].upper(), human_size(e.size)] if e.size else [e.ext[1:].upper()]
                if e.pages:
                    meta.append(tr("{n} pages", n=e.pages))
                lines.append(" · ".join(meta))
                return "<br>".join(lines)
            return f"<b>{it.name}</b><br>{tr('{n} documents', n=it.count)}"
        return None

    def refresh_path(self, path: str) -> None:
        for r in self._rows_by_path.get(path, ()):
            idx = self.index(r)
            self.dataChanged.emit(idx, idx)


# ------------------------------------------------------------------------ delegate
class TileDelegate(QStyledItemDelegate):
    def __init__(self, library: "LibraryWidget"):
        super().__init__(library)
        self.lib = library
        self.folder_pm = render("folder", 64, theme.TEXT_DIM)
        self.folder_small = render("folder", 16, "#ffcc66")

    def tile_size(self) -> QSize:
        w = self.lib.thumb_w
        return QSize(w + 18, int(w * 1.42) + 58)

    def sizeHint(self, option, index):
        return self.tile_size()

    def paint(self, p: QPainter, option, index):
        it: Item = index.data(Qt.UserRole)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = option.rect.adjusted(3, 3, -3, -3)
        hover = option.state & QStyle.State_MouseOver
        selected = option.state & QStyle.State_Selected
        if selected or hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#3a1b20" if selected else "#222228"))
            p.drawRoundedRect(r, 8, 8)

        w = self.lib.thumb_w
        cover = QRect(r.x() + (r.width() - w) // 2, r.y() + 6, w, int(w * 1.42))
        e = it.entry
        pm = self.lib.thumbs.get(it.path, e.size if e else None, e.mtime if e else None) if it.path else None

        if it.kind == "folder":
            # stacked look
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#26262d"))
            p.drawRoundedRect(cover.adjusted(8, -4, -8, -cover.height() + 10), 4, 4)
        if pm is not None and not pm.isNull():
            s = pm.size().scaled(cover.size(), Qt.KeepAspectRatio)
            target = QRect(cover.x() + (cover.width() - s.width()) // 2,
                           cover.bottom() - s.height() + 1, s.width(), s.height())
            path = QPainterPath()
            path.addRoundedRect(QRectF(target), 4, 4)
            p.setClipPath(path)
            p.drawPixmap(target, pm)
            p.setClipping(False)
            if it.kind == "folder":
                self._folder_badge(p, target, it.count)
            cover_area = target
        else:
            p.setPen(QPen(QColor("#2e2e36")))
            p.setBrush(QColor("#1d1d22"))
            p.drawRoundedRect(cover, 4, 4)
            if it.kind == "folder":
                p.drawPixmap(cover.center().x() - 32, cover.center().y() - 32, 64, 64, self.folder_pm)
                self._folder_badge(p, cover, it.count)
            elif e:
                col = QColor(EXT_COLORS.get(e.ext, "#888"))
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(QRect(cover.x(), cover.y(), cover.width(), 6), 3, 3)
                f = QFont(p.font())
                f.setPointSize(14)
                f.setBold(True)
                p.setFont(f)
                p.setPen(col)
                txt = e.ext[1:].upper()
                if self.lib.thumbs.is_failed(it.path):
                    txt += "\n?"
                p.drawText(cover, Qt.AlignCenter, txt)
            cover_area = cover

        # badges
        if e is not None:
            if it.path in self.lib.favorites:
                star = render("star_filled", 20, "#ffcc33")
                p.drawPixmap(cover_area.right() - 24, cover_area.top() + 4, star)
            prog = self.lib.progress.get(it.path)
            if prog and prog[1] > 0:
                frac = min(1.0, (prog[0] + 1) / prog[1])
                bar = QRect(cover_area.x() + 6, cover_area.bottom() - 9, cover_area.width() - 12, 4)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 170))
                p.drawRoundedRect(bar.adjusted(-1, -1, 1, 1), 2, 2)
                p.setBrush(QColor(theme.ACCENT))
                p.drawRoundedRect(QRect(bar.x(), bar.y(), max(3, int(bar.width() * frac)), bar.height()), 2, 2)
            f = QFont(p.font())
            f.setPointSize(7)
            f.setBold(True)
            p.setFont(f)
            label = e.ext[1:].upper()
            fm = QFontMetrics(f)
            bw = fm.horizontalAdvance(label) + 8
            badge = QRect(cover_area.x() + 4, cover_area.top() + 4, bw, fm.height() + 2)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(EXT_COLORS.get(e.ext, "#888")))
            p.drawRoundedRect(badge, 3, 3)
            p.setPen(QColor("white"))
            p.drawText(badge, Qt.AlignCenter, label)

        # title (2 lines, elided)
        f = QFont(option.font)
        f.setPointSize(9)
        p.setFont(f)
        fm = QFontMetrics(f)
        text_rect = QRect(r.x() + 6, cover.bottom() + 6, r.width() - 12, fm.height() * 2 + 2)
        lines = _two_lines(it.name, fm, text_rect.width())
        p.setPen(QColor(theme.TEXT))
        for i, line in enumerate(lines):
            p.drawText(QRect(text_rect.x(), text_rect.y() + i * fm.height(), text_rect.width(), fm.height()),
                       Qt.AlignHCenter | Qt.AlignVCenter, line)
        if it.sub:
            f.setPointSize(8)
            p.setFont(f)
            p.setPen(QColor(theme.TEXT_DIM))
            sub_y = text_rect.y() + len(lines) * fm.height() + 1
            p.drawText(QRect(r.x() + 4, sub_y, r.width() - 8, QFontMetrics(f).height()),
                       Qt.AlignHCenter, QFontMetrics(f).elidedText(it.sub, Qt.ElideMiddle, r.width() - 8))
        p.restore()


    def _folder_badge(self, p: QPainter, area: QRect, count: int) -> None:
        f = QFont(p.font())
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        txt = str(count)
        w = fm.horizontalAdvance(txt) + 30
        badge = QRect(area.right() - w - 5, area.bottom() - fm.height() - 11, w, fm.height() + 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 20, 24, 220))
        p.drawRoundedRect(badge, 5, 5)
        p.drawPixmap(badge.x() + 5, badge.y() + (badge.height() - 16) // 2, 16, 16, self.folder_small)
        p.setPen(QColor(theme.TEXT))
        p.drawText(badge.adjusted(23, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, txt)


def _two_lines(text: str, fm: QFontMetrics, width: int) -> list[str]:
    if fm.horizontalAdvance(text) <= width:
        return [text]
    words = text.split(" ")
    line = ""
    for i, w in enumerate(words):
        cand = (line + " " + w).strip()
        if fm.horizontalAdvance(cand) > width and line:
            rest = " ".join(words[i:])
            return [line, fm.elidedText(rest, Qt.ElideRight, width)]
        line = cand
    # single very long word
    return [fm.elidedText(text, Qt.ElideRight, width)]


# --------------------------------------------------------------------------- view
class LibraryWidget(QWidget):
    openRequested = Signal(str)
    settingsRequested = Signal()
    toolRequested = Signal(str, str)  # tool, path

    def __init__(self, db: Database, thumbs: ThumbnailManager, parent=None):
        super().__init__(parent)
        self.db = db
        self.thumbs = thumbs
        self.thumb_w = int(settings.get("library.thumb_size", 170))
        self.entries: list[Entry] = []
        self.by_path: dict[str, Entry] = {}
        self.dir_docs: dict[tuple, list[Entry]] = {}
        self.dir_children: dict[tuple, set[str]] = {}
        self.dir_count: dict[tuple, int] = {}
        self.dir_cover: dict[tuple, str] = {}
        self.favorites: set[str] = set()
        self.progress: dict[str, tuple[int, int]] = {}
        self.current_key: tuple = ("recent",)
        self.scanner: LibraryScanner | None = None
        self._tree_items: dict[tuple, QTreeWidgetItem] = {}
        if not db.recent(1):
            self.current_key = ("lib",)

        thumbs.ready.connect(self._on_thumb)
        thumbs.pages_known.connect(self._on_pages)
        self._build_ui()
        self._search_timer = QTimer(self, singleShot=True, interval=220, timeout=self._show_current)

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split)

        side = QFrame(objectName="sidebar")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(6, 8, 6, 6)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setIconSize(QSize(18, 18))
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.currentItemChanged.connect(self._on_tree_changed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        sl.addWidget(self.tree, 1)
        btns = QHBoxLayout()
        add = QPushButton(icon("folder"), tr("Add folder"))
        add.clicked.connect(self.add_root)
        btns.addWidget(add, 1)
        self.rescan_btn = QToolButton(icon=icon("refresh"), toolTip=tr("Rescan library"))
        self.rescan_btn.clicked.connect(self.rescan)
        btns.addWidget(self.rescan_btn)
        sl.addLayout(btns)
        split.addWidget(side)

        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(14, 10, 14, 6)
        head = QHBoxLayout()
        self.up_btn = QToolButton(icon=icon("parent"), toolTip=tr("Parent folder"))
        self.up_btn.clicked.connect(self.go_up)
        head.addWidget(self.up_btn)
        self.title = QLabel(objectName="title")
        head.addWidget(self.title)
        self.subtitle = QLabel(objectName="dim")
        head.addWidget(self.subtitle)
        head.addStretch(1)
        self.status = QLabel(objectName="dim")
        head.addWidget(self.status)
        self.search = QLineEdit(placeholderText=tr("Search the library…"), clearButtonEnabled=True)
        self.search.addAction(icon("search", theme.TEXT_DIM), QLineEdit.LeadingPosition)
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(lambda: self._search_timer.start())
        head.addWidget(self.search)
        self.sort = QComboBox()
        for key, label in (("name", tr("Name")), ("mtime", tr("Date modified")), ("size", tr("Size"))):
            self.sort.addItem(label, key)
        idx = self.sort.findData(settings.get("library.sort", "name"))
        self.sort.setCurrentIndex(max(0, idx))
        self.sort.currentIndexChanged.connect(self._on_sort)
        head.addWidget(self.sort)
        self.flat_btn = QToolButton(icon=icon("flatten"), checkable=True,
                                    toolTip=tr("Show all documents of sub-folders (flat view)"))
        self.flat_btn.setChecked(bool(settings.get("library.flatten")))
        self.flat_btn.toggled.connect(self._on_flatten)
        head.addWidget(self.flat_btn)
        ml.addLayout(head)

        self.stack = QStackedWidget()
        self.view = QListView()
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setMovement(QListView.Static)
        self.view.setUniformItemSizes(True)
        self.view.setLayoutMode(QListView.Batched)
        self.view.setBatchSize(400)
        self.view.setSpacing(4)
        self.view.setMouseTracking(True)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(40)
        self.view.setStyleSheet(f"QListView {{ background: {theme.BG}; }}")
        self.model = GridModel(self)
        self.view.setModel(self.model)
        self.delegate = TileDelegate(self)
        self.view.setItemDelegate(self.delegate)
        self.view.activated.connect(self._on_activated)
        self.view.doubleClicked.connect(self._on_activated)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._grid_menu)
        self.stack.addWidget(self.view)

        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.addStretch(1)
        self.empty_label = QLabel(alignment=Qt.AlignCenter, objectName="dim", wordWrap=True)
        el.addWidget(self.empty_label)
        self.empty_btn = QPushButton(icon("folder"), tr("Add a library folder"))
        self.empty_btn.setProperty("accent", True)
        self.empty_btn.clicked.connect(self.add_root)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.empty_btn)
        row.addStretch(1)
        el.addLayout(row)
        el.addStretch(2)
        self.stack.addWidget(self.empty)
        ml.addWidget(self.stack, 1)
        split.addWidget(main)
        split.setStretchFactor(1, 1)
        split.setSizes([270, 1200])

    # ------------------------------------------------------------ data
    def roots(self) -> list[str]:
        return [norm_path(r) for r in settings.get("library.roots", [])]

    def reload(self) -> None:
        """Re-read the index from the database and rebuild the category tree."""
        roots = self.roots()
        strip = settings.get("library.strip_prefixes", True)
        merge = settings.get("library.merge_single_doc_folders", True)
        self.entries = [Entry(*row) for row in self.db.library_rows(roots)]
        self.by_path = {e.path: e for e in self.entries}

        direct: dict[tuple, int] = {}
        subdirs: dict[tuple, set[str]] = {}
        for e in self.entries:
            direct[(e.root, e.rel_dir)] = direct.get((e.root, e.rel_dir), 0) + 1
            d = e.rel_dir
            while d:
                subdirs.setdefault((e.root, _parent(d)), set()).add(d)
                d = _parent(d)

        self.dir_docs, self.dir_children, self.dir_count, self.dir_cover = {}, {}, {}, {}
        for e in self.entries:
            d = e.rel_dir
            if merge and d and direct.get((e.root, d)) == 1 and not subdirs.get((e.root, d)):
                d = _parent(d)
            e.eff_dir = d
            self.dir_docs.setdefault((e.root, d), []).append(e)
        for (root, d) in list(self.dir_docs):
            cur = d
            while cur:
                self.dir_children.setdefault((root, _parent(cur)), set()).add(cur)
                cur = _parent(cur)
        for (root, d), docs in self.dir_docs.items():
            docs.sort(key=lambda x: natural_key(x.name))
            cur = d
            while True:
                k = (root, cur)
                self.dir_count[k] = self.dir_count.get(k, 0) + len(docs)
                if k not in self.dir_cover or natural_key(d) < natural_key(self.dir_cover[k][0]):
                    self.dir_cover[k] = (d, docs[0].path)
                if not cur:
                    break
                cur = _parent(cur)
        self._strip = strip
        self.favorites = self.db.favorite_set()
        self.progress = self.db.progress_map()
        self._rebuild_tree()

    def display_name(self, d: str, root: str = "") -> str:
        name = d.rsplit("/", 1)[-1] if d else os.path.basename(root) or root
        return strip_prefix(name) if self._strip else name

    def _rebuild_tree(self) -> None:
        self.tree.blockSignals(True)
        expanded = {k for k, it in self._tree_items.items() if it.isExpanded()}
        first_build = not self._tree_items
        self.tree.clear()
        self._tree_items = {}

        def add(parent, key, text, ic, count=None):
            it = QTreeWidgetItem([f"{text}  ({count})" if count is not None else text])
            it.setIcon(0, ic)
            it.setData(0, KEY_ROLE, key)
            if parent is None:
                self.tree.addTopLevelItem(it)
            else:
                parent.addChild(it)
            self._tree_items[key] = it
            return it

        add(None, ("recent",), tr("Recent"), icon("clock"))
        add(None, ("fav",), tr("Favorites"), icon("star_filled", "#ffcc33"))
        roots = self.roots()
        lib = add(None, ("lib",), tr("Library"), icon("library", theme.ACCENT),
                  sum(self.dir_count.get((r, ""), 0) for r in roots))
        folder_ic = icon("folder", theme.TEXT_DIM)

        def add_children(parent_item, root, d):
            kids = sorted(self.dir_children.get((root, d), ()),
                          key=lambda c: natural_key(self.display_name(c)))
            for c in kids:
                key = ("dir", root, c)
                it = add(parent_item, key, self.display_name(c), folder_ic, self.dir_count.get((root, c), 0))
                add_children(it, root, c)

        if len(roots) == 1:
            add_children(lib, roots[0], "")
        else:
            for r in roots:
                it = add(lib, ("dir", r, ""), self.display_name("", r), folder_ic, self.dir_count.get((r, ""), 0))
                add_children(it, r, "")
        lib.setExpanded(True)
        for k in expanded:
            if k in self._tree_items:
                self._tree_items[k].setExpanded(True)
        if first_build and len(roots) > 1:
            for r in roots:
                if ("dir", r, "") in self._tree_items:
                    self._tree_items[("dir", r, "")].setExpanded(True)
        key = self.current_key if self.current_key in self._tree_items else ("lib",)
        self.tree.setCurrentItem(self._tree_items[key])
        self.tree.blockSignals(False)
        self.current_key = key
        self._show_current()

    # ---------------------------------------------------------- views
    def _on_tree_changed(self, cur, _prev) -> None:
        if cur is None:
            return
        self.current_key = cur.data(0, KEY_ROLE)
        if self.search.text():
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self._show_current()

    def select_key(self, key: tuple) -> None:
        it = self._tree_items.get(key)
        if it is None:
            return
        p = it.parent()
        while p is not None:
            p.setExpanded(True)
            p = p.parent()
        self.tree.setCurrentItem(it)
        self.tree.scrollToItem(it)

    def _entry_for(self, path: str) -> Entry:
        e = self.by_path.get(path)
        if e is not None:
            return e
        name, ext = os.path.splitext(os.path.basename(path))
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0
        return Entry(path, "", os.path.dirname(path), name, ext.lower(), size, mtime)

    def _doc_item(self, e: Entry, with_folder: bool = False) -> Item:
        sub = ""
        if with_folder:
            sub = " / ".join(self.display_name(p) for p in e.eff_dir.split("/")) if e.root and e.eff_dir else \
                (os.path.basename(e.rel_dir) if not e.root else "")
        return Item("doc", e.name, e.path, entry=e, sub=sub)

    def _sorted_docs(self, docs: list[Entry]) -> list[Entry]:
        mode = self.sort.currentData()
        if mode == "mtime":
            return sorted(docs, key=lambda e: -(e.mtime or 0))
        if mode == "size":
            return sorted(docs, key=lambda e: -(e.size or 0))
        return sorted(docs, key=lambda e: natural_key(e.name))

    def _show_current(self) -> None:
        key = self.current_key
        query = self.search.text().strip()
        items: list[Item] = []
        roots = self.roots()
        self.up_btn.setEnabled(key[0] == "dir")
        if query:
            words = query.casefold().split()
            hits = [e for e in self.entries
                    if all(w in (e.name + " " + e.rel_dir).casefold() for w in words)]
            items = [self._doc_item(e, True) for e in self._sorted_docs(hits)]
            self.title.setText(tr("Search"))
            self.subtitle.setText(tr("“{q}” — {n} results", q=query, n=len(items)))
        elif key == ("recent",):
            paths = [p for p in self.db.recent(80) if os.path.exists(p)]
            items = [self._doc_item(self._entry_for(p), True) for p in paths]
            self.title.setText(tr("Recent"))
            self.subtitle.setText(tr("{n} documents", n=len(items)))
        elif key == ("fav",):
            paths = [p for p in self.db.favorites() if os.path.exists(p)]
            items = [self._doc_item(self._entry_for(p), True) for p in self._sorted_paths(paths)]
            self.title.setText(tr("Favorites"))
            self.subtitle.setText(tr("{n} documents", n=len(items)))
        else:
            if key == ("lib",):
                if len(roots) == 1:
                    root, d = roots[0], ""
                else:
                    root, d = None, None
                self.title.setText(tr("Library"))
            else:
                _, root, d = key
                self.title.setText(" / ".join(self.display_name(p) for p in d.split("/")) if d
                                   else self.display_name("", root))
            if root is None:
                for r in roots:
                    if (r, "") in self.dir_count:
                        items.append(Item("folder", self.display_name("", r), self.dir_cover[(r, "")][1],
                                          key=("dir", r, ""), count=self.dir_count[(r, "")],
                                          sub=tr("{n} documents", n=self.dir_count[(r, "")])))
                total = sum(self.dir_count.get((r, ""), 0) for r in roots)
            elif self.flat_btn.isChecked():
                docs = []
                for (r, dd), lst in self.dir_docs.items():
                    if r == root and (not d or dd == d or dd.startswith(d + "/")):
                        docs.extend(lst)
                items = [self._doc_item(e, True) for e in self._sorted_docs(docs)]
                total = len(items)
            else:
                kids = sorted(self.dir_children.get((root, d), ()),
                              key=lambda c: natural_key(self.display_name(c)))
                for c in kids:
                    k = (root, c)
                    items.append(Item("folder", self.display_name(c), self.dir_cover.get(k, ("", ""))[1],
                                      key=("dir", root, c), count=self.dir_count.get(k, 0),
                                      sub=tr("{n} documents", n=self.dir_count.get(k, 0))))
                items += [self._doc_item(e) for e in self._sorted_docs(self.dir_docs.get((root, d), []))]
                total = self.dir_count.get((root, d), 0)
            self.subtitle.setText(tr("{n} documents", n=total))

        self.thumbs.drop_pending()
        self.model.set_items(items)
        self.view.scrollToTop()
        if items:
            self.stack.setCurrentWidget(self.view)
        else:
            if not roots and key in (("lib",), ("recent",)) and not query:
                self.empty_label.setText(tr("Your library is empty.\nAdd a folder: its sub-folders become categories."))
                self.empty_btn.show()
            else:
                self.empty_label.setText(tr("Nothing here yet."))
                self.empty_btn.setVisible(not roots)
            self.stack.setCurrentWidget(self.empty)

    def _sorted_paths(self, paths: list[str]) -> list[str]:
        return [e.path for e in self._sorted_docs([self._entry_for(p) for p in paths])]

    def refresh_state(self) -> None:
        """Reload favorites / progress / recents (after reading)."""
        self.favorites = self.db.favorite_set()
        self.progress = self.db.progress_map()
        if self.current_key in (("recent",), ("fav",)) and not self.search.text():
            self._show_current()
        else:
            self.view.viewport().update()

    def go_up(self) -> None:
        key = self.current_key
        if key[0] != "dir":
            return
        _, root, d = key
        if not d:
            self.select_key(("lib",))
        else:
            parent = ("dir", root, _parent(d))
            if len(self.roots()) == 1 and not _parent(d):
                parent = ("lib",)
            self.select_key(parent)

    # ------------------------------------------------------- actions
    def _on_activated(self, index: QModelIndex) -> None:
        it: Item = index.data(Qt.UserRole)
        if it is None:
            return
        if it.kind == "folder":
            k = it.key
            if len(self.roots()) == 1 and k[2] == "":
                k = ("lib",)
            self.select_key(k)
        else:
            self.openRequested.emit(it.path)

    def _on_thumb(self, path: str) -> None:
        self.model.refresh_path(path)

    def _on_pages(self, path: str, pages: int) -> None:
        e = self.by_path.get(path)
        if e is not None and e.pages != pages:
            e.pages = pages
            self.db.set_pages(path, pages)

    def _on_sort(self) -> None:
        settings.set("library.sort", self.sort.currentData())
        self._show_current()

    def _on_flatten(self, on: bool) -> None:
        settings.set("library.flatten", on)
        self._show_current()

    def set_thumb_size(self, w: int) -> None:
        self.thumb_w = w
        self.model.layoutChanged.emit()

    def _grid_menu(self, pos: QPoint) -> None:
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        it: Item = idx.data(Qt.UserRole)
        m = QMenu(self)
        if it.kind == "folder":
            m.addAction(icon("folder"), tr("Open"), lambda: self._on_activated(idx))
            root, d = it.key[1], it.key[2]
            folder = os.path.join(root, *d.split("/")) if d else root
            m.addAction(icon("explorer"), tr("Show in Explorer"), lambda: show_in_explorer(folder))
            m.addSeparator()
            m.addAction(icon("convert"), tr("Convert the folder…"), lambda: self.toolRequested.emit("convert_folder", folder))
            m.addAction(icon("scissors"), tr("Split double pages in the folder…"),
                        lambda: self.toolRequested.emit("split_folder", folder))
            m.addAction(icon("search"), tr("Make the folder searchable (OCR)…"),
                        lambda: self.toolRequested.emit("ocr_folder", folder))
        else:
            m.addAction(icon("open"), tr("Open"), lambda: self.openRequested.emit(it.path))
            fav = it.path in self.favorites
            m.addAction(icon("star_filled" if not fav else "star", "#ffcc33"),
                        tr("Remove from favorites") if fav else tr("Add to favorites"),
                        lambda: self.toggle_favorite(it.path))
            if self.current_key == ("recent",) and not self.search.text():
                m.addAction(icon("close"), tr("Remove from recent"), lambda: self._remove_recent(it.path))
            m.addSeparator()
            m.addAction(icon("convert"), tr("Convert…"), lambda: self.toolRequested.emit("convert", it.path))
            if it.entry and it.entry.ext in (".cbz", ".cbr", ".pdf"):
                m.addAction(icon("scissors"), tr("Split double pages…"), lambda: self.toolRequested.emit("split", it.path))
            m.addAction(icon("merge"), tr("Merge with other files…"), lambda: self.toolRequested.emit("merge", it.path))
            if it.entry and it.entry.ext in (".cbz", ".cbr", ".pdf"):
                m.addAction(icon("search"), tr("Make searchable (OCR)…"), lambda: self.toolRequested.emit("ocr", it.path))
            m.addSeparator()
            m.addAction(icon("explorer"), tr("Show in Explorer"), lambda: show_in_explorer(it.path))
            m.addAction(icon("copy"), tr("Copy path"), lambda: QGuiApplication.clipboard().setText(it.path))
            e = it.entry
            if e and e.root:
                m.addAction(icon("folder"), tr("Go to category"), lambda: self.select_key(
                    ("dir", e.root, e.eff_dir) if (e.eff_dir or len(self.roots()) > 1) else ("lib",)))
        if self.current_key == ("recent",) and not self.search.text():
            m.addSeparator()
            m.addAction(icon("trash"), tr("Clear recent list"), self._clear_recent)
        m.exec(self.view.viewport().mapToGlobal(pos))

    def _tree_menu(self, pos: QPoint) -> None:
        it = self.tree.itemAt(pos)
        if it is None:
            return
        key = it.data(0, KEY_ROLE)
        m = QMenu(self)
        if key[0] == "dir":
            root, d = key[1], key[2]
            folder = os.path.join(root, *d.split("/")) if d else root
            m.addAction(icon("explorer"), tr("Show in Explorer"), lambda: show_in_explorer(folder))
            m.addAction(icon("convert"), tr("Convert the folder…"), lambda: self.toolRequested.emit("convert_folder", folder))
            m.addAction(icon("scissors"), tr("Split double pages in the folder…"),
                        lambda: self.toolRequested.emit("split_folder", folder))
            m.addAction(icon("search"), tr("Make the folder searchable (OCR)…"),
                        lambda: self.toolRequested.emit("ocr_folder", folder))
            if not d and root in self.roots():
                m.addAction(icon("trash"), tr("Remove this folder from the library"),
                            lambda: self.remove_root(root))
        elif key == ("lib",):
            m.addAction(icon("folder"), tr("Add folder"), self.add_root)
            m.addAction(icon("refresh"), tr("Rescan library"), self.rescan)
        elif key == ("recent",):
            m.addAction(icon("trash"), tr("Clear recent list"), self._clear_recent)
        if not m.isEmpty():
            m.exec(self.tree.viewport().mapToGlobal(pos))

    def toggle_favorite(self, path: str) -> None:
        self.db.set_favorite(path, path not in self.favorites)
        self.refresh_state()

    def _remove_recent(self, path: str) -> None:
        self.db.remove_recent(path)
        self._show_current()

    def _clear_recent(self) -> None:
        self.db.clear_recent()
        self._show_current()

    # ------------------------------------------------------ roots / scan
    def add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("Choose a library folder"))
        if not folder:
            return
        folder = norm_path(folder)
        roots = self.roots()
        if folder not in roots:
            roots.append(folder)
            settings.set("library.roots", roots)
            settings.save()
        self.rescan()

    def remove_root(self, root: str) -> None:
        roots = [r for r in self.roots() if r != root]
        settings.set("library.roots", roots)
        settings.save()
        self.db.drop_roots_except(roots)
        self.reload()

    def set_roots(self, roots: list[str]) -> None:
        roots = [norm_path(r) for r in roots]
        changed = roots != self.roots()
        settings.set("library.roots", roots)
        if changed:
            self.db.drop_roots_except(roots)
            self.rescan()
        else:
            self.reload()

    def rescan(self) -> None:
        if self.scanner is not None and self.scanner.isRunning():
            return
        roots = self.roots()
        if not roots:
            self.reload()
            return
        self.scanner = LibraryScanner(roots, self)
        self.scanner.progress.connect(lambda n: self.status.setText(tr("Scanning… {n} files", n=n)))
        self.scanner.done.connect(self._scan_done)
        self.rescan_btn.setEnabled(False)
        self.status.setText(tr("Scanning…"))
        self.scanner.start()

    def _scan_done(self, total: int) -> None:
        self.rescan_btn.setEnabled(True)
        self.status.setText(tr("{n} documents", n=total))
        QTimer.singleShot(4000, lambda: self.status.setText(""))
        self.reload()

    def stop_scan(self) -> None:
        if self.scanner is not None and self.scanner.isRunning():
            self.scanner.stop()
            self.scanner.wait(3000)
