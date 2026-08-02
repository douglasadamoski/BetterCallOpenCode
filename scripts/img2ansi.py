#!/usr/bin/env python3
"""img2ansi.py — turn a logo PNG into a truecolor terminal banner (half-block).

Self-contained replacement for `chafa --symbols=half --colors=full -t <a>` so the
skill has no external binary dependency (only Pillow). Each character cell is 1 px
wide and 2 px tall, rendered with the upper-half block '▀': foreground = top pixel,
background = bottom pixel. This mirrors the BetterCallGemini pipeline: the source
poster's flat background is "knocked out" to transparent, and transparent pixels
become "no color" (a space with the terminal's own background) so the logo floats
cleanly on any terminal.

Knockout: `--knockout black` (default) drops near-black pixels (for a black-bg
source like BetterCallChatGPT_black.png); `--knockout white` drops near-white;
`--knockout none` keeps everything and honors only the image's own alpha channel.

Usage:
  img2ansi.py <image.png> [--cols 88] [--alpha 128]
              [--knockout black|white|none] [--threshold 48]

Writes the ANSI art to stdout.
"""
import argparse
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required: pip install Pillow")

UPPER_HALF = "▀"  # ▀
RESET = "\033[0m"


def is_transparent(px, alpha_thresh, knockout, thresh):
    r, g, b, a = px
    if a < alpha_thresh:
        return True
    if knockout == "black" and max(r, g, b) <= thresh:
        return True
    if knockout == "white" and min(r, g, b) >= (255 - thresh):
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--cols", type=int, default=88, help="output width in terminal columns")
    ap.add_argument("--alpha", type=int, default=128, help="alpha below this = transparent")
    ap.add_argument("--knockout", choices=["black", "white", "none"], default="black",
                    help="flat background color to drop to transparent (default: black)")
    ap.add_argument("--threshold", type=int, default=48,
                    help="how far from the knockout color still counts as background (0-255)")
    args = ap.parse_args()

    img = Image.open(args.image).convert("RGBA")

    # Trim fully-transparent / white border so the logo fills the width.
    cols = max(1, args.cols)
    w, h = img.size
    rows_px = max(2, round(cols * h / w))
    if rows_px % 2:
        rows_px += 1
    img = img.resize((cols, rows_px), Image.LANCZOS)
    px = img.load()

    out = []
    for y in range(0, rows_px, 2):
        line = []
        cur = None  # (top_rgb_or_None, bot_rgb_or_None)
        for x in range(cols):
            top = px[x, y]
            bot = px[x, y + 1]
            t_tr = is_transparent(top, args.alpha, args.knockout, args.threshold)
            b_tr = is_transparent(bot, args.alpha, args.knockout, args.threshold)
            if t_tr and b_tr:
                line.append(RESET + " ")
                continue
            codes = []
            if not t_tr:
                codes.append("38;2;%d;%d;%d" % top[:3])
            else:
                codes.append("39")  # default fg (upper half invisible over bg)
            if not b_tr:
                codes.append("48;2;%d;%d;%d" % bot[:3])
            else:
                codes.append("49")  # default bg
            line.append("\033[" + ";".join(codes) + "m" + UPPER_HALF)
        out.append("".join(line) + RESET)
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
