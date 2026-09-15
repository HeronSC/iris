# File: ui/assets/build_icon.py

import io
import logging
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

logger = logging.getLogger(__name__)

BLUE = "#2563eb"

HEAD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">'

DETAIL = HEAD + """
  {bg}
  <g fill="none" stroke="{fg}" stroke-width="14" stroke-linejoin="round">
    <path d="M{x0} 128 Q128 {cy0} {x1} 128 Q128 {cy1} {x0} 128 Z"/>
    <circle cx="128" cy="128" r="{iris}"/>
  </g>
  <circle cx="128" cy="128" r="28" fill="{fg}"/>
  <circle cx="116" cy="116" r="9" fill="{hl}"/>
</svg>"""

MEDIUM = HEAD + """
  {bg}
  <path d="M{x0} 128 Q128 {cy0} {x1} 128 Q128 {cy1} {x0} 128 Z" fill="none" stroke="{fg}" stroke-width="18" stroke-linejoin="round"/>
  <circle cx="128" cy="128" r="30" fill="{fg}"/>
</svg>"""

SMALL = HEAD + """
  {bg}
  <path d="M{sx0} 128 Q128 {scy0} {sx1} 128 Q128 {scy1} {sx0} 128 Z" fill="{fg}"/>
  <circle cx="128" cy="128" r="{pupil}" fill="{hl}"/>
</svg>"""

BADGE_GEO = {"x0": 26, "x1": 230, "cy0": 26, "cy1": 230, "iris": 51,
             "sx0": 22, "sx1": 234, "scy0": 8, "scy1": 248, "pupil": 34}
FULL_GEO = {"x0": 10, "x1": 246, "cy0": 18, "cy1": 238, "iris": 55,
            "sx0": 6, "sx1": 250, "scy0": 2, "scy1": 254, "pupil": 38}

BADGE = '<circle cx="128" cy="128" r="124" fill="%s"/>' % BLUE

VARIANTS = {
    "badge": dict(bg=BADGE, fg="#ffffff", hl=BLUE, **BADGE_GEO),
    "mono-black": dict(bg="", fg="#000000", hl="#ffffff", **FULL_GEO),
    "mono-white": dict(bg="", fg="#ffffff", hl="#000000", **FULL_GEO),
}

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def tier(size):
    if size <= 24:
        return SMALL
    if size <= 40:
        return MEDIUM
    return DETAIL


def render(svg_text, size):
    renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    qbuf = QBuffer(data)
    qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(qbuf, "PNG")
    qbuf.close()
    return Image.open(io.BytesIO(data.data())).convert("RGBA")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    QGuiApplication(sys.argv)
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    made = {}
    for name, style in VARIANTS.items():
        frames = [render(tier(s).format(**style), s) for s in ICO_SIZES]
        made[name] = frames
        frames[-1].save(
            os.path.join(out, "iris-%s.ico" % name),
            format="ICO",
            sizes=[(s, s) for s in ICO_SIZES],
            append_images=frames[:-1],
        )
        for size, frame in zip(ICO_SIZES, frames):
            if size in (16, 32, 48, 256):
                frame.save(os.path.join(out, "iris-%s-%d.png" % (name, size)))
        for label, tpl in (("small", SMALL), ("medium", MEDIUM), ("detail", DETAIL)):
            with open(os.path.join(out, "iris-%s-%s.svg" % (name, label)), "w", encoding="utf-8") as handle:
                handle.write(tpl.format(**style))
    build_sheet(made, os.path.join(out, "preview.png"))
    logger.info("variants: %s", ", ".join(made))


def build_sheet(made, path):
    show = [16, 20, 24, 32, 48, 64]
    zoom, pad = 6, 16
    cell = max(show) * zoom
    rows = [("badge", "#f5f5f5"), ("badge", "#1f1f1f"),
            ("mono-black", "#f5f5f5"), ("mono-white", "#1f1f1f")]
    width = pad + len(show) * (cell + pad)
    sheet = Image.new("RGBA", (width, pad + len(rows) * (cell + pad)), "#ffffff")
    for r, (name, bg) in enumerate(rows):
        y = pad + r * (cell + pad)
        sheet.paste(Image.new("RGBA", (width, cell), bg), (0, y))
        for c, size in enumerate(show):
            big = made[name][ICO_SIZES.index(size)].resize((size * zoom, size * zoom), Image.NEAREST)
            x = pad + c * (cell + pad) + (cell - size * zoom) // 2
            sheet.alpha_composite(big, (x, y + (cell - size * zoom) // 2))
    sheet.convert("RGB").save(path)


main()
