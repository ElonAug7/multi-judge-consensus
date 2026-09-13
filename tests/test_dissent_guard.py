#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_dissent_guard.py — P1 异议保护离线测试（零 API）
背景：csqa-07 实测 votes={pass:2,revise:1} → pass，其中 glm-flash 标了 factual_error
      却投 pass，glm-plus 标 factual_error 投 revise → 纯票型裁决把异议压掉，修复链未启动。
规则：任一裁定标 factual_error/hallucination/logical_error（不看其 verdict）→ 多数 pass 不得直接放行。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc.arbiter import Arbiter, detect_blocking_dissent, dissent_config


def mk(judge, verdict, conf=0.9, issues=None):
    return {"judge_id": judge, "judge_display": judge, "verdict": verdict,
            "confidence": conf, "issues": issues or [], "final_reasoning": "t"}


FACT = [{"type": "factual_error", "description": "年份错误", "location": "输出"}]


class J:
    """离线假裁判：每次 review 返回固定意见"""
    def __init__(self, name, verdict, conf=0.9, issues=None):
        self.name = name
        self.display = name
        self._op = mk(name, verdict, conf, issues)

    def review(self, *a, **k):
        return dict(self._op)


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    guard = {"enabled": True, "types": ("factual_error", "hallucination", "logical_error"), "min_conf": 0.0}
    guard_off = {"enabled": False, "types": (), "min_conf": 0.0}

    # ---- detect_blocking_dissent ----
    ops = [mk("deepseek", "pass", 0.9), mk("glm-flash", "pass", 1.0, FACT), mk("glm-plus", "revise", 0.8, FACT)]
    hits = detect_blocking_dissent(ops, guard)
    check("detect：命中 2 条（含'投 pass 却标 factual_error'）", len(hits) == 2)
    check("detect：命中项带 judge/verdict/types", all(h.get("judge") and h.get("types") for h in hits))
    check("detect：guard 关闭 → 空", detect_blocking_dissent(ops, guard_off) == [])
    check("detect：空 opinions 安全", detect_blocking_dissent(None, guard) == [])

    # ---- decide：csqa-07 复现 ----
    v = ["pass", "pass", "revise"]
    check("csqa-07 复现：2 pass + 1 revise(factual) → revise（不再 pass）",
          Arbiter.decide(v, 2, ops, guard) == "revise")
    v2 = ["pass", "pass", "pass"]
    ops2 = [mk("a", "pass", 0.9), mk("b", "pass", 1.0, FACT), mk("c", "pass", 0.9)]
    check("3 pass 但 1 条标 factual_error → revise", Arbiter.decide(v2, 2, ops2, guard) == "revise")

    # ---- 不过度触发 ----
    ops3 = [mk("a", "pass", 0.9), mk("b", "pass", 0.9), mk("c", "reject", 0.9,
            [{"type": "style", "description": "语气", "location": "x"}])]
    check("2 pass + 1 reject(非阻塞 issue) → pass（不误伤）", Arbiter.decide(["pass", "pass", "reject"], 2, ops3, guard) == "pass")
    check("guard 关闭 → 2 pass 仍 pass", Arbiter.decide(v, 2, ops, guard_off) == "pass")
    check("无 opinions（向后兼容）→ 2 pass 仍 pass", Arbiter.decide(v, 2) == "pass")

    # ---- 既有规则不受影响 ----
    check("2 reject → reject", Arbiter.decide(["reject", "reject", "pass"], 2, ops3, guard) == "reject")
    check("1:1:1 → revise（原规则）", Arbiter.decide(["pass", "reject", "revise"], 2, [], guard) == "revise")
    check("全 error → need_human", Arbiter.decide(["error", "error", "error"], 2, [], guard) == "need_human")
    check("reject 优先于 guard（2 reject + 1 factual pass）",
          Arbiter.decide(["reject", "reject", "pass"], 2,
                         [mk("a", "reject"), mk("b", "reject"), mk("c", "pass", 1.0, FACT)], guard) == "reject")

    # ---- min_conf 过滤 ----
    g_conf = {"enabled": True, "types": ("factual_error",), "min_conf": 0.5}
    check("min_conf：低置信异议被过滤",
          detect_blocking_dissent([mk("a", "pass", 0.3, FACT)], g_conf) == [])
    check("min_conf：达阈值命中", len(detect_blocking_dissent([mk("a", "pass", 0.6, FACT)], g_conf)) == 1)

    # ---- 配置读取 ----
    cfg = dissent_config()
    check("dissent_config 默认启用", cfg["enabled"] is True and "factual_error" in cfg["types"])

    # ---- pipeline 集成：guard 贯穿到一次审查 ----
    from mjc import pipeline
    pool = [J("deepseek:deepseek-v4-flash", "pass", 0.9),
            J("glm:glm-4-flash", "pass", 1.0, FACT)]
    rec, meta = pipeline.run_review_once(
        "香港平安钟协会最早成立于哪一年？", "香港平安钟协会最早成立于1997年。", pool,
        use_verifier=False, use_screen=False, use_cache=False, trust_path=None)
    check("pipeline：2 pass 但含 factual_error → final=revise（修复链被启动）", rec["final"] == "revise")
    check("pipeline：record.dissent.triggered=True", bool(rec.get("dissent", {}).get("triggered")))

    # 对照：干净内容（无 issue）仍 pass
    pool2 = [J("deepseek:deepseek-v4-flash", "pass", 0.9), J("glm:glm-4-flash", "pass", 1.0)]
    rec2, _ = pipeline.run_review_once(
        "北京是哪个国家的首都？", "北京是中华人民共和国的首都。", pool2,
        use_verifier=False, use_screen=False, use_cache=False, trust_path=None)
    check("pipeline：干净内容仍 pass（不误伤）", rec2["final"] == "pass")

    print("== 异议保护全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
