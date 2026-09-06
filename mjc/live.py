#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · live.py — 实时审查事件流（P1：阶段闸门 + WebUI 实时动画的数据通道）
pipeline 每票落定/每阶段变化 → append 一行事件到 logs/auto/live.jsonl；
WebUI GET /api/live?after=<ts_ms> 增量拉取（≤2s 轮询），事件边到边播。

事件 kind：
  start / verifier_hit / opinion / degrade / final        （pipeline 内发）
  stage_start / stage_done / question / answer            （gate 命令/主 agent 发）
字段统一带 ts_ms（单调排序键）+ task_id（阶段会话分组）。
文件防膨胀：单文件超过 MAX_LINES 时归档为 live-<date>.jsonl.bak 重建（保留最近）。
"""
import datetime
import json
import os
import time

from mjc import paths

PATH = os.path.join(paths.AUTO_LOG_DIR, "live.jsonl")
MAX_LINES = 2000

_clock = 0


def _ts_ms():
    global _clock
    now = int(time.time() * 1000)
    if now <= _clock:
        now = _clock + 1  # 同毫秒防重（轮询 after 语义）
    _clock = now
    return now


def append(kind, task_id="?", **fields):
    ev = {"ts_ms": _ts_ms(), "at": datetime.datetime.now().isoformat(timespec="seconds"),
          "kind": kind, "task_id": str(task_id)[:40], **fields}
    try:
        os.makedirs(paths.AUTO_LOG_DIR, exist_ok=True)
        _prune()
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return ev


def _prune():
    """超限归档重建（保留全部行到 .bak，新文件从 0 开始）"""
    try:
        if os.path.exists(PATH) and sum(1 for _ in open(PATH, encoding="utf-8")) > MAX_LINES:
            bak = PATH + ".bak"
            os.replace(PATH, bak)
    except Exception:
        pass


def read_after(after_ms=0, limit=500):
    """返回 (events, last_ts_ms)。after_ms=0 → 尾部 limit 条。"""
    if not os.path.exists(PATH):
        return [], 0
    events = []
    last = 0
    try:
        with open(PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                last = max(last, ev.get("ts_ms", 0))
                if ev.get("ts_ms", 0) > after_ms:
                    events.append(ev)
    except Exception:
        return [], last
    if after_ms == 0:
        events = events[-limit:]
    return events, last


def task_sessions(limit=6):
    """最近活跃的 task_id 分组摘要（供 UI 列出进行中/最近会话）"""
    events, _ = read_after(0, 500)
    seen = {}
    for ev in reversed(events):
        tid = ev.get("task_id") or "?"
        if tid not in seen:
            seen[tid] = ev
    out = []
    for tid, ev in list(seen.items())[-limit:]:
        out.append({"task_id": tid, "kind": ev.get("kind"), "at": ev.get("at")})
    return out
