#!/usr/bin/env python3
"""blacken_to_transparent.py — make a chafa banner's black background the terminal's.

chafa renders the poster's flat black background as near-black truecolor SGR codes
and, in its default (non-half) symbol set, encodes sub-cell detail with PARTIAL-BLOCK
glyphs (▋ ╴ ┈ ▍ …) that carry a foreground AND a background color. To make "black"
fall through to the terminal background we must do two things per cell:

  1. near-black BACKGROUND (`48;2;r;g;b`, max(r,g,b) <= threshold)  -> default bg (`49`)
  2. near-black FOREGROUND (the black "ink" of a glyph)             -> blank the glyph
     (emit a space) so nothing is painted where the poster was black. Just recoloring
     the fg to default would leave visible default-colored speckles.

Bright letter colors (yellow/red) and their glyphs are untouched. Cursor-visibility /
private-mode / erase sequences chafa emits are stripped so the result is safe to
`printf` inline. Newlines are preserved.

Usage:
  blacken_to_transparent.py <in.txt> [--threshold 24] [-o out.txt]
Reads <in.txt> (or stdin with '-'); writes to -o or stdout.
"""
import argparse
import re
import sys

PRIVATE_RE = re.compile(r"\x1b\[\?[0-9;]*[A-Za-z]")   # ESC[?25l / ESC[?25h ...
ERASE_RE = re.compile(r"\x1b\[[0-9;]*[JK]")            # erase display/line
# Split keeping SGR sequences as their own tokens.
SGR_SPLIT_RE = re.compile(r"(\x1b\[[0-9;]*m)")
SGR_PARAMS_RE = re.compile(r"\x1b\[([0-9;]*)m")


def rewrite_and_track(params, thresh):
    """Rewrite one SGR param list: near-black fg->39, bg->49. Return (new_seq, fg_black)
    where fg_black is None if this sequence didn't set the foreground, else bool."""
    toks = params.split(";") if params else ["0"]
    out, i, n = [], 0, len(toks)
    fg_black = None
    while i < n:
        t = toks[i]
        if t in ("38", "48") and i + 1 < n and toks[i + 1] == "2" and i + 4 < n:
            r, g, b = (int(x or 0) for x in toks[i + 2:i + 5])
            black = max(r, g, b) <= thresh
            if t == "38":
                fg_black = black
                out.append("39" if black else "38;2;%d;%d;%d" % (r, g, b))
            else:
                out.append("49" if black else "48;2;%d;%d;%d" % (r, g, b))
            i += 5
        elif t in ("38", "48") and i + 1 < n and toks[i + 1] == "5" and i + 2 < n:
            if t == "38":
                fg_black = False   # 256-color fg: treat as real ink
            out.append("%s;5;%s" % (t, toks[i + 2]))
            i += 3
        else:
            if t in ("0", "", "39"):      # reset or explicit default fg
                fg_black = False
            out.append(t)
            i += 1
    return "\x1b[" + ";".join(out) + "m", fg_black


def blank_ink(text):
    """Replace visible glyphs with spaces (keep spaces and newlines)."""
    return "".join(c if c in (" ", "\n", "\t") else " " for c in text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("--threshold", type=int, default=24,
                    help="max(r,g,b) at/below this counts as black -> terminal default")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    data = sys.stdin.read() if args.infile == "-" else open(args.infile, encoding="utf-8").read()
    data = ERASE_RE.sub("", PRIVATE_RE.sub("", data))

    out = []
    fg_black = False
    for tok in SGR_SPLIT_RE.split(data):
        if not tok:
            continue
        m = SGR_PARAMS_RE.fullmatch(tok)
        if m:
            seq, fb = rewrite_and_track(m.group(1), args.threshold)
            if fb is not None:
                fg_black = fb
            out.append(seq)
        else:
            out.append(blank_ink(tok) if fg_black else tok)
    result = "".join(out)

    if args.out:
        open(args.out, "w", encoding="utf-8").write(result)
    else:
        sys.stdout.write(result)


if __name__ == "__main__":
    main()
