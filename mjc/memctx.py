#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · memctx.py — 审查记忆上下文（优先级链，2026-09-06 定案）
  1. Mnemosyne 记忆引擎（workspace/tools/memory-engine，recall 关键词检索，纯本地 <100ms）
  2. OpenClaw 官方记忆（memory_search）——无 headless search CLI 且索引 embedding 缺失 → 占位自动跳过
  3. 第三方（web/其他）——未配置 → 占位自动跳过
返回 (facts, meta)：facts 供 judge 背景记忆注入；meta 记录实际用了哪一级。
"""
import json
import os
import shutil
import subprocess

WS = os.path.expanduser("~/.openclaw/workspace")
ENGINE = os.path.join(WS, "tools", "memory-engine", "engine.js")


def _mnemosyne_recall(query, limit=3):
    """engine.js recall --query → flashbacks；失败返回 []"""
    if not os.path.exists(ENGINE):
        return []
    node = os.environ.get("MJC_NODE_BIN") or shutil.which("node") or "/usr/bin/node"
    try:
        r = subprocess.run(
            [node, ENGINE, "recall", "--query", query[:300]],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout or "").strip()
        if not out:
            return []
        # 引擎输出整段 JSON；容忍前置杂文本
        start = out.find("{")
        if start < 0:
            return []
        data = json.loads(out[start:])
        facts = []
        for fb in (data.get("flashbacks") or [])[:limit]:
            facts.append({
                "text": (fb.get("text") or "").strip().replace("\n", " ")[:320],
                "source": fb.get("source", "mnemosyne"),
                "date": fb.get("date", ""),
                "relevance": fb.get("relevance"),
            })
        return facts
    except Exception:
        return []


def _official_memory(query):
    """OpenClaw 官方记忆：本版本 CLI 只有 index/promote，无 search；且索引 embedding provider 缺失。
    预留实现位：将来有 `openclaw memory search` 或可用索引时在此接入（优先级高于第三方）。"""
    return []


def _third_party(query):
    """第三方（web 检索等）：未配置 provider → 空。"""
    return []


def fetch_context(query, limit=3):
    """按优先级取记忆。返回 (facts, meta)"""
    meta = {
        "primary": "none",
        "attempted": [],
        "notes": "",
    }
    facts = []
    # ① Mnemosyne
    meta["attempted"].append("mnemosyne")
    facts = _mnemosyne_recall(query, limit)
    if facts:
        meta["primary"] = "mnemosyne"
        return facts, meta
    # ② OpenClaw 官方记忆
    meta["attempted"].append("official")
    facts = _official_memory(query)
    if facts:
        meta["primary"] = "official"
        return facts, meta
    # ③ 第三方
    meta["attempted"].append("third_party")
    facts = _third_party(query)
    if facts:
        meta["primary"] = "third_party"
        return facts, meta
    meta["notes"] = "无可用记忆源（mnemosyne 空结果/不可用，official 无 search CLI，third_party 未配置）"
    return [], meta


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "测试"
    f, m = fetch_context(q)
    print(json.dumps({"facts": f, "meta": m}, ensure_ascii=False, indent=1))
