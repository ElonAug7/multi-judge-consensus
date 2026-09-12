#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MJC · 证据检索加固 v2（多后端合并）离线测试（零网络，mock BACKENDS）

覆盖（P6-⑤ / v0.9.1）：
  - 合并去重：跨后端同 URL 或同归一化文本的 snippet 只计一次
  - 阈值停止：去重累积 ≥ min_total 即停（后续后端不再调用）
  - max_backends 上限：最多按序试 N 个后端（哪怕未达 min_total）
  - 单后端失败不中断：异常 → 继续尝试下一个（且不重试异常后端）
  - 空结果重试一次：同后端第二次调用；仍空 → 继续下一个；merge=False 不重试
  - merge=False 输出兼容：键集合与原路径一致（无 backends_tried/fetched），缓存照旧
  - merge 不读写缓存：不污染非 merge 结果
  - 全空 → 正常空返（None，不抛）
  - 接线：evidence_check 默认 merge=True；_evidence_gate 读 merge_backends 开关
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import knowledge, repair


def S(text, url=""):
    return {"title": text[:24], "url": url, "text": text}


def _setup(mapping):
    """替换 BACKENDS / 关掉限速间隔 / 清缓存；返回 restore。"""
    old = (knowledge.BACKENDS, dict(knowledge._CACHE),
           dict(knowledge._LAST_CALL), knowledge._MIN_INTERVAL)
    knowledge.BACKENDS = mapping
    knowledge._CACHE.clear()
    knowledge._LAST_CALL.clear()
    knowledge._MIN_INTERVAL = 0  # 测试确定性与速度：不真的 sleep

    def restore():
        knowledge.BACKENDS = old[0]
        knowledge._CACHE.clear()
        knowledge._CACHE.update(old[1])
        knowledge._LAST_CALL.clear()
        knowledge._LAST_CALL.update(old[2])
        knowledge._MIN_INTERVAL = old[3]

    return restore


def test_merge_dedupe_cross_backend():
    """同 URL / 同归一化文本跨后端只计一次；重复不算新增、不重复计数"""
    calls = []

    def a(q, t):
        calls.append("a")
        return [S("片段甲" * 30), S("共享片段乙" * 12, url="http://example.com/1")]

    def b(q, t):
        calls.append("b")
        return [S("共享片段乙" * 12, url=""),                       # 归一化文本相同 → 去重
                S("另一段完全不同的文本内容", url="HTTP://Example.COM/1"),  # URL 相同（大小写归一）→ 去重
                S("片段丙" * 30)]                                  # 新增 1 条

    def c(q, t):
        calls.append("c")
        return [S("共享片段乙" * 12, url="http://other.com/2")]      # 文本同 → 去重

    restore = _setup({"a": a, "b": b, "c": c})
    try:
        r = knowledge.search("去重", backends=["a", "b", "c"], merge=True, min_total=5)
        assert r is not None, r
        assert len(r["snippets"]) == 3 and r["fetched"] == 3, r
        assert r["backend"] == "a+b", r          # c 全部重复 → 不计入合并标记
        assert r["backends_tried"] == ["a", "b", "c"], r
        assert calls == ["a", "b", "c"], calls
    finally:
        restore()
    print("  ✅ 合并去重：同 URL / 同归一化文本跨后端只计一次（a+b；c 全重复被去重）")


def test_merge_stop_on_min_total():
    """累积 ≥ min_total 即停：足够时后续后端不再调用"""
    calls = []

    def a(q, t):
        calls.append("a")
        return [S("甲" * 40), S("乙" * 40), S("丙" * 40)]   # 一次就给够

    def b(q, t):
        calls.append("b")
        return [S("丁" * 40)]

    restore = _setup({"a": a, "b": b})
    try:
        r = knowledge.search("阈值1", backends=["a", "b"], merge=True)
        assert calls == ["a"], calls                        # b 未被调用
        assert len(r["snippets"]) == 3 and r["backend"] == "a", r
    finally:
        restore()
    calls2 = []

    def a2(q, t):
        calls2.append("a2")
        return [S("甲" * 40), S("乙" * 40)]                  # 2 < 3

    def b2(q, t):
        calls2.append("b2")
        return [S("丙" * 40), S("丁" * 40)]                  # +2 → 4 ≥ 3 → 停

    def c2(q, t):
        calls2.append("c2")
        return [S("戊" * 40)]

    restore = _setup({"a2": a2, "b2": b2, "c2": c2})
    try:
        r2 = knowledge.search("阈值2", backends=["a2", "b2", "c2"], merge=True)
        assert calls2 == ["a2", "b2"], calls2               # c2 未被调用
        assert len(r2["snippets"]) == 4 and r2["backend"] == "a2+b2", r2
    finally:
        restore()
    print("  ✅ 阈值停止：累积 ≥ min_total(3) 即停（单后端够 / 后端间累积够 两式）")


def test_merge_max_backends_cap():
    """试满 max_backends 即停（未达 min_total 也停）；参数可调"""
    calls = []

    def mk(name):
        def f(q, t):
            calls.append(name)
            return [S(name * 30)]
        return f

    restore = _setup({n: mk(n) for n in "abcd"})
    try:
        r = knowledge.search("上限", backends=["a", "b", "c", "d"], merge=True, min_total=10)
        assert calls == ["a", "b", "c"], calls              # 默认最多 3 个；d 未试
        assert r["backends_tried"] == ["a", "b", "c"] and len(r["snippets"]) == 3, r
        calls.clear()
        r2 = knowledge.search("上限2", backends=["a", "b", "c", "d"], merge=True,
                              min_total=10, max_backends=2)
        assert calls == ["a", "b"], calls                   # 上限 2
        assert len(r2["snippets"]) == 2 and r2["backends_tried"] == ["a", "b"], r2
    finally:
        restore()
    print("  ✅ max_backends 上限：最多按序试 N 个后端（含参数化样例）")


def test_merge_exception_continues():
    """单后端异常不中断：继续下一个；异常后端不触发重试"""
    calls = []

    def a(q, t):
        calls.append("a")
        raise RuntimeError("boom")

    def b(q, t):
        calls.append("b")
        return [S("乙1" * 40)]

    def c(q, t):
        calls.append("c")
        return [S("丙1" * 40), S("丙2" * 40)]

    restore = _setup({"a": a, "b": b, "c": c})
    try:
        r = knowledge.search("异常", backends=["a", "b", "c"], merge=True)
        assert calls == ["a", "b", "c"], calls               # 异常后继续；无重试
        assert calls.count("a") == 1, calls
        assert len(r["snippets"]) == 3 and r["backend"] == "b+c", r
    finally:
        restore()
    print("  ✅ 失败不中断：异常 → 直接尝试下一后端（不重试异常后端）")


def test_merge_empty_retry_once():
    """空结果对同后端重试恰好 1 次（成功即用；仍空 → 下一后端）"""
    calls = []
    state = {"a": 0}

    def a(q, t):
        calls.append("a")
        state["a"] += 1
        return [] if state["a"] == 1 else [S("甲" * 40), S("乙" * 40), S("丙" * 40)]

    def b(q, t):
        calls.append("b")
        return [S("丁" * 40)]

    restore = _setup({"a": a, "b": b})
    try:
        r = knowledge.search("空重试1", backends=["a", "b"], merge=True)
        assert calls == ["a", "a"], calls                    # 第一次空 → 重试成功 → 停；b 未调用
        assert r["backend"] == "a" and len(r["snippets"]) == 3, r
    finally:
        restore()
    calls2 = []

    def a2(q, t):
        calls2.append("a2")
        return []

    def b2(q, t):
        calls2.append("b2")
        return [S("乙" * 40), S("丙" * 40), S("丁" * 40)]

    restore = _setup({"a2": a2, "b2": b2})
    try:
        r2 = knowledge.search("空重试2", backends=["a2", "b2"], merge=True)
        assert calls2 == ["a2", "a2", "b2"], calls2          # a2 恰好重试 1 次，不三连
        assert r2["backend"] == "b2" and len(r2["snippets"]) == 3, r2
    finally:
        restore()
    print("  ✅ 空结果重试：同一后端恰好重试 1 次（成功即用 / 仍空 → 下一后端）")


def test_no_retry_when_merge_false():
    """merge=False：空结果直接下一个后端（旧行为，不重试）"""
    calls = []

    def a(q, t):
        calls.append("a")
        return []

    def b(q, t):
        calls.append("b")
        return [S("乙" * 40)]

    restore = _setup({"a": a, "b": b})
    try:
        r = knowledge.search("不重试", backends=["a", "b"], merge=False)
        assert calls == ["a", "b"], calls
        assert r["backend"] == "b" and len(r["snippets"]) == 1, r
    finally:
        restore()
    print("  ✅ merge=False：空结果不重试（保持旧路径语义）")


def test_merge_false_unchanged():
    """merge=False 输出与原路径一致：键集合不变、缓存照旧、全失败 None"""
    calls = []

    def a(q, t):
        calls.append("a")
        return []

    def b(q, t):
        calls.append("b")
        return [S("乙" * 40)]

    restore = _setup({"a": a, "b": b})
    try:
        r = knowledge.search("兼容", backends=["a", "b"], merge=False)
        assert set(r.keys()) == {"backend", "query", "snippets"}, r   # 无 fetched/backends_tried
        assert r["backend"] == "b" and r["query"] == "兼容" and len(r["snippets"]) == 1, r
        n = len(calls)
        r2 = knowledge.search("兼容", backends=["a", "b"], merge=False)
        assert r2 == r and len(calls) == n, (r2, calls)              # 缓存命中：不再调用
        r3 = knowledge.search("兼容空", backends=["a"], merge=False)
        assert r3 is None
    finally:
        restore()
    print("  ✅ merge=False 兼容：键与原路径一致、缓存照旧、全失败返回 None")


def test_merge_cache_isolation():
    """merge 不读不写缓存：非 merge 结果不被污染"""
    calls = []

    def a(q, t):
        calls.append("a")
        return [S("甲" * 40)]

    def b(q, t):
        calls.append("b")
        return [S("乙" * 40), S("丙" * 40)]

    restore = _setup({"a": a, "b": b})
    try:
        r1 = knowledge.search("隔离", backends=["a"], merge=False)        # 写缓存（a 单条）
        assert r1["backend"] == "a" and len(r1["snippets"]) == 1, r1
        r2 = knowledge.search("隔离", backends=["a", "b"], merge=True)    # 不读缓存：真实合并
        assert r2["backend"] == "a+b" and len(r2["snippets"]) == 3, r2
        r3 = knowledge.search("隔离", backends=["a"], merge=False)        # 缓存未被污染
        assert r3 == r1 and r3["backend"] == "a", (r3, r1)
        assert calls == ["a", "a", "b"], calls
    finally:
        restore()
    print("  ✅ 缓存隔离：merge 不读不写缓存，非 merge 结果不受污染")


def test_merge_all_empty_none():
    """全空 → 每后端重试一次后正常空返 None（不抛）"""
    calls = []

    def mk(name):
        def f(q, t):
            calls.append(name)
            return []
        return f

    restore = _setup({n: mk(n) for n in "abc"})
    try:
        r = knowledge.search("全空", backends=["a", "b", "c"], merge=True)
        assert r is None, r
        assert calls == ["a", "a", "b", "b", "c", "c"], calls    # 每后端重试 1 次后放弃
    finally:
        restore()
    print("  ✅ 全空：每后端重试一次后正常空返 None（不抛）")


def test_merge_unknown_backend_skip():
    """名单里的未知后端：跳过且不占 max_backends 名额"""
    calls = []

    def a(q, t):
        calls.append("a")
        return [S("甲" * 40), S("乙" * 40), S("丙" * 40)]

    def b(q, t):
        calls.append("b")
        return [S("丁" * 40)]

    restore = _setup({"a": a, "b": b})
    try:
        r = knowledge.search("未知", backends=["nope", "a", "b"], merge=True)
        assert calls == ["a"], calls
        assert r["backends_tried"] == ["a"] and r["backend"] == "a", r
    finally:
        restore()
    print("  ✅ 未知后端：跳过、不占名额、不报错")


def test_evidence_check_merge_wiring():
    """接线：evidence_check 默认 merge=True；_evidence_gate 读 merge_backends（默认 True）"""
    from mjc import settings
    seen = []
    old_fetch = knowledge.fetch_evidence

    def fake_fetch(q, backends=None, **kw):
        seen.append(kw.get("merge"))
        return {"backend": "mock", "snippets": []}

    knowledge.fetch_evidence = fake_fetch
    try:
        r = repair.evidence_check("任务", {"1997"}, {"2009"})
        assert seen == [True] and r["verdict"] == "insufficient", (seen, r)
        seen.clear()
        repair.evidence_check("任务", {"1997"}, {"2009"}, merge=False)
        assert seen == [False], seen
    finally:
        knowledge.fetch_evidence = old_fetch
    old_load = settings.load
    try:
        knowledge.fetch_evidence = fake_fetch
        settings.load = lambda: {"repair": {"evidence_gate": {"enabled": True, "merge_backends": False}}}
        seen.clear()
        repair._evidence_gate("任务", {"1997"}, {"2009"})
        assert seen == [False], seen                      # 显式 false → 旧单后端路径
        settings.load = lambda: {"repair": {"evidence_gate": {"enabled": True}}}
        seen.clear()
        repair._evidence_gate("任务", {"1997"}, {"2009"})
        assert seen == [True], seen                       # 缺省 → 默认 True
        settings.load = lambda: {"repair": {"evidence_gate": {"enabled": False}}}
        seen.clear()
        assert repair._evidence_gate("任务", {"1997"}, {"2009"}) is None and seen == [], seen
    finally:
        knowledge.fetch_evidence = old_fetch
        settings.load = old_load
    print("  ✅ 接线：evidence_check 默认 merge=True；_evidence_gate 增读 merge_backends（默认 true）")


def test_fetch_evidence_passthrough():
    """fetch_evidence：merge/min_total/max_backends 透传 search（默认路径不变）"""
    old_search = knowledge.search
    got = {}

    def fake_search(query, backends=None, timeout=None, merge=False,
                    min_total=3, max_backends=3, retry_empty=True):
        got.update(query=query, backends=backends, merge=merge,
                   min_total=min_total, max_backends=max_backends)
        return {"backend": "x", "query": query, "snippets": []}

    knowledge.search = fake_search
    try:
        knowledge.fetch_evidence("Q1")
        assert got == {"query": "Q1", "backends": None, "merge": False,
                       "min_total": 3, "max_backends": 3}, got
        knowledge.fetch_evidence("Q2", merge=True, min_total=5, max_backends=2)
        assert got == {"query": "Q2", "backends": None, "merge": True,
                       "min_total": 5, "max_backends": 2}, got
    finally:
        knowledge.search = old_search
    print("  ✅ fetch_evidence 透传：默认非 merge / 可选 merge+阈值参数")


def main():
    print("== 证据检索加固 v2（多后端合并）离线测试（零网络）==")
    test_merge_dedupe_cross_backend()
    test_merge_stop_on_min_total()
    test_merge_max_backends_cap()
    test_merge_exception_continues()
    test_merge_empty_retry_once()
    test_no_retry_when_merge_false()
    test_merge_false_unchanged()
    test_merge_cache_isolation()
    test_merge_all_empty_none()
    test_merge_unknown_backend_skip()
    test_evidence_check_merge_wiring()
    test_fetch_evidence_passthrough()
    print("== 证据检索加固 v2 全部通过 ✅ ==")


if __name__ == "__main__":
    main()
