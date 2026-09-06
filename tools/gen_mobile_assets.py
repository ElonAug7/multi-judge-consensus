#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成手机屏幕友好的竖版长图 PNG：
  assets/benchmark-mobile.png        幻觉拦截率对比（纵向条形）
  assets/architecture-mobile.png     一次审查的旅程（纵向 7 步管道）
1080 宽；中文渲染用 Noto Sans CJK（系统字体，仅本地生成用，不入库字体文件）。
"""
import os

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
CJK_B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
CJK_R = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
assert os.path.exists(CJK_B)


def f(size, bold=True):
    return ImageFont.truetype(CJK_B if bold else CJK_R, size, index=2)


INK = "#1d1d1f"
GRAY = "#86868b"
BG = "#f5f5f7"
BLUE = "#0a84ff"
PURPLE = "#5e5ce6"
GREEN = "#248a3d"
RED = "#ff3b30"
ORANGE = "#ff9500"
W = 1080


def rounded(d, box, radius, **kw):
    d.rounded_rectangle(box, radius=radius, **kw)


def draw_bar(d, x, y, w, h, pct, color, track="#e5e5ea"):
    rounded(d, [x, y, x + w, y + h], h // 2, fill=track)
    if pct > 0:
        fw = max(int(w * pct / 100), 8)
        rounded(d, [x, y, x + fw, y + h], h // 2, fill=color)


def benchmark_mobile():
    H = 1620
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # 头部
    d.text((64, 64), "🦉 幻觉拦截率对比", font=f(58), fill=INK)
    d.text((66, 148), "红队基准 v1 · 21 条对抗样本 · 真 API 实测", font=f(28, False), fill=GRAY)
    rows = [
        ("无审查 · 直接交付", "18/18 缺陷原样流出", 100.0, RED, "100%"),
        ("单模型自查（glm-4-plus）", "漏 2/18 —— 最强模型也会漏", 11.1, ORANGE, "11.1%"),
        ("🦉 MJC 全流程", "18/18 全拦 · 干净内容 0/3 误杀", 0.0, GREEN, "0%"),
    ]
    y = 240
    for title, note, pct, color, big in rows:
        rounded(d, [48, y, W - 48, y + 380], 28, fill="#ffffff")
        d.text((88, y + 46), title, font=f(40), fill=INK)
        w_pct = d.textlength(big, font=f(56))
        d.text((W - 88 - w_pct, y + 40), big, font=f(56), fill=color)
        draw_bar(d, 88, y + 160, W - 176, 40, pct, color)
        d.text((88, y + 240), note, font=f(28, False), fill=GRAY)
        if title.startswith("🦉"):
            rounded(d, [88, y + 300, 88 + 340, y + 348], 24, fill="#e6f7ee")
            d.text((104, y + 308), "零漏网 · 零误杀", font=f(24, False), fill=GREEN)
        y += 420
    # 尾部
    d.text((W // 2, H - 130), "github.com/ElonAug7/multi-judge-consensus", font=f(30, False), fill=BLUE, anchor="mm")
    d.text((W // 2, H - 84), "口径：构造缺陷集 · 最坏情况拦截能力 · 可复现（bench --set v1-full）", font=f(22, False), fill=GRAY, anchor="mm")
    img.save(os.path.join(OUT, "benchmark-mobile.png"))
    return img


def architecture_mobile():
    steps = [
        ("⓪ 确定性验证器", "日期 · 百分比 · 求和", "0 LLM 成本", BLUE, "#e8f2ff"),
        ("① 缓存查重", "同内容直接复用上次裁决", "0 调用", BLUE, "#eef3ff"),
        ("② 初筛", "最便宜模型先看一遍", "1 次 ≈ ¥0.002", BLUE, "#e8f2ff"),
        ("③ 委员会", "3 模型并行独立审查", "结构化意见", PURPLE, "#efe9ff"),
        ("④ 仲裁器", "纯规则投票，不用 LLM 裁决", "规则可解释", PURPLE, "#efe9ff"),
        ("⑤ 辩论", "互看意见 · 允许改判", "≤2 轮 · 共识即停", PURPLE, "#efe9ff"),
        ("⑥ 终局裁决", "verdict + tokens + 费用", "全量日志", GREEN, "#e6f7ee"),
    ]
    card_h, gap = 236, 92
    H = 300 + len(steps) * (card_h + gap) + 60
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((64, 64), "一次审查的旅程", font=f(56), fill=INK)
    d.text((66, 146), "每一步都清楚：谁在干什么 · 花多少 · 为什么", font=f(28, False), fill=GRAY)
    y = 230
    for i, (title, sub, tag, accent, bg) in enumerate(steps):
        rounded(d, [48, y, W - 48, y + card_h], 26, fill=bg, outline=accent, width=2)
        d.ellipse([84, y + 34, 84 + 60, y + 94], fill=accent)
        d.text((84 + 30, y + 64), str(i), font=f(30), fill="#ffffff", anchor="mm")
        d.text((172, y + 34), title, font=f(38), fill=INK)
        d.text((172, y + 96), sub, font=f(28, False), fill=GRAY)
        # 右侧标签
        tag_w = d.textlength(tag, font=f(24, False)) + 40
        rounded(d, [W - 88 - tag_w, y + 34, W - 88, y + 82], 24, fill="#ffffff")
        d.text((W - 108 - tag_w / 2, y + 58), tag, font=f(24, False), fill=accent, anchor="mm")
        y += card_h
        if i < len(steps) - 1:
            d.text((W // 2, y + gap // 2), "↓", font=f(36), fill=GRAY, anchor="mm")
            y += gap
    d.text((W // 2, H - 40), "只升不降：路由判错最多多花钱，绝不降精度", font=f(24, False), fill=GRAY, anchor="mm")
    img.save(os.path.join(OUT, "architecture-mobile.png"))
    return img


if __name__ == "__main__":
    benchmark_mobile()
    architecture_mobile()
    for f in ("benchmark-mobile.png", "architecture-mobile.png"):
        p = os.path.join(OUT, f)
        print(f, Image.open(p).size, os.path.getsize(p))
