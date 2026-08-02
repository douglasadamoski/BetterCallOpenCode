#!/usr/bin/env python3
"""make_banner_svg.py — render the ANSI banner art into a Rich "terminal window" SVG.

Mirrors BetterCallGemini's assets/banner.svg: a terminal frame (title bar with the
three traffic-light dots + a window title) containing the truecolor half-block art.
Uses Rich's save_svg, so the output is the same `class="rich-terminal"` SVG format.

Usage:
  make_banner_svg.py [art.txt] [out.svg] [title]
Defaults: assets/better_call_myai.txt -> assets/banner.svg,
          title "It's Better Call MyAI!".
Requires: rich (pip install rich).
"""
import io
import sys

from rich.console import Console
from rich.text import Text

art = sys.argv[1] if len(sys.argv) > 1 else "assets/better_call_myai.txt"
out = sys.argv[2] if len(sys.argv) > 2 else "assets/banner.svg"
title = sys.argv[3] if len(sys.argv) > 3 else "It's Better Call MyAI!"

ansi = open(art, encoding="utf-8").read().rstrip("\n")
# Width = the art's VISIBLE column count (e.g. 88), measured via Rich's cell length so the
# ANSI escape bytes don't inflate it (len() on the raw line would count the escapes too).
width = max((Text.from_ansi(line).cell_len for line in ansi.splitlines()), default=88)
console = Console(record=True, width=width, file=io.StringIO(), force_terminal=True)
console.print(Text.from_ansi(ansi), end="")
console.save_svg(out, title=title)
print(f"wrote {out} (title={title!r}, width={width})")
