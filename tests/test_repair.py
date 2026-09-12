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


def main():
    print("== repair 双生产者修订共识离线测试（零 API）==")
    test_agreed()
    test_disagreed()
    test_incomplete()
    print("== repair 全部通过 ✅ ==")


if __name__ == "__main__":
    main()
