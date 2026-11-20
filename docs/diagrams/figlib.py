# -*- coding: utf-8 -*-
"""Minimal SVG helpers for print figures."""
from html import escape

FONT = "Microsoft YaHei, SimHei, sans-serif"


class SVG:
    def __init__(self, w, h, bg="#ffffff"):
        self.w, self.h, self.bg = w, h, bg
        self.parts = []

    def text_block(self, cx, y, w, h, lines, *, fs, bold, anchor, color):
        step = fs * 1.45
        total = step * len(lines)
        start = y + h / 2 - total / 2 + fs * 0.95
        for i, ln in enumerate(lines):
            weight = ' font-weight="600"' if (bold and i == 0) else ""
            self.parts.append(
                f'<text x="{cx}" y="{start + i * step}" font-family="{FONT}" '
                f'font-size="{fs}" text-anchor="{anchor}" fill="{color}"{weight}>{escape(ln)}</text>')

    def rect(self, x, y, w, h, lines=(), *, fill="#f8fafc", stroke="#475569", rx=6,
             sw=1.6, fs=15, bold=False, anchor="middle", color="#111827", dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                          f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')
        if lines:
            cx = x + (12 if anchor == "start" else w / 2)
            self.text_block(cx, y, w, h, lines, fs=fs, bold=bold, anchor=anchor, color=color)

    def process(self, x, y, w, h, lines, *, fill="#eff6ff", stroke="#1d4ed8", fs=15):
        self.rect(x, y, w, h, lines, fill=fill, stroke=stroke, rx=h / 2, sw=1.6, fs=fs)

    def store(self, x, y, w, h, lines, *, fill="#eef2ff", stroke="#4338ca", fs=14):
        ry = 12
        self.parts.append(f'<path d="M{x},{y + ry} A{w / 2},{ry} 0 0 1 {x + w},{y + ry} '
                          f'L{x + w},{y + h - ry} A{w / 2},{ry} 0 0 1 {x},{y + h - ry} Z" '
                          f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
        self.parts.append(f'<path d="M{x},{y + ry} A{w / 2},{ry} 0 0 0 {x + w},{y + ry}" '
                          f'fill="none" stroke="{stroke}" stroke-width="1.6"/>')
        self.text_block(x + w / 2, y + ry, w, h - ry * 2, lines, fs=fs, bold=False,
                        anchor="middle", color="#1e1b4b")

    def actor(self, x, y, w, h, lines, *, fill="#fff7ed", stroke="#c2410c", fs=14):
        self.rect(x, y, w, h, lines, fill=fill, stroke=stroke, rx=4, sw=1.6, fs=fs)

    def arrow(self, pts, label="", *, dash=None, color="#374151", fs=12,
              lx=None, ly=None, sw=1.4, anchor="middle"):
        d = " ".join(f"{p[0]},{p[1]}" for p in pts)
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<polyline points="{d}" fill="none" stroke="{color}" '
                          f'stroke-width="{sw}"{da} marker-end="url(#arrow)"/>')
        if label:
            items = label if isinstance(label, (list, tuple)) else [label]
            if lx is None:
                lx = (pts[0][0] + pts[-1][0]) / 2
            if ly is None:
                ly = (pts[0][1] + pts[-1][1]) / 2 - 6
            for i, ln in enumerate(items):
                self.parts.append(f'<text x="{lx}" y="{ly + i * (fs + 4)}" font-family="{FONT}" '
                                  f'font-size="{fs}" text-anchor="{anchor}" fill="#374151">{escape(ln)}</text>')

    def render(self):
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}">'
                f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
                f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                f'<path d="M0,0 L10,5 L0,10 z" fill="#374151"/></marker></defs>'
                f'<rect width="{self.w}" height="{self.h}" fill="{self.bg}"/>')
        return head + "".join(self.parts) + "</svg>"


def write(svg, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg.render())
