#!/usr/bin/env python3
"""
Generates icon.png for the DN-AUTO Monitor desktop launcher: a dark radio-
panel style badge with a teal analog meter arc/needle (matching the app's
FT-991A-inspired dark theme) and an orange power bar underneath, evoking an
amplifier's ALC/power meter. Run once to (re)produce assets/icon.png; no
external image assets needed, everything is drawn with Pillow.

    python3 assets/generate_icon.py
"""

import math
import os

from PIL import Image, ImageDraw, ImageFilter

SIZE = 256
BG = (18, 22, 26, 255)          # near-black panel, matches app background
BEZEL = (40, 46, 52, 255)       # slightly lighter bezel ring
TEAL = (64, 224, 208, 255)      # digital readout teal
TEAL_DIM = (32, 110, 104, 255)
ORANGE = (255, 140, 40, 255)
ORANGE_DIM = (140, 80, 30,255)
WHITE_DIM = (150, 160, 165, 255)


def rounded_square(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def main():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer bezel (rounded square, like a radio's front panel corner)
    margin = 8
    rounded_square(draw, (margin, margin, SIZE - margin, SIZE - margin), radius=40, fill=BEZEL)

    # Inner panel
    inset = 20
    rounded_square(draw, (inset, inset, SIZE - inset, SIZE - inset), radius=32, fill=BG)

    # Meter arc (analog VU/power-meter style), centered upper-middle
    cx, cy = SIZE // 2, 112
    r_outer = 78
    r_inner = 64
    start_deg, end_deg = 200, 340  # sweeping arc, like an S-meter

    # Tick marks along the arc
    for i in range(9):
        t = start_deg + (end_deg - start_deg) * i / 8
        rad = math.radians(t)
        x1, y1 = cx + r_inner * math.cos(rad), cy + r_inner * math.sin(rad)
        x2, y2 = cx + r_outer * math.cos(rad), cy + r_outer * math.sin(rad)
        color = ORANGE if i >= 6 else TEAL_DIM
        draw.line([(x1, y1), (x2, y2)], fill=color, width=4)

    # Arc track
    draw.arc(
        (cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner),
        start=start_deg, end=end_deg, fill=TEAL_DIM, width=3,
    )

    # Needle, pointing toward the "loaded" upper-right zone
    needle_deg = 300
    rad = math.radians(needle_deg)
    nx, ny = cx + (r_inner - 6) * math.cos(rad), cy + (r_inner - 6) * math.sin(rad)
    draw.line([(cx, cy), (nx, ny)], fill=ORANGE, width=6)
    draw.ellipse((cx - 7, cy - 7, cx + 7, cy + 7), fill=ORANGE)

    # Digital readout bar beneath the meter -- like a band/power segment display
    bar_y0, bar_y1 = 178, 200
    bar_x0, bar_x1 = 48, SIZE - 48
    n_segs = 12
    seg_gap = 4
    seg_w = (bar_x1 - bar_x0 - seg_gap * (n_segs - 1)) / n_segs
    lit = 8  # how many segments are "lit" teal vs dim
    for i in range(n_segs):
        x0 = bar_x0 + i * (seg_w + seg_gap)
        x1 = x0 + seg_w
        color = TEAL if i < lit else TEAL_DIM
        draw.rounded_rectangle((x0, bar_y0, x1, bar_y1), radius=3, fill=color)

    # Small "band" text glyphs feel -- three short ticks representing a band
    # strip, bottom of the panel
    strip_y = 216
    for i, x in enumerate(range(60, SIZE - 60, 24)):
        color = ORANGE if i == 3 else WHITE_DIM
        draw.rounded_rectangle((x, strip_y, x + 14, strip_y + 8), radius=2, fill=color)

    # Soft glow behind the needle tip for a bit of life
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), fill=(*ORANGE[:3], 120))
    glow = glow.filter(ImageFilter.GaussianBlur(8))
    img = Image.alpha_composite(glow, img)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "icon.png")
    img.save(out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
