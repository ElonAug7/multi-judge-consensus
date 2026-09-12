#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · scan.py — 转录扫描器（webchat 替代 message:sent 的触发源）
背景：本环境（webchat）不产生 message:sent 事件（memory-recorder 也靠转录补录）。
方案：每次 message:received（用户消息/心跳）→ 扫 ~/.openclaw/agents/main/sessions/*.jsonl，
找出"最新一条已完成的 assistant 终稿"（纯 text 块、长度 ≥ min_len（默认 1 ≈ 全量；NO_REPLY/纯符号跳过）、时间戳 > cursor）→ 交 auto_review。
状态：logs/auto/.cursor.json（最近已处理条目的 ISO 时间戳）；logs/auto/.scan-lock（60s 频率锁）。
首次运行（无 cursor）只初始化不审查，避免回溯轰炸历史回复。
cursor 损坏/非法 → 自动重置（视同首次）+ 记事件日志 logs/auto/scan-events.jsonl。
审查对象限定 main agent 的交互会话（main/dashboard/openclaw-* 渠道）；cron 隔离 run、subagent、
dreaming 等自动/机器会话排除（机器输出非用户对答，审错对象）；.trajectory.jsonl 是 trace 非转录，直接跳过。
sessions.json 条目会随任务清理但转录文件保留（真实目录 50 个转录中 37 个孤儿 cron）→
孤儿转录靠同 stem .trajectory.jsonl 的 sessionKey 元数据兑底识别。
"""
import json
import os
import re
import time
from datetime import datetime

WS = os.path.expanduser("~/.openclaw/workspace")
SESS_DIR = os.path.join(os.path.expanduser("~/.openclaw/agents/main/sessions"))
LOG_DIR = os.path.join(WS, "multi-judge-consensus", "logs", "auto")
CURSOR = os.path.join(LOG_DIR, ".cursor.json")
LOCK = os.path.join(LOG_DIR, ".scan-lock")
LOCK_MS = 60 * 1000
MIN_LEN = 1  # 默认门槛（字符）：全量介入（2026-09-12 定）；settings.limits.scan 可覆盖（cmd_scan 传参）
EVENTS_LOG = "scan-events.jsonl"


def _parse_ts(iso):
    """ISO 8601（可能带 Z/毫秒）→ epoch ms；失败返回 None"""
    try:
        iso = iso.strip()
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except Exception:
        return None


_corrupt_warned = None  # 进程内去重：同一 cursor 文件损坏只记一次事件，防刷屏


def _log_event(event, detail):
    """scan 生命周期事件（如 cursor 损坏重置）→ LOG_DIR/scan-events.jsonl；失败静默不炸"""
    global _corrupt_warned
    try:
        if _corrupt_warned == (CURSOR, event, detail):
            return
        _corrupt_warned = (CURSOR, event, detail)
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, EVENTS_LOG), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                "event": event, "detail": detail, "path": CURSOR},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def _trajectory_key(sid):
    """孤儿转录（sessions.json 无登记）→ 读同 stem .trajectory.jsonl 头部拿 sessionKey。
    失败/无文件 → None（该转录无法分类，按放行处理——通常是正在运行、尚未落盘的会话）。"""
    try:
        with open(os.path.join(SESS_DIR, f"{sid}.trajectory.jsonl"), encoding="utf-8") as fh:
            for _ in range(64):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except Exception:
                    continue
                if t.get("type") in ("trace.metadata", "session.started") and t.get("sessionKey"):
                    return t["sessionKey"]
    except Exception:
        pass
    return None


def _session_index():
    """sessionId → sessionKey。来源：① sessions.json 注册表；② 未登记转录的 trajectory 元数据兜底。
    任一环节失败不炸（回退到已有的部分映射 / 空映射 = 不过滤）。"""
    keymap = {}
    try:
        d = json.load(open(os.path.join(SESS_DIR, "sessions.json"), encoding="utf-8"))
        for k, v in d.items():
            if isinstance(v, dict) and v.get("sessionId"):
                keymap[v["sessionId"]] = k
    except Exception:
        pass
    try:
        plain = [f[:-6] for f in os.listdir(SESS_DIR)
                 if f.endswith(".jsonl") and not f.endswith(".trajectory.jsonl")]
    except Exception:
        plain = []
    for sid in plain:
        if sid in keymap:
            continue
        k = _trajectory_key(sid)
        if k:
            keymap[sid] = k
    return keymap


def _session_kind_allowed(key):
    """sessionKey → 是否纳入审查（allowlist，防审错对象）。
    main / dashboard / openclaw-*（weixin/qq/discord…渠道）为交互会话 → 放行；
    cron、subagent、dreaming 等自动/机器会话 → 排除；形态未知 → 保守放行。"""
    parts = key.split(":")
    if len(parts) < 3 or not parts[2]:
        return True
    kind = parts[2]
    return kind in ("main", "dashboard") or kind.startswith("openclaw-")


def _allowed_session(fn, keymap):
    """转录文件名 → 是否纳入审查。trajectory trace 永远排除；有 sessionKey → 按 allowlist；
    无 key（孤儿且无 trajectory 可查）→ 放行。"""
    if fn.endswith(".trajectory.jsonl"):
        return False  # trace 文件不是转录（虽也以 .jsonl 结尾）
    key = keymap.get(fn[:-6])
    return True if not key else _session_kind_allowed(key)


def locked():
    try:
        return time.time() * 1000 - os.path.getmtime(LOCK) * 1000 < LOCK_MS
    except Exception:
        return False


def set_lock():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOCK, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _cursor():
    """读 cursor ts_ms。文件缺失 → None（首次）。文件存在但损坏/缺 ts_ms →
    记事件日志后返回 None（调用方视同首次并 set_cursor 覆盖重置，不回溯轰炸）。"""
    if not os.path.exists(CURSOR):
        return None
    try:
        d = json.load(open(CURSOR, encoding="utf-8"))
    except Exception:
        _log_event("cursor_reset", "corrupt_json")
        return None
    if not isinstance(d, dict) or d.get("ts_ms") is None:
        _log_event("cursor_reset", "missing_ts")
        return None
    return d["ts_ms"]


def set_cursor(ts_ms):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(CURSOR, "w") as f:
            json.dump({"ts_ms": ts_ms}, f)
    except Exception:
        pass


def newest_candidate(min_len=None, max_age_h=36):
    """所有 session 转录里最新的 assistant 终稿（含 text≥min_len（None→MIN_LEN，默认 1）、无 toolCall）。
    返回 (entry, session_id) 或 (None, None)。只扫 36h 内有改动的文件。"""
    if min_len is None:
        min_len = MIN_LEN
    if not os.path.isdir(SESS_DIR):
        return None, None
    best, best_ts, best_sid = None, -1, None
    now = time.time() * 1000
    try:
        files = [f for f in os.listdir(SESS_DIR) if f.endswith(".jsonl")]
    except Exception:
        return None, None
    index = _session_index()
    for fn in files:
        if not _allowed_session(fn, index):
            continue  # cron/subagent 会话 + trajectory trace 排除
        path = os.path.join(SESS_DIR, fn)
        try:
            if now - os.path.getmtime(path) * 1000 > max_age_h * 3600 * 1000:
                continue
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") != "message":
                        continue
                    m = d.get("message") or {}
                    if m.get("role") != "assistant":
                        continue
                    blocks = m.get("content") or []
                    types = [c.get("type") for c in blocks]
                    if "toolCall" in types:
                        continue  # 中间步骤（带工具调用），不是终稿
                    text = "".join((c.get("text") or "") for c in blocks if c.get("type") == "text").strip()
                    if len(text) < min_len:
                        continue
                    if text in ("NO_REPLY", "HEARTBEAT_OK"):
                        continue  # 非答复占位，不送审
                    if not re.search(r"\w", text):
                        continue  # 纯符号/表情，无实质内容
                    ts = _parse_ts(d.get("timestamp") or "")
                    if ts is None:
                        continue
                    if ts > best_ts:
                        best_ts = ts
                        best = {"id": d.get("id"), "ts_ms": ts, "ts": d.get("timestamp"),
                                "content": text[:20000], "len": len(text)}
                        best_sid = fn[:-6]
        except Exception:
            continue
    return best, best_sid


def find_new():
    """比 cursor 新的最新终稿；无 cursor 时初始化并返回 None。"""
    cand, sid = newest_candidate()
    if cand is None:
        return None, None
    cur = _cursor()
    if cur is None:
        # 首次：只初始化（从当前最新位置开始盯），不回溯轰炸
        set_cursor(cand["ts_ms"])
        return None, None
    if cand["ts_ms"] <= cur:
        return None, None
    return cand, sid


if __name__ == "__main__":
    import sys
    c, s = newest_candidate()
    print(json.dumps({"candidate": c and {k: c[k] for k in ("id", "ts", "len")}, "session": s}, ensure_ascii=False))
