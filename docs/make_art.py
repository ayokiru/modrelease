"""Draws the README banner (dark and light) and the social preview: python docs/make_art.py (Windows fonts)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent
FONTS = Path(r"C:\Windows\Fonts")
S = 2  # drawn at twice the size, so it stays sharp on high-DPI screens

THEMES = {
    "dark": dict(bg=(11, 17, 32), bg2=(15, 23, 42), dot=(30, 41, 59), title=(248, 250, 252), text=(203, 213, 225),
                 muted=(148, 163, 184), accent=(34, 197, 94), chip=(22, 32, 51), chip_line=(51, 65, 85),
                 term=(8, 12, 22), term_bar=(22, 30, 46), red=(248, 113, 113), yellow=(250, 204, 21)),
    "light": dict(bg=(248, 250, 252), bg2=(236, 241, 247), dot=(220, 227, 236), title=(15, 23, 42), text=(51, 65, 85),
                  muted=(100, 116, 139), accent=(21, 128, 61), chip=(255, 255, 255), chip_line=(203, 213, 225),
                  term=(15, 23, 42), term_bar=(30, 41, 59), red=(248, 113, 113), yellow=(250, 204, 21)),
}


def mono(size, weight="Regular"):
    f = ImageFont.truetype(str(FONTS / "CascadiaCode.ttf"), size * S)
    f.set_variation_by_name(weight)
    return f


def sans(size, bold=False):
    return ImageFont.truetype(str(FONTS / ("seguisb.ttf" if bold else "segoeui.ttf")), size * S)


def terminal(d: ImageDraw.ImageDraw, im: Image.Image, box, c):
    x0, y0, x1, y1 = [v * S for v in box]
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([x0, y0 + 18, x1, y1 + 18], 22 * S, fill=(0, 0, 0, 90))
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28)))
    d.rounded_rectangle([x0, y0, x1, y1], 18 * S, fill=c["term"])
    d.rounded_rectangle([x0, y0, x1, y0 + 34 * S], 18 * S, fill=c["term_bar"])
    d.rectangle([x0, y0 + 20 * S, x1, y0 + 34 * S], fill=c["term_bar"])
    for i, col in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        cx = x0 + (22 + i * 20) * S
        d.ellipse([cx - 6 * S, y0 + 11 * S, cx + 6 * S, y0 + 23 * S], fill=col)
    f, green, white, dim = mono(15), (74, 222, 128), (226, 232, 240), (148, 163, 184)
    lines = [
        [("$ ", green), ("modrelease pack", white)],
        [("  ! debug.log: contains 'jdoe'", c["red"])],
        [("  ! mod.json: contains a Discord webhook URL", c["red"])],
        [("  - SwordTool.exe: Nexus quarantines executables", c["yellow"])],
        [("2 problem(s), nothing written", c["red"])],
        [],
        [("$ ", green), ("modrelease pack", white)],
        [("CoolSwords-1.2.zip (14 files, 2,310 KB)", green)],
        [("sha256 4c4649b29deafaaf9cb2d65f3474e50f", dim)],
    ]
    y = y0 + 50 * S
    for parts in lines:
        x = x0 + 22 * S
        for text, col in parts:
            d.text((x, y), text, font=f, fill=col)
            x += f.getlength(text)
        y += (14 if not parts else 25) * S  # the blank line between the two runs is a half line


def banner(theme: str, w=1280, h=400, path=None):
    c = THEMES[theme]
    im = Image.new("RGBA", (w * S, h * S), c["bg"])
    d = ImageDraw.Draw(im)
    for y in range(h * S):  # soft vertical gradient
        t = y / (h * S)
        d.line([(0, y), (w * S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c["bg"], c["bg2"])))
    for gx in range(0, w * S, 28 * S):  # dot grid, faded toward the text side
        for gy in range(0, h * S, 28 * S):
            if gx > 560 * S:
                d.ellipse([gx, gy, gx + 2 * S, gy + 2 * S], fill=c["dot"])
    x, top = 72 * S, (h - 400) // 2 * S  # the content block is laid out for 400 px and centered in taller images
    d.text((x, top + 92 * S), "game mod release toolkit", font=mono(16, "SemiBold"), fill=c["accent"])
    d.text((x - 4 * S, top + 116 * S), "modrelease", font=mono(64, "Bold"), fill=c["title"])
    tag = sans(24)
    d.text((x, top + 206 * S), "Check, pack and ship game mods", font=tag, fill=c["text"])
    d.text((x, top + 240 * S), "without leaking who you are.", font=tag, fill=c["text"])
    cx, cy, cf = x, top + 300 * S, sans(15, bold=True)
    for chip in ("leak & secret scan", "reproducible zip", "clean media", "Nexus upload"):
        tw = cf.getlength(chip)
        d.rounded_rectangle([cx, cy, cx + tw + 28 * S, cy + 34 * S], 17 * S, fill=c["chip"], outline=c["chip_line"], width=S)
        d.text((cx + 14 * S, cy + 17 * S), chip, font=cf, fill=c["text"], anchor="lm")
        cx += tw + 38 * S
    pad = (h - 400) // 2
    terminal(d, im, (w - 540, pad + 56, w - 56, h - pad - 56), c)
    out = im.convert("RGB").resize((w * 2, h * 2) if S == 2 else (w, h))
    Image.frombytes(out.mode, out.size, out.tobytes()).save(path or OUT / f"banner-{theme}.png", optimize=True)


if __name__ == "__main__":
    banner("dark")
    banner("light")
    banner("dark", 1280, 640, OUT / "social-preview.png")
    print("written to", OUT)
