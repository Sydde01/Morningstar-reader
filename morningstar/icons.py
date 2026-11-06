"""Inline SVG icon set (stroke style, 24x24) rendered to QIcons."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .config import CACHE_DIR

_P = {
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "open": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v1"/><path d="M3 7v11a2 2 0 0 0 2 2h12.5a2 2 0 0 0 1.9-1.4L22 12H7.5a2 2 0 0 0-1.9 1.4L3 20"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.6 4.5L20 16"/><path d="M20 20v-4h-4"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "prev": '<path d="m15 18-6-6 6-6"/>',
    "next": '<path d="m9 18 6-6-6-6"/>',
    "up": '<path d="m18 15-6-6-6 6"/>',
    "down": '<path d="m6 9 6 6 6-6"/>',
    "first": '<path d="m17 18-6-6 6-6M7 6v12"/>',
    "last": '<path d="m7 18 6-6-6-6M17 6v12"/>',
    "zoom_in": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M11 8v6M8 11h6"/>',
    "zoom_out": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M8 11h6"/>',
    "fit_width": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 12h10M7 12l2.5-2.5M7 12l2.5 2.5M17 12l-2.5-2.5M17 12l-2.5 2.5"/>',
    "fit_page": '<rect x="6" y="3" width="12" height="18" rx="1.5"/><path d="M9 7h6M9 11h6M9 15h4"/>',
    "continuous": '<rect x="6" y="2" width="12" height="8" rx="1"/><rect x="6" y="14" width="12" height="8" rx="1"/>',
    "single": '<rect x="5" y="3" width="14" height="18" rx="1.5"/>',
    "double": '<rect x="2" y="4" width="9.5" height="16" rx="1"/><rect x="12.5" y="4" width="9.5" height="16" rx="1"/>',
    "rotate": '<path d="M20 12a8 8 0 1 1-3-6.3"/><path d="M20 4v5h-5"/>',
    "moon": '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    "bookmark": '<path d="M6 3h12v18l-6-4-6 4z"/>',
    "bookmark_add": '<path d="M6 3h12v18l-6-4-6 4z"/><path d="M12 7v6M9 10h6"/>',
    "note": '<path d="M4 4h16v11l-5 5H4z"/><path d="M15 20v-5h5"/><path d="M8 9h8M8 13h5"/>',
    "toc": '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    "sidebar": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    "fullscreen": '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "star": '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z"/>',
    "star_filled": '<path fill="currentColor" d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "library": '<path d="M4 4h4v16H4zM10 4h4v16h-4z"/><path d="m16 5 3.8-1 3 15.5-3.8 1z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "parent": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "rtl": '<path d="M20 12H5M10 6l-6 6 6 6"/>',
    "font_up": '<path d="M3 19 8.5 5 14 19M5 14h7"/><path d="M18 8v6M15 11h6"/>',
    "font_down": '<path d="M3 19 8.5 5 14 19M5 14h7"/><path d="M15 11h6"/>',
    "check": '<path d="m5 12 5 5 9-10"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m14 6 4 4"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>',
    "explorer": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "home": '<path d="M3 11 12 4l9 7"/><path d="M5 10v10h14V10"/>',
    "goto": '<path d="M4 12h12M12 6l6 6-6 6"/><path d="M20 4v16"/>',
    "flatten": '<path d="M4 6h16M4 12h16M4 18h10"/>',
    "tiles": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    # editing
    "pointer": '<path d="M5 3l14 8-6.5 1.5L10 19z"/>',
    "text": '<path d="M5 6V4h14v2M12 4v16M9 20h6"/>',
    "edit_text": '<path d="M4 6V4h10v2M9 4v14M6.5 18h5"/><path d="M14 20h6M17 11v9"/>',
    "highlight": '<path d="m9 11-5 5v4h4l5-5"/><path d="m9 11 6-6 4 4-6 6z"/><path d="M14 20h7"/>',
    "underline": '<path d="M7 4v6a5 5 0 0 0 10 0V4"/><path d="M5 20h14"/>',
    "strike": '<path d="M16 6.5C15 5 13.6 4 12 4c-2.5 0-4.5 1.4-4.5 3.5 0 4.5 9 3 9 8 0 2.2-2 3.5-4.5 3.5-1.8 0-3.4-.9-4.3-2.4"/><path d="M4 12h16"/>',
    "pen": '<path d="M3 21c3 0 4-1.5 5.5-3L19 7.5a2.1 2.1 0 0 0-3-3L5.5 15C4 16.5 3 18 3 21z"/>',
    "square": '<rect x="4" y="5" width="16" height="14" rx="1.5"/>',
    "circle": '<ellipse cx="12" cy="12" rx="9" ry="7.5"/>',
    "arrow": '<path d="M5 19 19 5M10 5h9v9"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.8"/><path d="m21 16-5-5-9 9"/>',
    "signature": '<path d="M3 17c2-6 4-10 5.5-10S8 15 10 15s2.5-4 4-4 1 3 2.5 3S19 12 21 12"/><path d="M3 21h18"/>',
    "eraser": '<path d="m7 21-4-4L15 5l6 6-10 10z"/><path d="M7 21h14M10 10l6 6"/>',
    "redact": '<rect x="3" y="8" width="18" height="8" rx="1" fill="currentColor"/><path d="M3 4h10M3 20h14"/>',
    "pages": '<rect x="3" y="3" width="8" height="10" rx="1"/><rect x="13" y="3" width="8" height="10" rx="1"/><rect x="3" y="15" width="8" height="6" rx="1"/><rect x="13" y="15" width="8" height="6" rx="1"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
    "redo": '<path d="m15 14 5-5-5-5"/><path d="M20 9H9a5 5 0 0 0 0 10h3"/>',
    "save": '<path d="M5 3h11l4 4v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 1-2z"/><path d="M8 3v5h7V3M8 21v-7h8v7"/>',
    "save_as": '<path d="M5 3h11l4 4v5"/><path d="M12 21H6a2 2 0 0 1-2-2V5a2 2 0 0 1 1-2"/><path d="M8 3v5h7V3"/><path d="M15 21l1-3 4-4 2 2-4 4z"/>',
    "scissors": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8.1 8.1 20 20M8.1 15.9 20 4M13 12h0"/>',
    "convert": '<path d="M4 8h13l-3-3M20 16H7l3 3"/>',
    "merge": '<path d="M7 4v4a4 4 0 0 0 4 4h2a4 4 0 0 1 4 4v4M17 4v4a4 4 0 0 1-2 3.5"/><path d="m4 7 3-3 3 3"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "compress": '<path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/>',
    "tag": '<path d="M3 12V3h9l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    "tools": '<path d="M14.5 6.5a4 4 0 0 0-5.3 5.3L3 18l3 3 6.2-6.2a4 4 0 0 0 5.3-5.3l-2.6 2.6-2.4-.6-.6-2.4z"/>',
    "backup": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
    "form": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h4M7 14h10M14 9h3"/>',
}

_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
    'color="{color}">{body}</svg>'
)


def svg(name: str, color: str = "#e6e6e6") -> str:
    return _TEMPLATE.format(color=color, body=_P[name].replace("currentColor", color))


def render(name: str, size: int, color: str = "#e6e6e6", dpr: float = 1.0) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg(name, color).encode()))
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p, QRectF(0, 0, pm.width(), pm.height()))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


@lru_cache(maxsize=None)
def icon(name: str, color: str = "#e6e6e6") -> QIcon:
    ic = QIcon()
    for s in (16, 20, 24, 32, 48):
        ic.addPixmap(render(name, s, color))
    ic.addPixmap(render(name, 48, "#666666"), QIcon.Disabled)
    return ic


def write_ui_svgs() -> str:
    """Write a few SVGs used by the stylesheet; returns the folder (forward slashes)."""
    folder = CACHE_DIR / "ui"
    folder.mkdir(exist_ok=True)
    (folder / "close.svg").write_text(svg("close", "#9a9aa3"), encoding="utf-8")
    (folder / "check.svg").write_text(svg("check", "#ffffff"), encoding="utf-8")
    return folder.as_posix()
