#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · factcheck.py — 事实仲裁（对委员会“事实错误 + 替换建议”的独立复核）

目的（零幻觉架构 · 检测环节防误报 / 防瞎改）：
  委员会会有误报（csqa-03：作者归属被 GLM 误杀），也可能给出错误替换值（csqa-07：1997→1983 仍错）。
  在把“客观错误”交给修订环节之前，先让**与申诉人不同的模型**独立仲裁：
    原内容是否确错？建议替换值是否正确？
  只有 confirmed 的建议才允许触发具体值替换；refuted / unknown 一律只允许弱化表述。

用法：arbitrate_issues(task, content, issues, committee=None) → {"items": [...], "calls": N}
"""
import json

from mjc import providers
from mjc import settings
from mjc.judge import build_pool, extract_json

ARB_PROMPT = """你是独立仲裁员，负责复核另一位审查员的“事实错误”声明是否成立。该声明可能是误报，请独立判断，不要预设审查员正确。

【任务】
{task}

【被审内容（节选）】
{content}

【审查员声明】原内容问题：{desc}
【审查员建议】{sug}

请**先**独立回答（不看审查员结论）：
步骤一：就声明中涉及的争议点，你独立认为的正确答案是什么？（不确定就写“不确定”，严禁猜测）
然后输出严格 JSON（不要 markdown 代码块、不要多余文字）：
{{
  "my_answer": "（步骤一）你独立认为的正确答案；不确定写 不确定",
  "original_wrong": "yes | no | unknown",
  "suggestion_correct": "yes | no | unknown",
  "note": "一句话依据（不确定就说明为何不确定）"
}}
要求：
- original_wrong：原内容中被指部分是否确实有事实错误。不确定 → "unknown"（严禁凭印象断言）。
- suggestion_correct：审查员给出了具体替换值时，该值是否正确；无具体替换值 → "unknown"。
- 你的知识可能过时或不足；没有把握时必须填 "unknown"。
"""

_VALUE_YES = {"yes", "y", "true", "1", "是", "对"}
_VALUE_NO = {"no", "n", "false", "0", "否", "不是", "错"}


def _norm_bool(v):
    """仲裁输出归一：yes / no / unknown"""
    s = str(v or "").strip().lower()
    if s in _VALUE_YES:
        return "yes"
    if s in _VALUE_NO:
        return "no"
    return "unknown"


def _majority(votes, field):
    """多数票（按完整评委席 k，含错误/无效票占位）：严格过半才算；无过半 → unknown。
    例：k=2 时需 2/2 一致（单票+1错误票 = unknown）；k=1 时单票生效。"""
    k = len(votes)
    yes = sum(1 for v in votes if v.get(field) == "yes")
    no = sum(1 for v in votes if v.get(field) == "no")
    if yes * 2 > k:
        return "yes"
    if no * 2 > k:
        return "no"
    return "unknown"


def _decide(votes):
    """保守不对称规则：
    - confirmed：双字段都严格多数 yes（2/2 一致）——替换具体值必须高强度确认；
    - refuted：严格多数 no，或“存在 no 票且无任何 yes 票”→ 保留原文（安全方向）；
    - 其余（含单票 yes、冲突票）→ unknown（不得据此替换，仅可弱化）。"""
    ow = _majority(votes, "original_wrong")
    sc = _majority(votes, "suggestion_correct")
    if len(votes) >= 2 and sc == "yes" and ow == "yes":
        return "confirmed"  # 确认必须≥2 名独立仲裁者一致（单人面板只能否证/存疑）
    if ow == "no" or sc == "no":
        return "refuted"
    any_yes = any(v.get("suggestion_correct") == "yes" or v.get("original_wrong") == "yes" for v in votes)
    any_no = any(v.get("suggestion_correct") == "no" or v.get("original_wrong") == "no" for v in votes)
    if any_no and not any_yes:
        return "refuted"  # 单票否证、无人支持 → 安全方向：保留原文
    return "unknown"


def _pick_arbiters(committee, exclude_specs, k=2):
    """从委员会里挑仲裁者：排除所有同案申诉者，优先跨厂。返回 (judges, specs)。
    exclude_specs: 参与该声明（同 desc）的全部申诉模型 spec 列表。"""
    ex = set(exclude_specs or [])
    specs = [s for s in (committee or []) if s not in ex]
    if not specs:
        return [], []
    # 跨厂优先：优先排除任一申诉人所在厂商
    vends = {s.split(":", 1)[0] for s in ex}
    diff = [s for s in specs if s.split(":", 1)[0] not in vends]
    same = [s for s in specs if s.split(":", 1)[0] in vends]
    picks = (diff + same)[:k]
    return build_pool(picks), picks


def arbitrate_issues(task, content, issues, committee=None, timeout=90, max_issues=3):
    """issues: [{judge_id, type, desc, sug}]（调用方已过滤事实类）。
    返回 {"items": [{...issue, outcome, votes}], "calls": N}；基础设施异常 → {"error": ...} 不抛出。"""
    if committee is None:
        try:
            committee = settings.effective()["committee"]
        except Exception as e:
            return {"error": f"委员会不可用: {e}", "items": [], "calls": 0}
    items, calls = [], 0
    seen = set()
    for it in issues:
        if len(items) >= max_issues:
            break
        desc = (it.get("desc") or "").strip()
        if not desc:
            continue
        key = (desc[:120], (it.get("sug") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        excl = it.get("judge_ids") or ([it.get("judge_id")] if it.get("judge_id") else [])
        judges, _picks = _pick_arbiters(committee, excl, k=2)
        if not judges:
            items.append({"judge_id": it.get("judge_id"), "type": it.get("type"),
                          "desc": desc[:200], "sug": (it.get("sug") or "")[:150],
                          "outcome": "unknown", "votes": [], "note": "无可用的独立仲裁模型"})
            continue
        prompt = ARB_PROMPT.format(task=(task or "")[:400], content=(content or "")[:800],
                                   desc=desc[:300], sug=(it.get("sug") or "（无）")[:200])
        votes = []
        for j in judges:
            try:
                raw = providers.chat(j.provider, [{"role": "user", "content": prompt}],
                                     model=j.model, temperature=0.1, max_tokens=6000, timeout=timeout)
                calls += 1
                p = extract_json(raw) or {}
                votes.append({"judge": j.name,
                              "my_answer": str(p.get("my_answer", ""))[:80],
                              "original_wrong": _norm_bool(p.get("original_wrong")),
                              "suggestion_correct": _norm_bool(p.get("suggestion_correct")),
                              "note": str(p.get("note", ""))[:160]})
            except Exception as e:  # noqa
                calls += 1
                votes.append({"judge": j.name, "error": str(e)[:120]})
        items.append({"judge_id": it.get("judge_id"), "type": it.get("type"),
                      "desc": desc[:200], "sug": (it.get("sug") or "")[:150],
                      "outcome": _decide(votes), "votes": votes})
    return {"items": items, "calls": calls}


def summarize(items):
    """给日志/输出的压缩摘要 {n, confirmed, refuted, unknown}"""
    items = items or []
    out = {"n": len(items), "confirmed": 0, "refuted": 0, "unknown": 0}
    for it in items:
        o = it.get("outcome")
        if o in ("confirmed", "refuted", "unknown"):
            out[o] += 1
        else:
            out["unknown"] += 1
    return out
