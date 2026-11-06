"""Installed Windows fonts (family → style files) and fonts embedded in a PDF page."""
from __future__ import annotations

import os
import re
from functools import lru_cache

import pymupdf

_STYLE_WORDS = ("bold", "italic", "oblique", "regular")
ALIASES = {"helvetica": "Arial", "arialmt": "Arial", "times": "Times New Roman", "timesroman": "Times New Roman",
           "courier": "Courier New", "symbol": "Symbol", "zapfdingbats": "Wingdings"}


def _font_dirs() -> list[str]:
    dirs = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    return dirs


def _registry_fonts() -> list[tuple[str, str]]:
    """(display name, file path) from the Windows registry (machine + user fonts)."""
    out = []
    try:
        import winreg
    except ImportError:
        return out
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_path) as k:
                i = 0
                while True:
                    try:
                        name, value, _t = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if not isinstance(value, str):
                        continue
                    path = value if os.path.isabs(value) else os.path.join(_font_dirs()[0], value)
                    out.append((name, path))
        except OSError:
            continue
    return out


def _parse(name: str) -> tuple[str, bool, bool]:
    name = re.sub(r"\s*\((TrueType|OpenType|All res)\)\s*$", "", name, flags=re.I).strip()
    name = name.split(" & ")[0]
    low = name.lower()
    bold = bool(re.search(r"\bbold\b", low))
    italic = bool(re.search(r"\b(italic|oblique)\b", low))
    words = name.split()
    while len(words) > 1 and words[-1].lower() in _STYLE_WORDS:
        words.pop()
    return " ".join(words), bold, italic


@lru_cache(maxsize=1)
def installed_families() -> dict[str, dict[tuple[bool, bool], str]]:
    """{family: {(bold, italic): path}} for TrueType / OpenType fonts that MuPDF can embed."""
    fams: dict[str, dict[tuple[bool, bool], str]] = {}
    for name, path in _registry_fonts():
        if not path.lower().endswith((".ttf", ".otf", ".ttc")) or not os.path.exists(path):
            continue
        family, bold, italic = _parse(name)
        styles = fams.setdefault(family, {})
        styles.setdefault((bold, italic), path)
    return dict(sorted(((f, s) for f, s in fams.items() if (False, False) in s or s),
                       key=lambda kv: kv[0].casefold()))


def font_file(family: str, bold: bool, italic: bool) -> str | None:
    styles = installed_families().get(family)
    if not styles:
        return None
    for key in ((bold, italic), (bold, False), (False, italic), (False, False)):
        if key in styles:
            return styles[key]
    return next(iter(styles.values()))


def clean_font_name(name: str) -> str:
    """'ABCDEF+Calibri-BoldItalic' → 'Calibri'."""
    name = name.split("+", 1)[-1]
    name = re.split(r"[-,]", name)[0]
    name = re.sub(r"(MT|PS|Std|Pro)$", "", name)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).strip()


def match_family(pdf_font: str) -> str | None:
    """Best installed family for a PDF font name (Calibri-Bold → Calibri, TimesNewRomanPSMT → Times New Roman)."""
    target = clean_font_name(pdf_font).replace(" ", "").casefold()
    if not target:
        return None
    fams = installed_families()
    alias = ALIASES.get(target)
    if alias in fams:
        return alias
    for fam in fams:
        if fam.replace(" ", "").casefold() == target:
            return fam
    for fam in fams:
        f = fam.replace(" ", "").casefold()
        if f.startswith(target) or target.startswith(f):
            return fam
    return None


def embedded_font(page: pymupdf.Page, span_font: str) -> tuple[str, bytes] | None:
    """The page's own embedded font used by a text span, if it can be reused: (ext, buffer)."""
    doc = page.parent
    want = span_font.split("+", 1)[-1]
    for xref, ext, ftype, basefont, *_rest in page.get_fonts(full=True):
        if basefont.split("+", 1)[-1] != want or ftype == "Type3" or ext in ("n/a", ""):
            continue
        try:
            _name, ext2, _t, buf = doc.extract_font(xref)
        except Exception:  # noqa: BLE001
            return None
        if buf and ext2 in ("ttf", "otf", "cff", "pfa", "pfb"):
            return ext2, buf
    return None


def covers(buffer: bytes, text: str) -> bool:
    """True when the font has a glyph for every character of `text`."""
    try:
        f = pymupdf.Font(fontbuffer=buffer)
    except Exception:  # noqa: BLE001
        return False
    return all(c.isspace() or f.has_glyph(ord(c)) for c in text)


def file_covers(path: str, text: str) -> bool:
    try:
        f = pymupdf.Font(fontfile=path)
    except Exception:  # noqa: BLE001
        return False
    return all(c.isspace() or f.has_glyph(ord(c)) for c in text)
