"""Minimal translation layer: source strings are English, French is provided below."""
from __future__ import annotations

from .config import settings

LANGUAGES = {"fr": "Français", "en": "English"}

FR: dict[str, str] = {}  # filled from translations_fr

try:
    from .translations_fr import FR as _FR

    FR.update(_FR)
except ImportError:  # pragma: no cover
    pass


def tr(text: str, **kw) -> str:
    if settings.get("language") == "fr":
        text = FR.get(text, text)
    return text.format(**kw) if kw else text
