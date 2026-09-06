#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · trust.py — 信任分 / 降级（P3.3）
每个 Judge 与最终裁决的一致性追踪（持久化 logs/trust.json）：
  reviews  累计审查次数
  agree    与最终裁决一致的累计次数
  streak   连续一致次数（连续一致 → 该 Judge 可降频）
  last_final / last_ts
规则：
  - 一致性定义：该 Judge 本轮 verdict == 最终 final（need_human 终局无共识 → 全员 streak 清零，不计一致）
  - error 票（P1.3）：调用失败/解析失败不是该 Judge 的真实意见 → 中性处理：不清 streak、
    不计 agree/不一致，仅记 reviews（可观测）；need_human 清零也不波及 error 票（它没参与该轮投票）
  - 降级门槛：streak >= MIN_STREAK(默认 5) → 该 Judge 可被跳过（降频），由编排层决定
一致性数据来自每次完整审查的 record（rounds[0] 为第 1 轮独立意见，与 final 对比）。
"""
import json
import os
import tempfile
import time

MIN_STREAK = 5  # 连续一致 ≥5 次 → 可降频（编排层 --degrade 生效）


def default_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "trust.json")


def load(path=None):
    path = path or default_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"judges": {}, "updated": time.time()}


def save(data, path=None):
    path = path or default_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data["updated"] = time.time()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def update_with_record(record, path=None):
    """用一次完整审查 record 更新信任分。record 须含 rounds[0].opinions（第 1 轮独立意见）与 final。"""
    if not record:
        return None
    data = load(path)
    final = record.get("final")
    rounds = record.get("rounds") or []
    r1 = rounds[0].get("opinions") if rounds else []
    no_consensus = final == "need_human"  # 无共识终局：不奖不罚，全部 streak 清零
    for o in r1:
        jid = o.get("judge_id")
        if not jid:
            continue
        st = data["judges"].setdefault(jid, {"reviews": 0, "agree": 0, "streak": 0})
        st["reviews"] = st.get("reviews", 0) + 1
        st["last_ts"] = time.time()
        if o.get("verdict") == "error":
            # P1.3 error 票中性：基础设施/解析失败，非该 Judge 立场——不清 streak、不计一致/不一致
            continue
        st["last_final"] = final
        if no_consensus:
            st["streak"] = 0
            continue
        ok = 1 if o.get("verdict") == final else 0
        st["agree"] = st.get("agree", 0) + ok
        st["streak"] = st.get("streak", 0) + 1 if ok else 0
        conf = o.get("confidence")
        if isinstance(conf, (int, float)):
            bucket = "ge9" if conf >= 0.9 else ("ge7" if conf >= 0.7 else "lt7")
            b = st.setdefault("conf_b", {}).setdefault(bucket, [0, 0])
            b[0] += 1
            b[1] += ok
    save(data, path)
    return data


def eligible_degrade(data=None, min_streak=MIN_STREAK, path=None):
    """streak 达标（连续一致≥min_streak）且 reviews≥min_streak 的 Judge 名列表"""
    data = data if data is not None else load(path)
    out = []
    for jid, st in (data.get("judges") or {}).items():
        if st.get("streak", 0) >= min_streak and st.get("reviews", 0) >= min_streak:
            out.append(jid)
    return out


def describe(data=None, path=None):
    data = data if data is not None else load(path)
    lines = []
    for jid, st in sorted((data.get("judges") or {}).items()):
        cb = st.get("conf_b") or {}
        cal = ""
        if cb.get("ge9") and cb["ge9"][0] >= 3:
            n, a = cb["ge9"]
            cal = f" | 高置信一致 {a}/{n} ({round(100.0 * a / n)}%)"
        lines.append(
            f"{jid}: reviews={st.get('reviews',0)} agree={st.get('agree',0)} "
            f"streak={st.get('streak',0)}{' ⬇可降频' if st.get('streak',0) >= MIN_STREAK else ''}{cal}"
        )
    return "\n".join(lines) or "(暂无数据)"
