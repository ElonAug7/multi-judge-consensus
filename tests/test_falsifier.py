#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · falsifier 证伪者离线测试（零 API，mock providers.chat）
  python3 tests/test_falsifier.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import falsifier, providers, settings as S


def _patch(load=None, eff=None, hk=None, chat=None):
    olds = (S.load, S.effective, providers.has_key, providers.chat)

    if load is not None:
        S.load = load
    if eff is not None:
        S.effective = eff
    if hk is not None:
        providers.has_key = hk
    if chat is not None:
        providers.chat = chat

    def restore():
        S.load, S.effective, providers.has_key, providers.chat = olds
    return restore


def test_resolve_spec():
    restore = _patch(load=lambda data=None: {"falsifier": {"enabled": False}})
    try:
        assert falsifier.resolve_spec() is None
    finally:
        restore()
    committee = ["deepseek:deepseek-v4-flash", "glm:glm-4-flash", "glm:glm-4-plus"]
    restore = _patch(load=lambda data=None: {"falsifier": {"enabled": True, "model": ""}},
                     eff=lambda data=None: {"committee": committee},
                     hk=lambda n: n in ("deepseek", "glm", "dashscope"))
    try:
        assert falsifier.resolve_spec() == "dashscope:qwen-max"  # 跨厂优先
    finally:
        restore()
    restore = _patch(load=lambda data=None: {"falsifier": {"enabled": True, "model": "glm:glm-4.5"}},
                     hk=lambda n: n in ("glm",))
    try:
        assert falsifier.resolve_spec() == "glm:glm-4.5"  # 显式配置
    finally:
        restore()
    restore = _patch(load=lambda data=None: {"falsifier": {"enabled": True, "model": ""}},
                     eff=lambda data=None: {"committee": committee},
                     hk=lambda n: n in ("deepseek", "glm"))
    try:
        assert falsifier.resolve_spec() == "glm:glm-4.5"  # 无跨厂时回退（非委员会同款）
    finally:
        restore()
    print("  ✅ resolve_spec：关闭/跨厂优先/显式配置/回退")


def test_challenge_parse():
    restore = _patch(load=lambda data=None: {"falsifier": {"enabled": True, "model": "dashscope:qwen-max", "min_len": 10}},
                     hk=lambda n: True,
                     chat=lambda *a, **k: ('{"challenges":[{"type":"factual_error","desc":"d1","suggestion":"s1"},'
                                           '{"type":"hallucination","desc":"d1","suggestion":"s2"},'
                                           '{"type":"premise","desc":"d2"},{"type":"x","desc":"d3"},{"type":"x","desc":"d4"}],'
                                           '"note":"n"}'))
    try:
        r = falsifier.challenge("t", "x" * 100)
        assert r["spec"] == "dashscope:qwen-max" and r["calls"] == 1, r
        descs = [c["desc"] for c in r["challenges"]]
        assert descs == ["d1", "d2", "d3"], descs  # 去重 + cap 3
        # 太短跳过
        r2 = falsifier.challenge("t", "短")
        assert r2.get("skipped") == "too_short", r2
    finally:
        restore()
    restore = _patch(load=lambda data=None: {"falsifier": {"enabled": True, "model": "dashscope:qwen-max", "min_len": 5}},
                     hk=lambda n: True,
                     chat=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    try:
        r3 = falsifier.challenge("t", "x" * 100)
        assert r3.get("error") and r3.get("calls") == 0, r3
    finally:
        restore()
    print("  ✅ challenge：解析/去重/截断/太短跳过/异常不抛")


def test_context_prompt():
    """v0.13.0：会话上下文注入证伪提示词（无则不出）。"""
    seen = []

    def _chat(provider, msgs, **kw):
        seen.append(msgs[0]["content"])
        return '{"challenges":[],"note":"n"}'

    restore = _patch(load=lambda data=None: {"falsifier": {"enabled": True, "model": "dashscope:qwen-max", "min_len": 5}},
                     hk=lambda n: True, chat=_chat)
    try:
        r = falsifier.challenge("任务T", "x" * 100,
                                context="用户上一条消息：帮我查入口\nAgent 本轮真实工具调用记录（自动提取，可能不全）：\n1. web_fetch: https://a → ok")
        assert "会话上下文" in seen[-1] and "帮我查入口" in seen[-1], "上下文应注入提示词"
        assert "防误杀" in seen[-1]
        r2 = falsifier.challenge("任务T", "x" * 100)
        assert "会话上下文" not in seen[-1], "无上下文时不应出现该段"
        assert r["challenges"] == [] and r2["challenges"] == []
    finally:
        restore()
    print("  ✅ context：会话上下文注入证伪提示词（无则不出）")


def main():
    print("== falsifier 证伪者离线测试（零 API）==")
    test_resolve_spec()
    test_challenge_parse()
    test_context_prompt()
    print("== falsifier 全部通过 ✅ ==")


if __name__ == "__main__":
    main()
