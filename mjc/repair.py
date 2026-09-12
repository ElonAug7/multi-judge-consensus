#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · repair.py — 双生产者修订共识（零幻觉架构 · 修复环节第二道保险）

背景（2026-09-12 实测）：单一生产者的修订会引入新错误（csqa-02/07/17 类）。
规则：
  - 两位来自不同厂商的生产者，用同一《修订守则》+ 同一反馈，各自独立修订；
  - 归一化后一致 → 采纳（agreed）；
  - 不一致 → 安全方向：不采纳任何一版（保留原文），返回 disagreed（附两版供处置）；
  - 有效修订 < 2 路 → incomplete（保守不采纳）。

用法：dual_revise(task, prev, feedback, specs=None)
      → {"mode": "agreed|disagreed|incomplete|error", "applied": str, "revs": [...], "tokens": N}
"""
import re

from mjc import providers
from mjc.revision import build_messages

DEFAULT_SPECS = ("deepseek:deepseek-v4-flash", "glm:glm-4-plus")
_MAX_TOKENS = (12000, 16000)  # 推理模型：预算给足，防空响应


def _split(spec):
    p, _, m = spec.partition(":")
    return p, (m or None)


def _norm(text):
    t = text or ""
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[。．.，,；;：:、“”\"'（）()\[\]【】!！?？\-—_]", "", t)
    return t.lower()


def _equivalent(a, b):
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # 长文本允许极小差异（≥0.99 相似度）；短文本必须一致
    import difflib
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.99


def _call_one(spec, messages, timeout=120):
    provider, model = _split(spec)
    last_err = None
    for budget in _MAX_TOKENS:
        try:
            text = providers.chat(provider, messages, model=model, temperature=0.3,
                                  max_tokens=budget, timeout=timeout)
            if (text or "").strip():
                return {"spec": spec, "text": text.strip(), "error": None}
        except Exception as e:  # noqa
            last_err = str(e)[:160]
            break  # 认证类等错误直接记
    return {"spec": spec, "text": "", "error": last_err or "空响应"}


def available_producers(specs=None):
    """默认双厂生产者；按 key 过滤。"""
    specs = list(specs or DEFAULT_SPECS)
    out = []
    for s in specs:
        p, _ = _split(s)
        try:
            if providers.has_key(p):
                out.append(s)
        except Exception:
            continue
    return out


def dual_revise(task, prev, feedback, specs=None, timeout=120):
    """返回共识结果 dict（不抛出；基础设施错误 → mode=error）。"""
    specs = available_producers(specs)
    if len(specs) < 2:
        return {"mode": "error", "applied": prev, "revs": [], "tokens": 0,
                "note": "可用生产者不足 2 个（跨厂）"}
    messages = build_messages(task, prev, feedback)
    revs = [_call_one(s, messages, timeout=timeout) for s in specs]
    ok = [r for r in revs if r.get("text")]
    result = {"mode": "incomplete", "applied": prev, "revs": revs, "tokens": 0}
    if len(ok) < 2:
        result["note"] = "有效修订不足 2 路，保守保留原文"
        return result
    if all(_equivalent(ok[0]["text"], r["text"]) for r in ok[1:]):
        result["mode"] = "agreed"
        result["applied"] = ok[0]["text"]
    else:
        result["mode"] = "disagreed"
        result["note"] = "两版修订不一致 → 保留原文（防新幻觉）"
    return result


if __name__ == "__main__":
    print(__doc__)
