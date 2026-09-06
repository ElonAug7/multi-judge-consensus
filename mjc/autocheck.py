#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · autocheck.py — 自动审查（auto_review / cmd_auto / cmd_scan）
P3 结构拆分：自 mjc.cli 移出。cli.py 保持薄壳，顶层再导出本模块符号（旧 import 不破）。
  auto_review  内容+记忆 → 初筛/委员会 → logs/auto/{day}.jsonl（hook 转录扫描 / 代码交付工作流）；
               同 sha+同 kind 内容 6h 内已审 → 跳过（当日日志尾 100 行比对，P4.2 去重）
  cmd_auto     CLI auto 子命令实现（argparse 接线仍在 cli.main）
  cmd_scan     转录扫描（webchat 触发源）：找最新未审终稿 → auto_review
"""
import datetime
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc.judge import build_pool
from mjc import pipeline
from mjc.paths import LOG_DIR, AUTO_LOG_DIR

# 同 sha（+同 kind）内容 N 小时内去重：读当日审查日志尾 100 行比对（P4.2）
DEDUPE_H = 6
DEDUPE_TAIL = 100
DEDUPE_TAIL_BYTES = 64 * 1024


def _tail_lines(path, n=DEDUPE_TAIL):
    """读文件末尾最多 n 行（物理读 ≤64KB），损坏/缺失 → []"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            f.seek(max(0, size - DEDUPE_TAIL_BYTES))
            data = f.read().decode("utf-8", "replace")
        lines = [l for l in data.splitlines() if l.strip()]
        return lines[-n:]
    except Exception:
        return []


def _recent_duplicate(sha, kind, hours=DEDUPE_H):
    """当日审查日志（AUTO_LOG_DIR/{day}.jsonl）尾 100 行里，存在同 sha+同 kind 且
    N 小时内的条目 → 返回其 ts；否则 None。channel 不参与去重：内容相同 = 同一份审查结论。"""
    day = datetime.date.today().isoformat()
    path = os.path.join(AUTO_LOG_DIR, f"{day}.jsonl")
    if not os.path.exists(path):
        return None
    now = time.time()
    for line in _tail_lines(path):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("sha") != sha or e.get("kind") != kind:
            continue
        try:
            ts = datetime.datetime.fromisoformat(e["ts"]).timestamp()
        except Exception:
            continue
        if now - ts < hours * 3600:
            return e.get("ts")
    return None


def auto_review(content, channel="?", task=None, no_memory=False, kind="message", verbose=False,
              emit=None, task_id=None, min_len=80):
    """自动审查核心（供 cmd_auto 与 cmd_scan 复用）。返回 (exit_code, summary_dict)。
    kind: message（回复文本）| code（代码任务收尾审查，交付前用）"""
    content = (content or "").strip()
    channel = channel or "?"
    kind = kind or "message"
    if len(content) < min_len:
        return 0, {"skipped": "too_short", "len": len(content)}
    sha = hashlib.sha1(content.encode()).hexdigest()[:10]
    dup_ts = _recent_duplicate(sha, kind)
    if dup_ts:
        # 同内容 N 小时内已审过（且结论相同——内容未变）→ 跳过，省一次 API 审查
        return 0, {"skipped": "dup", "sha": sha, "kind": kind, "since": dup_ts}
    os.makedirs(AUTO_LOG_DIR, exist_ok=True)
    try:
        from mjc import settings
        eff = settings.effective()
        pool = build_pool(eff["committee"])
    except Exception as e:
        return 1, {"error": f"档位配置不可用: {e}"}
    if len(pool) < 2:
        return 1, {"error": "可用 Judge <2"}
    screen_j = pipeline.resolve_screen_judge(None)
    # 记忆上下文（Mnemosyne 优先）
    facts, mem_meta = [], {"primary": "none", "attempted": [], "notes": "memctx 未启用"}
    if not no_memory:
        from mjc import memctx
        try:
            facts, mem_meta = memctx.fetch_context(content[:400])
        except Exception as e:
            mem_meta = {"primary": "none", "attempted": ["mnemosyne"], "notes": f"memctx 异常: {e}"}
    task_text = (task or "").strip()[:500] or ("（代码任务收尾审查）回顾以下改动描述与代码片段是否准确" if kind == "code" else "（自动审查）回顾以下 Agent 输出是否准确可靠")
    log = os.path.join(LOG_DIR, f"auto-{int(time.time())}.jsonl")
    # 默认实时事件（task_id=内容 sha10；env MJC_LIVE=0 关闭）
    if emit is None and os.environ.get("MJC_LIVE") != "0":
        try:
            import hashlib as _hl
            from mjc import live as _live
            _sha = _hl.sha1(content.encode()).hexdigest()[:10]
            emit = lambda ev: _live.append(ev["kind"], _sha,
                **{k: v for k, v in ev.items() if k not in ("kind", "ts_ms", "at", "task_id")})
        except Exception:
            emit = None
    try:
        record, meta = pipeline.run_review_once(
            task_text, content, pool,
            screen_judge=screen_j, screen_conf=None,
            use_screen=screen_j is not None, use_cache=False, use_degrade=False,
            trust_path=None, debate_log_dir=None, log_path=log,
            memory=facts or None, emit=emit,
        )
    except Exception as e:
        return 1, {"error": f"审查失败: {e}"}
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "kind": kind, "channel": channel, "len": len(content),
        "sha": sha,
        "verdict": record["final"], "pass_votes": record.get("pass_votes"),
        "reject_votes": record.get("reject_votes"), "revise_votes": record.get("revise_votes"),
        "debate_rounds": record.get("debate_rounds", 0),
        "api_calls": meta.get("api_calls", 0), "screened": meta.get("screened"),
        "screen_passed": meta.get("screen_passed"),
        "tokens": record.get("tokens"), "cost_yuan": record.get("cost_yuan"),
        "memory": mem_meta,
        "issues": [{"type": i.get("type"),
                     "judge": o.get("judge_display") or o.get("judge_id") or "?",
                     "desc": i.get("description", "")[:200],
                     "sug": (i.get("suggestion") or "")[:150]}
                    for rnd in (record.get("rounds") or [])
                    for o in (rnd.get("opinions") or [])
                    for i in (o.get("issues") or [])][:6],
    }
    day = datetime.date.today().isoformat()
    with open(os.path.join(AUTO_LOG_DIR, f"{day}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    out = {"reviewed": True, "kind": kind, "verdict": entry["verdict"], "api_calls": entry["api_calls"],
           "memory_source": mem_meta.get("primary"), "sha": entry["sha"],
           "len": entry["len"], "screened": entry["screened"]}
    if record["final"] in ("reject", "need_human"):
        with open(os.path.join(AUTO_LOG_DIR, "findings.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"⛔ 自动审查发现: {record['final']}（{record.get('reject_votes', 0)}rej）", file=sys.stderr)
    elif record["final"] == "revise":
        print("⚠️ 自动审查 revise（软提示，非击杀，详见日文件）", file=sys.stderr)
    return 0, out


def cmd_auto(args):
    """自动审查（hook 转录扫描 / 代码交付工作流直接调用）。"""
    if getattr(args, "in_file", None):
        try:
            ev = json.load(open(args.in_file, encoding="utf-8"))
        except Exception as e:
            print(json.dumps({"error": f"读输入失败: {e}"}, ensure_ascii=False))
            return 1
    else:
        ev = {"content": args.content or ""}
    code, out = auto_review(
        ev.get("content") or "",
        channel=ev.get("channel") or "?",
        task=ev.get("task") or getattr(args, "task", None) or "",
        kind=ev.get("kind") or getattr(args, "kind", None) or "message",
        no_memory=getattr(args, "no_memory", False),
    )
    print(json.dumps(out, ensure_ascii=False))
    return code


def cmd_scan(args):
    """转录扫描自动审查（webchat 触发源）：找最新未审终稿 → auto_review。"""
    from mjc import scan as scan_mod
    if scan_mod.locked():
        print(json.dumps({"skipped": "locked"}))
        return 0
    cand, sid = scan_mod.newest_candidate()
    cur = scan_mod._cursor()
    if cand is None:
        print(json.dumps({"skipped": "no_candidate"}))
        return 0
    if cur is None:
        scan_mod.set_cursor(cand["ts_ms"])
        print(json.dumps({"primed": True, "cursor": cand["ts"]}))
        return 0
    if cand["ts_ms"] <= cur:
        print(json.dumps({"skipped": "up_to_date", "candidate": cand["ts"]}))
        return 0
    if not getattr(args, "force", False) and len(cand.get("content") or "") < 150:
        scan_mod.set_cursor(cand["ts_ms"])
        print(json.dumps({"skipped": "too_short"}))
        return 0
    scan_mod.set_lock()
    channel = f"session:{sid}" if sid else "?"
    code, out = auto_review(cand["content"], channel=channel, no_memory=getattr(args, "no_memory", False))
    scan_mod.set_cursor(cand["ts_ms"])
    out.update({"cursor": cand["ts"], "entry_id": cand.get("id")})
    print(json.dumps(out, ensure_ascii=False))
    return code


if __name__ == "__main__":
    # 便捷：python3 -m mjc.autocheck auto/scan 需要 argparse；这里直接提示用 cli 入口
    print("请通过 python3 -m mjc.cli auto|scan 调用（argparse 接线在 cli.main）", file=sys.stderr)
    sys.exit(2)
