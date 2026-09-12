#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · dispositions.py — 审查意见处置记录（纠正过程可视化数据源）
主 agent 跑完 auto --kind code 拿到 issues 后，逐条给处置：
  {adopted: true/false, action: "fixed"|"ignored"|"false_positive", note: "..."}
落盘 logs/auto/dispositions.jsonl（一行一条，含审查摘要 + 处置明细），
WebUI「纠正过程」页读取并动画化：审查节点 → 提建议 → 主 agent 回复采纳/不采纳。
"""
import datetime
import json
import os

from mjc import paths

PATH = os.path.join(paths.AUTO_LOG_DIR, "dispositions.jsonl")
MAX_NOTE = 300
ACTIONS = ("fixed", "ignored", "false_positive", "pending")


def _review_summary(entry):
    """从 auto 日文件 entry 提炼展示用摘要（不引原始大文本）"""
    return {
        "ts": entry.get("ts", ""),
        "kind": entry.get("kind", "message"),
        "sha": entry.get("sha", ""),
        "verdict": entry.get("verdict", "?"),
        "pass_votes": entry.get("pass_votes"),
        "reject_votes": entry.get("reject_votes"),
        "revise_votes": entry.get("revise_votes"),
        "api_calls": entry.get("api_calls"),
        "memory_source": (entry.get("memory") or {}).get("primary"),
        "issues": entry.get("issues", []),
        "arbitration": [{"outcome": a.get("outcome"),
                          "desc": (a.get("desc") or "")[:160],
                          "votes": [{"judge": v.get("judge"),
                                     "my_answer": str(v.get("my_answer") or "")[:60],
                                     "original_wrong": v.get("original_wrong"),
                                     "suggestion_correct": v.get("suggestion_correct"),
                                     "error": (v.get("error") or "")[:80]}
                                    for v in (a.get("votes") or [])]}
                         for a in (entry.get("arbitration") or [])][:4],
        "task": (entry.get("task") or "")[:200],
        "len": entry.get("len"),
    }


def _task_of(entry):
    return (entry.get("task") or "")[:200] or "（无任务描述）"


def append(entry, decisions):
    """entry: auto 日文件行（dict）；decisions: [{idx, adopted, action, note}] → 返回落盘 record"""
    issues = entry.get("issues") or []
    if not issues:
        raise ValueError("entry 没有 issues，无需处置")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("decisions 为空：每条意见都要给处置（adopted=false 也须写明 note）")
    seen = set()
    for d in decisions:
        idx = d.get("idx")
        if not isinstance(idx, int) or idx < 0 or idx >= len(issues):
            raise ValueError(f"idx 越界: {idx}（issues 共 {len(issues)} 条）")
        if idx in seen:
            raise ValueError(f"idx 重复: {idx}")
        seen.add(idx)
        if "adopted" not in d:
            raise ValueError(f"idx {idx} 缺 adopted")
        d["adopted"] = bool(d["adopted"])
        action = d.get("action", "fixed" if d["adopted"] else "ignored")
        if action not in ACTIONS:
            raise ValueError(f"action 非法: {action}")
        d["action"] = action
        d["note"] = (d.get("note") or "")[:MAX_NOTE]
    missing = [i for i in range(len(issues)) if i not in seen]
    if missing:
        raise ValueError(f"有意见未处置: idx {missing}——每条都要给 adopted+note（可 false_positive 带理由）")
    record = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "review": _review_summary(entry),
        "decisions": decisions,
    }
    os.makedirs(paths.AUTO_LOG_DIR, exist_ok=True)
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def recent(n=12):
    """最近 n 条处置记录（新→旧）"""
    if not os.path.exists(PATH):
        return []
    out = []
    try:
        with open(PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-n:][::-1]


if __name__ == "__main__":
    import sys
    print(f"dispositions 文件: {PATH} | 当前 {len(recent(999))} 条")
