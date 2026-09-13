#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_parallel_search.py — P3b 多后端并行检索（opt-in）离线测试（零网络，mock BACKENDS）
要点：默认关（串行早停语义不动）；开启后并行发请求、结果按原名次合并、去重一致、异常不中断。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import knowledge


def S(text, url=""):
    return {"title": text[:24], "url": url, "text": text}


def _setup(mapping):
    old = (knowledge.BACKENDS, dict(knowledge._CACHE),
           dict(knowledge._LAST_CALL), knowledge._MIN_INTERVAL, os.environ.get("MJC_KNOWLEDGE_PARALLEL"))
    knowledge.BACKENDS = mapping
    knowledge._CACHE.clear()
    knowledge._LAST_CALL.clear()
    knowledge._MIN_INTERVAL = 0
    os.environ.pop("MJC_KNOWLEDGE_PARALLEL", None)

    def restore():
        knowledge.BACKENDS = old[0]
        knowledge._CACHE.clear(); knowledge._CACHE.update(old[1])
        knowledge._LAST_CALL.clear(); knowledge._LAST_CALL.update(old[2])
        knowledge._MIN_INTERVAL = old[3]
        if old[4] is None:
            os.environ.pop("MJC_KNOWLEDGE_PARALLEL", None)
        else:
            os.environ["MJC_KNOWLEDGE_PARALLEL"] = old[4]

    return restore


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    slow = 0.35  # 每个后端假延迟

    def mk(name, n=2, delay=slow):
        def f(q, t):
            time.sleep(delay)
            return [S(f"{name}-{i}" * 30) for i in range(n)]
        return f

    backends = {n: mk(n) for n in "abc"}

    # ---- 默认：串行（不并行）----
    restore = _setup(dict(backends))
    try:
        t0 = time.time()
        r = knowledge.search("串行", backends=["a", "b", "c"], merge=True, min_total=10)
        dt = time.time() - t0
        check("默认串行：耗时 ≈ 3×单次延迟", dt >= slow * 2.5)
        check("默认串行：结果正常", r and len(r["snippets"]) >= 2)
    finally:
        restore()

    # ---- opt-in 并行 ----
    restore = _setup(dict(backends))
    try:
        t0 = time.time()
        r = knowledge.search("并行", backends=["a", "b", "c"], merge=True, min_total=10, parallel=True)
        dt = time.time() - t0
        check("并行开启：耗时 ≈ 1×单次延迟（< 3×）", dt < slow * 2.0)
        check("并行：合并全部 6 条", r and len(r["snippets"]) == 6 and r["fetched"] == 6)
        check("并行：backends_tried 覆盖全部按序", r and r["backends_tried"] == ["a", "b", "c"])
    finally:
        restore()

    # ---- 并行 + 去重（跨后端同文本只计一次）----
    def a(q, t):
        time.sleep(slow); return [S("共享" * 20), S("甲" * 40)]

    def b(q, t):
        time.sleep(slow); return [S("共享" * 20), S("乙" * 40)]

    restore = _setup({"a": a, "b": b, "c": mk("c")})
    try:
        r = knowledge.search("并行去重", backends=["a", "b", "c"], merge=True, min_total=10, parallel=True)
        check("并行去重：共享片段只计一次（2+2+2→5）", r and len(r["snippets"]) == 5)
    finally:
        restore()

    # ---- 并行 + 异常不中断 ----
    def boom(q, t):
        time.sleep(slow); raise RuntimeError("boom")

    restore = _setup({"a": boom, "b": mk("b", 3)})
    try:
        r = knowledge.search("并行异常", backends=["a", "b"], merge=True, min_total=10, parallel=True)
        check("并行异常：单后端爆不影响其它后端", r and len(r["snippets"]) == 3 and r["backend"] == "b")
    finally:
        restore()

    # ---- 并行：全空 → None ----
    restore = _setup({n: (lambda q, t: []) for n in "ab"})
    try:
        r = knowledge.search("并行全空", backends=["a", "b"], merge=True, parallel=True)
        check("并行全空 → None（不抛）", r is None)
    finally:
        restore()

    # ---- 开关：env / settings ----
    restore = _setup(dict(backends))
    try:
        os.environ["MJC_KNOWLEDGE_PARALLEL"] = "1"
        check("env=1 → 开关开", knowledge._parallel_enabled() is True)
        os.environ["MJC_KNOWLEDGE_PARALLEL"] = "0"
        check("env=0 → 开关关", knowledge._parallel_enabled() is False)
        os.environ.pop("MJC_KNOWLEDGE_PARALLEL", None)
        check("默认关（settings 未设）", knowledge._parallel_enabled() is False)
    finally:
        restore()

    print("== 并行检索（opt-in）全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
