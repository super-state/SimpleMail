#!/usr/bin/env python3
"""Generate the SimpleMail app icon (assets/icon.ico, multi-size)."""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "assets" / "icon.ico"


def make_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # rounded-square background, gradient-ish blue
    r = size // 5
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(37, 99, 235, 255))
    # lighter top half for depth
    d.rounded_rectangle([0, 0, size - 1, int(size * 0.45)], radius=r, fill=(59, 130, 246, 255))
    # envelope body
    m = size * 0.12
    top = size * 0.28
    bot = size * 0.72
    w = size * 0.76
    d.rounded_rectangle([m, top, m + w, bot], radius=size * 0.03,
                        fill=(255, 255, 255, 255))
    # envelope flap (chevron)
    d.line([(m, top), (m + w / 2, top + (bot - top) * 0.55), (m + w, top)],
           fill=(37, 99, 235, 255), width=max(2, size // 22), joint="curve")
    return img


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [make_icon(s) for s in sizes]
    imgs[-1].save(OUT, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
    print(f"Wrote {OUT} ({len(sizes)} sizes)")


if __name__ == "__main__":
    sys.exit(main())
