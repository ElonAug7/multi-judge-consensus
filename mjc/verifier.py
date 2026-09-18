#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · verifier.py — 确定性验证器（P0：快/准/省三角的规则层）
零 LLM、零成本、零延迟；只抓 100% 可验算的错误，**宁可不抓、绝不误报**：

  1. 日期差：'8月31日…9月5日，历时 4 天' / ISO 日期对 + 历时/共/间隔 N 天
  2. 百分比基数：'从 A 降到 B，节省/下降/提升 X%'（自动验算，容差 ±2.5pp）
  3. 显式求和：'18+12+20+25=85' / '= 75'（容差 ±0.01）
  4. stdlib membership: 'pandas is a Python stdlib' (checked via sys.stdlib_module_names, zero LLM)
  5. self-contradiction: same quantity stated as two different values ('4th digit is 9' vs '4th digit is 5')
  6. power/exponent: '2 to the 10th power = 1000' (recompute X**Y)
  7. pi constant: 'the Nth decimal of pi is D' (compare against pi's decimals)
  8. API existence: 'os.foobar()' (hasattr on safe-whitelist modules)

命中 → 直接产出 revise 裁决（跳过 LLM，全程 0 调用），并附在审查记录里。
歧义输入（缺基数、跨 2 月无年份、非显式算式）一律跳过——验证器的信誉 > 覆盖率。
"""
import datetime
import importlib
import re
import sys

MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _is_leap(y):
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)


def _days_in_month(m, y=None):
    if m == 2:
        return 29 if (y is not None and _is_leap(y)) else 28
    return MONTH_DAYS[m - 1]


def _same_year_days(m1, d1, m2, d2, y=None):
    """同年内 m1/d1 → m2/d2 的自然日差（m2>=m1）。跨 2 月且年份未知 → None(歧义)。"""
    if m2 < m1 or (m2 == m1 and d2 < d1):
        return None
    if m1 <= 2 <= m2 and y is None and (m1 != m2 or m1 == 2):
        # 跨度含 2 月且无年份 → 平/闰歧义，跳过
        if m1 < 2 < m2 or m1 == 2 or m2 == 2:
            if not (m1 == m2 == 2 and d1 == d2):
                return None
    if m2 == m1:
        return d2 - d1
    days = _days_in_month(m1, y) - d1
    for m in range(m1 + 1, m2):
        days += _days_in_month(m, y)
    days += d2
    return days


def _round1(x):
    return round(x, 1)


DATE_CN = re.compile(
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"           # 起点
    r".{0,24}?"                                    # 中间（克制长度）
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"[^。；\n]{0,14}?(?:历时|共|间隔|相差)\s*(\d{1,4})\s*(?:天|日)")
PCT = re.compile(
    r"从\s*(\d+(?:\.\d+)?)\s*[^。；\n]{0,16}?"
    r"(?:降到|降至|升到|升至|提升到|提高到|变成|减到|涨到)\s*(\d+(?:\.\d+)?)\s*[^。；\n]{0,16}?"
    r"(?:节省|节约|下降|降低|减少|提升|提高|增长|上涨|增加)\s*(?:约|大概|近)?\s*(\d+(?:\.\d+)?)\s*%")
SUM_EXPR = re.compile(r"\d+(?:\.\d+)?(?:\s*[+＋]\s*\d+(?:\.\d+)?){1,8}\s*[=＝]\s*\d+(?:\.\d+)?")
DATE_ISO = re.compile(
    r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"
    r".{0,24}?"
    r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"
    r"[^。；\n]{0,14}?(?:历时|共|间隔|相差)\s*(\d{1,4})\s*(?:天|日)")

# stdlib membership: 'pandas is a Python stdlib' / 'requests belongs to the stdlib'
STDLIB_CLAIM = re.compile(
    r"([a-zA-Z_]\w*)\s*(?:是|属于|为)\s*(?:Python\s*(?:的)?\s*)?(?:标准库|内置模块|内置库|标准模块)")
STDLIB_NEG = re.compile(
    r"([a-zA-Z_]\w*)\s*(?:不是|不属于|并非)\s*(?:Python\s*(?:的)?\s*)?(?:标准库|内置模块|内置库|标准模块)")

# self-contradiction: 'Nth digit is A' vs 'Nth digit is B' (A != B)
DIGIT_CLAIM = re.compile(r"第\s*(\d+)\s*位\s*(?:小数|数字|位)?\s*(?:是|=|为)\s*(\d+)")

# power/exponent: '2 to the 10th power equals 1000'
POWER_EXPR = re.compile(r"(\d+(?:\.\d+)?)\s*的\s*(\d+)\s*(?:次方|次幂)\s*(?:等于|=|＝)\s*(\d+(?:\.\d+)?)")

# decimals of pi (known constant, avoid depending on math)
PI_DECIMALS = "14159265358979323846"
PI_DIGIT = re.compile(r"(?:圆周率|π|pi)\s*[^。；\n]{0,24}?第\s*(\d+)\s*位\s*(?:小数|数字)?\s*(?:是|=|为)\s*(\d+)")

# API existence: 'os.foobar()' call form (only safe-whitelist modules, avoid import side effects)
API_CALL = re.compile(r"([a-z_]\w*)\.([a-zA-Z_]\w*)\s*\(")
SAFE_STDLIB = {
    "os", "sys", "re", "json", "math", "datetime", "time", "random", "string",
    "csv", "collections", "itertools", "functools", "pathlib", "io", "logging",
    "argparse", "dataclasses", "enum", "typing", "decimal", "fractions",
    "statistics", "copy", "heapq", "bisect", "uuid", "base64", "hashlib",
    "textwrap", "shlex", "struct", "tempfile", "glob", "fnmatch", "unicodedata",
    "sqlite3", "html", "urllib", "socket", "subprocess",
}
_module_cache = {}



def _mk(loc, desc, sug, typ="factual_error"):
    return {"type": typ, "location": loc, "description": desc, "suggestion": sug, "certain": True}


def check_dates(text):
    """中文日期跨度 & ISO 日期跨度。返回 issues"""
    out = []
    for m in DATE_CN.finditer(text):
        try:
            m1, d1, m2, d2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            claim = int(m.group(5))
        except ValueError:
            continue
        if not (1 <= m1 <= 12 and 1 <= m2 <= 12 and 1 <= d1 <= 31 and 1 <= d2 <= 31):
            continue
        if d1 > _days_in_month(m1) or d2 > _days_in_month(m2):
            continue
        got = _same_year_days(m1, d1, m2, d2)
        if got is None:
            continue  # 歧义（跨 2 月无年份等）
        if got != claim and not _quoted(m, text):
            span = f"{m.group(1)}月{m.group(2)}日 → {m.group(3)}月{m.group(4)}日"
            out.append(_mk(span, f"日期跨度算错：{span} 应为 {got} 天，文中写 {claim} 天",
                           f"改为 {got} 天"))
    for m in DATE_ISO.finditer(text):
        try:
            y1, m1, d1, y2, m2, d2 = (int(m.group(i)) for i in range(1, 7))
            claim = int(m.group(7))
        except ValueError:
            continue
        try:
            a, b = datetime.date(y1, m1, d1), datetime.date(y2, m2, d2)
        except ValueError:
            continue
        got = (b - a).days
        if got != claim and not _quoted(m, text):
            span = f"{m.group(1)}-{m.group(2)}-{m.group(3)} → {m.group(4)}-{m.group(5)}-{m.group(6)}"
            out.append(_mk(span, f"日期跨度算错：{span} 应为 {got} 天，文中写 {claim} 天",
                           f"改为 {got} 天"))
    return out


def check_percent(text):
    out = []
    for m in PCT.finditer(text):
        try:
            old, new = float(m.group(1)), float(m.group(2))
            claim = float(m.group(3))
        except ValueError:
            continue
        if old == 0:
            continue
        delta = (new - old) / old * 100.0
        # 下降类词 → 期望负变化绝对值；提升类 → 正
        verb = m.group(0)
        down = any(k in verb for k in ("节省", "节约", "下降", "降低", "减少", "降到", "降至", "减到"))
        expect = -delta if down else delta
        if expect < 0:
            expect = -expect  # 说的都是幅度
        if abs(expect - claim) > 2.5 and not _quoted(m, text):
            out.append(_mk(
                f"…{m.group(0)[:60]}…",
                f"百分比基数算错：从 {m.group(1)} 变到 {m.group(2)}，"
                f"{'降' if down else '升'}幅应为 {expect:.1f}%（{old}→{new}），文中写 {claim:.1f}%",
                f"改为 {expect:.1f}%（或补充你的计算口径）"))
    return out


def check_sum(text):
    out = []
    for m in SUM_EXPR.finditer(text):
        expr = m.group(0).replace("＋", "+").replace("＝", "=")
        left, _, right = expr.rpartition("=")
        try:
            nums = [float(x) for x in left.split("+")]
            claim = float(right.strip())
        except ValueError:
            continue
        got = round(sum(nums), 2)
        if abs(got - claim) > 0.01 and not _quoted(m, text):
            out.append(_mk(expr[:70], f"求和算错：{' + '.join(str(n) for n in nums)} = {got}，文中写 {claim:g}",
                           f"改为 {got:g}"))
    return out


# sys.stdlib_module_names only exists on Python 3.10+; skip the check on older versions
STDLIB_MODULES = getattr(sys, "stdlib_module_names", None)


def check_stdlib(text):
    """Stdlib membership check: 'X is / is not a Python stdlib'. Looked up via sys.stdlib_module_names (Python 3.10+)."""
    if STDLIB_MODULES is None:
        return []  # Python < 3.10: no stdlib_module_names, skip the check
    out = []
    for m in STDLIB_CLAIM.finditer(text):
        name = m.group(1)
        if _quoted(m, text):
            continue
        if name not in STDLIB_MODULES:
            out.append(_mk(name, f"{name} is not a Python stdlib (third-party, needs pip install), but the text claims it is",
                           f"{name} is not part of the stdlib, install it separately"))
    for m in STDLIB_NEG.finditer(text):
        name = m.group(1)
        if _quoted(m, text):
            continue
        if name in STDLIB_MODULES:
            out.append(_mk(name, f"{name} is a Python stdlib, but the text claims it is not",
                           f"{name} is part of the stdlib, no install needed"))
    return out


def check_self_contradiction(text):
    """Self-contradiction check: the same quantity (Nth digit) stated as two different values. The contradiction itself is the defect, so _quoted is not applied."""
    out = []
    seen = {}
    for m in DIGIT_CLAIM.finditer(text):
        pos, digit = m.group(1), m.group(2)
        if pos in seen and seen[pos] != digit:
            out.append(_mk(f"digit {pos}", f"self-contradiction: digit {pos} is stated as both {seen[pos]} and {digit}",
                           "unify to a single definite value", typ="logical_error"))
            seen[pos] = digit  # keep tracking subsequent mentions
        else:
            seen[pos] = digit
    return out


def check_power(text):
    """Power/exponent check: 'X to the Yth power = Z'. Recompute X**Y."""
    out = []
    for m in POWER_EXPR.finditer(text):
        try:
            base, exp, claim = float(m.group(1)), int(m.group(2)), float(m.group(3))
        except ValueError:
            continue
        if exp > 64:  # avoid overflow on huge exponents
            continue
        got = base ** exp
        if abs(got - claim) > 1e-6 * max(1.0, abs(got)) and not _quoted(m, text):
            out.append(_mk(m.group(0)[:60], f"{base:g} to the {exp}th power = {got:g}, but the text says {claim:g}",
                           f"change to {got:g}"))
    return out


def check_pi(text):
    """Pi constant check: 'the Nth decimal of pi is D'. Compare against pi's decimals."""
    out = []
    for m in PI_DIGIT.finditer(text):
        try:
            pos, d = int(m.group(1)), int(m.group(2))
        except ValueError:
            continue
        if not (1 <= pos <= len(PI_DECIMALS)):
            continue
        actual = PI_DECIMALS[pos - 1]
        if actual != str(d) and not _quoted(m, text):
            out.append(_mk(f"digit {pos}", f"pi's {pos}th decimal is {actual}, but the text says {d}",
                           f"change to {actual}"))
    return out


def _has_attr(mod, attr):
    """Whether a safe-whitelist module has this attribute. Returns True/False; non-whitelist or import-fail -> None (unknown, no verdict)."""
    if mod not in SAFE_STDLIB:
        return None
    if mod not in _module_cache:
        try:
            _module_cache[mod] = importlib.import_module(mod)
        except Exception:
            _module_cache[mod] = None
    module = _module_cache[mod]
    if module is None:
        return None
    return hasattr(module, attr)


def check_api_exists(text):
    """API existence check: 'os.foobar()' call form. Only checks safe-whitelist stdlib modules, prefer miss over false-positive."""
    out = []
    for m in API_CALL.finditer(text):
        mod, attr = m.group(1), m.group(2)
        if _quoted(m, text):
            continue
        exists = _has_attr(mod, attr)
        if exists is False:
            out.append(_mk(f"{mod}.{attr}",
                           f"{mod} stdlib has no {attr} function/attribute (suspected fabricated API)",
                           f"verify {mod}'s real API"))
    return out


QUOTE_MARKERS = ("文中写", "原文写", "写的是", "写了", "应为", "应改为", "应该为", "而不是", "误写", "写成", "按说", "实际应为")


def _quoted(m, text, pad=40):
    """命中片段附近出现转述/纠错标记（文中写 X、应为 Y）→ 视为引用别人错误，跳过（防语境误报）"""
    lo = max(0, m.start() - pad)
    hi = min(len(text), m.end() + pad)
    win = text[lo:hi]
    return any(k in win for k in QUOTE_MARKERS)


def verify(text):
    """入口：text → issues（全部 certain）。无歧义保证：只返回能 100% 验算的错误。"""
    if not text:
        return []
    return (check_dates(text) + check_percent(text) + check_sum(text)
            + check_stdlib(text) + check_self_contradiction(text)
            + check_power(text) + check_pi(text) + check_api_exists(text))


if __name__ == "__main__":
    import sys
    demo = ("改动 8 月 31 日提交，9 月 5 日完成验收，历时 4 天；"
            "每次审查平均 API 调用从 3.2 次降到 0.9 次，节省约 28%；"
            "采购合计 18+12+20+25=85 万元。")
    print(json_issues := None) if False else None
    import json
    print(json.dumps(verify(demo), ensure_ascii=False, indent=1))
