#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 MJC 项目视觉资产（SVG + PNG 同源坐标）：
  assets/logo.svg / logo.png            方徽章（三审查员 + 对勾）
  assets/logo-wide.svg / logo-wide.png  横向字标（徽章 + Multi-Judge Consensus）
  assets/architecture.svg / .png        审查流水线架构图
  assets/og-social.svg / .png           开源社交分享卡 1280x640
"""
import os

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

BLUE = "#0a84ff"
PURPLE = "#5e5ce6"
INK = "#1d1d1f"
GRAY = "#86868b"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
assert os.path.exists(FONT_B), "缺 DejaVu 字体"


def font(sz, bold=True):
    return ImageFont.truetype(FONT_B if bold else FONT, sz)


# ---------- 方徽章 ----------
def badge_svg(size=256):
    r = size * 0.22
    s = size
    cx = [0.42, 0.58, 0.50]
    cy = [0.38, 0.38, 0.60]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" width="{s}" height="{s}">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{BLUE}"/><stop offset="1" stop-color="{PURPLE}"/></linearGradient></defs>
<rect width="{s}" height="{s}" rx="{s*0.22}" fill="url(#g)"/>
<circle cx="{s*cx[0]}" cy="{s*cy[0]}" r="{r}" fill="#ffffff" fill-opacity="0.35"/>
<circle cx="{s*cx[1]}" cy="{s*cy[1]}" r="{r}" fill="#ffffff" fill-opacity="0.35"/>
<circle cx="{s*cx[2]}" cy="{s*cy[2]}" r="{r}" fill="#ffffff" fill-opacity="0.55"/>
<path d="M {s*0.34} {s*0.52} L {s*0.46} {s*0.64} L {s*0.68} {s*0.40}" fill="none" stroke="#ffffff" stroke-width="{s*0.055}" stroke-linecap="round" stroke-linejoin="round"/>
</svg>'''


def badge_png(size=512):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    s = size
    from PIL import ImageDraw as ID
    def lerp(c1, c2, t):
        return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))
    def hex2rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    b1, b2 = hex2rgb(BLUE), hex2rgb(PURPLE)
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        gd.line([(0, y), (s, y)], fill=lerp(b1, b2, y / s) + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s, s], radius=int(s * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    r = s * 0.21
    for cx, cy, a in ((0.42, 0.38, 90), (0.58, 0.38, 90), (0.50, 0.60, 140)):
        ov = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(ov).ellipse([s*cx - r, s*cy - r, s*cx + r, s*cy + r], fill=(255, 255, 255, a))
        img.alpha_composite(ov)
    d.line([(s*0.34, s*0.52), (s*0.46, s*0.64), (s*0.68, s*0.40)], fill=(255, 255, 255, 255),
           width=int(s * 0.055), joint="curve")
    img = img.resize((256, 256), Image.LANCZOS)
    img.save(os.path.join(OUT, "logo.png"))
    return img


# ---------- 横向字标 ----------
def wide_svg():
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 128" width="560" height="128">
<rect width="128" height="128" rx="28" fill="url(#gw)"/>
<defs><linearGradient id="gw" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{BLUE}"/><stop offset="1" stop-color="{PURPLE}"/></linearGradient></defs>
<circle cx="54" cy="48" r="27" fill="#fff" fill-opacity="0.35"/>
<circle cx="74" cy="48" r="27" fill="#fff" fill-opacity="0.35"/>
<circle cx="64" cy="76" r="27" fill="#fff" fill-opacity="0.55"/>
<path d="M44 66 L59 82 L86 51" fill="none" stroke="#fff" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
<text x="152" y="62" font-family="-apple-system,Segoe UI,DejaVu Sans,sans-serif" font-size="30" font-weight="700" fill="{INK}">Multi-Judge Consensus</text>
<text x="152" y="92" font-family="-apple-system,Segoe UI,DejaVu Sans,sans-serif" font-size="15" fill="{GRAY}">MJC · cross-vendor LLM review committee</text>
</svg>'''


def wide_png(w=1120, h=256):
    img = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    badge = Image.open(os.path.join(OUT, "logo.png")).resize((int(h * 0.92), int(h * 0.92)), Image.LANCZOS)
    img.alpha_composite(badge, (int(h * 0.04), int(h * 0.04)))
    d = ImageDraw.Draw(img)
    x0 = int(h * 1.12)
    d.text((x0, int(h * 0.30)), "Multi-Judge Consensus", font=font(int(h * 0.24)), fill=INK)
    d.text((x0, int(h * 0.62)), "MJC · cross-vendor LLM review committee", font=font(int(h * 0.105), bold=False), fill=GRAY)
    img.save(os.path.join(OUT, "logo-wide.png"))
    return img


# ---------- 架构流程图 ----------
STEPS = [
    ("⓪ Verifier", "dates · percentages · sums\n0 LLM cost", "#e8f2ff", BLUE),
    ("① Cache", "identical content\n0 calls", "#eef3ff", BLUE),
    ("② Screen", "glm-4-flash\npass + conf → done", "#e8f2ff", BLUE),
    ("③ Committee", "3 models in parallel\nstructured opinions", "#efe9ff", PURPLE),
    ("④ Arbiter", "rule-based vote\nno LLM judge", "#efe9ff", PURPLE),
    ("⑤ Debate", "≤ 2 rounds\nsee others · may change", "#efe9ff", PURPLE),
    ("⑥ Verdict", "verdict + tokens + cost\nfully logged", "#e6f7ee", "#248a3d"),
]


def architecture_svg():
    W, H = 1500, 560
    parts = []
    # 上排 4，下排 3
    for i, (title, sub, bg, accent) in enumerate(STEPS):
        row, col = divmod(i, 4)
        x, y = 40 + col * 370, 60 + row * 270
        parts.append(f'''<rect x="{x}" y="{y}" width="330" height="210" rx="20" fill="{bg}" stroke="{accent}" stroke-opacity="0.35" stroke-width="2"/>
<text x="{x+165}" y="{y+52}" font-family="DejaVu Sans,sans-serif" font-size="24" font-weight="700" fill="{INK}" text-anchor="middle">{title}</text>
<text x="{x+165}" y="{y+90}" font-family="DejaVu Sans,sans-serif" font-size="15" fill="{GRAY}" text-anchor="middle">{sub.split(chr(10))[0]}</text>
<text x="{x+165}" y="{y+118}" font-family="DejaVu Sans,sans-serif" font-size="15" fill="{GRAY}" text-anchor="middle">{sub.split(chr(10))[1]}</text>''')
        if col < 3:
            parts.append(f'<path d="M {x+330} {y+105} L {x+362} {y+105}" stroke="{GRAY}" stroke-width="3" marker-end="url(#arr)"/>')
        if col == 3 and row == 0:
            parts.append(f'<path d="M {40+3*370+165} {y+210} L {40+3*370+165} {y+242} L {40+2*370+165} {y+242} L {40+2*370+165} {y+268}" fill="none" stroke="{GRAY}" stroke-width="3" marker-end="url(#arr)"/>')
    parts.append(f'''<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="{GRAY}"/></marker></defs>''')
    parts.append(f'<text x="40" y="{H-18}" font-family="DejaVu Sans,sans-serif" font-size="14" fill="{GRAY}">MJC pipeline — deterministic checks first, escalation is monotonic (never downgrades precision)</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">' + "".join(parts) + "</svg>"


def architecture_png(w=1500, h=560):
    img = Image.new("RGB", (w, h), "#f5f5f7")
    d = ImageDraw.Draw(img)
    for i, (title, sub, bg, accent) in enumerate(STEPS):
        row, col = divmod(i, 4)
        x, y = 40 + col * 370, 60 + row * 270
        d.rounded_rectangle([x, y, x + 330, y + 210], radius=20, fill=bg, outline=accent, width=2)
        d.text((x + 165, y + 40), title, font=font(24), fill=INK, anchor="ma")
        lines = sub.split("\n")
        d.text((x + 165, y + 92), lines[0], font=font(16, bold=False), fill=GRAY, anchor="ma")
        d.text((x + 165, y + 122), lines[1], font=font(16, bold=False), fill=GRAY, anchor="ma")
        if col < 3:
            d.line([(x + 330, y + 105), (x + 366, y + 105)], fill=GRAY, width=3)
            d.polygon([(x + 370, y + 105), (x + 356, y + 97), (x + 356, y + 113)], fill=GRAY)
        if col == 3 and row == 0:
            pts = [(40 + 3*370 + 165, y + 210), (40 + 3*370 + 165, y + 242),
                   (40 + 2*370 + 165, y + 242), (40 + 2*370 + 165, y + 266)]
            d.line(pts, fill=GRAY, width=3)
            d.polygon([(pts[-1][0], pts[-1][1] + 8), (pts[-1][0] - 8, pts[-1][1]), (pts[-1][0] + 8, pts[-1][1])], fill=GRAY)
    d.text((40, h - 24), "MJC pipeline — deterministic checks first, escalation is monotonic (never downgrades precision)",
           font=font(14, bold=False), fill=GRAY)
    img.save(os.path.join(OUT, "architecture.png"))
    return img


# ---------- 社交分享卡 ----------
def og_png(w=1280, h=640):
    from PIL import ImageDraw as ID
    def hex2rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    b1, b2 = hex2rgb(BLUE), hex2rgb(PURPLE)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        c = tuple(int(a + (bb - a) * t) for a, bb in zip(b1, b2))
        d.line([(0, y), (w, y)], fill=c)
    badge = Image.open(os.path.join(OUT, "logo.png")).resize((220, 220), Image.LANCZOS)
    img.paste(badge, (72, 90), badge)
    d.text((72, 340), "Multi-Judge Consensus", font=font(54), fill="#ffffff")
    d.text((72, 420), "A cross-vendor LLM committee reviews your agent output before it ships.", font=font(26, bold=False), fill="#dbe6ff")
    chips = ["Recall 1.0 on red-team bench", "0 false kills on clean content", "0 third-party dependencies"]
    x = 72
    for c in chips:
        d.rounded_rectangle([x, 490, x + 30 + int(len(c) * 14.2), 546], radius=27, fill=(255, 255, 255, 40))
        d.text((x + 30, 518), c, font=font(21, bold=False), fill="#ffffff", anchor="lm")
        x += 40 + int(len(c) * 14.2)
    d.text((72, 590), "github.com/ElonAug7/multi-judge-consensus", font=font(22, bold=False), fill="#c9d8ff")
    img.save(os.path.join(OUT, "og-social.png"))
    return img


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, "logo.svg"), "w", encoding="utf-8").write(badge_svg())
    badge_png()
    open(os.path.join(OUT, "logo-wide.svg"), "w", encoding="utf-8").write(wide_svg())
    wide_png()
    open(os.path.join(OUT, "architecture.svg"), "w", encoding="utf-8").write(architecture_svg())
    architecture_png()
    og_png()
    for f in sorted(os.listdir(OUT)):
        print(f, os.path.getsize(os.path.join(OUT, f)))
    print("assets generated →", OUT)
