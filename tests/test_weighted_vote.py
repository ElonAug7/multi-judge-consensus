#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_weighted_vote.py — P4 加权投票离线测试（零 API）
验证 trust.py 的 agree/reviews 准确率权重接进 arbiter 投票：
  1) 无 trust 数据 → 与纯票型 decide 完全等价（向后兼容）
  2) need_human 平局 → 用 trust 权重打破，倾向更可靠 Judge
  3) 安全边界：reject/revise/pass 绝不因加权被放松（不把 reject/revise 回退成 pass）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc.arbiter import Arbiter, trust_weights


def mk(judge, verdict, conf=0.9, issues=None):
    return {"judge_id": judge, "judge_display": judge, "verdict": verdict,
            "confidence": conf, "issues": issues or [], "final_reasoning": "t"}


GUARD = {"enabled": True, "types": ("factual_error", "hallucination", "logical_error"), "min_conf": 0.0}


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    # ---- trust_weights 单元 ----
    check("trust_weights：空数据 → 空 dict", trust_weights(None) == {})
    check("trust_weights：无 reviews → 跳过",
          trust_weights({"judges": {"a": {"reviews": 0, "agree": 0}}}) == {})
    w = trust_weights({"judges": {"a": {"reviews": 7, "agree": 6}, "b": {"reviews": 6, "agree": 6}}})
    check("trust_weights：agree/reviews 归一", abs(w["a"] - 6 / 7) < 1e-9 and w["b"] == 1.0)
    check("trust_weights：非法值安全", trust_weights({"judges": {"a": {"reviews": "x"}}}) == {})

    # ---- 1) 无 trust 数据 → 与 decide 等价 ----
    v = ["reject", "revise", "error"]
    ops = [mk("a", "reject"), mk("b", "revise"), mk("c", "error")]
    check("无 trust：need_human 平局不被打破",
          Arbiter.decide_weighted(v, 2, ops, GUARD, None) == "need_human")
    check("无 trust：pass 不受影响",
          Arbiter.decide_weighted(["pass", "pass", "pass"], 2, ops, GUARD, None) == "pass")

    # ---- 2) need_human → 加权破平局 ----
    td = {"judges": {"a": {"reviews": 10, "agree": 9},   # a: 0.9（reject）
                     "b": {"reviews": 10, "agree": 1}}}  # b: 0.1（revise）
    check("need_human → 高权重 reject Judge 破局为 reject",
          Arbiter.decide_weighted(v, 2, ops, GUARD, td) == "reject")
    # 反向：revise 高权重 → revise
    td2 = {"judges": {"a": {"reviews": 10, "agree": 1},   # a: 0.1（reject）
                      "b": {"reviews": 10, "agree": 9}}}  # b: 0.9（revise）
    check("need_human → 高权重 revise Judge 破局为 revise",
          Arbiter.decide_weighted(v, 2, ops, GUARD, td2) == "revise")
    # 全中性权重（0.5 对 0.5）→ 无严格过半 → 仍 need_human（保守）
    td_neutral = {"judges": {"a": {"reviews": 4, "agree": 2}, "b": {"reviews": 4, "agree": 2}}}
    check("中性权重无过半 → 仍 need_human（不冒进）",
          Arbiter.decide_weighted(v, 2, ops, GUARD, td_neutral) == "need_human")

    # ---- 3) 安全边界：绝不放松 reject/revise/pass ----
    check("2 reject 绝不因加权回退 pass",
          Arbiter.decide_weighted(["reject", "reject", "pass"], 2,
                                  [mk("a", "reject"), mk("b", "reject"), mk("c", "pass")],
                                  GUARD, td) == "reject")
    # 异议保护：2 pass + 1 高权重 pass 但标 factual_error → 仍 revise
    FACT = [{"type": "factual_error", "description": "年份错", "location": "x"}]
    check("加权不压掉异议保护（factual → revise）",
          Arbiter.decide_weighted(["pass", "pass", "pass"], 2,
                                  [mk("a", "pass", 1.0, FACT), mk("b", "pass"), mk("c", "pass")],
                                  GUARD, td) == "revise")
    # pass 已达成 → 保持不变（即使某 Judge 权重极低）
    check("pass 已达成 → 保持不变",
          Arbiter.decide_weighted(["pass", "pass", "revise"], 2,
                                  [mk("a", "pass"), mk("b", "pass"), mk("c", "revise")],
                                  GUARD, td) == "pass")

    print("== 加权投票全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
