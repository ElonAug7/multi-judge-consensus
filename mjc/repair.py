#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · repair.py — 双生产者修订共识（零幻觉架构 · 修复环节第二道保险）

背景（2026-09-12 实测）：单一生产者的修订会引入新错误（csqa-02/07/17 类）。
规则：
  - 两位来自不同厂商的生产者，用同一《修订守则》+ 同一反馈，各自独立修订；
  - 归一化后一致 → 采纳（agreed/exact）；
  - v0.7.2 值级共识：跨模型措辞天然不同，"逐字近等"过于苛刻。改为比对数值集合：
      · value-set：两版数值集合相同且 ≠ 原文 → 采纳较保守（较短）一版
      · value-subset：一侧数值集合为另一侧真子集、且相对原文有改动 → 采纳子集侧
      · 纯文字改写（无数值变化）不自动采纳（无知识依据，防新幻觉）
      · 共识版本过短（< max(4, 40% 原文)）视为"清空式" → 不采纳
  - v0.8.0 安全门（C6 实测 csqa-13 误伤：双方一致删值 → 委员会误杀时无护栏）：
      · 值操作原则：只允许"具体值→具体值"的净替换；纯删除（丢值不加值/删光）与
        纯新增（加值不换值）一律阻断（drop-blocked / add-blocked）；
      · 盲重采样门（settings.repair.resample_gate，默认关）：值替换需在"看不见原答"
        的重复采样中获得多数支持（supported），否则阻断（resample-xxx）。
  - 未达共识 → 安全方向：不采纳任何一版（保留原文），返回 disagreed（附两版供处置）；
  - 有效修订 < 2 路 → incomplete（保守不采纳）。

用法：dual_revise(task, prev, feedback, specs=None)
      → {"mode": "agreed|disagreed|incomplete|error", "applied": str, "revs": [...],
         "agreement": "exact|value-set|value-subset|drop-blocked|add-blocked|resample-*",
         "values": {...}, "resample": {...}?, "tokens": N}
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


_VALUE_TRANS = str.maketrans("０１２３４５６７８９．", "0123456789.")


def _values(text):
    """抽取文本中的数值集合（年份/日期/金额/数量等）；全角数字先归一。"""
    t = (text or "").translate(_VALUE_TRANS)
    return set(re.findall(r"\d+(?:\.\d+)?", t))


def _too_gutted(orig, cand):
    """共识版本是否"清空式"过短（< max(4, 40% 原文)）——防把答案改成无信息量短句。"""
    return len(_norm(cand)) < max(4, 0.4 * len(_norm(orig)))


def _value_op_block(o, v):
    """v0.8.0 值操作原则：只允许"具体值→具体值"的净替换。
    返回 None（允许）或阻断原因：
      - drop-blocked：删光 或 只删不加（保留的是旧值子集）
      - add-blocked ：只加不换（没有旧值被丢弃）
    """
    if v == o:
        return None
    if not v:
        return "drop-blocked"
    if not (v - o):
        return "drop-blocked"
    if not (o - v):
        return "add-blocked"
    return None


def _resample_gate(task, old_vals, new_vals):
    """盲重采样门（settings.repair.resample_gate，默认关）。
    未启用/基础设施不具备 → None（不阻塞）；否则返回 resample.evaluate 结果。"""
    try:
        from mjc import settings as _settings
        cfg = ((_settings.load().get("repair") or {}).get("resample_gate") or {})
        if not cfg.get("enabled"):
            return None
        from mjc import resample
        r = resample.evaluate(task, old_vals, new_vals,
                              n=int(cfg.get("n", 3)), min_support=int(cfg.get("min_support", 2)))
        if not isinstance(r, dict) or r.get("verdict") in (None, "skipped"):
            return None
        return r
    except Exception:
        return None


def _block_note(reason):
    if reason == "drop-blocked":
        return "值删除被阻断（v0.8.0：仅允许具体值→具体值替换）"
    if reason == "add-blocked":
        return "值新增被阻断（v0.8.0：缺旧值替换依据）"
    if reason and reason.startswith("resample-"):
        return f"盲重采样未支持（{reason}）→ 保留原文"
    return "安全门阻断 → 保留原文"


def _safety_checks(task, prev, cand_text):
    """v0.8.0 安全门 → (ok, reason, extra)。
    1) 值操作原则；2) 值替换时过盲重采样门。"""
    o, v = _values(prev), _values(cand_text)
    block = _value_op_block(o, v)
    if block:
        return False, block, {}
    if v != o and (v - o):  # 值替换 → 重采样门
        gate = _resample_gate(task, o, v - o)
        if gate is not None and gate.get("verdict") != "supported":
            return False, f"resample-{gate.get('verdict')}", {
                "resample": {"verdict": gate.get("verdict"), "support": gate.get("support"),
                             "n": gate.get("n"), "model": gate.get("model")}}
        if gate is not None:
            return True, "", {"resample": {"verdict": "supported", "support": gate.get("support"),
                                             "n": gate.get("n"), "model": gate.get("model")}}
    return True, "", {}


def _consensus(orig, a, b):
    """值级共识（v0.7.2）→ (agreed, pick('a'|'b'), reason)。
    1) exact：归一化等价（原逻辑）
    2) value-set：两版数值集合相同且 ≠ 原文 → 采纳较短一版
    3) value-subset：一侧数值集合为另一侧真子集、且相对原文有改动 → 采纳子集侧（主张更少）
    其余 → 不采纳（no-consensus）
    """
    if _equivalent(a, b):
        return True, "a", "exact"
    o, va, vb = _values(orig), _values(a), _values(b)
    if va == vb and va != o:
        pick = "a" if len(_norm(a)) <= len(_norm(b)) else "b"
        return True, pick, "value-set"
    if va < vb and va != o and (o - va):
        return True, "a", "value-subset"
    if vb < va and vb != o and (o - vb):
        return True, "b", "value-subset"
    return False, None, "no-consensus"


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
    if len(ok) == 2:
        agreed, pick, reason = _consensus(prev, ok[0]["text"], ok[1]["text"])
        cand = ok[0] if pick != "b" else ok[1]
        result["agreement"] = reason
        result["values"] = {
            "orig": sorted(_values(prev)),
            "a": sorted(_values(ok[0]["text"])),
            "b": sorted(_values(ok[1]["text"])),
        }
        if agreed and not _too_gutted(prev, cand["text"]):
            ok2, why, extra = _safety_checks(task, prev, cand["text"])
            result.update(extra)
            if ok2:
                result["mode"] = "agreed"
                result["applied"] = cand["text"]
                if _equivalent(cand["text"], prev):
                    result["note"] = "共识=与原文无实质差异"
            else:
                result["mode"] = "disagreed"
                result["agreement"] = why
                result["note"] = _block_note(why)
        else:
            result["mode"] = "disagreed"
            if agreed:
                result["note"] = "共识版本过短（清空式）→ 保守保留原文"
            else:
                result["note"] = "两版修订未达值级共识 → 保留原文（防新幻觉）"
    elif all(_equivalent(ok[0]["text"], r["text"]) for r in ok[1:]):
        # >2 生产者：维持旧的严格等价路径（罕见），同样过安全门
        ok2, why, extra = _safety_checks(task, prev, ok[0]["text"])
        result.update(extra)
        if ok2:
            result["mode"] = "agreed"
            result["applied"] = ok[0]["text"]
            result["agreement"] = "exact"
        else:
            result["mode"] = "disagreed"
            result["agreement"] = why
            result["note"] = _block_note(why)
    else:
        result["mode"] = "disagreed"
        result["note"] = "多版修订不一致 → 保留原文（防新幻觉）"
    return result


if __name__ == "__main__":
    print(__doc__)
