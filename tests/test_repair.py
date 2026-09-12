#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MJC · repair 双生产者修订共识离线测试（零 API，mock providers.chat）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import repair, providers


def _ctx(behavior):
    old_chat, old_hk = providers.chat, providers.has_key
    providers.has_key = lambda n: True
    calls = []

    def fake(name, messages, model=None, **kw):
        calls.append(model)
        b = behavior.get(model, behavior.get("*", "OK"))
        if isinstance(b, Exception):
            raise b
        return b

    providers.chat = fake

    def restore():
        providers.chat, providers.has_key = old_chat, old_hk

    return calls, restore


def test_agreed():
    calls, restore = _ctx({"deepseek-v4-flash": "答案：1997年。", "glm-4-plus": "答案：1997 年"})
    try:
        r = repair.dual_revise("t", "1995年", "fb")
        assert r["mode"] == "agreed" and "1997" in r["applied"], r
    finally:
        restore()
    print("  ✅ agreed：两版归一化一致 → 采纳")


def test_disagreed():
    calls, restore = _ctx({"deepseek-v4-flash": "1997年", "glm-4-plus": "1983年（另一说法）"})
    try:
        r = repair.dual_revise("t", "1995年", "fb")
        assert r["mode"] == "disagreed" and r["applied"] == "1995年", r
    finally:
        restore()
    print("  ✅ disagreed：两版不一致 → 保留原文（防新幻觉）")


def test_incomplete():
    calls, restore = _ctx({"deepseek-v4-flash": "1997年", "glm-4-plus": RuntimeError("HTTP 500")})
    try:
        r = repair.dual_revise("t", "1995年", "fb")
        assert r["mode"] == "incomplete" and r["applied"] == "1995年", r
    finally:
        restore()
    print("  ✅ incomplete：有效修订不足 2 路 → 保守保留原文")


def test_value_subset_applies():
    # v0.7.2：跨模型措辞不同但数值集合为真子集 → 采纳更保守（数值更少）一版
    calls, restore = _ctx({"deepseek-v4-flash": "该院于1997年正式挂牌成立，2006年更为现名。",
                           "glm-4-plus": "中国中医科学院望京医院于1997年正式挂牌成立。"})
    try:
        r = repair.dual_revise("t", "中国中医科学院望京医院于1995年正式挂牌成立。", "fb")
        assert r["mode"] == "agreed" and "1997" in r["applied"] and "2006" not in r["applied"], r
        assert r["agreement"] == "value-subset", r
    finally:
        restore()
    print("  ✅ value-subset：数值真子集（更少主张）→ 采纳")


def test_value_set_same_applies():
    # 两版数值集合相同且 ≠ 原文 → 采纳较短一版
    calls, restore = _ctx({"deepseek-v4-flash": "该协会成立于2009年，是行业组织。",
                           "glm-4-plus": "它成立于2009年。"})
    try:
        r = repair.dual_revise("t", "该协会最早成立于1997年。", "fb")
        assert r["mode"] == "agreed" and "2009" in r["applied"], r
        assert r["agreement"] == "value-set", r
    finally:
        restore()
    print("  ✅ value-set：数值集合一致 → 采纳（取较短版）")


def test_text_only_edit_not_applied():
    # 无数值的纯文字改写（如换名字）→ 不自动采纳（无知识依据）
    calls, restore = _ctx({"deepseek-v4-flash": "主题曲应为《凡间路》，原名有误。",
                           "glm-4-plus": "主题曲为《武夷仙凡界》，但流传说法有出入。"})
    try:
        r = repair.dual_revise("t", "主题曲名为《武夷仙凡界》。", "fb")
        assert r["mode"] == "disagreed" and r["applied"] == "主题曲名为《武夷仙凡界》。", r
    finally:
        restore()
    print("  ✅ 纯文字改写 → 不自动采纳（保留原文）")


def test_gutted_refusal_blocked():
    # 共识版本为"清空式"短句（< max(4, 40% 原文)）→ 不采纳
    orig = "国际空无展览首次举办于1960年，在艺术界具有重要影响。"
    calls, restore = _ctx({"deepseek-v4-flash": "无法确认。", "glm-4-plus": "无法确认。"})
    try:
        r = repair.dual_revise("t", orig, "fb")
        assert r["mode"] == "disagreed" and r["applied"] == orig, r
        assert "过短" in r.get("note", ""), r
    finally:
        restore()
    print("  ✅ 清空式短句 → 不采纳（保留原文）")


def test_value_drop_blocked():
    # v0.8.0：双方一致删光数值（csqa-13 样式）→ 阻断，保留原文
    orig = "国际空无展览首次举办于1960年。"
    calls, restore = _ctx({"deepseek-v4-flash": "“国际空无展览”这一事件无法确认。",
                           "glm-4-plus": "无法确认该事件是否真实存在。"})
    try:
        r = repair.dual_revise("t", orig, "fb")
        assert r["mode"] == "disagreed" and r["applied"] == orig, r
        assert r["agreement"] == "drop-blocked", r
    finally:
        restore()
    print("  ✅ drop-blocked：一致删值 → 阻断（v0.8.0）")


def test_resample_gate_blocks():
    # 值替换已达成共识，但重采样门说 conflict → 阻断
    calls, restore = _ctx({"deepseek-v4-flash": "该院于1997年成立。", "glm-4-plus": "该院1997年成立。"})
    old_gate = repair._resample_gate
    repair._resample_gate = lambda task, o, n: {"verdict": "conflict", "support": 0, "n": 3, "model": "mock"}
    try:
        r = repair.dual_revise("t", "该院于1995年成立。", "fb")
        assert r["mode"] == "disagreed" and r.get("agreement") == "resample-conflict", r
        assert r["applied"] == "该院于1995年成立。", r
    finally:
        repair._resample_gate = old_gate
        restore()
    print("  ✅ resample-conflict：盲重采样未支持 → 阻断")


def test_resample_gate_supports():
    calls, restore = _ctx({"deepseek-v4-flash": "该院于1997年成立。", "glm-4-plus": "该院1997年成立。"})
    old_gate = repair._resample_gate
    repair._resample_gate = lambda task, o, n: {"verdict": "supported", "support": 3, "n": 3, "model": "mock"}
    try:
        r = repair.dual_revise("t", "该院于1995年成立。", "fb")
        assert r["mode"] == "agreed" and "1997" in r["applied"], r
        assert (r.get("resample") or {}).get("verdict") == "supported", r
    finally:
        repair._resample_gate = old_gate
        restore()
    print("  ✅ resample-supported：盲重采样支持 → 采纳")


def main():
    print("== repair 双生产者修订共识离线测试（零 API）==")
    test_agreed()
    test_disagreed()
    test_incomplete()
    test_value_subset_applies()
    test_value_set_same_applies()
    test_text_only_edit_not_applied()
    test_gutted_refusal_blocked()
    test_value_drop_blocked()
    test_resample_gate_blocks()
    test_resample_gate_supports()
    print("== repair 全部通过 ✅ ==")


if __name__ == "__main__":
    main()
