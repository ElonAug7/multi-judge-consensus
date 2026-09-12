#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · factcheck 事实仲裁离线测试（零 API，mock providers.chat）
  python3 tests/test_factcheck.py

覆盖（2026-09-12 零幻觉架构 v0.6.0）：
  - confirmed：双仲裁都确认“原错+建议对” → confirmed
  - refuted：仲裁否证原错 / 否证建议值 → refuted（误杀防线）
  - unknown：票型 1:1 或调用异常 → unknown（不得据此替换）
  - _pick_arbiters：排除申诉人、跨厂优先
  - 去重 + max_issues 截断；summarize 计数
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import factcheck, providers

COMMITTEE = ["deepseek:deepseek-v4-flash", "glm:glm-4-flash", "glm:glm-4-plus"]


def _mk_chat(answers):
    calls = []

    def fake(name, messages, model=None, **kw):
        calls.append((name, model))
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    return fake, calls


def _ctx(answers):
    """临时替换 providers.chat / has_key；隔离外部知识源。返回 (fake, calls, restore)"""
    from mjc import knowledge as _kb
    old_chat, old_hk, old_cb = providers.chat, providers.has_key, _kb.configured_backends
    providers.has_key = lambda n: True
    _kb.configured_backends = lambda: []  # 测试隔离：不打网络
    fake, calls = _mk_chat(answers)
    providers.chat = fake

    def restore():
        providers.chat, providers.has_key = old_chat, old_hk
        _kb.configured_backends = old_cb

    return fake, calls, restore


def test_confirmed_and_arbiter_selection():
    answers = ['{"original_wrong":"yes","suggestion_correct":"yes","note":"ok"}',
               '{"original_wrong":"yes","suggestion_correct":"yes","note":"agree"}']
    fake, calls, restore = _ctx(answers)
    try:
        r = factcheck.arbitrate_issues("t", "c", [
            {"judge_id": "glm:glm-4-plus", "type": "factual_error",
             "desc": "1995 应为 1997", "sug": "改为1997"}], committee=COMMITTEE)
        it = r["items"][0]
        assert it["outcome"] == "confirmed", it
        assert r["calls"] == 2, r
        assert all(m != "glm-4-plus" for _, m in calls), calls
        assert [m for _, m in calls] == ["deepseek-v4-flash", "glm-4-flash"], calls  # 跨厂优先
    finally:
        restore()
    print("  ✅ confirmed：双仲裁确认 → confirmed；排除申诉人 + 跨厂优先")


def test_refuted_by_original():
    answers = ['{"original_wrong":"no","suggestion_correct":"no","note":"Victoria Raymond 是对的"}',
               '{"original_wrong":"no","suggestion_correct":"unknown","note":"原文无误"}']
    fake, calls, restore = _ctx(answers)
    try:
        r = factcheck.arbitrate_issues("t", "V2Ray原作者是Victoria Raymond。", [
            {"judge_id": "glm:glm-4-flash", "type": "factual_error",
             "desc": "作者应为 Allan", "sug": "更正为 Allan"}], committee=COMMITTEE)
        assert r["items"][0]["outcome"] == "refuted", r["items"][0]
    finally:
        restore()
    print("  ✅ refuted：仲裁否证原错 → refuted（误杀防线）")


def test_refuted_by_suggestion():
    answers = ['{"original_wrong":"unknown","suggestion_correct":"no","note":"建议值错误"}',
               '{"original_wrong":"yes","suggestion_correct":"no","note":"1983 不对"}']
    fake, calls, restore = _ctx(answers)
    try:
        r = factcheck.arbitrate_issues("t", "1997", [
            {"judge_id": "deepseek:deepseek-v4-flash", "type": "factual_error",
             "desc": "年份错", "sug": "改为1983"}], committee=COMMITTEE)
        assert r["items"][0]["outcome"] == "refuted", r["items"][0]
    finally:
        restore()
    print("  ✅ refuted：仲裁否证建议值 → refuted（瞎改防线）")


def test_unknown_and_error_votes():
    # 1:1 平票 → unknown
    answers = ['{"original_wrong":"yes","suggestion_correct":"yes"}',
               '{"original_wrong":"no","suggestion_correct":"unknown"}']
    fake, calls, restore = _ctx(answers)
    try:
        r = factcheck.arbitrate_issues("t", "c", [
            {"judge_id": "glm:glm-4-plus", "type": "hallucination", "desc": "d", "sug": "s"}],
            committee=COMMITTEE)
        assert r["items"][0]["outcome"] == "unknown", r["items"][0]
    finally:
        restore()
    # 调用异常 → error 票 → unknown，但 calls 计数
    answers = [RuntimeError("HTTP 503"), '{"original_wrong":"yes","suggestion_correct":"yes"}']
    fake, calls, restore = _ctx(answers)
    try:
        r = factcheck.arbitrate_issues("t", "c", [
            {"judge_id": "glm:glm-4-plus", "type": "factual_error", "desc": "d2", "sug": "s2"}],
            committee=COMMITTEE)
        assert r["items"][0]["outcome"] == "unknown", r["items"][0]
        assert r["calls"] == 2 and any("error" in v for v in r["items"][0]["votes"]), r
    finally:
        restore()
    # 单票否证 + 无人支持 → refuted（保守不对称）
    answers = ['{"original_wrong":"no","suggestion_correct":"no","note":"原文无误"}',
               '{"original_wrong":"unknown","suggestion_correct":"unknown","note":"不确定"}']
    fake, calls, restore = _ctx(answers)
    try:
        r = factcheck.arbitrate_issues("t", "c", [
            {"judge_id": "glm:glm-4-flash", "type": "factual_error", "desc": "d3", "sug": "s3"}],
            committee=COMMITTEE)
        assert r["items"][0]["outcome"] == "refuted", r["items"][0]
    finally:
        restore()
    print("  ✅ unknown：平票 / 异常票 → unknown；单票否证（无人反对）→ refuted（保守不对称）")


def test_dedupe_and_cap():
    answers = ['{"original_wrong":"yes","suggestion_correct":"yes"}'] * 6
    fake, calls, restore = _ctx(answers)
    try:
        issues = [
            {"judge_id": "a:x", "type": "factual_error", "desc": "同一问题", "sug": "s"},
            {"judge_id": "b:x", "type": "factual_error", "desc": "同一问题", "sug": "s"},  # 重复
            {"judge_id": "a:x", "type": "style", "desc": "风格", "sug": "s"},              # 非事实类（调用方已滤，但直接调用也应宽容）
            {"judge_id": "a:x", "type": "factual_error", "desc": "问题2", "sug": "s2"},
            {"judge_id": "a:x", "type": "factual_error", "desc": "问题3", "sug": "s3"},
            {"judge_id": "a:x", "type": "factual_error", "desc": "问题4", "sug": "s4"},
        ]
        r = factcheck.arbitrate_issues("t", "c", issues, committee=COMMITTEE)
        assert len(r["items"]) == 3, r  # 去重后 4 条事实类 → 截断为 3
        assert r["calls"] == 6, r
    finally:
        restore()
    print("  ✅ 去重 + max_issues=3 截断")


def test_multi_claimant_exclusion():
    """同一声明由同厂两家提出 → 两家都排除（只留独立第三方仲裁）"""
    answers = ['{"my_answer":"不确定","original_wrong":"unknown","suggestion_correct":"unknown"}']
    fake, calls, restore = _ctx(answers)
    try:
        r = factcheck.arbitrate_issues("t", "c", [
            {"judge_ids": ["glm:glm-4-flash", "glm:glm-4-plus"], "type": "factual_error",
             "desc": "同一声明", "sug": "s"}], committee=COMMITTEE)
        assert r["calls"] == 1, r  # 只剩 deepseek 一个独立仲裁
        assert calls == [("deepseek", "deepseek-v4-flash")], calls
    finally:
        restore()
    print("  ✅ 多申诉人排除：同厂两家都排除，仅留独立第三方")
    # 单人面板：只能否证/存疑，不能确认
    answers = ['{"my_answer":"1983","original_wrong":"yes","suggestion_correct":"yes"}']
    fake, calls, restore = _ctx(answers)
    try:
        r = factcheck.arbitrate_issues("t", "c", [
            {"judge_ids": ["glm:glm-4-flash", "glm:glm-4-plus"], "type": "factual_error",
             "desc": "单仲裁者场景", "sug": "s"}], committee=COMMITTEE)
        assert r["items"][0]["outcome"] == "unknown", r["items"][0]  # 单人 yes 不够格确认
    finally:
        restore()
    print("  ✅ 单人面板：单票 yes 不构成 confirmed（只能否证/存疑）")


def test_evidence_injection():
    """知识源启用时：检索片段注入仲裁提示（且不影响无源时的行为）"""
    from mjc import knowledge as _kb
    old_cb, old_budget = _kb.configured_backends, _kb.Budget
    old_hk = providers.has_key
    providers.has_key = lambda n: True  # 无 key 环境（CI）下需委员会可用

    class FakeBudget:
        def __init__(self, *a, **k):
            self.used = 0

        def take(self, q, backends=None):
            self.used += 1
            return {"backend": "fake", "query": q,
                    "snippets": [{"title": "t", "url": "", "text": "据某 blog：V2Ray 由 Victoria Raymond 开发。"}]}

    _kb.configured_backends = lambda: ["fake"]
    _kb.Budget = FakeBudget
    prompts = []

    def fake(name, messages, model=None, **kw):
        prompts.append(messages[0]["content"])
        return '{"my_answer":"Victoria Raymond","original_wrong":"no","suggestion_correct":"no","note":"ok"}'

    old_chat = providers.chat
    providers.chat = fake
    try:
        r = factcheck.arbitrate_issues("V2Ray原作者是谁？", "V2Ray原作者是Victoria Raymond。", [
            {"judge_ids": ["glm:glm-4-flash"], "type": "factual_error", "desc": "应为 Allan", "sug": "改为 Allan"}],
            committee=COMMITTEE)
        it = r["items"][0]
        assert it["outcome"] == "refuted", it
        assert it.get("evidence", {}).get("backend") == "fake", it
        assert any("外部检索片段" in p and "Victoria Raymond 开发" in p for p in prompts), "evidence 未注入提示"
    finally:
        providers.chat = old_chat
        _kb.configured_backends, _kb.Budget = old_cb, old_budget
        providers.has_key = old_hk
    print("  ✅ 知识源：检索片段注入仲裁提示 + 证据记录")


def main():
    print("== factcheck 事实仲裁离线测试（零 API）==")
    test_confirmed_and_arbiter_selection()
    test_refuted_by_original()
    test_refuted_by_suggestion()
    test_unknown_and_error_votes()
    test_dedupe_and_cap()
    test_multi_claimant_exclusion()
    test_evidence_injection()
    print("== factcheck 全部通过 ✅ ==")


if __name__ == "__main__":
    main()
