#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_evidence_independence.py — 证据独立性回归测试（v0.11.0 · 零 API/零网络）

背景（2026-09-13 实测发现的效度缺陷）：
  · 旧 `_evidence_query` = 任务截断 + 新增值 → 证据门**自我证明**：检索"2009"必然返回含
    2009 的页面，support 只反映"检索词命中"，不构成独立证据（P6-② 演示的 query 字段即为
    "香港平安钟协会最早成立于哪一年 2009"）。
  · 旧查询还含题面指令"请用一句话以内回答下面的问题："→ 抓取型后端命中"请"的字典页，
    实测 6/6 片段全是"请"字释义，证据全废。

本测试锁定两条不变量：
  1) 检索查询只含问题句：不含 old/new 值、不含指令模板；
  2) 证据门不得因"查询里带了答案"而判支持（自证）——只有检索结果**独立**含新值才算支持。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import knowledge, providers, repair

TASK = "请用一句话以内回答下面的问题：\n香港平安钟协会最早成立于哪一年？"
OLD, NEW = {"1997"}, {"2009"}


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    # ---- 1) 指令模板剥离 ----
    check("多行题面 → 只取问题句", repair.task_question(TASK) == "香港平安钟协会最早成立于哪一年？")
    check("halluqa 题面同样剥离",
          repair.task_question("请回答下面的问题：\n南方人都喜欢用的短视频APP是哪个？")
          == "南方人都喜欢用的短视频APP是哪个？")
    check("单行「指令：问题」→ 取冒号后",
          repair.task_question("请用一句话以内回答下面的问题：甲午战争爆发于哪一年？")
          == "甲午战争爆发于哪一年？")
    check("已是干净问题 → 原样返回",
          repair.task_question("香港平安钟协会最早成立于哪一年？")
          == "香港平安钟协会最早成立于哪一年？")
    check("冒号后非指令（题干含冒号）→ 不误剥",
          repair.task_question("下列说法正确的是：A 甲，B 乙")
          == "下列说法正确的是：A 甲，B 乙")
    check("空任务 → 空串", repair.task_question("") == "")

    # ---- 2) 查询不含 old/new 值 ----
    q = repair._evidence_query(TASK, OLD, NEW)
    check(f"查询不含新值 2009（实际 {q!r}）", "2009" not in q)
    check("查询不含旧值 1997", "1997" not in q)
    check("查询不含指令模板「请用一句话」", "请用一句话" not in q)
    check("查询 ≤80 字", len(q) <= 80)
    check("查询保留问题句主体", q == "香港平安钟协会最早成立于哪一年？")
    q2 = repair._evidence_query("甲" * 100, {"1997"}, {"2009"})
    check("长任务截断 ≤80 字", len(q2) <= 80 and q2.startswith("甲" * 80))

    # ---- 3) 自证封锁：关键词搜索引擎只在查询含该词时才返回它 ----
    old_fetch = knowledge.fetch_evidence

    def keyword_engine(query, *a, **k):
        """模拟关键词检索：结果包含查询里的词——即"你搜什么就给你什么"。"""
        snips = []
        for term in ("2009", "1997", "1983"):
            if term in query:
                snips.append({"title": f"关于 {term} 的资料", "url": f"http://x/{term}",
                              "text": f"香港平安钟协会成立时间 {term} 年。"})
        return {"backend": "mock", "query": query, "snippets": snips}

    try:
        knowledge.fetch_evidence = keyword_engine
        r = repair.evidence_check(TASK, OLD, NEW, min_snippets=2)
        check("自证封锁：查询不带答案 → 关键词引擎返回 0 条 → insufficient",
              r["verdict"] == "insufficient" and r["support"] == 0)
        check("自证封锁：query 字段本身不含 2009", "2009" not in r["query"])

        # 对照：说明旧实现为何危险——旧式查询（任务里带答案）在同一引擎下必然"自证"
        leaked_q = "香港平安钟协会最早成立于哪一年 2009"
        raw = keyword_engine(leaked_q)
        check("对照：旧式查询（答案混在检索词里）被同一引擎『自证』出支持片段",
              any("2009" in s["text"] for s in raw["snippets"]))
        # 保险：即便调用方把答案混进 task，新实现也会在构建查询时剥掉
        check("保险：答案混进 task 时被字面量剥离（不会自证）",
              "2009" not in repair._evidence_query(leaked_q, OLD, NEW))

        # ---- 4) 真·独立证据仍应放行（不能因噎废食） ----
        def independent_engine(query, *a, **k):
            return {"backend": "mock", "query": query,
                    "snippets": [{"title": "企业登记", "url": "http://a", "text": "成立时间：2009-01-06"},
                                 {"title": "协会官网", "url": "http://b", "text": "由 2009 年成立至今"}]}

        knowledge.fetch_evidence = independent_engine
        r2 = repair.evidence_check(TASK, OLD, NEW, min_snippets=2)
        check("独立证据（与查询无关地含新值）→ supported", r2["verdict"] == "supported" and r2["support"] == 2)
    finally:
        knowledge.fetch_evidence = old_fetch

    # ---- 5) 生产者失败外显（glm 欠费 429 类静默降级） ----
    old_chat, old_has = providers.chat, providers.has_key
    old_load = repair.__dict__.get("_resample_gate")

    def boom(provider, messages, **k):
        if provider == "glm":
            raise RuntimeError("[glm] HTTP 429: 余额不足或无可用资源包,请充值。")
        return "香港平安钟协会最早成立于1997年。"

    try:
        providers.chat = boom
        providers.has_key = lambda p: True
        r3 = repair.dual_revise(TASK, "香港平安钟协会最早成立于1997年。", "- [factual_error] 年份可能有误")
        check("单生产者失败 → mode=incomplete", r3.get("mode") == "incomplete")
        note = r3.get("note") or ""
        check("失败原因外显在 note（含 429/余额）", "429" in note or "余额" in note)
        check("producer_errors 结构化记录", bool(r3.get("producer_errors")))
    finally:
        providers.chat, providers.has_key = old_chat, old_has

    print("== 证据独立性（v0.11.0）全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
