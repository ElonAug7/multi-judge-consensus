#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_savings.py — 节省账本离线测试（v0.11.0l · 零 API）

覆盖：
  · record()：给真实用量 → estimated=False；不给 → 自动估算并标 estimated=True
  · summary()：按机制分组 + 总计；format_summary() 可读
  · estimate_cost()：走 providers 价目表（贵模型 > 便宜模型）
  · MJC_SAVINGS_PATH 可隔离账本（测试/多环境分账）
  · 损坏行容错（账本被写坏不影响统计）
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import savings  # noqa: E402


def run():
    ok = True

    def check(name, cond, extra=None):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond or extra is None else f"  ← {extra}"))
        if not cond:
            ok = False

    tmp = tempfile.mktemp(suffix="-savings.jsonl")

    # ① 真实用量 → 不标估算
    r1 = savings.record("cache_hit", calls=4, tokens=5000, cost=0.02, seconds=41.0,
                        note="委员会缓存命中", path=tmp)
    check("真实用量 → estimated=False", r1["estimated"] is False and r1["tokens"] == 5000)
    check("字段完整", set(r1) >= {"ts", "mechanism", "calls", "tokens", "cost_yuan", "seconds", "estimated"})

    # ② 只给 calls → 自动估算并标估算
    r2 = savings.record("screen_pass", calls=3, path=tmp)
    check("无用量 → estimated=True 且估算出 tokens/cost/秒",
          r2["estimated"] is True and r2["tokens"] > 0 and r2["cost_yuan"] > 0 and r2["seconds"] > 0)

    # ③ 汇总
    s = savings.summary(days=30, path=tmp)
    check("总计：2 笔 7 次调用", s["totals"]["n"] == 2 and s["totals"]["calls"] == 7, s["totals"])
    check("按机制分组", set(s["by_mechanism"]) == {"cache_hit", "screen_pass"}, s["by_mechanism"])
    check("tokens 合计 = 5000 + 估算", s["totals"]["tokens"] > 5000)
    check("估算占比 50%", abs(s["estimated_share"] - 0.5) < 0.01, s["estimated_share"])
    txt = savings.format_summary(s)
    check("摘要含调用数与 token 数", "7 次调用" in txt and "tokens" in txt)

    # ④ 价目表：贵模型估算 > 便宜模型
    cheap = savings.estimate_cost("glm:glm-4-flash", 1)
    dear = savings.estimate_cost("dashscope:qwen-max", 1)
    check("估算走价目表（qwen-max > glm-4-flash）", dear > cheap, (cheap, dear))

    # ⑤ 损坏行容错
    with open(tmp, "a", encoding="utf-8") as f:
        f.write("{坏行不是JSON\n")
    s2 = savings.summary(days=30, path=tmp)
    check("损坏行被忽略且不影响统计", s2["totals"]["n"] == 2, s2["totals"])

    # ⑥ 窗口过滤：30 天前的记录不计入
    old = tempfile.mktemp(suffix="-savings.jsonl")
    with open(old, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 0, "mechanism": "cache_hit", "calls": 99, "tokens": 999,
                            "cost_yuan": 1.0, "seconds": 1.0, "estimated": False}) + "\n")
        f.write(json.dumps({"ts": savings._now(), "mechanism": "cache_hit", "calls": 1, "tokens": 1,
                            "cost_yuan": 0.0, "seconds": 0.0, "estimated": False}) + "\n")
    s3 = savings.summary(days=1, path=old)
    check("窗口外的旧记录被排除", s3["totals"]["calls"] == 1, s3["totals"])

    for p in (tmp, old):
        try:
            os.remove(p)
        except OSError:
            pass
    print("== 节省账本（v0.11.0l）全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
