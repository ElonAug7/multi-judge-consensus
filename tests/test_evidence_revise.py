#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_evidence_revise.py — P2 检索增强修订（RARR 式）离线测试（零 API/零网络）
背景：证据原先只当"事后闸门"，从不喂给生产者 → 生产者不知道正确值 → 改不动（csqa-* "agree 但没改"）。
修复：evidence_gate 开 或 evidence_revise.enabled 开 → 先检索，把片段注入生产者提示词。
本测试 mock providers.chat / knowledge.fetch_evidence / settings.load，断言提示词内容与门控。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import repair, providers, knowledge, settings


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    old_chat, old_has = providers.chat, providers.has_key
    old_fetch, old_load = knowledge.fetch_evidence, settings.load

    captured = []
    fetched = []

    def fake_chat(provider, messages, **k):
        captured.append(messages)
        return "香港平安钟协会最早成立于2009年。"

    def fake_fetch(query, *a, **k):
        fetched.append(query)
        return {"backend": "mock", "snippets": [{"text": "香港平安钟协会于2009年成立，服务至今。",
                                                 "title": "", "url": "http://x1"},
                                                {"text": "该公司登记资料显示 2009 年设立。",
                                                 "title": "", "url": "http://x2"}]}

    try:
        providers.chat = fake_chat
        providers.has_key = lambda p: True
        knowledge.fetch_evidence = fake_fetch

        # ---- 1) evidence_revise 显式开 → 证据注入生产者 ----
        settings.load = lambda: {"repair": {"evidence_gate": {"enabled": True},
                                            "evidence_revise": {"enabled": True}}}
        captured.clear(); fetched.clear()
        r = repair.dual_revise("香港平安钟协会最早成立于哪一年？", "香港平安钟协会最早成立于1997年。",
                               "- [factual_error] 年份可能有误")
        check("证据门开 → 确实发起了检索", len(fetched) >= 1)
        joined = "\n".join(str(m.get("content")) for m in (captured[0] if captured else []))
        check("生产者提示词含外部证据片段（2009）", "2009" in joined and "外部检索证据" in joined)
        check("结果标记 evidence_revise.snippets=2", (r.get("evidence_revise") or {}).get("snippets") == 2)
        check("两生产者一致 → mode=agreed", r.get("mode") == "agreed")
        check("采纳版含正确值（2009）", "2009" in (r.get("applied") or ""))

        # ---- 1b) 默认关：仅证据门开、未显式开 evidence_revise → 不注入（不影响既有路径）----
        settings.load = lambda: {"repair": {"evidence_gate": {"enabled": True}}}
        captured.clear(); fetched.clear()
        r1b = repair.dual_revise("题目", "原答案 1997年。", "- [factual_error] x")
        j1b = "\n".join(str(m.get("content")) for m in (captured[0] if captured else []))
        check("默认关：仅证据门开不注入（opt-in）", "外部检索证据" not in j1b and
              (r1b.get("evidence_revise") or {}).get("enabled") is False)

        # ---- 2) 都没开 → 不检索、不注入、行为同旧 ----
        settings.load = lambda: {"repair": {"evidence_gate": {"enabled": False}}}
        captured.clear(); fetched.clear()
        r2 = repair.dual_revise("题目", "原答案 1997年。", "- [factual_error] x")
        check("证据门关 → 不检索", len(fetched) == 0)
        j2 = "\n".join(str(m.get("content")) for m in (captured[0] if captured else []))
        check("提示词不含证据段", "外部检索证据" not in j2)
        check("evidence_revise.enabled=False", (r2.get("evidence_revise") or {}).get("enabled") is False)

        # ---- 3) 显式 evidence_revise.enabled=true（证据门关）→ 仍注入 ----
        settings.load = lambda: {"repair": {"evidence_gate": {"enabled": False},
                                            "evidence_revise": {"enabled": True, "max_snippets": 3}}}
        captured.clear(); fetched.clear()
        r3 = repair.dual_revise("题目", "原答案 1997年。", "- [factual_error] x")
        check("显式开启 → 独立生效（有检索）", len(fetched) >= 1 and (r3.get("evidence_revise") or {}).get("enabled") is True)

        # ---- 4) 检索失败/空 → 不注入、不崩 ----
        def boom_fetch(*a, **k):
            raise RuntimeError("net down")
        knowledge.fetch_evidence = boom_fetch
        settings.load = lambda: {"repair": {"evidence_gate": {"enabled": True}}}
        captured.clear()
        r4 = repair.dual_revise("题目", "原答案 1997年。", "- [factual_error] x")
        j4 = "\n".join(str(m.get("content")) for m in (captured[0] if captured else []))
        check("检索异常 → 静默降级（无证据段、仍产出）", "外部检索证据" not in j4 and r4.get("mode") in ("agreed", "disagreed", "incomplete"))

        # ---- 5) _evidence_rules 格式 ----
        txt = repair._evidence_rules(["片段A", "片段B"])
        check("_evidence_rules：编号 + 约束句", "1. 片段A" in txt and "2. 片段B" in txt and "忽略之" in txt)
        check("_evidence_rules：空 → 空串", repair._evidence_rules([]) == "")
    finally:
        providers.chat, providers.has_key = old_chat, old_has
        knowledge.fetch_evidence, settings.load = old_fetch, old_load

    print("== 检索增强修订（RARR）全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
