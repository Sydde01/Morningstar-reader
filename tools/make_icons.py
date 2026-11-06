"""Generate the icon proposals (SVG + PNG) and the final multi-size .ico.

Usage:
    python tools/make_icons.py            # write proposals + build assets/icon.* from the chosen design
    python tools/make_icons.py --choose 3 # pick another design for the app icon
"""
from __future__ import annotations

import argparse
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
PROPOSALS = os.path.join(ASSETS, "icon-proposals")

RED = "#e0303f"
RED_DARK = "#8e1622"

BG = f"""
<defs>
  <radialGradient id="bg" cx="50%" cy="42%" r="70%">
    <stop offset="0" stop-color="#2a0c10"/>
    <stop offset="1" stop-color="#0b0b0d"/>
  </radialGradient>
  <linearGradient id="horn" x1="0" y1="1" x2="0" y2="0">
    <stop offset="0" stop-color="{RED_DARK}"/>
    <stop offset="1" stop-color="{RED}"/>
  </linearGradient>
  <radialGradient id="glow" cx="50%" cy="50%" r="50%">
    <stop offset="0" stop-color="{RED}" stop-opacity="0.55"/>
    <stop offset="1" stop-color="{RED}" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect x="8" y="8" width="240" height="240" rx="54" fill="url(#bg)"/>
<rect x="8" y="8" width="240" height="240" rx="54" fill="none" stroke="#3a1418" stroke-width="3"/>
"""


def svg(body: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">{BG}{body}</svg>'


def star_path(cx, cy, r_out, r_in, points=8, rot=-90) -> str:
    pts = []
    for i in range(points * 2):
        r = r_out if i % 2 == 0 else r_in
        a = math.radians(rot + i * 180 / points)
        pts.append(f"{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}")
    return "M" + " L".join(pts) + " Z"


# 1 — Horned grimoire: an open book with two demon horns
HORNED_BOOK = svg(f"""
<ellipse cx="128" cy="150" rx="100" ry="70" fill="url(#glow)"/>
<path d="M104 118 C 92 86, 70 66, 46 46 C 84 54, 112 80, 122 114 Z" fill="url(#horn)"/>
<path d="M152 118 C 164 86, 186 66, 210 46 C 172 54, 144 80, 134 114 Z" fill="url(#horn)"/>
<path d="M128 210 C 98 196, 68 194, 36 200 L 36 112 C 68 104, 100 106, 128 122 Z" fill="#1c0a0d" stroke="{RED}" stroke-width="5" stroke-linejoin="round"/>
<path d="M128 210 C 158 196, 188 194, 220 200 L 220 112 C 188 104, 156 106, 128 122 Z" fill="#1c0a0d" stroke="{RED}" stroke-width="5" stroke-linejoin="round"/>
<path d="M128 200 C 102 188, 74 186, 46 190 L 46 118 C 74 112, 102 114, 128 128 Z" fill="#f4f1ee"/>
<path d="M128 200 C 154 188, 182 186, 210 190 L 210 118 C 182 112, 154 114, 128 128 Z" fill="#ffffff"/>
<path d="M128 128 L 128 200" stroke="#bdb6b0" stroke-width="2.5"/>
<g stroke="#9c9690" stroke-width="4" stroke-linecap="round">
  <path d="M60 136 C 80 132, 98 134, 114 140"/><path d="M60 152 C 80 148, 98 150, 114 156"/>
  <path d="M60 168 C 80 164, 98 166, 114 172"/>
  <path d="M142 140 C 158 134, 176 132, 196 136"/><path d="M142 156 C 158 150, 176 148, 196 152"/>
</g>
<path d="{star_path(170, 172, 11, 4, 4)}" fill="{RED}"/>
""")

# 2 — Morningstar: an eight-point star inside a demonic sigil ring
MORNINGSTAR = svg(f"""
<circle cx="128" cy="128" r="104" fill="url(#glow)"/>
<circle cx="128" cy="128" r="88" fill="none" stroke="{RED}" stroke-width="5"/>
<circle cx="128" cy="128" r="76" fill="none" stroke="{RED}" stroke-width="2" stroke-dasharray="6 7"/>
<path d="{star_path(128, 128, 70, 18, 8)}" fill="#ffffff"/>
<path d="{star_path(128, 128, 40, 12, 4, -45)}" fill="{RED}" opacity="0.9"/>
<circle cx="128" cy="128" r="9" fill="#ffffff"/>
<path d="M96 52 C 88 34, 74 24, 58 18 C 76 32, 82 46, 84 62 Z" fill="url(#horn)"/>
<path d="M160 52 C 168 34, 182 24, 198 18 C 180 32, 174 46, 172 62 Z" fill="url(#horn)"/>
""")

# 3 — The CEO of Hell: white hair, horns and glowing red eyes (original silhouette)
DEMON = svg(f"""
<ellipse cx="128" cy="140" rx="96" ry="90" fill="url(#glow)"/>
<path d="M84 74 C 70 50, 52 38, 30 34 C 50 50, 60 70, 66 96 Z" fill="#141416" stroke="{RED}" stroke-width="4"/>
<path d="M172 74 C 186 50, 204 38, 226 34 C 206 50, 196 70, 190 96 Z" fill="#141416" stroke="{RED}" stroke-width="4"/>
<path d="M128 46 C 70 46, 46 92, 50 140 C 52 176, 44 206, 34 228 L 82 228 C 84 196, 86 168, 92 148
         L 164 148 C 170 168, 172 196, 174 228 L 222 228 C 212 206, 204 176, 206 140 C 210 92, 186 46, 128 46 Z"
      fill="#f3f1f4"/>
<path d="M90 150 C 92 110, 104 92, 128 84 C 152 92, 164 110, 166 150 C 160 186, 146 204, 128 210
         C 110 204, 96 186, 90 150 Z" fill="#0e0e10"/>
<path d="M92 122 C 104 96, 118 86, 128 84 C 122 100, 110 112, 92 122 Z" fill="#f3f1f4"/>
<path d="M164 122 C 152 96, 138 86, 128 84 C 134 100, 146 112, 164 122 Z" fill="#f3f1f4"/>
<path d="M102 146 L 122 150 L 118 158 Z" fill="{RED}"/>
<path d="M154 146 L 134 150 L 138 158 Z" fill="{RED}"/>
<circle cx="113" cy="152" r="10" fill="{RED}" opacity="0.25"/>
<circle cx="143" cy="152" r="10" fill="{RED}" opacity="0.25"/>
""")

# 4 — Pancakes from hell (a nod to the game's favourite breakfast)
PANCAKES = svg(f"""
<ellipse cx="128" cy="160" rx="104" ry="70" fill="url(#glow)"/>
<ellipse cx="128" cy="200" rx="92" ry="18" fill="#e6e1dc"/>
<g>
  <ellipse cx="128" cy="184" rx="78" ry="20" fill="#9a5a22"/>
  <ellipse cx="128" cy="176" rx="78" ry="20" fill="#d8954a"/>
  <ellipse cx="128" cy="160" rx="76" ry="20" fill="#9a5a22"/>
  <ellipse cx="128" cy="152" rx="76" ry="20" fill="#e2a457"/>
  <ellipse cx="128" cy="136" rx="74" ry="20" fill="#9a5a22"/>
  <ellipse cx="128" cy="128" rx="74" ry="20" fill="#eab067"/>
</g>
<path d="M70 126 C 90 112, 166 112, 186 126 C 182 134, 176 136, 174 150 C 172 158, 164 158, 164 148
         C 164 140, 150 140, 140 142 C 126 144, 120 156, 112 152 C 104 148, 112 138, 96 138 C 84 138, 82 150, 76 146
         C 70 142, 76 134, 70 126 Z" fill="{RED}"/>
<rect x="112" y="108" width="32" height="18" rx="4" fill="#fff3b0"/>
<path d="M98 112 C 88 88, 76 76, 60 64 C 82 70, 98 86, 108 110 Z" fill="url(#horn)"/>
<path d="M158 112 C 168 88, 180 76, 196 64 C 174 70, 158 86, 148 110 Z" fill="url(#horn)"/>
""")

# 5 — Gothic monogram: an "M" crowned with horns and a red star
MONOGRAM = svg(f"""
<circle cx="128" cy="132" r="100" fill="url(#glow)"/>
<path d="M58 206 L 58 86 L 84 86 L 128 150 L 172 86 L 198 86 L 198 206 L 172 206 L 172 132 L 136 184
         L 120 184 L 84 132 L 84 206 Z" fill="#ffffff"/>
<path d="M58 86 C 50 62, 38 48, 24 38 C 46 44, 62 58, 74 86 Z" fill="url(#horn)"/>
<path d="M198 86 C 206 62, 218 48, 232 38 C 210 44, 194 58, 182 86 Z" fill="url(#horn)"/>
<path d="{star_path(128, 72, 24, 7, 4)}" fill="{RED}"/>
<rect x="44" y="214" width="168" height="6" rx="3" fill="{RED}"/>
""")

DESIGNS = [
    ("1-horned-grimoire", HORNED_BOOK),
    ("2-morningstar", MORNINGSTAR),
    ("3-ceo-of-hell", DEMON),
    ("4-hell-pancakes", PANCAKES),
    ("5-gothic-monogram", MONOGRAM),
]


def render_png(svg_text: str, size: int):
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg_text.encode())).render(p, QRectF(0, 0, size, size))
    p.end()
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--choose", type=int, default=1, help="design number used for the app icon")
    args = ap.parse_args()
    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    os.makedirs(PROPOSALS, exist_ok=True)
    for name, text in DESIGNS:
        with open(os.path.join(PROPOSALS, f"{name}.svg"), "w", encoding="utf-8") as fh:
            fh.write(text)
        render_png(text, 256).save(os.path.join(PROPOSALS, f"{name}.png"))

    name, text = DESIGNS[args.choose - 1]
    with open(os.path.join(ASSETS, "icon.svg"), "w", encoding="utf-8") as fh:
        fh.write(text)
    render_png(text, 512).save(os.path.join(ASSETS, "icon.png"))
    # Multi-size Windows icon
    from PIL import Image

    tmp = os.path.join(ASSETS, "_icon_256.png")
    render_png(text, 256).save(tmp)
    im = Image.open(tmp)
    im.save(os.path.join(ASSETS, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                     (128, 128), (256, 256)])
    im.close()
    os.remove(tmp)
    print(f"app icon: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
