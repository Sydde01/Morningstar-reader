"""Document opening and page rendering helpers on top of PyMuPDF.

Kept free of Qt imports so it can be used from thumbnail worker processes.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

import pymupdf

from .config import COMIC_EXTS, CONVERT_DIR, REFLOW_EXTS

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".jxr", ".avif"}
CONVERT_CACHE_LIMIT = 4 * 1024 ** 3  # converted RAR comics kept on disk (LRU)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class OpenError(Exception):
    pass


def ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def is_comic(path: str) -> bool:
    return ext_of(path) in COMIC_EXTS


def is_reflow(path: str) -> bool:
    return ext_of(path) in REFLOW_EXTS


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ----------------------------------------------------------------------------- RAR
def find_rar_tool() -> tuple[str, str] | None:
    """Return (kind, exe) with kind in {"7z", "unrar", "tar"}.

    Windows 10 (1803+) and 11 ship bsdtar (libarchive) as System32\\tar.exe, which reads RAR/RAR5,
    so no extra install is normally needed.
    """
    for name, kind in (("7z", "7z"), ("unrar", "unrar")):
        found = shutil.which(name)
        if found:
            return kind, found
    pf = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
    for base in pf:
        if not base:
            continue
        for rel, kind in ((("7-Zip", "7z.exe"), "7z"), (("WinRAR", "UnRAR.exe"), "unrar")):
            cand = os.path.join(base, *rel)
            if os.path.exists(cand):
                return kind, cand
    sys_tar = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
    if os.path.exists(sys_tar):
        return "tar", sys_tar
    found = shutil.which("bsdtar")
    return ("tar", found) if found else None


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, timeout=timeout)


def _extract_all(path: str, dest: str) -> None:
    tool = find_rar_tool()
    if not tool:
        raise OpenError("rar_tool_missing")
    kind, exe = tool
    if kind == "7z":
        cmd = [exe, "x", "-y", "-bso0", "-bsp0", f"-o{dest}", path]
    elif kind == "unrar":
        cmd = [exe, "x", "-y", "-idq", path, dest + os.sep]
    else:
        cmd = [exe, "-xf", path, "-C", dest]
    r = _run(cmd)
    if r.returncode != 0 and not any(files for _r, _d, files in os.walk(dest)):
        raise OpenError("rar_extract_failed: " + r.stderr.decode("utf-8", "replace")[:300])


def _collect_images(folder: str) -> list[tuple[str, str]]:
    images = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if ext_of(f) in IMAGE_EXTS:
                full = os.path.join(root, f)
                images.append((os.path.relpath(full, folder), full))
    images.sort(key=lambda t: _natural_key(t[0]))
    return images


def _prune_convert_cache(keep: str) -> None:
    try:
        files = [(f.stat().st_mtime, f.stat().st_size, f) for f in CONVERT_DIR.glob("*.cbz")]
    except OSError:
        return
    total = sum(s for _m, s, _f in files)
    for _m, size, f in sorted(files):
        if total <= CONVERT_CACHE_LIMIT:
            break
        if str(f) == keep:
            continue
        try:
            f.unlink()
            total -= size
        except OSError:
            pass


def is_rar(path: str) -> bool:
    if ext_of(path) != ".cbr":
        return False
    with open(path, "rb") as fh:
        return not fh.read(4).startswith(b"PK")


def cbr_to_cbz(path: str) -> str:
    """Convert a RAR comic to a cached, uncompressed CBZ. Returns the CBZ path."""
    st = os.stat(path)
    key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}".encode("utf-8", "replace")).hexdigest()
    out = CONVERT_DIR / f"{key}.cbz"
    if out.exists():
        try:
            os.utime(out)
        except OSError:
            pass
        return str(out)
    tmp = tempfile.mkdtemp(prefix="msr_cbr_", dir=CONVERT_DIR)
    try:
        _extract_all(path, tmp)
        images = _collect_images(tmp)
        if not images:
            raise OpenError("empty_archive")
        part = str(out) + ".part"
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as z:
            for i, (rel, full) in enumerate(images):
                z.write(full, f"{i:05d}{ext_of(rel)}")
        os.replace(part, out)
        _prune_convert_cache(str(out))
        return str(out)
    except subprocess.SubprocessError as e:
        raise OpenError(f"rar_extract_failed: {e}") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def rar_cover(path: str) -> tuple[bytes, str, int]:
    """First image of a RAR comic without extracting everything: (data, ext, image_count)."""
    tool = find_rar_tool()
    if not tool:
        raise OpenError("rar_tool_missing")
    kind, exe = tool
    if kind == "7z":
        listing = _run([exe, "l", "-ba", "-slt", path], 120).stdout.decode("utf-8", "replace")
        names = [ln[7:] for ln in listing.splitlines() if ln.startswith("Path = ")]
    elif kind == "unrar":
        names = _run([exe, "lb", path], 120).stdout.decode("utf-8", "replace").splitlines()
    else:
        names = _run([exe, "-tf", path], 120).stdout.decode("utf-8", "replace").splitlines()
    images = sorted((n for n in names if ext_of(n) in IMAGE_EXTS), key=_natural_key)
    if not images:
        raise OpenError("empty_archive")
    first = images[0]
    if kind == "7z":
        data = _run([exe, "e", "-so", path, first], 120).stdout
    elif kind == "unrar":
        data = _run([exe, "p", "-inul", path, first], 120).stdout
    else:
        data = _run([exe, "-xOf", path, first], 120).stdout
    if not data:  # unusual file names: fall back to a full extraction
        tmp = tempfile.mkdtemp(prefix="msr_cov_", dir=CONVERT_DIR)
        try:
            _extract_all(path, tmp)
            imgs = _collect_images(tmp)
            if not imgs:
                raise OpenError("empty_archive")
            with open(imgs[0][1], "rb") as fh:
                data = fh.read()
            first = imgs[0][0]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return data, ext_of(first)[1:], len(images)


# --------------------------------------------------------------------------- open
def open_document(path: str, font_size: float | None = None,
                  page_w: float = 480, page_h: float = 680) -> pymupdf.Document:
    ext = ext_of(path)
    try:
        if ext == ".cbr":
            doc = pymupdf.open(cbr_to_cbz(path) if is_rar(path) else path, filetype="cbz")
        elif ext == ".cbz":
            doc = pymupdf.open(path, filetype="cbz")
        else:
            doc = pymupdf.open(path)
    except OpenError:
        raise
    except Exception as e:  # noqa: BLE001 - MuPDF raises many kinds
        raise OpenError(str(e)) from e
    if doc.needs_pass:
        raise OpenError("password_protected")
    if doc.is_reflowable and font_size:
        doc.layout(width=page_w, height=page_h, fontsize=font_size)
    return doc


def hex_to_int(color: str) -> int:
    return int(color.lstrip("#")[:6], 16)


def apply_dark(pix: pymupdf.Pixmap, bg: int, fg: int, keep: list[pymupdf.IRect] | None = None) -> None:
    """In-place: page background -> bg, text -> fg. `keep` areas are restored untouched."""
    original = pymupdf.Pixmap(pix, 0) if keep else None  # same size, no alpha
    pix.invert_irect()
    pix.tint_with(bg, fg)
    if original is not None:
        for r in keep:
            r = r & pix.irect
            if not r.is_empty:
                pix.copy(original, r)


# ---------------------------------------------------------------------- thumbnails
def render_thumbnail(path: str, out_file: str, width: int = 320) -> int:
    """Worker-process entry: render the cover to a JPEG. Returns the page count."""
    count = None
    if is_rar(path):
        data, ext, count = rar_cover(path)
        doc = pymupdf.open(stream=data, filetype=ext)
    else:
        doc = open_document(path, font_size=11 if is_reflow(path) else None)
    try:
        n = doc.page_count
        if n == 0:
            raise OpenError("empty")
        page = doc.load_page(0)
        r = page.rect
        scale = width / max(r.width, 1)
        scale = min(scale, 2400 / max(r.height, 1))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        tmp = out_file + ".part"
        pix.save(tmp, output="jpeg", jpg_quality=82)
        os.replace(tmp, out_file)
        return count or n
    finally:
        doc.close()


def thumb_worker(path: str, out_file: str, width: int) -> tuple[str, str, int, str]:
    try:
        n = render_thumbnail(path, out_file, width)
        return path, out_file, n, ""
    except Exception as e:  # noqa: BLE001
        return path, out_file, 0, str(e) or e.__class__.__name__
