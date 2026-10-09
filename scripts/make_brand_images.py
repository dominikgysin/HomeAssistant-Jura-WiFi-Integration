"""Draw the brand images of the integration: a coffee cup whose steam is a radio signal.

Original artwork, not derived from any manufacturer logo. Everything is drawn at 4x
and scaled down for clean edges. Needs Pillow::

    pip install pillow
    python scripts/make_brand_images.py [output folder]

The images go to ``custom_components/jura_wifi/brand`` unless a folder is given.
"""

from __future__ import annotations

import math
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

DEFAULT_OUT = Path(__file__).parents[1] / "custom_components" / "jura_wifi" / "brand"

SUPERSAMPLING = 4
GRID = 1024  # design grid of the mark

Color = tuple[int, int, int]
BG_TOP: Color = (124, 76, 44)
BG_BOTTOM: Color = (56, 33, 20)
CUP: Color = (255, 243, 224)
SAUCER: Color = (240, 214, 176)
SIGNAL: Color = (255, 183, 77)
TEXT_LIGHT_THEME: Color = (62, 38, 24)
TEXT_DARK_THEME: Color = (255, 243, 224)


def _vertical_gradient(size: int, top: Color, bottom: Color) -> Image.Image:
    mask = Image.linear_gradient("L").resize((size, size), Image.Resampling.BILINEAR)
    return Image.composite(
        Image.new("RGB", (size, size), bottom),
        Image.new("RGB", (size, size), top),
        mask,
    )


def _layer(size: int, color: Color, mask: Image.Image) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (*color, 0))
    layer.putalpha(mask)
    return layer


def mark(size: int) -> Image.Image:
    """Return the square badge at ``size`` px."""
    s = size * SUPERSAMPLING
    k = s / GRID

    def box(x0: float, y0: float, x1: float, y1: float) -> list[float]:
        return [x0 * k, y0 * k, x1 * k, y1 * k]

    # the badge, with transparent rounded corners
    badge = _vertical_gradient(s, BG_TOP, BG_BOTTOM).convert("RGBA")
    corners = Image.new("L", (s, s), 0)
    ImageDraw.Draw(corners).rounded_rectangle(
        [0, 0, s - 1, s - 1], radius=232 * k, fill=255
    )
    badge.putalpha(corners)

    # optical centering: the handle is thin, the cup body carries the weight
    dx = 30

    # cup with handle
    cup = Image.new("L", (s, s), 0)
    draw = ImageDraw.Draw(cup)
    # the handle is a ring; the body is drawn over its left half
    draw.ellipse(box(560 + dx, 536, 840 + dx, 760), fill=255)
    draw.ellipse(box(616 + dx, 590, 784 + dx, 706), fill=0)
    # the body: straight top, rounded bottom
    draw.rounded_rectangle(
        box(270 + dx, 520, 670 + dx, 792),
        radius=150 * k,
        fill=255,
        corners=(False, False, True, True),
    )
    # the rim
    draw.rounded_rectangle(box(246 + dx, 498, 694 + dx, 548), radius=25 * k, fill=255)

    saucer = Image.new("L", (s, s), 0)
    ImageDraw.Draw(saucer).rounded_rectangle(
        box(196 + dx, 812, 744 + dx, 868), radius=28 * k, fill=255
    )

    # the signal: a dot and three arcs above the cup
    signal = Image.new("L", (s, s), 0)
    draw = ImageDraw.Draw(signal)
    cx, cy = 470 + dx, 446
    stroke = 54
    draw.ellipse(box(cx - 38, cy - 38, cx + 38, cy + 38), fill=255)
    for radius in (112, 196, 280):
        reach = radius + stroke / 2
        draw.arc(
            box(cx - reach, cy - reach, cx + reach, cy + reach),
            start=227,
            end=313,
            fill=255,
            width=round(stroke * k),
        )
        # round caps
        for angle in (227, 313):
            ex = cx + radius * math.cos(math.radians(angle))
            ey = cy + radius * math.sin(math.radians(angle))
            draw.ellipse(
                box(ex - stroke / 2, ey - stroke / 2, ex + stroke / 2, ey + stroke / 2),
                fill=255,
            )

    for layer in (
        _layer(s, SAUCER, saucer),
        _layer(s, CUP, cup),
        _layer(s, SIGNAL, signal),
    ):
        badge = Image.alpha_composite(badge, layer)
    return badge.resize((size, size), Image.Resampling.LANCZOS)


def logo(height: int, text_color: Color) -> Image.Image:
    """Return the wide logo: the badge on the left, the wordmark on the right."""
    s = height * SUPERSAMPLING
    font = ImageFont.load_default(size=round(s * 0.36))
    label = "Wi-Fi Connect"
    stroke = round(s * 0.006)
    left, top, right, bottom = font.getbbox(label, stroke_width=stroke)
    text_w, text_h = right - left, bottom - top

    # the wordmark at 4x, vertically centred on the badge
    text_hi = Image.new("RGBA", (text_w, s), (0, 0, 0, 0))
    ImageDraw.Draw(text_hi).text(
        (-left, (s - text_h) / 2 - top),
        label,
        font=font,
        fill=text_color,
        stroke_width=stroke,
        stroke_fill=text_color,
    )
    text_layer = text_hi.resize(
        (math.ceil(text_w / SUPERSAMPLING), height), Image.Resampling.LANCZOS
    )

    gap = round(height * 0.13)
    canvas = Image.new("RGBA", (height + gap + text_layer.width, height), (0, 0, 0, 0))
    canvas.alpha_composite(mark(height), (0, 0))
    canvas.alpha_composite(text_layer, (height + gap, 0))
    return canvas


def main() -> None:
    """Write the icon and the light and dark logo in both resolutions."""
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)

    def save(image: Image.Image, name: str) -> None:
        image.save(out / name, optimize=True)
        print(f"{name}: {image.size[0]}x{image.size[1]}")

    icon_2x = mark(512)
    save(icon_2x, "icon@2x.png")
    save(icon_2x.resize((256, 256), Image.Resampling.LANCZOS), "icon.png")
    for suffix, height in (("", 256), ("@2x", 512)):
        save(logo(height, TEXT_LIGHT_THEME), f"logo{suffix}.png")
        save(logo(height, TEXT_DARK_THEME), f"dark_logo{suffix}.png")


if __name__ == "__main__":
    main()
