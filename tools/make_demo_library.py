"""Create a small fake library (PDF / EPUB / CBZ) for testing and screenshots.

Usage: python tools/make_demo_library.py <target-folder>
"""
from __future__ import annotations

import io
import os
import random
import sys
import zipfile

import pymupdf

WORDS = ("night stars shadow ancient city dream sea whisper door stone hollow light silence tower "
         "forgotten moon crimson river iron garden old voice ember ash glass road winter").split()


def sentence(rng: random.Random) -> str:
    n = rng.randint(8, 18)
    s = " ".join(rng.choice(WORDS) for _ in range(n))
    return s[0].upper() + s[1:] + "."


def paragraph(rng: random.Random) -> str:
    return " ".join(sentence(rng) for _ in range(rng.randint(3, 7)))


def cover_page(doc: pymupdf.Document, title: str, author: str, color: tuple, w=420, h=620) -> None:
    page = doc.new_page(width=w, height=h)
    page.draw_rect(page.rect, color=None, fill=color)
    page.draw_rect(pymupdf.Rect(24, 24, w - 24, h - 24), color=(1, 1, 1), width=2)
    for i in range(6):
        r = 40 + i * 28
        page.draw_circle((w / 2, h * 0.45), r, color=(1, 1, 1), width=0.6, stroke_opacity=0.25)
    page.insert_textbox(pymupdf.Rect(40, h * 0.12, w - 40, h * 0.32), title, fontsize=30, fontname="tibo",
                        color=(1, 1, 1), align=1)
    page.insert_textbox(pymupdf.Rect(40, h * 0.82, w - 40, h * 0.92), author.upper(), fontsize=14,
                        fontname="helv", color=(1, 1, 1), align=1)


def make_pdf(path: str, title: str, author: str, color: tuple, pages: int, seed: int) -> None:
    rng = random.Random(seed)
    doc = pymupdf.open()
    cover_page(doc, title, author, color)
    toc = []
    for i in range(pages):
        page = doc.new_page(width=420, height=620)
        y = 60
        if i % 6 == 0:
            ch = f"Chapter {i // 6 + 1}"
            page.insert_text((50, 80), ch, fontsize=20, fontname="tibo")
            toc.append([1, ch, page.number + 1])
            y = 110
        page.insert_textbox(pymupdf.Rect(50, y, 370, 580), "\n\n".join(paragraph(rng) for _ in range(3)),
                            fontsize=10.5, fontname="tiro", align=3)
        page.insert_text((200, 600), str(i + 1), fontsize=9, fontname="helv", color=(0.4, 0.4, 0.4))
    doc.set_toc(toc)
    doc.save(path, garbage=3, deflate=True)


def make_cbz(path: str, title: str, color: tuple, pages: int, seed: int) -> None:
    rng = random.Random(seed)
    doc = pymupdf.open()
    cover_page(doc, title, "Demo Comics", color, 400, 600)
    for i in range(pages):
        page = doc.new_page(width=400, height=600)
        page.draw_rect(page.rect, fill=(1, 1, 1), color=None)
        for row in range(3):
            for col in range(2 if row != 1 else 1):
                w = 170 if row != 1 else 350
                x = 20 + col * 190
                r = pymupdf.Rect(x, 20 + row * 190, x + w, 190 + row * 190)
                c = tuple(min(1, v * rng.uniform(0.6, 1.4)) for v in color)
                page.draw_rect(r, color=(0, 0, 0), fill=c, width=3)
                page.draw_circle((r.x0 + r.width / 2, r.y0 + r.height / 2), rng.randint(15, 50), color=(0, 0, 0), fill=(1, 1, 1), width=2)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i, p in enumerate(doc):
            z.writestr(f"{i + 1:03d}.jpg", p.get_pixmap(dpi=110).tobytes("jpeg", jpg_quality=85))
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())


def make_epub(path: str, title: str, author: str, color: tuple, chapters: int, seed: int) -> None:
    rng = random.Random(seed)
    doc = pymupdf.open()
    cover_page(doc, title, author, color, 600, 900)
    cover_png = doc[0].get_pixmap(dpi=72).tobytes("png")
    items, spine, nav = [], [], []
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/cover.png", cover_png)
        z.writestr("OEBPS/cover.xhtml",
                   '<html xmlns="http://www.w3.org/1999/xhtml"><body style="margin:0">'
                   '<img src="cover.png" style="width:100%"/></body></html>')
        items.append('<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>')
        items.append('<item id="coverimg" href="cover.png" media-type="image/png" properties="cover-image"/>')
        spine.append('<itemref idref="cover"/>')
        for c in range(1, chapters + 1):
            body = "".join(f"<p>{paragraph(rng)}</p>" for _ in range(12))
            z.writestr(f"OEBPS/ch{c}.xhtml",
                       f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter {c}</title></head>'
                       f'<body><h1>Chapter {c}</h1>{body}</body></html>')
            items.append(f'<item id="ch{c}" href="ch{c}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="ch{c}"/>')
            nav.append(f'<li><a href="ch{c}.xhtml">Chapter {c}</a></li>')
        z.writestr("OEBPS/nav.xhtml",
                   '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                   f'<nav epub:type="toc"><ol>{"".join(nav)}</ol></nav></body></html>')
        items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                   f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">demo-{seed}</dc:identifier>'
                   f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:language>en</dc:language></metadata>'
                   f'<manifest>{"".join(items)}</manifest><spine>{"".join(spine)}</spine></package>')


LIBRARY = [
    ("01_Novels/Cosmic Horror", "pdf", "The Silent Tower", "A. Whitmore", (0.12, 0.16, 0.22)),
    ("01_Novels/Cosmic Horror", "epub", "Beneath the Crimson Sea", "A. Whitmore", (0.45, 0.05, 0.08)),
    ("01_Novels/Cosmic Horror", "epub", "Ashes of the Old Moon", "A. Whitmore", (0.2, 0.2, 0.3)),
    ("01_Novels/Fantasy", "epub", "The Ember Garden", "L. Corvin", (0.1, 0.3, 0.2)),
    ("01_Novels/Fantasy", "pdf", "Iron Roads", "L. Corvin", (0.35, 0.25, 0.1)),
    ("01_Novels/Fantasy/The Glass Winter (2019)", "epub", "The Glass Winter", "M. Hale", (0.15, 0.3, 0.45)),
    ("02_Comics/00_Demon Press/Hell Office", "cbz", "Hell Office #1", "", (0.75, 0.1, 0.15)),
    ("02_Comics/00_Demon Press/Hell Office", "cbz", "Hell Office #2", "", (0.65, 0.12, 0.2)),
    ("02_Comics/00_Demon Press/Hell Office", "cbz", "Hell Office #10", "", (0.55, 0.1, 0.1)),
    ("02_Comics/00_Demon Press/Morning Star (2024)", "cbz", "Morning Star", "", (0.85, 0.55, 0.1)),
    ("02_Comics/Indie", "cbz", "Night Garden", "", (0.2, 0.5, 0.35)),
    ("02_Comics/Indie", "cbz", "Stone Whisper", "", (0.3, 0.3, 0.6)),
    ("03_Manga", "cbz", "Crimson River vol. 1", "", (0.9, 0.2, 0.3)),
    ("03_Manga", "cbz", "Crimson River vol. 2", "", (0.8, 0.25, 0.4)),
    ("04_Docs", "pdf", "User Manual", "Morningstar", (0.25, 0.25, 0.28)),
]


def main(target: str) -> int:
    for i, (folder, kind, title, author, color) in enumerate(LIBRARY):
        d = os.path.join(target, *folder.split("/"))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{title}.{kind}")
        if kind == "pdf":
            make_pdf(path, title, author, color, 40, i)
        elif kind == "epub":
            make_epub(path, title, author, color, 8, i)
        else:
            make_cbz(path, title, color, 12, i)
        print("created", path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "demo-library"))
