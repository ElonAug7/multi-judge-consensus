#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · falsifier.py — 证伪者（红队审查员，独立性工程 ①）

动机（2026-09-12 实测）：委员会会"集体通过"含错内容（共同盲区：共享训练数据 → 错误相关，
投票涨不了正确率——Condorcet 定理）。对策：增设一个**对抗性角色**，立场与普通审查员相反：
默认假设内容有问题，专职找最可能被质疑的点（波普尔式证伪），且优先使用与委员会**跨厂**的模型。

产出交给"事实仲裁"独立复核：只有 confirmed 的挑战才允许升级/触发修订（防为找而找）。
配置（settings.json）：
  "falsifier": {"enabled": true, "model": "", "min_len": 10}
  model 为空 → 自动挑：候选里优先"厂商不在委员会"且 key 就绪的型号。
"""
import json

from mjc import providers
from mjc import settings
from mjc.judge import extract_json

FALSIFY_PROMPT = """你是专职证伪者（红队审查员），任务是对下述内容做**对抗性找错**。
立场：默认假设内容里藏着问题；找出最可能被质疑的点，特别是：
- 事实性断言（年份、数字、人名、归属）是否有误；
- 是否顺着问题/任务的错误预设作答（编造不存在的事物）；
- 是否有夸大、绝对化、无法验证的细节；
- 逻辑是否自洽。
但**严禁为找而找**：每条挑战必须说明为什么可疑；若确实找不到有可靠理由的疑点，返回空列表。
【任务】
{task}
【内容】
{content}
输出严格 JSON（不要 markdown 代码块）：
{{"challenges":[{{"type":"factual_error|hallucination|logical_error|premise","desc":"疑点描述","suggestion":"建议核查/修正方向（可选）"}}],"note":"一句话总结"}}
挑战最多 3 条，按可疑程度排序。"""

CANDIDATES = ("dashscope:qwen-max", "qwen:qwen3.8-max", "kimi:kimi-latest",
              "doubao:doubao-seed-1.6", "glm:glm-4.5", "deepseek:deepseek-chat")

MAX_CHALLENGES = 3


def _cfg():
    try:
        return settings.load().get("falsifier") or {}
    except Exception:
        return {}


def resolve_spec(committee=None):
    """挑证伪者模型：显式配置 > 候选里"跨厂+有 key"优先。无可用 → None。"""
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return None
    explicit = (cfg.get("model") or "").strip()
    if explicit:
        p = explicit.split(":", 1)[0]
        try:
            if providers.has_key(p):
                return explicit
        except Exception:
            pass
    try:
        committee = committee or settings.effective()["committee"]
    except Exception:
        committee = []
    vend = {s.split(":", 1)[0] for s in committee}
    fallback = None
    for c in CANDIDATES:
        p = c.split(":", 1)[0]
        try:
            if not providers.has_key(p):
                continue
        except Exception:
            continue
        if p not in vend and c not in committee:
            return c            # 跨厂优先
        if fallback is None and c not in committee:
            fallback = c
    return fallback


def challenge(task, content, timeout=90):
    """运行证伪者。返回 {spec, challenges:[{type,desc,suggestion}], calls, note} 或 {skipped/error}。
    不抛出。"""
    spec = resolve_spec()
    if not spec:
        return {"skipped": "disabled_or_no_model"}
    min_len = int(_cfg().get("min_len", 10))
    if len((content or "").strip()) < min_len:
        return {"skipped": "too_short", "len": len(content or "")}
    provider, _, model = spec.partition(":")
    prompt = FALSIFY_PROMPT.format(task=(task or "")[:400], content=(content or "")[:3000])
    try:
        raw = providers.chat(provider, [{"role": "user", "content": prompt}],
                             model=(model or None), temperature=0.3, max_tokens=4000, timeout=timeout)
    except Exception as e:  # noqa
        return {"error": str(e)[:160], "spec": spec, "calls": 0}
    parsed = extract_json(raw) or {}
    chs, seen = [], set()
    for c in (parsed.get("challenges") or []):
        if not isinstance(c, dict):
            continue
        desc = (c.get("desc") or "").strip()
        if not desc:
            continue
        key = desc[:100]
        if key in seen:
            continue
        seen.add(key)
        chs.append({"type": (c.get("type") or "factual_error")[:32],
                    "desc": desc[:200],
                    "suggestion": (c.get("suggestion") or "").strip()[:150]})
        if len(chs) >= MAX_CHALLENGES:
            break
    return {"spec": spec, "challenges": chs, "calls": 1,
            "note": str(parsed.get("note", ""))[:160]}


if __name__ == "__main__":
    print(json.dumps({"spec": resolve_spec()}, ensure_ascii=False))
