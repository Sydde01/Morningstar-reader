"""Application paths and persistent settings (JSON)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

APP_FOLDER = "MorningstarReader"


def _base_dir(env: str, fallback: str) -> Path:
    root = os.environ.get(env) or os.path.expanduser(fallback)
    p = Path(root) / APP_FOLDER
    p.mkdir(parents=True, exist_ok=True)
    return p


DATA_DIR = _base_dir("APPDATA", "~/.config")
CACHE_DIR = _base_dir("LOCALAPPDATA", "~/.cache")
THUMB_DIR = CACHE_DIR / "thumbs"
CONVERT_DIR = CACHE_DIR / "converted"
for _d in (THUMB_DIR, CONVERT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "library.db"

# Formats MuPDF opens natively + CBR (handled through conversion).
DOC_EXTS = {".pdf", ".epub", ".cbz", ".cbr", ".xps", ".oxps", ".fb2", ".mobi"}
COMIC_EXTS = {".cbz", ".cbr"}
REFLOW_EXTS = {".epub", ".fb2", ".mobi"}

DEFAULTS: dict = {
    "language": "fr",
    "restore_session": True,
    "session_tabs": [],
    "window_geometry": "",
    "window_maximized": True,
    "library": {
        "roots": [],
        "strip_prefixes": True,
        "merge_single_doc_folders": True,
        "scan_on_startup": True,
        "thumb_size": 170,
        "flatten": False,
        "sort": "name",
    },
    "reading": {
        "view_mode": "continuous",  # continuous | single | double
        "fit": "width",  # width | page | none
        "page_gap": 12,
        "epub_font_size": 12,
        "epub_page_width": 480,
        "epub_page_height": 680,
        "comics_view_mode": "single",
        "comics_fit": "page",
        "comics_rtl": False,
        "cover_alone": True,
        "show_sidebar": True,
    },
    "dark_doc": {
        "default_pdf": False,
        "default_reflow": True,
        "default_comics": False,
        "background": "#121212",
        "text": "#e8e6e3",
        "keep_images": True,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    """Tiny dotted-key settings store: settings.get("reading.fit")."""

    def __init__(self, path: Path = SETTINGS_FILE):
        self.path = path
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        self.data = _merge(DEFAULTS, data)

    def get(self, key: str, default=None):
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value) -> None:
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


settings = Settings()
