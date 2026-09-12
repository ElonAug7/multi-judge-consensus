#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · resample.py — 盲重采样支持门（独立性工程 ③ · 采样一致性风险分）

理论：Self-Consistency (Wang et al. 2022, arXiv:2203.11171)；SelfCheckGPT (Manakul et al. 2023)；
      语义熵 (Farquhar et al., Nature 2024)。

动机（MJC 实测）：修复提案的"值替换"（如 1995→1997）可能源于单模型偏差或错觉。
在采纳前加一道独立证据：让模型**在看不到原答案与提案的情况下**，对同一问题独立重答
N 次（temp≈0.7），按"对提案新值的支持 / 对旧值的复现"判定：
  - supported   ：≥k 次重采样含全部新值且不含旧值 → 允许采纳
  - conflict    ：≥k 次重采样仍复现旧值 → 阻断替换（保留原文）
  - inconclusive：其余 → 上层保守处理（默认阻断值替换）

边界：只用于"值替换"类修订的准入；软化/改写类仍走双生产者共识。
      （Huang et al. 2023 "LLMs Cannot Self-Correct Reasoning Yet"：无外部反馈的自纠有限，
       故本模块是"证据补充"而非万能——与知识源/仲裁并用。）

用法：
    from mjc import resample
    r = resample.evaluate(task, old_values={"1995"}, new_values={"1997"}, spec=None, n=3)
    # → {"verdict": "supported|conflict|inconclusive|skipped", "support": 2, "n": 3,
    #    "calls": 3, "resample_values": [["1997"], ["1997","2006"], ["1997"]],
    #    "model": "deepseek:deepseek-v4-flash"}
"""
from mjc import providers

DEFAULT_SPECS = ("deepseek:deepseek-v4-flash", "glm:glm-4-flash", "glm:glm-4-plus")
N_DEFAULT = 3
TEMP = 0.7
MIN_SUPPORT = 2

PROMPT = """请独立、直接地回答下面的问题（不要参考任何已有回答或评论）。
若答案包含具体数字/年份/人名/地名等关键信息，请给出你确信的最简答案，不要解释。
【问题】
{task}
只输出答案本身。"""


def _values(text):
    """数值集合抽取（与 repair 共用同一实现）。"""
    from mjc.repair import _values as V
    return V(text)


def _has_new_and_no_old(new, old, vals):
    """v0.8.0：多值替换时不要求全部新值命中（避免混合替换误杀）——
    命中任一新值且不含旧值即算一条支持。"""
    s = set(vals)
    return bool(new) and bool(new & s) and not (old & s)


def _pick_spec(spec=None):
    if spec:
        p = spec.split(":", 1)[0]
        try:
            return spec if providers.has_key(p) else None
        except Exception:
            return None
    for s in DEFAULT_SPECS:
        p = s.split(":", 1)[0]
        try:
            if providers.has_key(p):
                return s
        except Exception:
            continue
    return None


def evaluate(task, old_values, new_values, spec=None, n=N_DEFAULT, timeout=60,
             min_support=MIN_SUPPORT):
    """盲重采样 → 支持度判定。不抛出；基础设施错误计入 errors 并降级。"""
    spec = _pick_spec(spec)
    if not spec:
        return {"verdict": "skipped", "note": "无可用模型", "calls": 0, "n": 0}
    p, _, model = spec.partition(":")
    old = {str(v) for v in (old_values or [])}
    new = {str(v) for v in (new_values or [])}
    vals_list, errors = [], 0
    for _ in range(max(1, int(n))):
        try:
            text = providers.chat(
                p, [{"role": "user", "content": PROMPT.format(task=(task or "")[:800])}],
                model=(model or None), temperature=TEMP, max_tokens=800, timeout=timeout)
            vals_list.append(sorted(_values(text or "")))
        except Exception:  # noqa
            errors += 1
            vals_list.append(None)
    ok = [v for v in vals_list if v is not None]
    if not ok:
        return {"verdict": "skipped", "note": "全部重采样失败", "calls": len(vals_list),
                "n": 0, "errors": errors, "model": spec}
    support = sum(1 for v in ok if _has_new_and_no_old(new, old, v))
    conflict = sum(1 for v in ok if old and (old & set(v)))
    verdict = "inconclusive"
    if support >= min_support:
        verdict = "supported"
    elif conflict >= min_support:
        verdict = "conflict"
    return {"verdict": verdict, "support": support, "conflict": conflict, "n": len(ok),
            "model": spec, "resample_values": vals_list, "calls": len(vals_list),
            "errors": errors}


if __name__ == "__main__":
    import json
    import sys
    args = sys.argv[1:]
    task = args[0] if args else "测试问题"
    r = evaluate(task, old_values={"1995"}, new_values={"1997"})
    print(json.dumps(r, ensure_ascii=False))
