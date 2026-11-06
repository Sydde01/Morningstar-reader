"""Document editing helpers (no Qt): saving with .bak backups, comic export, text/fonts."""
from __future__ import annotations

import os
import zipfile

import pymupdf

from .convert import page_image

# Windows TrueType fonts give full Unicode (€, œ…) for inserted / replaced text.
_FONT_DIR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")
FONT_FILES = {
    ("sans", False, False): "arial.ttf", ("sans", True, False): "arialbd.ttf",
    ("sans", False, True): "ariali.ttf", ("sans", True, True): "arialbi.ttf",
    ("serif", False, False): "times.ttf", ("serif", True, False): "timesbd.ttf",
    ("serif", False, True): "timesi.ttf", ("serif", True, True): "timesbi.ttf",
    ("mono", False, False): "cour.ttf", ("mono", True, False): "courbd.ttf",
    ("mono", False, True): "couri.ttf", ("mono", True, True): "courbi.ttf",
}
BASE14 = {
    ("sans", False, False): "helv", ("sans", True, False): "hebo", ("sans", False, True): "heit",
    ("sans", True, True): "hebi", ("serif", False, False): "tiro", ("serif", True, False): "tibo",
    ("serif", False, True): "tiit", ("serif", True, True): "tibi", ("mono", False, False): "cour",
    ("mono", True, False): "cobo", ("mono", False, True): "coit", ("mono", True, True): "cobi",
}


def font_for(family: str, bold: bool, italic: bool) -> tuple[str, str | None]:
    """(fontname, fontfile) usable by Page.insert_text."""
    key = (family, bold, italic)
    path = os.path.join(_FONT_DIR, FONT_FILES.get(key, "arial.ttf"))
    if os.path.exists(path):
        return f"msr-{family}{'-b' if bold else ''}{'-i' if italic else ''}", path
    return BASE14.get(key, "helv"), None


def span_style(span: dict) -> tuple[str, bool, bool]:
    flags = span.get("flags", 0)
    font = (span.get("font") or "").lower()
    family = "mono" if flags & 8 or "cour" in font or "mono" in font else \
        "serif" if flags & 4 or "times" in font or "serif" in font and "sans" not in font else "sans"
    bold = bool(flags & 16) or "bold" in font or "black" in font
    italic = bool(flags & 2) or "italic" in font or "oblique" in font
    return family, bold, italic


def int_to_rgb(c: int) -> tuple[float, float, float]:
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


# --------------------------------------------------------------------- saving
def write_pdf(doc: pymupdf.Document, dst: str, *, compress: bool = False, subset: bool = False,
              password: str | None = None, remove_password: bool = False) -> None:
    """Write `doc` to `dst` (atomically through a temporary file)."""
    if subset:
        try:
            doc.subset_fonts()
        except Exception:  # noqa: BLE001
            pass
    opts = dict(garbage=3, deflate=True)
    if compress:
        opts.update(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True, use_objstms=1)
    if password:
        opts.update(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw=password, user_pw=password,
                    permissions=-1)
    elif remove_password:
        opts.update(encryption=pymupdf.PDF_ENCRYPT_NONE)
    tmp = dst + ".saving"
    doc.save(tmp, **opts)
    os.replace(tmp, dst)


def write_cbz(doc: pymupdf.Document, dst: str, progress=None) -> None:
    """Export a (working) PDF of a comic back to CBZ, keeping original images when untouched."""
    tmp = dst + ".saving"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for i in range(doc.page_count):
            if progress:
                progress(i, doc.page_count)
            data, ext = page_image(doc, i, dpi=_native_dpi(doc, i), quality=92)
            z.writestr(f"{i + 1:04d}.{ext}", data)
    os.replace(tmp, dst)


def _native_dpi(doc: pymupdf.Document, pno: int) -> int:
    try:
        infos = doc.load_page(pno).get_image_info()
        best = 0
        for info in infos:
            bw = pymupdf.Rect(info["bbox"]).width
            if bw > 0:
                best = max(best, 72 * info["width"] / bw)
        if best:
            return int(max(72, min(400, best)))
    except Exception:  # noqa: BLE001
        pass
    return 200


def backup_path(path: str) -> str:
    return path + ".bak"


def replace_with_backup(new_file: str, original: str, target: str | None = None) -> str | None:
    """Move `original` to `<original>.bak` (only if no backup exists yet, so the first original is
    always kept), then move `new_file` to `target` (default: `original`). Returns the .bak path
    when one was created."""
    target = target or original
    bak = backup_path(original)
    created = None
    if os.path.exists(original):
        if not os.path.exists(bak):
            os.replace(original, bak)
            created = bak
        else:
            os.remove(original)
    os.replace(new_file, target)
    return created


def restore_backup(bak: str, original: str) -> None:
    if os.path.exists(original):
        os.remove(original)
    os.replace(bak, original)


def insert_words(page: pymupdf.Page, origin, text: str, fontsize: float, color, *,
                 fontfile: str | None = None, fontbuffer: bytes | None = None) -> None:
    """Write `text` at `origin` word by word with an embedded font.

    Placing words individually (instead of writing space glyphs) keeps text extraction and search
    correct: TrueType fonts often map their space glyph to U+00A0, which breaks searching."""
    if fontbuffer is not None:
        font = pymupdf.Font(fontbuffer=fontbuffer)
    elif fontfile:
        font = pymupdf.Font(fontfile=fontfile)
    else:
        font = pymupdf.Font("helv")
    tw = pymupdf.TextWriter(page.rect, color=color)
    x, y = origin.x, origin.y
    space = font.text_length(" ", fontsize=fontsize) or fontsize * 0.25
    for i, word in enumerate(text.split(" ")):
        if word:
            tw.append((x, y), word, font=font, fontsize=fontsize)
            x += font.text_length(word, fontsize=fontsize)
        if i < len(text.split(" ")) - 1:
            x += space
    tw.write_text(page)


# ------------------------------------------------------------------- XFA forms
def has_xfa(doc: pymupdf.Document) -> bool:
    if not doc.is_pdf:
        return False
    try:
        kind, _val = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")
        return kind not in ("null", "")
    except Exception:  # noqa: BLE001
        return False


def is_dynamic_xfa(doc: pymupdf.Document) -> bool:
    """Pure XFA form (no regular form fields): only Adobe Reader can display it."""
    return has_xfa(doc) and not doc.is_form_pdf


def remove_xfa(doc: pymupdf.Document) -> None:
    """Hybrid forms: drop the XFA copy so every reader (Adobe included) shows the filled-in fields."""
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm/XFA", "null")
    try:
        doc.xref_set_key(doc.pdf_catalog(), "NeedsRendering", "null")
    except Exception:  # noqa: BLE001
        pass
