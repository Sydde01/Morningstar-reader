"""Conversion, double-page splitting and merging (no Qt: runs in worker processes).

Every job writes to ``<dst>.part`` and renames at the very end, so a cancelled or failed job never
leaves a half-written file behind.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
import zipfile

import pymupdf

from .docbackend import (IMAGE_EXTS, OpenError, _natural_key, cbr_to_cbz, ext_of, is_rar, open_document)

TARGETS = ("pdf", "epub", "cbz", "cbr")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------------- helpers
def find_rar_writer() -> str | None:
    """RAR archives can only be *created* with WinRAR's command-line tool."""
    found = shutil.which("rar")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        if base:
            cand = os.path.join(base, "WinRAR", "Rar.exe")
            if os.path.exists(cand):
                return cand
    return None


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width/height from the image header (JPEG, PNG, GIF, WEBP, BMP) without decoding."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return w, abs(h)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return w, h
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
        if data[:2] == b"\xff\xd8":
            i = 2
            n = len(data)
            while i < n:
                while i < n and data[i] != 0xFF:
                    i += 1
                while i < n and data[i] == 0xFF:
                    i += 1
                if i >= n:
                    break
                marker = data[i]
                i += 1
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    continue
                seg = struct.unpack(">H", data[i:i + 2])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 3:i + 7])
                    return w, h
                i += seg
    except (struct.error, IndexError):
        pass
    try:
        pix = pymupdf.Pixmap(data)
        return pix.width, pix.height
    except Exception:  # noqa: BLE001
        return None


def comic_zip(path: str) -> str:
    """Path of a ZIP holding the comic's images (RAR comics are converted first)."""
    return cbr_to_cbz(path) if is_rar(path) else path


def comic_entries(zip_path: str) -> list[str]:
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if ext_of(n) in IMAGE_EXTS and not n.endswith("/")
                 and "__macosx" not in n.lower()]
    return sorted(names, key=_natural_key)


def is_comic_path(path: str) -> bool:
    return ext_of(path) in (".cbz", ".cbr")


def _norm_ext(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return {"jpeg": "jpg", "jpx": "jp2"}.get(ext, ext)


def _layout(doc: pymupdf.Document, opts: dict) -> None:
    if doc.is_reflowable:
        doc.layout(width=opts.get("page_w", 420), height=opts.get("page_h", 595),
                   fontsize=opts.get("font_size", 11))


def _open(path: str, opts: dict) -> pymupdf.Document:
    doc = open_document(path)
    if doc.needs_pass:
        raise OpenError("password_protected")
    _layout(doc, opts)
    return doc


def page_image(doc: pymupdf.Document, pno: int, dpi: int = 150, quality: int = 88) -> tuple[bytes, str]:
    """One page as an image. Scanned / comic PDF pages give back their original image losslessly."""
    page = doc.load_page(pno)
    if doc.is_pdf and page.rotation == 0 and not page.first_annot:
        imgs = page.get_images(full=True)
        if len(imgs) == 1 and not page.get_text("text").strip():
            infos = page.get_image_info(xrefs=True)
            pr = page.rect
            if len(infos) == 1:
                bb = pymupdf.Rect(infos[0]["bbox"])
                inside = bb.x0 >= pr.x0 - 2 and bb.y0 >= pr.y0 - 2 and bb.x1 <= pr.x1 + 2 and bb.y1 <= pr.y1 + 2
                if inside and bb.get_area() >= 0.9 * pr.get_area():
                    try:
                        x = doc.extract_image(imgs[0][0])
                        ext = _norm_ext(x.get("ext", ""))
                        if ext in ("jpg", "png") and not x.get("smask"):
                            return x["image"], ext
                    except Exception:  # noqa: BLE001
                        pass
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return pix.tobytes("jpeg", jpg_quality=quality), "jpg"


def iter_page_images(path: str, opts: dict, progress, cancel):
    """Yield (bytes, ext) for every page of any supported document."""
    if is_comic_path(path):
        zp = comic_zip(path)
        names = comic_entries(zp)
        with zipfile.ZipFile(zp) as z:
            for i, n in enumerate(names):
                cancel()
                progress(i, len(names))
                yield z.read(n), _norm_ext(ext_of(n))
        return
    doc = _open(path, opts)
    try:
        n = doc.page_count
        for i in range(n):
            cancel()
            progress(i, n)
            yield page_image(doc, i, opts.get("dpi", 150), opts.get("quality", 88))
    finally:
        doc.close()


def _title(path: str, doc: pymupdf.Document | None = None) -> str:
    if doc is not None:
        t = (doc.metadata or {}).get("title") or ""
        if t.strip():
            return t.strip()
    return os.path.splitext(os.path.basename(path))[0]


# ---------------------------------------------------------------------- → PDF
def to_pdf(src: str, dst: str, opts: dict, progress, cancel) -> None:
    part = dst + ".part"
    if ext_of(src) == ".pdf":
        shutil.copyfile(src, part)
        os.replace(part, dst)
        return
    out = pymupdf.open()
    if is_comic_path(src):
        for data, _ext in iter_page_images(src, opts, progress, cancel):
            size = image_size(data) or (1000, 1500)
            page = out.new_page(width=size[0] * 0.75, height=size[1] * 0.75)
            page.insert_image(page.rect, stream=data)
    else:
        doc = _open(src, opts)
        cancel()
        progress(0, 2)
        # one pass: page ranges are unreliable for reflowable documents in PyMuPDF 2.x
        written = pymupdf.open("pdf", doc.convert_to_pdf())
        cancel()
        progress(1, 2)
        out.insert_pdf(written)
        written.close()
        try:
            toc = doc.get_toc(simple=True)
            if toc:
                out.set_toc(toc)
        except Exception:  # noqa: BLE001
            pass
        out.set_metadata({"title": _title(src, doc), "author": (doc.metadata or {}).get("author", "")})
        doc.close()
    out.save(part, garbage=3, deflate=True)
    out.close()
    os.replace(part, dst)


# ---------------------------------------------------------------------- → CBZ / CBR
def _write_cbz_from_images(images, part: str) -> int:
    count = 0
    with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as z:
        for data, ext in images:
            count += 1
            z.writestr(f"{count:04d}.{ext}", data)
    return count


def to_cbz(src: str, dst: str, opts: dict, progress, cancel) -> None:
    part = dst + ".part"
    if ext_of(src) == ".cbz":
        shutil.copyfile(src, part)
    else:
        _write_cbz_from_images(iter_page_images(src, opts, progress, cancel), part)
    os.replace(part, dst)


def to_cbr(src: str, dst: str, opts: dict, progress, cancel) -> None:
    rar = find_rar_writer()
    if not rar:
        raise OpenError("winrar_missing")
    if ext_of(src) == ".cbr" and is_rar(src):
        shutil.copyfile(src, dst + ".part")
        os.replace(dst + ".part", dst)
        return
    tmp = tempfile.mkdtemp(prefix="msr_rar_")
    try:
        n = 0
        for data, ext in iter_page_images(src, opts, progress, cancel):
            n += 1
            with open(os.path.join(tmp, f"{n:04d}.{ext}"), "wb") as fh:
                fh.write(data)
        part = dst + ".part.rar"
        r = subprocess.run([rar, "a", "-ep1", "-m0", "-idq", "-y", part, os.path.join(tmp, "*")],
                           capture_output=True, creationflags=_NO_WINDOW)
        if r.returncode != 0 or not os.path.exists(part):
            raise OpenError("rar failed: " + r.stderr.decode("utf-8", "replace")[:200])
        os.replace(part, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------- → EPUB
_DATA_IMG = re.compile(r'src="data:image/([a-zA-Z0-9.+-]+);base64,([^"]+)"')


class _Epub:
    """Minimal EPUB 3 writer (valid for Calibre, readers and e-ink devices)."""

    def __init__(self, title: str, author: str, fixed: bool, lang: str = "fr"):
        self.title, self.author, self.fixed, self.lang = title, author, fixed, lang
        self.items: list[tuple[str, str, str, bytes]] = []  # id, href, media-type, data
        self.spine: list[str] = []
        self.nav: list[tuple[str, str]] = []
        self.cover_id: str | None = None
        self.uid = str(uuid.uuid4())

    def add(self, href: str, media: str, data: bytes | str, spine: bool = False, props: str = "") -> str:
        iid = f"i{len(self.items)}"
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.items.append((iid, href, media + ("|" + props if props else ""), data))
        if spine:
            self.spine.append(iid)
        return iid

    def xhtml(self, title: str, body: str, head_extra: str = "") -> str:
        return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
                f'xml:lang="{self.lang}"><head><meta charset="utf-8"/><title>{html.escape(title)}</title>'
                f'{head_extra}</head><body>{body}</body></html>')

    def write(self, path: str) -> None:
        nav_items = "".join(f'<li><a href="{h}">{html.escape(t)}</a></li>' for t, h in self.nav) \
            or f'<li><a href="{self.items[0][1]}">{html.escape(self.title)}</a></li>'
        nav = self.xhtml("Contents", f'<nav epub:type="toc" id="toc"><h1>{html.escape(self.title)}</h1>'
                                     f'<ol>{nav_items}</ol></nav>')
        manifest = []
        for iid, href, media, _d in self.items:
            m, _, props = media.partition("|")
            p = f' properties="{props}"' if props else ""
            manifest.append(f'<item id="{iid}" href="{href}" media-type="{m}"{p}/>')
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        spine_props = ' page-progression-direction="ltr"'
        fixed_meta = ('<meta property="rendition:layout">pre-paginated</meta>'
                      '<meta property="rendition:spread">landscape</meta>') if self.fixed else ""
        cover_meta = f'<meta name="cover" content="{self.cover_id}"/>' if self.cover_id else ""
        opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" '
               'prefix="rendition: http://www.idpf.org/vocab/rendition/#">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:identifier id="uid">urn:uuid:{self.uid}</dc:identifier>'
               f'<dc:title>{html.escape(self.title)}</dc:title>'
               f'<dc:creator>{html.escape(self.author or "Unknown")}</dc:creator>'
               f'<dc:language>{self.lang}</dc:language>'
               '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>'
               f'{fixed_meta}{cover_meta}</metadata>'
               f'<manifest>{"".join(manifest)}</manifest>'
               f'<spine{spine_props}>{"".join(f"<itemref idref={chr(34)}{s}{chr(34)}/>" for s in self.spine)}</spine>'
               '</package>')
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0"?><container version="1.0" '
                       'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                       '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                       '</rootfiles></container>')
            z.writestr("OEBPS/content.opf", opf)
            z.writestr("OEBPS/nav.xhtml", nav)
            for _iid, href, media, data in self.items:
                z.writestr(f"OEBPS/{href}", data,
                           compress_type=zipfile.ZIP_STORED if media.startswith("image/") else zipfile.ZIP_DEFLATED)


_MEDIA = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}


def _epub_images(src: str, dst: str, opts: dict, progress, cancel) -> None:
    title = _title(src)
    ep = _Epub(title, opts.get("author", ""), fixed=True, lang=opts.get("lang", "fr"))
    for i, (data, ext) in enumerate(iter_page_images(src, opts, progress, cancel)):
        if ext not in _MEDIA:
            pix = pymupdf.Pixmap(data)
            data, ext = pix.tobytes("jpeg", jpg_quality=90), "jpg"
        w, h = image_size(data) or (1000, 1500)
        img_href = f"images/p{i + 1:04d}.{ext}"
        iid = ep.add(img_href, _MEDIA[ext], data, props="cover-image" if i == 0 else "")
        if i == 0:
            ep.cover_id = iid
        head = (f'<meta name="viewport" content="width={w}, height={h}"/>'
                '<style>html,body{margin:0;padding:0}img{width:100%;height:100%;display:block}</style>')
        page = ep.xhtml(f"{title} {i + 1}", f'<img src="{img_href}" alt=""/>', head)
        href = f"p{i + 1:04d}.xhtml"
        ep.add(href, "application/xhtml+xml", page, spine=True)
        if i == 0:
            ep.nav.append((title, href))
    part = dst + ".part"
    ep.write(part)
    os.replace(part, dst)


def _epub_text(src: str, dst: str, opts: dict, progress, cancel) -> None:
    doc = _open(src, opts)
    title = _title(src, doc)
    ep = _Epub(title, (doc.metadata or {}).get("author", "") or opts.get("author", ""), fixed=False,
               lang=opts.get("lang", "fr"))
    css = ("body{font-family:serif;line-height:1.45;margin:0 4%}p{margin:0 0 .6em;text-align:justify}"
           "img{max-width:100%;height:auto;display:block;margin:.8em auto}h1,h2,h3{text-align:center}")
    ep.add("style.css", "text/css", css)
    n = doc.page_count
    toc = [t for t in doc.get_toc(simple=True) if t[0] == 1 and 1 <= t[2] <= n]
    starts = sorted({0} | {t[2] - 1 for t in toc})
    if len(starts) == 1:
        starts = list(range(0, n, 20))
    bounds = list(zip(starts, starts[1:] + [n]))
    page_file: dict[int, str] = {}
    img_no = 0
    cover_done = False
    for ci, (a, b) in enumerate(bounds):
        parts = []
        for pno in range(a, b):
            cancel()
            progress(pno, n)
            page_file[pno] = f"c{ci + 1:03d}.xhtml"
            frag = doc.load_page(pno).get_text("xhtml")
            frag = re.sub(r'^<div id="page\d+">|</div>\s*$', "", frag.strip())

            def repl(m):
                nonlocal img_no, cover_done
                ext = _norm_ext(m.group(1))
                data = base64.b64decode(m.group(2))
                if ext not in _MEDIA:
                    data, ext = pymupdf.Pixmap(data).tobytes("jpeg", jpg_quality=88), "jpg"
                img_no += 1
                href = f"images/img{img_no:04d}.{ext}"
                iid = ep.add(href, _MEDIA[ext], data, props="" if cover_done else "cover-image")
                if not cover_done:
                    ep.cover_id, cover_done = iid, True
                return f'src="{href}"'

            parts.append(_DATA_IMG.sub(repl, frag))
        body = "\n".join(parts)
        href = f"c{ci + 1:03d}.xhtml"
        ep.add(href, "application/xhtml+xml",
               ep.xhtml(title, body, '<link rel="stylesheet" type="text/css" href="style.css"/>'), spine=True)
    if toc:
        ep.nav = [(t[1], page_file.get(t[2] - 1, "c001.xhtml")) for t in toc]
    else:
        ep.nav = [(f"{title} — {i + 1}", f"c{i + 1:03d}.xhtml") for i in range(len(bounds))]
    doc.close()
    part = dst + ".part"
    ep.write(part)
    os.replace(part, dst)


def to_epub(src: str, dst: str, opts: dict, progress, cancel) -> None:
    mode = opts.get("epub_mode", "auto")
    if mode == "auto":
        mode = "images" if is_comic_path(src) or _looks_scanned(src, opts) else "text"
    if ext_of(src) == ".epub" and mode == "text":
        shutil.copyfile(src, dst + ".part")
        os.replace(dst + ".part", dst)
        return
    (_epub_images if mode == "images" else _epub_text)(src, dst, opts, progress, cancel)


def _looks_scanned(src: str, opts: dict) -> bool:
    if ext_of(src) != ".pdf":
        return False
    doc = _open(src, opts)
    try:
        sample = range(0, doc.page_count, max(1, doc.page_count // 10))
        empty = sum(1 for i in sample if not doc.load_page(i).get_text("text").strip())
        return empty >= 0.7 * len(sample)
    finally:
        doc.close()


CONVERTERS = {"pdf": to_pdf, "cbz": to_cbz, "cbr": to_cbr, "epub": to_epub}


def convert(src: str, target: str, out_dir: str | None, opts: dict, progress, cancel) -> str:
    base = os.path.splitext(os.path.basename(src))[0]
    dst = unique_path(os.path.join(out_dir or os.path.dirname(src), f"{base}.{target}"))
    try:
        CONVERTERS[target](src, dst, opts, progress, cancel)
    except BaseException:
        for p in (dst + ".part", dst + ".part.rar"):
            if os.path.exists(p):
                os.remove(p)
        raise
    return dst


# ------------------------------------------------------------ double pages
def page_sizes(path: str) -> list[tuple[float, float]]:
    """Width/height of every page (cheap: image headers for comics)."""
    if is_comic_path(path):
        zp = comic_zip(path)
        out = []
        with zipfile.ZipFile(zp) as z:
            for n in comic_entries(zp):
                with z.open(n) as fh:
                    head = fh.read(256 * 1024)
                out.append(image_size(head) or image_size(z.read(n)) or (1, 1))
        return out
    doc = _open(path, {})
    try:
        return [(doc.load_page(i).rect.width, doc.load_page(i).rect.height) for i in range(doc.page_count)]
    finally:
        doc.close()


def detect_doubles(sizes: list[tuple[float, float]], ratio: float = 1.15, skip_first: bool = True) -> list[int]:
    return [i for i, (w, h) in enumerate(sizes) if w > h * ratio and not (skip_first and i == 0)]


def split_output_path(src: str) -> str:
    base, ext = os.path.splitext(src)
    ext = ".pdf" if ext.lower() == ".pdf" else ".cbz"
    return unique_path(f"{base} (single pages){ext}")


def _halves(w: int, h: int, rtl: bool) -> list[tuple[int, int, int, int]]:
    mid = w // 2
    left, right = (0, 0, mid, h), (mid, 0, w, h)
    return [right, left] if rtl else [left, right]


def split_doubles(src: str, dst: str, pages: list[int] | None, rtl: bool, progress, cancel,
                  quality: int = 95) -> str:
    part = dst + ".part"
    try:
        if ext_of(src) == ".pdf":
            doc = pymupdf.open(src)
            pages_set = set(detect_doubles(page_sizes(src)) if pages is None else pages)
            out = pymupdf.open()
            mapping = {}
            for i in range(doc.page_count):
                cancel()
                progress(i, doc.page_count)
                mapping[i] = out.page_count
                if i in pages_set:
                    r = doc.load_page(i).rect
                    mid = r.width / 2
                    halves = [pymupdf.Rect(0, 0, mid, r.height), pymupdf.Rect(mid, 0, r.width, r.height)]
                    if rtl:
                        halves.reverse()
                    for clip in halves:
                        p = out.new_page(width=clip.width, height=clip.height)
                        p.show_pdf_page(p.rect, doc, i, clip=clip)
                else:
                    out.insert_pdf(doc, from_page=i, to_page=i)
            toc = [[lvl, t, mapping.get(pg - 1, 0) + 1] for lvl, t, pg in doc.get_toc(simple=True) if pg >= 1]
            if toc:
                out.set_toc(toc)
            out.save(part, garbage=3, deflate=True)
            out.close()
            doc.close()
        else:
            zp = comic_zip(src)
            names = comic_entries(zp)
            with zipfile.ZipFile(zp) as z:
                if pages is None:
                    sizes = [image_size(z.read(n)) or (1, 1) for n in names]
                    pages = detect_doubles(sizes)
                pages_set = set(pages)

                def gen():
                    for i, n in enumerate(names):
                        cancel()
                        progress(i, len(names))
                        data = z.read(n)
                        ext = _norm_ext(ext_of(n))
                        if i not in pages_set:
                            yield data, ext
                            continue
                        pix = pymupdf.Pixmap(data)
                        if pix.alpha:
                            pix = pymupdf.Pixmap(pix, 0)
                        if pix.colorspace is None or pix.colorspace.n not in (1, 3):
                            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                        for x0, y0, x1, y1 in _halves(pix.width, pix.height, rtl):
                            ir = pymupdf.IRect(x0, y0, x1, y1)
                            half = pymupdf.Pixmap(pix.colorspace, ir, False)
                            half.copy(pix, ir)
                            half.set_origin(0, 0)
                            if ext == "png":
                                yield half.tobytes("png"), "png"
                            else:
                                yield half.tobytes("jpeg", jpg_quality=quality), "jpg"

                _write_cbz_from_images(gen(), part)
        os.replace(part, dst)
        return dst
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise


# ------------------------------------------------------------------- merge
def merge(files: list[str], dst: str, opts: dict, progress, cancel) -> str:
    part = dst + ".part"
    try:
        if ext_of(dst) == ".pdf":
            out = pymupdf.open()
            toc = []
            for k, f in enumerate(files):
                cancel()
                progress(k, len(files))
                start = out.page_count
                if ext_of(f) == ".pdf":
                    src = pymupdf.open(f)
                    out.insert_pdf(src)
                    src.close()
                else:
                    tmp = os.path.join(tempfile.gettempdir(), f"msr_merge_{uuid.uuid4().hex}.pdf")
                    try:
                        to_pdf(f, tmp, opts, lambda *_: None, cancel)
                        src = pymupdf.open(tmp)
                        out.insert_pdf(src)
                        src.close()
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                toc.append([1, os.path.splitext(os.path.basename(f))[0], start + 1])
            out.set_toc(toc)
            out.save(part, garbage=3, deflate=True)
            out.close()
        else:
            def gen():
                for k, f in enumerate(files):
                    progress(k, len(files))
                    yield from iter_page_images(f, opts, lambda *_: None, cancel)

            _write_cbz_from_images(gen(), part)
        os.replace(part, dst)
        return dst
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise


# --------------------------------------------------------------------- OCR
OCR_DOWNLOAD_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{code}.traineddata"


def bundled_tessdata() -> str | None:
    import sys
    base = getattr(sys, "_MEIPASS", None)
    for c in ([os.path.join(base, "assets", "tessdata")] if base else []) + [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "tessdata")]:
        if os.path.isdir(c):
            return c
    return None


def user_tessdata() -> str:
    from .config import CACHE_DIR
    return str(CACHE_DIR / "tessdata")


def tessdata_dir() -> str | None:
    """Tesseract language data (MuPDF has the OCR engine built in). Languages downloaded by the user live in
    %LOCALAPPDATA%; the bundled French / English models are copied next to them (MuPDF needs one folder)."""
    import sys
    user = user_tessdata()
    if os.path.isdir(user) and any(f.endswith(".traineddata") for f in os.listdir(user)):
        bundled = bundled_tessdata()
        if bundled:
            for f in os.listdir(bundled):
                if f.endswith(".traineddata") and not os.path.exists(os.path.join(user, f)):
                    shutil.copyfile(os.path.join(bundled, f), os.path.join(user, f))
        return user
    cands = [os.environ.get("TESSDATA_PREFIX", "")]
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "assets", "tessdata"))
    cands.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "tessdata"))
    cands.append(r"C:\Program Files\Tesseract-OCR\tessdata")
    for c in cands:
        if c and os.path.isdir(c) and any(f.endswith(".traineddata") for f in os.listdir(c)):
            return c
    return None


def ocr_languages() -> list[str]:
    d = tessdata_dir()
    if not d:
        return []
    return sorted(f[:-12] for f in os.listdir(d) if f.endswith(".traineddata") and f != "osd.traineddata")


def ocr_output_path(src: str) -> str:
    return unique_path(os.path.splitext(src)[0] + " (OCR).pdf")


def ocr(src: str, dst: str, opts: dict, progress, cancel) -> str:
    """Add an invisible, searchable / selectable text layer to scanned pages. Images are untouched."""
    td = tessdata_dir()
    if not td:
        raise OpenError("tessdata_missing")
    lang = opts.get("ocr_lang", "fra+eng")
    dpi = int(opts.get("ocr_dpi", 300))
    force = bool(opts.get("ocr_force", False))
    if ext_of(src) == ".pdf":
        doc = pymupdf.open(src)
        if doc.needs_pass and not doc.authenticate(opts.get("password") or ""):
            raise OpenError("password_protected")
    else:
        tmp = pymupdf.open(src, filetype="cbz") if is_comic_path(src) and not is_rar(src) else _open(src, opts)
        if is_rar(src):
            tmp = pymupdf.open(comic_zip(src), filetype="cbz")
        doc = pymupdf.open("pdf", tmp.convert_to_pdf())
        tmp.close()
    font_path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", "arial.ttf")
    font = pymupdf.Font(fontfile=font_path) if os.path.exists(font_path) else pymupdf.Font("helv")
    n = doc.page_count
    done = 0
    for i in range(n):
        cancel()
        progress(i, n)
        page = doc.load_page(i)
        if not force and page.get_text("text").strip():
            continue
        tp = page.get_textpage_ocr(language=lang, dpi=dpi, full=True, tessdata=td)
        words = page.get_text("words", textpage=tp)
        if not words:
            continue
        tw = pymupdf.TextWriter(page.rect)
        for x0, y0, x1, y1, text, *_ in words:
            h = y1 - y0
            if h <= 0 or not text.strip():
                continue
            fs = h * 0.88
            natural = font.text_length(text, fontsize=fs)
            if natural <= 0:
                continue
            # scale the font so the invisible word covers the visible one (accurate selection)
            fs = min(fs * (x1 - x0) / natural, h * 1.6)
            try:
                tw.append((x0, y1 - h * 0.18), text, font=font, fontsize=fs)
            except Exception:  # noqa: BLE001
                continue
        tw.write_text(page, render_mode=3)
        done += 1
    part = dst + ".part"
    try:
        doc.subset_fonts()
    except Exception:  # noqa: BLE001
        pass
    doc.save(part, garbage=3, deflate=True)
    doc.close()
    os.replace(part, dst)
    return dst


# ---------------------------------------------------------------- job entry
def run_job(kind: str, items: list, opts: dict, queue, cancel_event) -> None:
    """Worker-process entry point. Messages: (type, ...) tuples on `queue`."""

    def cancel():
        if cancel_event.is_set():
            raise Cancelled()

    total = len(items)
    for idx, item in enumerate(items):
        def progress(done, n, _idx=idx):
            frac = (_idx + (done / max(1, n))) / max(1, total)
            queue.put(("progress", frac, _idx))

        try:
            if kind == "convert":
                out = convert(item, opts["target"], opts.get("out_dir"), opts, progress, cancel)
            elif kind == "split":
                src, pages = item
                out = split_doubles(src, split_output_path(src), pages, opts.get("rtl", False), progress, cancel)
            elif kind == "merge":
                out = merge(item[0], item[1], opts, progress, cancel)
            elif kind == "ocr":
                dst = opts.get("dst_for", {}).get(item) or ocr_output_path(item)
                try:
                    out = ocr(item, dst, opts, progress, cancel)
                except BaseException:
                    if os.path.exists(dst + ".part"):
                        os.remove(dst + ".part")
                    raise
            elif kind == "analyze":
                cancel()
                sizes = page_sizes(item)
                out = json.dumps({"pages": len(sizes),
                                  "doubles": detect_doubles(sizes, opts.get("ratio", 1.15),
                                                            opts.get("skip_first", True))})
            else:
                raise ValueError(kind)
            queue.put(("item_done", idx, out))
        except Cancelled:
            queue.put(("cancelled",))
            return
        except Exception as e:  # noqa: BLE001
            queue.put(("item_error", idx, str(e) or e.__class__.__name__))
    queue.put(("finished",))
