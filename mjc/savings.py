#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MJC · savings.py — 节省账本（省了多少 token / 多少钱 / 多少时间）

定位：现有 usage 只记"花了多少"，这里记"**省了多少**"。每当某个机制避免了一次本会发生的
调用（或整轮审查），就追加一笔到 logs/savings.jsonl。可累计、可审计、可展示。

机制（mechanism）与省下的东西：
  cache_hit          命中审查缓存        → 省下整轮委员会（用量取自缓存记录，非估算）
  screen_pass        初筛干净内容放行    → 省下委员会全部席位
  batch_arbitration  批量仲裁            → 省下 (意见数-1) × 仲裁员数 次调用（v0.11.0k）
  dedupe             同内容 6h 内重复送审 → 省下整轮
  parallel           并行化              → 只省**墙钟时间**（token 不省，如实记 0）

估算口径（诚实优先，能拿真实值就不用估）：
  · 价格用 providers.price_of()（¥/1K，输入/输出分开），与"花了多少"同一套价目；
  · 命中缓存时用缓存记录里的**真实** tokens/cost；
  · 其余用 _EST_CALL 估（默认一次审查类调用 ≈ 1800 输入 + 600 输出 token、12 秒），
    并在账本里标 estimated=True —— 展示层会明确区分"实测"与"估算"。

用法：
  from mjc import savings
  savings.record("screen_pass", calls=3, note="初筛 0.9 直接放行")
  savings.record("cache_hit", calls=4, tokens=1234, cost=0.0042, seconds=41.0)
  savings.summary(days=30)  → {"totals": {...}, "by_mechanism": {...}, "n": N}
"""
import json
import os
import time

from mjc import paths

# 可用 MJC_SAVINGS_PATH 覆盖（测试隔离 / 多环境分账）
PATH = os.environ.get("MJC_SAVINGS_PATH") or os.path.join(paths.LOG_DIR, "savings.jsonl")
KEEP_DAYS = 180          # 超过则丢弃（写时顺手清理）

# 单次"委员会席位级"调用的保守估算（无真实用量时使用）
_EST_PROMPT_TOKENS = 1800
_EST_COMPLETION_TOKENS = 600
_EST_SECONDS_PER_CALL = 12.0
_DEFAULT_SPEC = "glm:glm-4-flash"   # 估算基准：委员会里的白菜席


def _now():
    return time.time()


def estimate_cost(spec=None, calls=1, prompt_tokens=None, completion_tokens=None):
    """按价格表估算 calls 次调用的 ¥（拿不到价格表 → 用默认价）。"""
    try:
        from mjc import providers
        p, _, m = (spec or _DEFAULT_SPEC).partition(":")
        pin, pout = providers.price_of(p, m or None)
    except Exception:
        pin, pout = (0.002, 0.008)
    pt = (_EST_PROMPT_TOKENS if prompt_tokens is None else prompt_tokens) * calls
    ct = (_EST_COMPLETION_TOKENS if completion_tokens is None else completion_tokens) * calls
    return round(pt / 1000.0 * pin + ct / 1000.0 * pout, 5)


def record(mechanism, calls=0, tokens=None, cost=None, seconds=None, note="", spec=None,
           estimated=None, path=None):
    """记一笔节省。tokens/cost 给真实值时不标估算；否则按 _EST_* 估算并标 estimated=True。"""
    est = estimated
    if tokens is None:
        tokens = calls * (_EST_PROMPT_TOKENS + _EST_COMPLETION_TOKENS)
        est = True if est is None else est
    if cost is None:
        cost = estimate_cost(spec, calls) if calls else 0.0
        est = True if est is None else est
    if seconds is None:
        seconds = calls * _EST_SECONDS_PER_CALL
    rec = {"ts": round(_now(), 3), "mechanism": str(mechanism)[:40], "calls": int(calls or 0),
           "tokens": int(tokens or 0), "cost_yuan": round(float(cost or 0.0), 5),
           "seconds": round(float(seconds or 0.0), 1), "estimated": bool(est),
           "note": str(note)[:160]}
    p = path or PATH
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def _load(path=None, days=KEEP_DAYS):
    p = path or PATH
    if not os.path.exists(p):
        return []
    cutoff = _now() - days * 86400
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts", 0) >= cutoff:
                    out.append(r)
    except Exception:
        return []
    return out


def summary(days=30, path=None):
    """近 days 天的节省汇总：总计 + 按机制分组。"""
    cutoff = _now() - days * 86400
    rows = [r for r in _load(path=path) if r.get("ts", 0) >= cutoff]
    tot = {"calls": 0, "tokens": 0, "cost_yuan": 0.0, "seconds": 0.0, "n": len(rows)}
    by = {}
    est_rows = 0
    for r in rows:
        if r.get("estimated"):
            est_rows += 1
        d = by.setdefault(r["mechanism"], {"calls": 0, "tokens": 0, "cost_yuan": 0.0,
                                           "seconds": 0.0, "n": 0})
        for k in ("calls", "tokens", "cost_yuan", "seconds"):
            d[k] += r.get(k) or 0
            tot[k] += r.get(k) or 0
        d["n"] += 1
    tot["cost_yuan"] = round(tot["cost_yuan"], 5)
    tot["seconds"] = round(tot["seconds"], 1)
    for d in by.values():
        d["cost_yuan"] = round(d["cost_yuan"], 5)
        d["seconds"] = round(d["seconds"], 1)
    return {"days": days, "totals": tot, "by_mechanism": by,
            "estimated_share": round(est_rows / len(rows), 3) if rows else 0.0}


def format_summary(s=None):
    """人类可读摘要（CLI / 日志用）。"""
    s = s or summary()
    t = s["totals"]
    lines = [f"近 {s['days']} 天共省：{t['calls']} 次调用 · {t['tokens']:,} tokens · "
             f"≈¥{t['cost_yuan']} · 约 {t['seconds'] / 60:.1f} 分钟",
             f"（其中 {int(s['estimated_share'] * 100)}% 的记录为估算值，其余来自实测用量）"]
    for m, d in sorted(s["by_mechanism"].items(), key=lambda kv: -kv[1]["tokens"]):
        lines.append(f"  · {m:18} {d['n']:4} 笔 · {d['calls']:5} 次调用 · "
                     f"{d['tokens']:>9,} tokens · ≈¥{d['cost_yuan']:<8} · {d['seconds'] / 60:.1f} 分钟")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_summary())
