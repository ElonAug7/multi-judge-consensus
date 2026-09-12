#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MJC · resample 盲重采样支持门离线测试（零 API，mock providers.chat）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import resample, providers


def _ctx(responses):
    """responses: 依次返回的文本列表（每次调用弹出队首）。"""
    old_chat, old_hk = providers.chat, providers.has_key
    providers.has_key = lambda n: True
    calls = []
    queue = list(responses)

    def fake(name, messages, model=None, **kw):
        calls.append(model)
        b = queue.pop(0) if queue else "1997年"
        if isinstance(b, Exception):
            raise b
        return b

    providers.chat = fake

    def restore():
        providers.chat, providers.has_key = old_chat, old_hk

    return calls, restore


def test_supported():
    calls, restore = _ctx(["1997年。", "1997 年", "该院1997年成立，2006年更名。"])
    try:
        r = resample.evaluate("望京医院何时成立", {"1995"}, {"1997"})
        assert r["verdict"] == "supported" and r["support"] == 3 and r["calls"] == 3, r
    finally:
        restore()
    print("  ✅ supported：3/3 重采样含新值且无旧值 → 允许采纳")


def test_conflict():
    calls, restore = _ctx(["1995年。", "1995 年成立", "1997年。"])
    try:
        r = resample.evaluate("望京医院何时成立", {"1995"}, {"1997"})
        assert r["verdict"] == "conflict" and r["conflict"] == 2, r
    finally:
        restore()
    print("  ✅ conflict：2/3 重采样复现旧值 → 阻断替换")


def test_inconclusive():
    calls, restore = _ctx(["1975年", "1983年（另一说法）", "1997年"])
    try:
        r = resample.evaluate("平安钟协会何时成立", {"1995"}, {"1997"})
        assert r["verdict"] == "inconclusive" and r["support"] == 1 and r["conflict"] == 0, r
    finally:
        restore()
    print("  ✅ inconclusive：支持与冲突都不足 → 保守处理（默认阻断）")


def test_no_model():
    old_chat, old_hk = providers.chat, providers.has_key
    providers.has_key = lambda n: False
    try:
        r = resample.evaluate("t", {"1"}, {"2"})
        assert r["verdict"] == "skipped" and r["calls"] == 0, r
    finally:
        providers.chat, providers.has_key = old_chat, old_hk
    print("  ✅ skipped：无可用模型 → 不阻塞、不调用")


def test_partial_errors():
    calls, restore = _ctx(["1997年。", RuntimeError("HTTP 500"), "1997年"])
    try:
        r = resample.evaluate("t", {"1995"}, {"1997"})
        assert r["verdict"] == "supported" and r["support"] == 2 and r["errors"] == 1, r
    finally:
        restore()
    print("  ✅ 部分失败：2/3 有效且均支持 → supported（错误计数）")


def main():
    print("== resample 盲重采样支持门离线测试（零 API）==")
    test_supported()
    test_conflict()
    test_inconclusive()
    test_no_model()
    test_partial_errors()
    print("== resample 全部通过 ✅ ==")


if __name__ == "__main__":
    main()
