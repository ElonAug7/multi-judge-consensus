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
import re

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
{evidence_section}
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

ARB_BATCH_PROMPT = """你是独立仲裁员，负责**逐条**复核另一位审查员的多条“事实错误”声明是否成立。这些声明可能是误报，请独立判断，不要预设审查员正确。

【任务】
{task}

【被审内容（节选）】
{content}

【审查意见（共 {n} 条，请逐条复核）】
{issues}
{evidence_section}

请对每一条意见**先**独立回答（不看审查员结论），再输出严格 JSON 数组（不要 markdown 代码块、不要多余文字）：
[
  {{
    "idx": 1,
    "my_answer": "（该条争议点）你独立认为的正确答案；不确定写 不确定",
    "original_wrong": "yes | no | unknown",
    "suggestion_correct": "yes | no | unknown",
    "note": "一句话依据（不确定就说明为何不确定）"
  }}
]
要求：
- 必须为**每一条**意见各输出一个对象，idx 与上面编号一一对应（1…{n}），不得遗漏或合并。
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


def _evidence_section(ev):
    """外部检索片段 → 提示段（可能低质，明示只作线索）"""
    if not ev or not (ev.get("snippets")):
        return ""
    lines = [f"【外部检索片段】（{ev.get('backend', '?')}；可能不相关/低质，仅作线索——不得因检索结果盲信，也不得因检索不到就断定对错）"]
    for i, s in enumerate((ev.get("snippets") or [])[:4], 1):
        lines.append(f"{i}. {str(s.get('text', ''))[:300]}")
    return "\n".join(lines) + "\n"


def _extract_any(raw):
    """抽取 JSON（**兼容顶层数组**）。judge.extract_json 只认对象，而批量仲裁要求返回数组——
    直接用它会导致每条意见都拿不到票 → 全部 unknown（保守但白花钱）。"""
    t = (raw or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return extract_json(t)


def _as_list(obj):
    """把 extract_json 的结果规整成 list（容忍 {"items":[...]} / 单个对象）。"""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("items", "results", "verdicts", "data"):
            if isinstance(obj.get(k), list):
                return obj[k]
        return [obj]
    return []


def _batch_arbitrate(picked, judges, task, content, kb_budget, timeout):
    """批量仲裁（v0.11.0k · 轻量化）：**一次调用复核全部意见**，而非每条意见各调一次。

    开销对比：原实现 = 意见数 × 仲裁员数（4 条 × 2 人 = 8 次）；批量为 2 次（每个仲裁员 1 次）。
    语义不变：仍按 `_decide(votes)` 逐条出 outcome，票来自各仲裁员对应对项的 JSON 条目；
    解析失败/缺项 → 该条 outcome=unknown（保守，与"无可用仲裁模型"一致）。

    返回 (items, calls)。
    """
    lines = []
    for i, it in enumerate(picked, 1):
        lines.append(f'{i}. 意见：{(it.get("desc") or "")[:300]}\n   建议：{(it.get("sug") or "（无）")[:200]}')
    ev_blocks = []
    for i, it in enumerate(picked, 1):
        ev = None
        if kb_budget:
            try:
                q = (it.get("query") or ((task or "")[:60] + " " + (it.get("desc") or "")[:60])).strip()[:140]
                ev = kb_budget.take(q)
            except Exception:
                ev = None
        it["_ev"] = ev
        if ev:
            ev_blocks.append(f"（第 {i} 条相关检索证据）" + _evidence_section(ev).strip())
    prompt = ARB_BATCH_PROMPT.format(
        n=len(picked), task=(task or "")[:400], content=(content or "")[:800],
        issues="\n".join(lines), evidence_section=("\n".join(ev_blocks) if ev_blocks else "（无外部证据）"))
    per_issue = {i: [] for i in range(1, len(picked) + 1)}
    calls = 0

    def _ask(j):
        """单个仲裁员一次调用（并行单元）→ (judge, entries, err)"""
        try:
            raw = providers.chat(j.provider, [{"role": "user", "content": prompt}],
                                 model=j.model, temperature=0.1, max_tokens=6000, timeout=timeout)
            return j, _as_list(_extract_any(raw)), None
        except Exception as e:  # noqa
            return j, [], str(e)[:120]

    # v0.11.0l：仲裁员之间**互相独立**（同一提示词、无共享状态）→ 并行发起，省一半墙钟时间。
    # 默认并行；settings.factcheck.serial=true 可退回串行（便于排查限流类问题）。
    try:
        from mjc import settings as _st
        serial = bool((_st.load().get("factcheck") or {}).get("serial", False))
    except Exception:
        serial = False
    if serial or len(judges) < 2:
        results = [_ask(j) for j in judges]
    else:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=len(judges)) as _ex:
            results = list(_ex.map(_ask, judges))
    for j, entries, err in results:
        calls += 1
        if err:
            per_issue.setdefault(0, []).append({"judge": j.name, "error": err})
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                idx = int(e.get("idx") or e.get("index") or 0)
            except (TypeError, ValueError):
                continue
            if idx in per_issue:
                per_issue[idx].append({"judge": j.name,
                                       "my_answer": str(e.get("my_answer", ""))[:80],
                                       "original_wrong": _norm_bool(e.get("original_wrong")),
                                       "suggestion_correct": _norm_bool(e.get("suggestion_correct")),
                                       "note": str(e.get("note", ""))[:160]})
    items = []
    for i, it in enumerate(picked, 1):
        votes = per_issue.get(i) or []
        ev = it.get("_ev")
        items.append({"judge_id": it.get("judge_id"), "type": it.get("type"),
                      "desc": (it.get("desc") or "")[:200], "sug": (it.get("sug") or "")[:150],
                      "outcome": _decide(votes) if votes else "unknown", "votes": votes,
                      "evidence": ({"backend": ev.get("backend"), "n": len(ev.get("snippets") or [])} if ev else None)})
    avoided = max(0, len(picked) * len(judges) - len(judges))
    if avoided:
        try:
            from mjc import savings as _sav
            _sav.record("batch_arbitration", calls=avoided,
                        note=f"{len(picked)} 条意见 × {len(judges)} 名仲裁员 → 批量 {len(judges)} 次")
        except Exception:
            pass
    return items, calls


def arbitrate_issues(task, content, issues, committee=None, timeout=90, max_issues=3, batch=False):
    """issues: [{judge_id, type, desc, sug}]（调用方已过滤事实类）。
    返回 {"items": [{...issue, outcome, votes}], "calls": N}；基础设施异常 → {"error": ...} 不抛出。
    batch=True（v0.11.0k）：批量化——每个仲裁员一次调用复核全部意见（N×2 次 → 2 次）。"""
    if committee is None:
        try:
            committee = settings.effective()["committee"]
        except Exception as e:
            return {"error": f"委员会不可用: {e}", "items": [], "calls": 0}
    items, calls = [], 0
    kb_budget = None
    try:  # 外部知识源（默认 off；配置后启用；预算受限）
        from mjc import knowledge
        if knowledge.configured_backends():
            kb_budget = knowledge.Budget()
    except Exception:
        kb_budget = None
    seen = set()
    picked = []
    for it in issues:
        if len(picked) >= max_issues:
            break
        desc = (it.get("desc") or "").strip()
        if not desc:
            continue
        key = (desc[:120], (it.get("sug") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        picked.append(it)
    if batch and picked:
        excl = set()
        for it in picked:
            excl.update(it.get("judge_ids") or ([it.get("judge_id")] if it.get("judge_id") else []))
        judges, _picks = _pick_arbiters(committee, sorted(excl), k=2)
        if not judges:
            return {"items": [{"judge_id": it.get("judge_id"), "type": it.get("type"),
                               "desc": (it.get("desc") or "")[:200], "sug": (it.get("sug") or "")[:150],
                               "outcome": "unknown", "votes": [],
                               "note": "无可用的独立仲裁模型"} for it in picked],
                    "calls": 0, "batch": True}
        b_items, b_calls = _batch_arbitrate(picked, judges, task, content, kb_budget, timeout)
        return {"items": b_items, "calls": b_calls, "batch": True}
    for it in picked:
        desc = (it.get("desc") or "").strip()
        excl = it.get("judge_ids") or ([it.get("judge_id")] if it.get("judge_id") else [])
        judges, _picks = _pick_arbiters(committee, excl, k=2)
        if not judges:
            items.append({"judge_id": it.get("judge_id"), "type": it.get("type"),
                          "desc": desc[:200], "sug": (it.get("sug") or "")[:150],
                          "outcome": "unknown", "votes": [], "note": "无可用的独立仲裁模型"})
            continue
        ev_used = None
        if kb_budget:
            try:
                q = (it.get("query") or ((task or "")[:60] + " " + desc[:60])).strip()[:140]
                ev_used = kb_budget.take(q)
            except Exception:
                ev_used = None
        prompt = ARB_PROMPT.format(task=(task or "")[:400], content=(content or "")[:800],
                                   desc=desc[:300], sug=(it.get("sug") or "（无）")[:200],
                                   evidence_section=_evidence_section(ev_used))
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
                      "outcome": _decide(votes), "votes": votes,
                      "evidence": ({"backend": ev_used.get("backend"), "n": len(ev_used.get("snippets") or [])} if ev_used else None)})
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
