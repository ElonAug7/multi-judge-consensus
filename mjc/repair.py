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
  - v0.9.0 知识证据门（settings.repair.evidence_gate，默认关 · opt-in）：
      · 值替换在盲重采样"未支持"（inconclusive/skipped）或重采样门未启用时，可由外部
        检索证据放行：查询 = 任务（截断 ≤80 字）+ 新增值 → knowledge.fetch_evidence；
        片段中"含新值且不含旧值"数 ≥ min_snippets → 放行，否则阻断（resample-evidence-insufficient）；
      · conflict 不可被证据覆盖（直接阻断）；检索异常保守阻断（resample-evidence-error）；
        两门都关时行为与此前完全一致（默认放行）。
  - v0.9.1 证据检索加固：知识证据门默认改用多后端合并检索（settings.repair.evidence_gate
      .merge_backends，默认开）——累积去重 + 空结果重试 + 失败降级，消除抓取后端波动
      导致的证据误判（csqa-07 复现：首调某后端瞬时空结果即降级误判）。
  - 未达共识 → 安全方向：不采纳任何一版（保留原文），返回 disagreed（附两版供处置）；
  - 有效修订 < 2 路 → incomplete（保守不采纳）。

用法：dual_revise(task, prev, feedback, specs=None)
      → {"mode": "agreed|disagreed|incomplete|error", "applied": str, "revs": [...],
         "agreement": "exact|value-set|value-subset|drop-blocked|add-blocked|resample-*|resample-evidence-*",
         "values": {...}, "resample": {...}?, "evidence": {...}?, "tokens": N}
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
    未启用/基础设施不具备（skipped）→ None（不阻塞，链上继续交给知识证据门）；
    否则返回 resample.evaluate 结果（supported/conflict/inconclusive）。"""
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


def _evidence_query(task, new_vals):
    """证据检索查询：任务截断 ≤80 字 + 新增值（空格分隔；总长 ≤120）。"""
    t = re.sub(r"\s+", " ", (task or "")).strip()[:80]
    vals = " ".join(sorted(str(v) for v in (new_vals or [])))
    q = (t + (" " + vals if vals else "")).strip()
    return q[:120]


def _snippet_supports(text, old_vals, new_vals):
    """片段是否支持"新值"：含任一新增值且不含旧值（与 resample._has_new_and_no_old 同语义）。"""
    s = str(text or "")
    if not s:
        return False
    if not any(str(v) in s for v in (new_vals or [])):
        return False
    return not any(str(v) in s for v in (old_vals or []))


def evidence_check(task, old_vals, new_vals, min_snippets=2, merge=True):
    """执行一次证据检索并统计支持度（不读开关；知识证据门、CLI 调试与演示共用）。
    merge=True（默认，v0.9.1）走多后端合并检索；merge=False 保持旧「首个非空后端」路径。
    返回 {"verdict": supported|insufficient|error, "support", "n_snippets", "min_snippets",
          "query", "backend", "supporting": [{title,url,excerpt}...]}；不抛出。
    基础设施异常 → verdict=error（调用方需保守处理，不得放行）。"""
    try:
        ns = max(1, int(min_snippets))
    except Exception:
        ns = 2
    query = _evidence_query(task, new_vals)
    try:
        from mjc import knowledge
        res = knowledge.fetch_evidence(query, merge=merge)
    except Exception as e:  # noqa：基础设施异常，保守（error ≠ 支持）
        return {"verdict": "error", "support": 0, "n_snippets": 0, "min_snippets": ns,
                "query": query, "note": str(e)[:160]}
    snippets = list((res or {}).get("snippets") or [])
    support, samples = 0, []
    for sn in snippets:
        text = " ".join(str(sn.get(k) or "") for k in ("title", "text"))
        if _snippet_supports(text, old_vals, new_vals):
            support += 1
            if len(samples) < 3:  # 取证样例：最多 3 条
                samples.append({"title": str(sn.get("title") or "")[:60],
                                "url": str(sn.get("url") or "")[:120],
                                "excerpt": str(sn.get("text") or "")[:80]})
    return {"verdict": "supported" if support >= ns else "insufficient",
            "support": support, "n_snippets": len(snippets), "min_snippets": ns,
            "query": query, "backend": (res or {}).get("backend"), "supporting": samples}


def _evidence_gate(task, old_vals, new_vals):
    """知识证据门（settings.repair.evidence_gate，默认关 · v0.9.0 引入 / v0.9.1 合并检索）。
    未启用/配置不可读 → None（不引入约束）；启用 → evidence_check 结果（默认 merge=True，
    可被 merge_backends=false 关回旧单后端路径；检索异常 → verdict=error，上层保守阻断）。"""
    try:
        from mjc import settings as _settings
        cfg = ((_settings.load().get("repair") or {}).get("evidence_gate") or {})
        if not cfg.get("enabled"):
            return None
    except Exception:
        return None
    return evidence_check(task, old_vals, new_vals, min_snippets=cfg.get("min_snippets", 2),
                          merge=bool(cfg.get("merge_backends", True)))


def _block_note(reason):
    if reason == "drop-blocked":
        return "值删除被阻断（v0.8.0：仅允许具体值→具体值替换）"
    if reason == "add-blocked":
        return "值新增被阻断（v0.8.0：缺旧值替换依据）"
    if reason and reason.startswith("resample-evidence-"):
        if reason.endswith("error"):
            return "知识证据门异常（保守阻断）→ 保留原文"
        return "外部检索证据不足（未达 min_snippets 条支持）→ 保留原文"
    if reason and reason.startswith("resample-"):
        return f"盲重采样未支持（{reason}）→ 保留原文"
    return "安全门阻断 → 保留原文"


def _resample_sum(rg):
    """重采样结果摘要（供 result["resample"] 取证）。"""
    return {"verdict": rg.get("verdict"), "support": rg.get("support"),
            "n": rg.get("n"), "model": rg.get("model")}


def _safety_checks(task, prev, cand_text):
    """v0.8.0 安全门 + v0.9.0 知识证据门链 → (ok, reason, extra)。
    1) 值操作原则；
    2) 值替换（值集合变化且 v-o 非空）→ 依次过两个 opt-in 门：
       · 盲重采样门：supported → 放行；conflict → 直接阻断（证据不可覆盖）；
         inconclusive/skipped → 继续进入证据门；
       · 知识证据门（enabled 时）：≥ min_snippets 条"含新值且不含旧值"片段 → 放行；
         不足/异常 → 阻断（resample-evidence-*）；
       · 两门都关 → 维持原行为放行。"""
    o, v = _values(prev), _values(cand_text)
    block = _value_op_block(o, v)
    if block:
        return False, block, {}
    if v != o and (v - o):  # 值替换 → 重采样门 → 知识证据门（opt-in 链）
        rg = _resample_gate(task, o, v - o)
        if rg is not None and rg.get("verdict") == "supported":
            return True, "", {"resample": _resample_sum(rg)}
        if rg is not None and rg.get("verdict") == "conflict":
            return False, "resample-conflict", {"resample": _resample_sum(rg)}
        # 重采样门未启用 / inconclusive / skipped → 继续知识证据门
        extra = {}
        if rg is not None:
            extra["resample"] = _resample_sum(rg)
        ev = _evidence_gate(task, o, v - o)
        if ev is not None:
            extra["evidence"] = ev
            if ev.get("verdict") == "supported":
                return True, "", extra
            return False, f"resample-evidence-{ev.get('verdict')}", extra
        # 证据门未启用/不可读：维持 v0.8.0 对重采样结果的处置
        # （inconclusive → 阻断；skipped 已映射为 None，不阻塞）
        if rg is not None and rg.get("verdict") != "supported":
            return False, f"resample-{rg.get('verdict')}", extra
        return True, "", extra
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


def _evidence_revise_cfg():
    """v0.10 P2：检索增强修订（RARR 式）配置。
    默认关（opt-in）——需显式 settings.repair.evidence_revise.enabled=true。
    返回 (enabled, max_snippets)。"""
    try:
        from mjc import settings as _settings
        rep = (_settings.load().get("repair") or {})
    except Exception:
        rep = {}
    cfg = rep.get("evidence_revise") or {}
    enabled = cfg.get("enabled", False)  # 默认关（opt-in）；C 臂在 settings 显式打开
    try:
        max_snip = int(cfg.get("max_snippets", 6))
    except (TypeError, ValueError):
        max_snip = 6
    return bool(enabled), max(1, min(max_snip, 12))


def _fetch_revise_evidence(task, prev, max_snippets=6):
    """给修订者先取证：query = 任务(≤80字) + 原文数值；返回片段文本列表（失败 → []）。"""
    try:
        from mjc import knowledge
    except Exception:
        return []
    q = (task or "").strip()[:80]
    vals = sorted(_values(prev))
    if vals:
        q = (q + " " + " ".join(vals)).strip()
    if not q:
        return []
    try:
        res = knowledge.fetch_evidence(q, merge=True, min_total=3, max_backends=3)
    except Exception:
        return []
    snips = (res or {}).get("snippets") or []
    out = []
    for s in snips[:max_snippets]:
        t = (s.get("text") or s.get("title") or "").strip()
        if t:
            out.append(t[:300])
    return out


def _evidence_rules(snips):
    """把检索片段转成追加守则（仅供核验；相关才用）。"""
    if not snips:
        return ""
    lines = ["【外部检索证据】（公开检索，可能不相关/过时；仅在直接相关时用于核验）"]
    for i, t in enumerate(snips, 1):
        lines.append(f"{i}. {t}")
    lines.append("若证据明确给出正确值，可采用；若与本题无关或不足，忽略之；不得据此引入其它新事实。")
    return "\n".join(lines)


def dual_revise(task, prev, feedback, specs=None, timeout=120):
    """返回共识结果 dict（不抛出；基础设施错误 → mode=error）。
    v0.10 P2：证据门开时先检索证据并注入生产者提示词（RARR），让修复有据可依。"""
    specs = available_producers(specs)
    if len(specs) < 2:
        return {"mode": "error", "applied": prev, "revs": [], "tokens": 0,
                "note": "可用生产者不足 2 个（跨厂）"}
    ev_on, ev_max = _evidence_revise_cfg()
    ev_snips = _fetch_revise_evidence(task, prev, ev_max) if ev_on else []
    messages = build_messages(task, prev, feedback, extra_rules=_evidence_rules(ev_snips))
    revs = [_call_one(s, messages, timeout=timeout) for s in specs]
    ok = [r for r in revs if r.get("text")]
    result = {"mode": "incomplete", "applied": prev, "revs": revs, "tokens": 0,
              "evidence_revise": {"enabled": ev_on, "snippets": len(ev_snips)}}
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
