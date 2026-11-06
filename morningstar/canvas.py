"""PageCanvas: the document viewport (layout, tiled rendering, zoom, selection, links)."""
from __future__ import annotations

import bisect
import math
import statistics
import time
from collections import OrderedDict

import pymupdf
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractScrollArea, QToolTip

from . import theme
from .docbackend import apply_dark, hex_to_int

PT2PX = 96.0 / 72.0
TILE = 2048
PREVIEW_W = 360
MIN_ZOOM, MAX_ZOOM = 0.1, 8.0
CACHE_BYTES = 450 * 1024 * 1024


class PageData:
    __slots__ = ("page", "dl", "w", "h", "rotm", "links", "_words", "_images")

    def __init__(self, page: pymupdf.Page):
        self.page = page
        self.dl = page.get_displaylist()
        r = page.rect
        self.w, self.h = r.width, r.height
        self.rotm = page.rotation_matrix
        try:
            self.links = page.get_links()
        except Exception:  # noqa: BLE001
            self.links = []
        for ln in self.links:
            ln["from"] = ln["from"] * self.rotm
        self._words = None
        self._images = None

    def words(self) -> list:
        if self._words is None:
            try:
                ws = self.page.get_text("words", sort=False)
            except Exception:  # noqa: BLE001
                ws = []
            out = []
            for w in ws:
                r = pymupdf.Rect(w[:4]) * self.rotm
                out.append((r, w[4], w[5], w[6]))
            self._words = out
        return self._words

    def image_rects(self) -> list[pymupdf.Rect]:
        if self._images is None:
            area = max(self.w * self.h, 1)
            out = []
            try:
                for info in self.page.get_image_info():
                    r = pymupdf.Rect(info["bbox"]) * self.rotm
                    if r.is_empty or r.is_infinite:
                        continue
                    if (r & pymupdf.Rect(0, 0, self.w, self.h)).get_area() / area < 0.8:
                        out.append(r)
            except Exception:  # noqa: BLE001
                pass
            self._images = out
        return self._images


class PageCanvas(QAbstractScrollArea):
    pageChanged = Signal(int)
    zoomChanged = Signal(float)
    layoutChanged = Signal()
    linkActivated = Signal(dict)
    contextRequested = Signal(int, QPointF, QPoint)  # page, page point, global pos
    noteClicked = Signal(int)
    selectionChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QAbstractScrollArea.NoFrame)
        self.viewport().setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.verticalScrollBar().setSingleStep(48)
        self.horizontalScrollBar().setSingleStep(48)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.horizontalScrollBar().valueChanged.connect(self._on_scroll)

        self.doc: pymupdf.Document | None = None
        self.n = 0
        self.sizes: list[tuple[float, float]] = []
        self.known: list[bool] = []
        self.mode = "continuous"
        self.fit = "width"
        self.zoom = 1.0
        self.rotation = 0
        self.rtl = False
        self.cover_alone = True
        self.gap = 12
        self.margin = 16
        self.dark = False
        self.dark_bg = "#121212"
        self.dark_fg = "#e8e6e3"
        self.keep_images = True

        self.rows: list[list[int]] = []
        self.row_of: list[int] = []
        self.cur_row = 0  # single/double paged modes
        self.row_tops: list[int] = []
        self.row_heights: list[int] = []
        self.page_rects: dict[int, QRect] = {}
        self.content_w = 0
        self.content_h = 0
        self.current_page = 0

        self._pages: OrderedDict[int, PageData] = OrderedDict()
        self._cache: OrderedDict[tuple, tuple[QImage, int, int]] = OrderedDict()
        self._cache_bytes = 0
        self._jobs: list[tuple] = []
        self._job_timer = QTimer(self, singleShot=True, interval=0, timeout=self._run_job)
        self._probe_timer = QTimer(self, singleShot=True, interval=30, timeout=self._probe_sizes)
        self._relayout_timer = QTimer(self, singleShot=True, interval=60, timeout=self._relayout_keep)
        self._probe_next = 0

        self.search_hits: dict[int, list[pymupdf.Rect]] = {}
        self.search_current: tuple[int, int] | None = None
        self.notes: list[dict] = []
        self.bookmarked: set[int] = set()

        self._press_pos: QPoint | None = None
        self._press_link: dict | None = None
        self._drag_mode: str | None = None  # select | pan
        self._pan_origin: tuple[int, int] = (0, 0)
        self._sel_page = -1
        self._sel_start: pymupdf.Point | None = None
        self._sel_rects: list[pymupdf.Rect] = []
        self.selected_text = ""
        # Editing hooks: an object with on_press / on_drag / on_release / on_hover / on_double /
        # on_key / on_text_selected / field_press / field_hover / paint (all optional).
        self.interactor = None
        self.tool: str | None = None
        self._tool_page = -1

    # ------------------------------------------------------------------ setup
    def set_document(self, doc: pymupdf.Document) -> None:
        self.clear_caches()
        self.doc = doc
        self.n = doc.page_count
        self._reset_sizes()
        self.cur_row = 0
        self.relayout()

    def _reset_sizes(self) -> None:
        self.n = self.doc.page_count if self.doc else 0
        if self.n:
            p0 = self._page(0)
            est = (p0.w, p0.h)
        else:
            est = (595.0, 842.0)
        self.sizes = [est] * self.n
        self.known = [False] * self.n
        if self.n:
            self.known[0] = True
        self._probe_next = 1
        if self.doc is not None and not self.doc.is_reflowable:
            self._probe_timer.start()
        else:
            self.known = [True] * self.n

    def clear_caches(self) -> None:
        self._pages.clear()
        self._cache.clear()
        self._cache_bytes = 0
        self._jobs.clear()
        self._sel_rects = []
        self.selected_text = ""

    def invalidate_page(self, page: int) -> None:
        """Forget cached rendering of one page after it was modified."""
        self._pages.pop(page, None)
        for k in [k for k in self._cache if k[1] == page]:
            img = self._cache.pop(k)[0]
            self._cache_bytes -= img.sizeInBytes()
        self._schedule()
        self.viewport().update()

    def replace_document(self, doc: pymupdf.Document) -> None:
        """Swap the document (after undo / page operations) keeping the reading position."""
        page, frac = self.current_page, self.position_frac()
        self.clear_caches()
        self.doc = doc
        self._reset_sizes()
        self.cur_row = 0
        self.relayout()
        self.goto_page(min(page, max(0, self.n - 1)), frac)

    def _hook(self, name: str):
        it = self.interactor
        return getattr(it, name, None) if it is not None else None

    def close_document(self) -> None:
        self._probe_timer.stop()
        self._job_timer.stop()
        self.clear_caches()
        self.doc = None
        self.n = 0

    def _page(self, i: int) -> PageData:
        pd = self._pages.get(i)
        if pd is not None:
            self._pages.move_to_end(i)
            return pd
        pd = PageData(self.doc.load_page(i))
        self._pages[i] = pd
        while len(self._pages) > 40:
            self._pages.popitem(last=False)
        if i < len(self.sizes):
            if not self.known[i]:
                self.known[i] = True
            if abs(self.sizes[i][0] - pd.w) > 0.5 or abs(self.sizes[i][1] - pd.h) > 0.5:
                self.sizes[i] = (pd.w, pd.h)
                self._relayout_timer.start()
        return pd

    def _probe_sizes(self) -> None:
        """Learn real page sizes progressively without blocking the UI."""
        if self.doc is None:
            return
        t0 = time.perf_counter()
        changed = False
        i = self._probe_next
        while i < self.n and time.perf_counter() - t0 < 0.012:
            if not self.known[i]:
                try:
                    r = self.doc.load_page(i).rect
                    if abs(self.sizes[i][0] - r.width) > 0.5 or abs(self.sizes[i][1] - r.height) > 0.5:
                        self.sizes[i] = (r.width, r.height)
                        changed = True
                except Exception:  # noqa: BLE001
                    pass
                self.known[i] = True
            i += 1
        self._probe_next = i
        if changed:
            self._relayout_timer.start()
        if i < self.n:
            self._probe_timer.start()

    # ----------------------------------------------------------------- layout
    @property
    def paged(self) -> bool:
        return self.mode in ("single", "double")

    @property
    def scale(self) -> float:
        return self.zoom * PT2PX

    def _disp_size(self, i: int) -> tuple[float, float]:
        w, h = self.sizes[i]
        return (h, w) if self.rotation in (90, 270) else (w, h)

    def _build_rows(self) -> None:
        rows = []
        if self.mode == "double":
            i = 0
            if self.cover_alone and self.n:
                rows.append([0])
                i = 1
            while i < self.n:
                rows.append([i, i + 1] if i + 1 < self.n else [i])
                i += 2
        else:
            rows = [[i] for i in range(self.n)]
        self.rows = rows
        self.row_of = [0] * self.n
        for r, pages in enumerate(rows):
            for p in pages:
                self.row_of[p] = r

    def _row_size_pt(self, r: int) -> tuple[float, float]:
        pages = self.rows[r]
        ws = [self._disp_size(p)[0] for p in pages]
        hs = [self._disp_size(p)[1] for p in pages]
        return sum(ws) + (len(pages) - 1) * self.gap / max(self.scale, 1e-6), max(hs)

    def _compute_fit_zoom(self) -> float:
        if not self.rows:
            return self.zoom
        vw = max(50, self.viewport().width() - 2 * self.margin)
        vh = max(50, self.viewport().height() - 2 * self.margin)
        sample = range(len(self.rows))
        if len(self.rows) > 400:
            step = len(self.rows) // 200
            sample = range(0, len(self.rows), step)
        widths, heights = [], []
        for r in sample:
            pages = self.rows[r]
            widths.append(sum(self._disp_size(p)[0] for p in pages))
            heights.append(max(self._disp_size(p)[1] for p in pages))
        w = statistics.median(widths)
        h = statistics.median(heights)
        gaps = (2 - 1) * self.gap if self.mode == "double" else 0
        zw = (vw - gaps) / (w * PT2PX)
        if self.fit == "page":
            zh = vh / (h * PT2PX)
            return max(MIN_ZOOM, min(MAX_ZOOM, min(zw, zh)))
        return max(MIN_ZOOM, min(MAX_ZOOM, zw))

    def relayout(self) -> None:
        self._build_rows()
        if self.fit != "none":
            self.zoom = self._compute_fit_zoom()
        s = self.scale
        self.cur_row = max(0, min(self.cur_row, len(self.rows) - 1))
        visible_rows = [self.cur_row] if (self.paged and self.rows) else range(len(self.rows))
        self.page_rects = {}
        self.row_tops = []
        self.row_heights = []
        y = self.margin
        max_w = 0
        row_geom = []
        for r in visible_rows:
            pages = self.rows[r]
            ws = [max(1, round(self._disp_size(p)[0] * s)) for p in pages]
            hs = [max(1, round(self._disp_size(p)[1] * s)) for p in pages]
            rw = sum(ws) + self.gap * (len(pages) - 1)
            rh = max(hs)
            row_geom.append((r, pages, ws, hs, rw, rh, y))
            self.row_tops.append(y)
            self.row_heights.append(rh)
            max_w = max(max_w, rw)
            y += rh + self.gap
        self.content_w = max_w + 2 * self.margin
        self.content_h = y - self.gap + self.margin
        vw = self.viewport().width()
        cw = max(self.content_w, vw)
        for r, pages, ws, hs, rw, rh, ry in row_geom:
            x = (cw - rw) // 2
            order = list(zip(pages, ws, hs))
            if self.rtl and len(order) > 1:
                order.reverse()
            for p, w, h in order:
                self.page_rects[p] = QRect(int(x), int(ry + (rh - h) // 2), w, h)
                x += w + self.gap
        self._visible_row_ids = [g[0] for g in row_geom]
        self._update_scrollbars()
        self.zoomChanged.emit(self.zoom)
        self.layoutChanged.emit()
        self._schedule()
        self.viewport().update()

    def _update_scrollbars(self) -> None:
        vw, vh = self.viewport().width(), self.viewport().height()
        hs, vs = self.horizontalScrollBar(), self.verticalScrollBar()
        hs.setRange(0, max(0, self.content_w - vw))
        hs.setPageStep(vw)
        vs.setRange(0, max(0, self.content_h - vh))
        vs.setPageStep(vh)

    # -------------------------------------------------------------- anchors
    def _anchor_at(self, wpos: QPoint) -> tuple[int, float, float, QPoint]:
        """Document anchor under widget position: (page, fx, fy, wpos)."""
        cx = wpos.x() + self.horizontalScrollBar().value()
        cy = wpos.y() + self.verticalScrollBar().value()
        best, bestd = self.current_page, None
        for p, r in self.page_rects.items():
            d = 0 if r.contains(cx, cy) else min(abs(cy - r.center().y()), 10 ** 9)
            if bestd is None or d < bestd:
                best, bestd = p, d
                if d == 0:
                    break
        r = self.page_rects.get(best)
        if r is None:
            return best, 0.0, 0.0, wpos
        return best, (cx - r.x()) / max(1, r.width()), (cy - r.y()) / max(1, r.height()), wpos

    def _restore_anchor(self, anchor) -> None:
        page, fx, fy, wpos = anchor
        if self.paged and page < len(self.row_of) and self.row_of[page] != self.cur_row:
            self.cur_row = self.row_of[page]
            self.relayout()
        r = self.page_rects.get(page)
        if r is None:
            return
        self.horizontalScrollBar().setValue(int(r.x() + fx * r.width() - wpos.x()))
        self.verticalScrollBar().setValue(int(r.y() + fy * r.height() - wpos.y()))

    def _relayout_keep(self) -> None:
        anchor = self._anchor_at(QPoint(self.viewport().width() // 2, 0))
        self.relayout()
        self._restore_anchor(anchor)

    # ------------------------------------------------------------ properties
    def set_zoom(self, z: float, around: QPoint | None = None) -> None:
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        if around is None:
            around = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
        anchor = self._anchor_at(around)
        self.fit = "none"
        self.zoom = z
        self.relayout()
        self._restore_anchor(anchor)

    def zoom_step(self, direction: int, around: QPoint | None = None) -> None:
        steps = [0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5,
                 3.0, 4.0, 5.0, 6.4, 8.0]
        z = self.zoom
        if direction > 0:
            nz = next((s for s in steps if s > z + 1e-3), MAX_ZOOM)
        else:
            nz = next((s for s in reversed(steps) if s < z - 1e-3), MIN_ZOOM)
        self.set_zoom(nz, around)

    def set_fit(self, fit: str) -> None:
        anchor = self._anchor_at(QPoint(self.viewport().width() // 2, 0))
        self.fit = fit
        self.relayout()
        self._restore_anchor((anchor[0], 0.5, anchor[2], QPoint(self.viewport().width() // 2, 0)))

    def set_mode(self, mode: str) -> None:
        page = self.current_page
        self.mode = mode
        self._build_rows()
        self.cur_row = self.row_of[page] if self.n else 0
        self.relayout()
        self.goto_page(page)

    def set_rotation(self, rot: int) -> None:
        page = self.current_page
        self.rotation = rot % 360
        self.relayout()
        self.goto_page(page)

    def set_rtl(self, rtl: bool) -> None:
        self.rtl = rtl
        self._relayout_keep()

    def set_dark(self, on: bool, bg: str | None = None, fg: str | None = None,
                 keep_images: bool | None = None) -> None:
        self.dark = on
        if bg:
            self.dark_bg = bg
        if fg:
            self.dark_fg = fg
        if keep_images is not None:
            self.keep_images = keep_images
        self._jobs.clear()
        self._schedule()
        self.viewport().update()

    def _dark_sig(self):
        return (self.dark_bg, self.dark_fg, self.keep_images) if self.dark else None

    # ------------------------------------------------------------ navigation
    def goto_page(self, page: int, frac: float = 0.0, y_pt: float | None = None) -> None:
        if not self.n:
            return
        page = max(0, min(self.n - 1, page))
        if self.paged and self.row_of[page] != self.cur_row:
            self.cur_row = self.row_of[page]
            self.relayout()
        r = self.page_rects.get(page)
        if r is None:
            return
        if y_pt is not None:
            _w, h = self._disp_size(page)
            frac = max(0.0, min(1.0, y_pt / max(h, 1)))
            off = int(frac * r.height()) - 20
        else:
            off = int(frac * r.height()) if frac else -self.gap // 2 - self.margin // 2
        self.verticalScrollBar().setValue(r.y() + off)
        if self.content_w > self.viewport().width():
            self.horizontalScrollBar().setValue(r.x() + (r.width() - self.viewport().width()) // 2)
        self._update_current(force=True)

    def next_page(self) -> None:
        if self.paged:
            r = self.row_of[self.current_page] if self.n else 0
            if r + 1 < len(self.rows):
                self.goto_page(self.rows[r + 1][0])
        else:
            self.goto_page(self.current_page + 1)

    def prev_page(self) -> None:
        if self.paged:
            r = self.row_of[self.current_page] if self.n else 0
            if r > 0:
                self.goto_page(self.rows[r - 1][0])
        else:
            vs = self.verticalScrollBar()
            r = self.page_rects.get(self.current_page)
            if r is not None and vs.value() > r.y() + 5:
                self.goto_page(self.current_page)
            else:
                self.goto_page(self.current_page - 1)

    def position_frac(self) -> float:
        r = self.page_rects.get(self.current_page)
        if r is None:
            return 0.0
        return max(0.0, min(1.0, (self.verticalScrollBar().value() - r.y()) / max(1, r.height())))

    def ensure_visible(self, page: int, rect: pymupdf.Rect) -> None:
        if self.paged and self.row_of[page] != self.cur_row:
            self.cur_row = self.row_of[page]
            self.relayout()
        wr = self.page_to_widget_rect(page, rect)
        if wr is None:
            return
        vh, vw = self.viewport().height(), self.viewport().width()
        vs, hs = self.verticalScrollBar(), self.horizontalScrollBar()
        if wr.top() < 40 or wr.bottom() > vh - 40:
            vs.setValue(vs.value() + wr.center().y() - vh // 3)
        if wr.left() < 0 or wr.right() > vw:
            hs.setValue(hs.value() + wr.center().x() - vw // 2)
        self.viewport().update()

    def _on_scroll(self) -> None:
        self._update_current()
        self._schedule()
        self.viewport().update()

    def _update_current(self, force: bool = False) -> None:
        if not self.page_rects:
            return
        if self.paged:
            page = self.rows[self.cur_row][0] if self.rows else 0
        else:
            y = self.verticalScrollBar().value() + int(self.viewport().height() * 0.35)
            i = bisect.bisect_right(self.row_tops, y) - 1
            i = max(0, min(i, len(self._visible_row_ids) - 1))
            page = self.rows[self._visible_row_ids[i]][0]
        if page != self.current_page or force:
            self.current_page = page
            self.pageChanged.emit(page)

    def visible_pages(self, margin: int = 0) -> list[int]:
        vy = self.verticalScrollBar().value()
        vh = self.viewport().height()
        lo = bisect.bisect_right(self.row_tops, vy - margin) - 1
        lo = max(0, lo)
        out = []
        for i in range(lo, len(self.row_tops)):
            if self.row_tops[i] > vy + vh + margin:
                break
            if self.row_tops[i] + self.row_heights[i] < vy - margin:
                continue
            out.extend(self.rows[self._visible_row_ids[i]])
        return out

    # ------------------------------------------------------------- geometry
    def _view_matrix(self, page: int, dpr: float = 1.0) -> tuple[pymupdf.Matrix, pymupdf.Rect]:
        s = self.scale * dpr
        m = pymupdf.Matrix(s, s).prerotate(self.rotation)
        w, h = self.sizes[page]
        bbox = pymupdf.Rect(0, 0, w, h) * m
        return m, bbox

    def page_to_widget(self, page: int, pt: pymupdf.Point) -> QPointF | None:
        r = self.page_rects.get(page)
        if r is None:
            return None
        m, bbox = self._view_matrix(page)
        q = pt * m
        return QPointF(r.x() + q.x - bbox.x0 - self.horizontalScrollBar().value(),
                       r.y() + q.y - bbox.y0 - self.verticalScrollBar().value())

    def page_to_widget_rect(self, page: int, rect: pymupdf.Rect) -> QRectF | None:
        r = self.page_rects.get(page)
        if r is None:
            return None
        m, bbox = self._view_matrix(page)
        q = rect * m
        ox = r.x() - bbox.x0 - self.horizontalScrollBar().value()
        oy = r.y() - bbox.y0 - self.verticalScrollBar().value()
        return QRectF(q.x0 + ox, q.y0 + oy, q.width, q.height)

    def widget_to_page(self, wpos: QPoint | QPointF, clamp_page: int | None = None) -> tuple[int, pymupdf.Point] | None:
        cx = wpos.x() + self.horizontalScrollBar().value()
        cy = wpos.y() + self.verticalScrollBar().value()
        target = None
        if clamp_page is not None:
            target = clamp_page
        else:
            for p, r in self.page_rects.items():
                if r.adjusted(-4, -4, 4, 4).contains(int(cx), int(cy)):
                    target = p
                    break
        if target is None or target not in self.page_rects:
            return None
        r = self.page_rects[target]
        m, bbox = self._view_matrix(target)
        q = pymupdf.Point(cx - r.x() + bbox.x0, cy - r.y() + bbox.y0) * ~m
        return target, q

    # ------------------------------------------------------------- rendering
    def _tile_grid(self, page: int, dpr: float) -> tuple[pymupdf.Matrix, pymupdf.Rect, int, int]:
        m, bbox = self._view_matrix(page, dpr)
        nx = max(1, math.ceil(bbox.width / TILE))
        ny = max(1, math.ceil(bbox.height / TILE))
        return m, bbox, nx, ny

    def _schedule(self) -> None:
        if self.doc is None or not self.page_rects:
            return
        dpr = self.devicePixelRatioF()
        rs = round(self.scale * dpr, 4)
        sig = self._dark_sig()
        rot = self.rotation
        vx = self.horizontalScrollBar().value()
        vy = self.verticalScrollBar().value()
        vw, vh = self.viewport().width(), self.viewport().height()
        view = QRect(vx, vy, vw, vh)
        jobs: list[tuple] = []
        visible = self.visible_pages()
        around = self.visible_pages(margin=vh)
        for p in visible:
            k = ("pv", p, rot, sig)
            if k not in self._cache:
                jobs.append(k)
        for group, extra in ((visible, 0), (around, vh)):
            area = view.adjusted(-vw // 2, -extra, vw // 2, extra)
            for p in group:
                r = self.page_rects.get(p)
                if r is None:
                    continue
                m, bbox, nx, ny = self._tile_grid(p, dpr)
                for ty in range(ny):
                    for tx in range(nx):
                        tr_ = QRect(r.x() + int(tx * TILE / dpr), r.y() + int(ty * TILE / dpr),
                                    int(TILE / dpr) + 1, int(TILE / dpr) + 1)
                        if not tr_.intersects(area):
                            continue
                        k = ("t", p, rs, rot, sig, tx, ty)
                        if k not in self._cache and k not in jobs:
                            jobs.append(k)
        for p in around:
            k = ("pv", p, rot, sig)
            if k not in self._cache and k not in jobs:
                jobs.append(k)
        self._jobs = jobs
        if jobs and not self._job_timer.isActive():
            self._job_timer.start()

    def _run_job(self) -> None:
        if not self._jobs or self.doc is None:
            return
        key = self._jobs.pop(0)
        if key not in self._cache:
            try:
                self._render(key)
            except Exception:  # noqa: BLE001 - damaged page: skip it
                pass
            self.viewport().update()
        if self._jobs:
            self._job_timer.start()

    def _render(self, key: tuple) -> None:
        kind, page = key[0], key[1]
        pd = self._page(page)
        sig = key[3] if kind == "pv" else key[4]
        if kind == "pv":
            w, h = self._disp_size(page)
            s = PREVIEW_W / max(w, 1)
            m = pymupdf.Matrix(s, s).prerotate(self.rotation)
            pix = pd.dl.get_pixmap(matrix=m, alpha=False)
            bbox = pymupdf.Rect(0, 0, pd.w, pd.h) * m
        else:
            rs, tx, ty = key[2], key[5], key[6]
            m = pymupdf.Matrix(rs, rs).prerotate(self.rotation)
            bbox = pymupdf.Rect(0, 0, pd.w, pd.h) * m
            dev = pymupdf.Rect(bbox.x0 + tx * TILE, bbox.y0 + ty * TILE,
                               min(bbox.x1, bbox.x0 + (tx + 1) * TILE), min(bbox.y1, bbox.y0 + (ty + 1) * TILE))
            clip = dev * ~m
            pix = pd.dl.get_pixmap(matrix=m, clip=clip, alpha=False)
        if sig is not None:
            keep = None
            if sig[2]:
                keep = [(r * m).irect for r in pd.image_rects()]
            apply_dark(pix, hex_to_int(sig[0]), hex_to_int(sig[1]), keep)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        ox = pix.x - bbox.x0
        oy = pix.y - bbox.y0
        self._cache[key] = (img, int(round(ox)), int(round(oy)))
        self._cache_bytes += img.sizeInBytes()
        while self._cache_bytes > CACHE_BYTES and len(self._cache) > 1:
            _k, (old, _x, _y) = self._cache.popitem(last=False)
            self._cache_bytes -= old.sizeInBytes()

    # --------------------------------------------------------------- painting
    def paintEvent(self, _ev) -> None:
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), QColor(theme.CANVAS))
        if self.doc is None:
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        dpr = self.devicePixelRatioF()
        rs = round(self.scale * dpr, 4)
        sig = self._dark_sig()
        rot = self.rotation
        vx = self.horizontalScrollBar().value()
        vy = self.verticalScrollBar().value()
        page_bg = QColor(self.dark_bg if self.dark else "#ffffff")
        for page in self.visible_pages():
            r = self.page_rects.get(page)
            if r is None:
                continue
            wr = r.translated(-vx, -vy)
            p.fillRect(wr.adjusted(-1, -1, 1, 1), QColor("#000000"))
            p.fillRect(wr, page_bg)
            pv = self._cache.get(("pv", page, rot, sig))
            m, bbox, nx, ny = self._tile_grid(page, dpr)
            tiles = []
            complete = True
            for ty in range(ny):
                for tx in range(nx):
                    t = self._cache.get(("t", page, rs, rot, sig, tx, ty))
                    tile_rect = QRect(wr.x() + int(tx * TILE / dpr), wr.y() + int(ty * TILE / dpr),
                                      int(TILE / dpr) + 1, int(TILE / dpr) + 1)
                    if t is None:
                        if tile_rect.intersects(self.viewport().rect()):
                            complete = False
                    else:
                        tiles.append(t)
            if pv is not None and not complete:
                p.drawImage(QRectF(wr), pv[0])
            for img, ox, oy in tiles:
                p.drawImage(QRectF(wr.x() + ox / dpr, wr.y() + oy / dpr, img.width() / dpr, img.height() / dpr), img)
            self._paint_overlays(p, page, wr)
        p.end()

    def _paint_overlays(self, p: QPainter, page: int, wr: QRect) -> None:
        hook = self._hook("paint")
        if hook is not None:
            p.save()
            hook(p, page, wr)
            p.restore()
        hits = self.search_hits.get(page)
        if hits:
            p.setPen(Qt.NoPen)
            for idx, rect in enumerate(hits):
                q = self.page_to_widget_rect(page, rect)
                if q is None:
                    continue
                cur = self.search_current == (page, idx)
                p.setBrush(QColor(255, 120, 0, 150) if cur else QColor(255, 210, 0, 90))
                p.drawRect(q.adjusted(-1, -1, 1, 1))
        if self._sel_rects and page == self._sel_page:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(80, 150, 255, 90))
            for rect in self._sel_rects:
                q = self.page_to_widget_rect(page, rect)
                if q is not None:
                    p.drawRect(q)
        if page in self.bookmarked:
            x = wr.right() - 34
            poly = QPolygonF([QPointF(x, wr.y()), QPointF(x + 20, wr.y()), QPointF(x + 20, wr.y() + 30),
                              QPointF(x + 10, wr.y() + 23), QPointF(x, wr.y() + 30)])
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.ACCENT))
            p.drawPolygon(poly)
        for note in self.notes:
            if note["page"] != page:
                continue
            q = self.page_to_widget(page, pymupdf.Point(note["x"], note["y"]))
            if q is None:
                continue
            r = QRectF(q.x() - 9, q.y() - 9, 18, 18)
            p.setPen(QPen(QColor("#7a5b00"), 1))
            p.setBrush(QColor("#ffd54a"))
            p.drawRoundedRect(r, 3, 3)
            p.setPen(QPen(QColor("#7a5b00"), 1.4))
            p.drawLine(QPointF(r.x() + 4, r.y() + 6), QPointF(r.right() - 4, r.y() + 6))
            p.drawLine(QPointF(r.x() + 4, r.y() + 10), QPointF(r.right() - 6, r.y() + 10))

    # ----------------------------------------------------------------- events
    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        if self.doc is None:
            return
        if self.fit != "none" or self.content_w <= self.viewport().width():
            self._relayout_keep()
        else:
            self._update_scrollbars()

    def wheelEvent(self, ev) -> None:
        if self.doc is None:
            return
        dy = ev.angleDelta().y()
        if ev.modifiers() & Qt.ControlModifier:
            if dy:
                factor = 1.0 + 0.1 * (dy / 120)
                self.set_zoom(self.zoom * factor, ev.position().toPoint())
            return
        vs = self.verticalScrollBar()
        if self.paged and dy:
            if dy < 0 and vs.value() >= vs.maximum():
                self.next_page()
                return
            if dy > 0 and vs.value() <= vs.minimum():
                if self.cur_row > 0:
                    self.prev_page()
                    vs.setValue(vs.maximum())
                return
        if ev.modifiers() & Qt.ShiftModifier:
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - dy)
            return
        super().wheelEvent(ev)

    def keyPressEvent(self, ev) -> None:
        if self.doc is None:
            return super().keyPressEvent(ev)
        k = ev.key()
        key_hook = self._hook("on_key")
        if key_hook is not None and key_hook(ev):
            return
        vs = self.verticalScrollBar()
        hs = self.horizontalScrollBar()
        paged = self.paged
        no_hscroll = hs.maximum() == 0
        if k in (Qt.Key_PageDown, Qt.Key_Space) and not (ev.modifiers() & Qt.ShiftModifier):
            if self.paged and vs.value() >= vs.maximum():
                self.next_page()
            else:
                vs.setValue(vs.value() + int(self.viewport().height() * 0.9))
        elif k == Qt.Key_PageUp or (k == Qt.Key_Space and ev.modifiers() & Qt.ShiftModifier):
            if self.paged and vs.value() <= 0:
                if self.cur_row > 0:
                    self.prev_page()
                    vs.setValue(vs.maximum())
            else:
                vs.setValue(vs.value() - int(self.viewport().height() * 0.9))
        elif k in (Qt.Key_Right, Qt.Key_Left) and (paged or no_hscroll):
            forward = (k == Qt.Key_Right) != (self.rtl and self.mode == "double")
            self.next_page() if forward else self.prev_page()
        elif k == Qt.Key_Down:
            vs.setValue(vs.value() + vs.singleStep())
        elif k == Qt.Key_Up:
            vs.setValue(vs.value() - vs.singleStep())
        elif k == Qt.Key_Right:
            hs.setValue(hs.value() + hs.singleStep())
        elif k == Qt.Key_Left:
            hs.setValue(hs.value() - hs.singleStep())
        elif k == Qt.Key_Home:
            self.goto_page(0)
        elif k == Qt.Key_End:
            self.goto_page(self.n - 1)
        elif k == Qt.Key_Escape and self._sel_rects:
            self.clear_selection()
        else:
            super().keyPressEvent(ev)

    def _link_at(self, page: int, pt: pymupdf.Point) -> dict | None:
        pd = self._pages.get(page)
        if pd is None:
            return None
        for ln in pd.links:
            if ln["from"].contains(pt):
                return ln
        return None

    def _note_at(self, wpos: QPoint) -> dict | None:
        for note in self.notes:
            q = self.page_to_widget(note["page"], pymupdf.Point(note["x"], note["y"]))
            if q is not None and abs(q.x() - wpos.x()) <= 10 and abs(q.y() - wpos.y()) <= 10:
                return note
        return None

    def mousePressEvent(self, ev) -> None:
        if self.doc is None:
            return
        self.setFocus()
        pos = ev.position().toPoint()
        self._press_pos = pos
        self._press_link = None
        if ev.button() == Qt.MiddleButton:
            self._start_pan(pos)
            return
        if ev.button() != Qt.LeftButton:
            return
        note = self._note_at(pos) if self.tool is None else None
        if note is not None:
            self._drag_mode = None
            self.noteClicked.emit(note["id"])
            return
        hit = self.widget_to_page(pos)
        if hit is None:
            self._start_pan(pos)
            return
        page, pt = hit
        field_hook = self._hook("field_press")
        if self.tool is None and field_hook is not None and field_hook(page, pt):
            self._drag_mode = None
            return
        press_hook = self._hook("on_press")
        if self.tool is not None and press_hook is not None:
            mode = press_hook(page, pt, ev.modifiers())
            if mode == "drag":
                self._drag_mode = "tool"
                self._tool_page = page
                return
            if mode == "done":
                self._drag_mode = None
                return
            if mode == "pan":
                self._start_pan(pos)
                return
            # "select": fall through to the normal text selection
        self._press_link = self._link_at(page, pt)
        pd = self._page(page)
        if pd.words() and not (ev.modifiers() & Qt.AltModifier):
            self._drag_mode = "select"
            self._sel_page = page
            self._sel_start = pt
            if self._sel_rects:
                self.clear_selection()
        else:
            self._start_pan(pos)

    def _start_pan(self, pos: QPoint) -> None:
        self._drag_mode = "pan"
        self._pan_origin = (self.horizontalScrollBar().value() + pos.x(),
                            self.verticalScrollBar().value() + pos.y())
        self.viewport().setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position().toPoint()
        if self._drag_mode == "pan":
            self.horizontalScrollBar().setValue(self._pan_origin[0] - pos.x())
            self.verticalScrollBar().setValue(self._pan_origin[1] - pos.y())
            return
        if self._drag_mode == "tool":
            hit = self.widget_to_page(pos, clamp_page=self._tool_page)
            drag_hook = self._hook("on_drag")
            if hit is not None and drag_hook is not None:
                drag_hook(hit[0], hit[1])
            vh = self.viewport().height()
            if pos.y() > vh - 16:
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() + 16)
            elif pos.y() < 16:
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - 16)
            self.viewport().update()
            return
        if self._drag_mode == "select" and self._sel_start is not None:
            if self._press_pos is not None and (pos - self._press_pos).manhattanLength() < 4:
                return
            hit = self.widget_to_page(pos, clamp_page=self._sel_page)
            if hit is not None:
                self._update_selection(self._sel_start, hit[1])
            # autoscroll near edges
            vh = self.viewport().height()
            if pos.y() > vh - 20:
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() + 20)
            elif pos.y() < 20:
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - 20)
            return
        if self.doc is None:
            return
        if self._note_at(pos) is not None:
            note = self._note_at(pos)
            self.viewport().setCursor(Qt.PointingHandCursor)
            QToolTip.showText(QCursor.pos(), note.get("text") or "", self)
            return
        hit = self.widget_to_page(pos)
        if hit is not None:
            page, pt = hit
            hover = self._hook("on_hover") if self.tool is not None else self._hook("field_hover")
            if hover is not None:
                cursor = hover(page, pt)
                if cursor is not None:
                    self.viewport().setCursor(cursor)
                    return
            if self.tool is not None:
                self.viewport().setCursor(Qt.CrossCursor)
                return
            if page in self._pages and self._link_at(page, pt):
                self.viewport().setCursor(Qt.PointingHandCursor)
                ln = self._link_at(page, pt)
                if ln.get("uri"):
                    QToolTip.showText(QCursor.pos(), ln["uri"], self)
                return
            pd = self._pages.get(page)
            if pd is not None and pd._words:
                self.viewport().setCursor(Qt.IBeamCursor)
                return
        self.viewport().setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, ev) -> None:
        pos = ev.position().toPoint()
        moved = self._press_pos is not None and (pos - self._press_pos).manhattanLength() >= 4
        if self._drag_mode == "pan":
            self.viewport().setCursor(Qt.ArrowCursor)
        if self._drag_mode == "tool":
            self._drag_mode = None
            hit = self.widget_to_page(pos, clamp_page=self._tool_page)
            release_hook = self._hook("on_release")
            if hit is not None and release_hook is not None:
                release_hook(hit[0], hit[1], moved)
            self.viewport().update()
            return
        if (ev.button() == Qt.LeftButton and not moved and self._press_link is not None
                and self.tool is None):
            link = self._press_link
            self._press_link = None
            self._drag_mode = None
            self.linkActivated.emit(link)
            return
        if self._drag_mode == "select" and self._sel_rects:
            self.selectionChanged.emit(self.selected_text)
            sel_hook = self._hook("on_text_selected")
            if sel_hook is not None and self.tool is not None:
                sel_hook(self._sel_page, list(self._sel_rects))
        self._drag_mode = None

    def mouseDoubleClickEvent(self, ev) -> None:
        hit = self.widget_to_page(ev.position().toPoint())
        if hit is None:
            return
        page, pt = hit
        dbl_hook = self._hook("on_double")
        if self.tool is not None and dbl_hook is not None and dbl_hook(page, pt):
            return
        for r, text, *_ in self._page(page).words():
            if r.contains(pt):
                self._sel_page = page
                self._sel_rects = [r]
                self.selected_text = text
                self.viewport().update()
                self.selectionChanged.emit(text)
                return

    def contextMenuEvent(self, ev) -> None:
        hit = self.widget_to_page(ev.pos())
        page, pt = hit if hit else (self.current_page, pymupdf.Point(20, 20))
        self.contextRequested.emit(page, QPointF(pt.x, pt.y), ev.globalPos())

    # -------------------------------------------------------------- selection
    def _nearest_word(self, words, pt: pymupdf.Point) -> int:
        best, bestd = 0, None
        for i, (r, *_rest) in enumerate(words):
            if r.contains(pt):
                return i
            dx = max(r.x0 - pt.x, 0, pt.x - r.x1)
            dy = max(r.y0 - pt.y, 0, pt.y - r.y1)
            d = dy * 4 + dx
            if bestd is None or d < bestd:
                best, bestd = i, d
        return best

    def _update_selection(self, a: pymupdf.Point, b: pymupdf.Point) -> None:
        words = self._page(self._sel_page).words()
        if not words:
            return
        i0 = self._nearest_word(words, a)
        i1 = self._nearest_word(words, b)
        if i0 > i1:
            i0, i1 = i1, i0
        rects: dict[tuple, pymupdf.Rect] = {}
        parts = []
        prev_line = None
        for r, text, block, line in words[i0:i1 + 1]:
            key = (block, line)
            if key in rects:
                rects[key] |= r
            else:
                rects[key] = pymupdf.Rect(r)
            if prev_line is not None and key != prev_line:
                parts.append("\n")
            elif parts:
                parts.append(" ")
            parts.append(text)
            prev_line = key
        self._sel_rects = list(rects.values())
        self.selected_text = "".join(parts)
        self.viewport().update()

    def clear_selection(self) -> None:
        self._sel_rects = []
        self.selected_text = ""
        self.viewport().update()
        self.selectionChanged.emit("")

    def sizeHint(self) -> QSize:
        return QSize(900, 700)
