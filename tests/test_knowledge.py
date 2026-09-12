#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · knowledge 外部知识源离线测试（零网络，mock 后端）
  python3 tests/test_knowledge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import knowledge


def test_configured_backends():
    old_env = os.environ.pop("MJC_KNOWLEDGE", None)
    old_cfg = knowledge._settings_cfg
    try:
        # settings 未启用 → []
        knowledge._settings_cfg = lambda: {}
        assert knowledge.configured_backends() == []
        # settings 启用
        knowledge._settings_cfg = lambda: {"enabled": True, "backends": ["sogou", "bing"]}
        assert knowledge.configured_backends() == ["sogou", "bing"]
        # env 覆盖（off 优先级最高）
        os.environ["MJC_KNOWLEDGE"] = "off"
        assert knowledge.configured_backends() == []
        os.environ["MJC_KNOWLEDGE"] = "baidu, sogou ,bing"
        assert knowledge.configured_backends() == ["baidu", "sogou", "bing"]
    finally:
        if old_env is None:
            os.environ.pop("MJC_KNOWLEDGE", None)
        else:
            os.environ["MJC_KNOWLEDGE"] = old_env
        knowledge._settings_cfg = old_cfg
    print("  ✅ configured_backends：settings/env/off 优先级")


def test_search_chain_and_cache():
    calls = []

    def fake_a(q, t):
        calls.append(("a", q))
        return []

    def fake_b(q, t):
        calls.append(("b", q))
        return [{"title": "t", "url": "", "text": "证据" * 30}]

    old_backends, old_http = knowledge.BACKENDS, knowledge._http_get
    knowledge.BACKENDS = {"a": fake_a, "b": fake_b}
    knowledge._CACHE.clear()
    knowledge._LAST_CALL.clear()
    try:
        r = knowledge.search("测试查询", backends=["a", "b"])
        assert r and r["backend"] == "b" and len(r["snippets"]) == 1, r
        # 缓存命中：不再调用
        n = len(calls)
        r2 = knowledge.search("测试查询", backends=["a", "b"])
        assert r2["backend"] == "b" and len(calls) == n, calls
        # 全部失败 → None（不抛）
        r3 = knowledge.search("另一个查询", backends=["a"])
        assert r3 is None
    finally:
        knowledge.BACKENDS = old_backends
        knowledge._http_get = old_http
    print("  ✅ search：按序降级、缓存命中、全失败安全返回")


def test_budget():
    old_b, old_fetch = knowledge.Budget, knowledge.fetch_evidence
    cnt = {"n": 0}

    def fake_fetch(q, backends=None):
        cnt["n"] += 1
        return {"backend": "x", "snippets": []}

    knowledge.fetch_evidence = fake_fetch
    try:
        b = knowledge.Budget(2)
        assert b.take("q1") is not None and b.take("q2") is not None
        assert b.take("q3") is None and cnt["n"] == 2
    finally:
        knowledge.Budget, knowledge.fetch_evidence = old_b, old_fetch
    print("  ✅ Budget：单次审查检索次数上限")


def test_evidence_section():
    from mjc import factcheck
    sec = factcheck._evidence_section({"backend": "sogou", "snippets": [{"text": "片段A"}, {"text": "片段B"}]})
    assert "外部检索片段" in sec and "sogou" in sec and "片段A" in sec
    assert factcheck._evidence_section(None) == ""
    print("  ✅ _evidence_section：格式化 + 空值安全")


def main():
    print("== knowledge 外部知识源离线测试（零网络）==")
    test_configured_backends()
    test_search_chain_and_cache()
    test_budget()
    test_evidence_section()
    print("== knowledge 全部通过 ✅ ==")


if __name__ == "__main__":
    main()
