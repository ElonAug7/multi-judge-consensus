#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MJC · 知识证据门 v1 离线测试（零 API/零网络，mock settings + knowledge）
覆盖：
  - ≥2 片段支持 → 放行；1 片段 → 阻断；片段含旧值 → 阻断（不计支持）
  - 检索异常/无结果 → 保守阻断；门关 → 不调用不阻断（默认行为不变）
  - 链式：resample inconclusive → 证据救援放行；conflict → 直接阻断（证据不可覆盖）
  - 查询构建：任务截断 ≤80 字 + 新增值
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import repair, providers, settings, knowledge

TASK = "香港平安钟协会最早成立于哪一年"
ORIG = "香港平安钟协会最早成立于1997年。"
CAND_A = "香港平安钟协会最早成立于2009年。"
CAND_B = "香港平安钟协会成立于2009年。"


def _ctx(behavior):
    """producers mock：按 model 名返回修订文本（同 test_repair 风格）。"""
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


def _mock_settings(repair_cfg):
    """mock settings.load → {"repair": repair_cfg}；返回 restore。"""
    old_load = settings.load
    settings.load = lambda: {"repair": repair_cfg}
    return lambda: setattr(settings, "load", old_load)


def _mock_fetch(result=None, exc=None):
    """mock knowledge.fetch_evidence；返回 (calls, restore)。"""
    old = knowledge.fetch_evidence
    calls = []

    def fake(q, backends=None, **kw):
        calls.append(q)
        if exc:
            raise exc
        return result

    knowledge.fetch_evidence = fake
    return calls, (lambda: setattr(knowledge, "fetch_evidence", old))


def _revise():
    """两位生产者输出 2009 版（值级共识达成），并返回 restore。"""
    calls, restore = _ctx({"deepseek-v4-flash": CAND_A, "glm-4-plus": CAND_B})
    return calls, restore


SNIP_2OK = [{"title": "", "url": "", "text": "香港平安钟协会于2009年成立，服务长者。"},
            {"title": "", "url": "", "text": "协会由多间机构合办，2009年投入服务。"},
            {"title": "", "url": "", "text": "与本题无关的片段内容。"}]


def test_gate_supports_apply():
    """≥2 条片段含新值且不含旧值 → 证据门放行（dual_revise 采纳替换）"""
    _, restore = _revise()
    rs = _mock_settings({"evidence_gate": {"enabled": True, "min_snippets": 2}})
    calls, rf = _mock_fetch({"backend": "mock", "snippets": SNIP_2OK})
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "agreed" and "2009" in r["applied"], r
        ev = r.get("evidence") or {}
        assert ev.get("verdict") == "supported" and ev.get("support") == 2, r
        assert ev.get("n_snippets") == 3 and "2009" in ev.get("query", ""), ev
        assert len(calls) == 1 and "2009" in calls[0], calls
    finally:
        rf(); rs(); restore()
    print("  ✅ supported：2/3 片段支持新值 → 放行（evidence 取证字段齐全）")


def test_gate_insufficient_blocks():
    """仅 1 条支持片段（< min_snippets=2）→ 阻断保留原文"""
    _, restore = _revise()
    rs = _mock_settings({"evidence_gate": {"enabled": True, "min_snippets": 2}})
    calls, rf = _mock_fetch({"backend": "mock", "snippets": [SNIP_2OK[0], SNIP_2OK[2]]})
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "disagreed" and r["applied"] == ORIG, r
        assert r.get("agreement") == "resample-evidence-insufficient", r
        assert (r.get("evidence") or {}).get("support") == 1, r
    finally:
        rf(); rs(); restore()
    print("  ✅ insufficient：1 条支持 → 阻断（resample-evidence-insufficient）")


def test_gate_old_value_in_snippet_blocks():
    """片段含新值但也含旧值（或仅旧值）→ 不计支持 → 阻断"""
    _, restore = _revise()
    rs = _mock_settings({"evidence_gate": {"enabled": True, "min_snippets": 2}})
    calls, rf = _mock_fetch({"backend": "mock", "snippets": [
        {"title": "", "url": "", "text": "该协会1997年成立，后于2009年更名。"},
        {"title": "", "url": "", "text": "资料称其最早成立于1997年。"},
    ]})
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "disagreed" and r["applied"] == ORIG, r
        assert (r.get("evidence") or {}).get("support") == 0, r
    finally:
        rf(); rs(); restore()
    print("  ✅ 含旧值：不计数 → 阻断（防旧值语境偷换）")


def test_gate_error_conservative():
    """检索异常 → verdict=error → 保守阻断（不误放行）"""
    _, restore = _revise()
    rs = _mock_settings({"evidence_gate": {"enabled": True, "min_snippets": 2}})
    calls, rf = _mock_fetch(exc=RuntimeError("网络超时"))
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "disagreed" and r["applied"] == ORIG, r
        assert r.get("agreement") == "resample-evidence-error", r
        assert (r.get("evidence") or {}).get("verdict") == "error", r
    finally:
        rf(); rs(); restore()
    print("  ✅ error：基础设施异常 → 保守阻断（resample-evidence-error）")


def test_gate_no_result_blocks():
    """检索无结果（后端全失败/未配置）→ 不足 → 阻断"""
    _, restore = _revise()
    rs = _mock_settings({"evidence_gate": {"enabled": True, "min_snippets": 2}})
    calls, rf = _mock_fetch(None)
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "disagreed" and r["applied"] == ORIG, r
        assert (r.get("evidence") or {}).get("n_snippets") == 0, r
    finally:
        rf(); rs(); restore()
    print("  ✅ 无结果：0 片段 → 阻断（保守）")


def test_gate_off_not_called_not_blocked():
    """证据门显式关闭 → 不调用检索、不阻断（其余配置照旧）"""
    _, restore = _revise()
    rs = _mock_settings({"evidence_gate": {"enabled": False, "min_snippets": 2}})
    calls, rf = _mock_fetch({"backend": "mock", "snippets": SNIP_2OK})
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "agreed" and "2009" in r["applied"], r
        assert "evidence" not in r and "resample" not in r, r
        assert calls == [], calls
    finally:
        rf(); rs(); restore()
    print("  ✅ 门关：不调用 fetch、不阻断、无 evidence 字段")


def test_default_no_gates_unchanged():
    """默认（无任何门配置）→ 维持原行为放行，零额外调用"""
    _, restore = _revise()
    rs = _mock_settings({})
    calls, rf = _mock_fetch({"backend": "mock", "snippets": SNIP_2OK})
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "agreed" and "2009" in r["applied"], r
        assert calls == [], calls
    finally:
        rf(); rs(); restore()
    print("  ✅ 默认两门都关：行为不变、不调用（默认行为绝不可变）")


def test_resample_inconclusive_evidence_rescues():
    """链式：重采样 inconclusive → 证据门 2 条支持 → 救援放行"""
    _, restore = _revise()
    old_gate = repair._resample_gate
    repair._resample_gate = lambda task, o, n: {
        "verdict": "inconclusive", "support": 1, "n": 3, "model": "mock"}
    rs = _mock_settings({"evidence_gate": {"enabled": True, "min_snippets": 2}})
    calls, rf = _mock_fetch({"backend": "mock", "snippets": SNIP_2OK})
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "agreed" and "2009" in r["applied"], r
        assert (r.get("resample") or {}).get("verdict") == "inconclusive", r
        assert (r.get("evidence") or {}).get("verdict") == "supported", r
    finally:
        rf(); rs(); repair._resample_gate = old_gate; restore()
    print("  ✅ 链式救援：resample inconclusive + 证据支持 → 放行（P6-① 目标场景）")


def test_resample_conflict_not_overridden():
    """链式：重采样 conflict → 直接阻断，证据不覆盖、不调用"""
    _, restore = _revise()
    old_gate = repair._resample_gate
    repair._resample_gate = lambda task, o, n: {
        "verdict": "conflict", "support": 0, "n": 3, "model": "mock"}
    rs = _mock_settings({"evidence_gate": {"enabled": True, "min_snippets": 2}})
    calls, rf = _mock_fetch({"backend": "mock", "snippets": SNIP_2OK})
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "disagreed" and r["applied"] == ORIG, r
        assert r.get("agreement") == "resample-conflict", r
        assert calls == [], calls  # conflict 直接阻断，证据不得覆盖
    finally:
        rf(); rs(); repair._resample_gate = old_gate; restore()
    print("  ✅ conflict 不可覆盖：直接阻断，证据检索不触发")


def test_resample_inconclusive_evidence_off_blocks():
    """链式：重采样 inconclusive + 证据门未启用 → 维持 v0.8.0 语义阻断"""
    _, restore = _revise()
    old_gate = repair._resample_gate
    repair._resample_gate = lambda task, o, n: {
        "verdict": "inconclusive", "support": 1, "n": 3, "model": "mock"}
    rs = _mock_settings({"evidence_gate": {"enabled": False}})
    calls, rf = _mock_fetch({"backend": "mock", "snippets": SNIP_2OK})
    try:
        r = repair.dual_revise(TASK, ORIG, "fb")
        assert r["mode"] == "disagreed" and r["applied"] == ORIG, r
        assert r.get("agreement") == "resample-inconclusive", r
        assert calls == [], calls
    finally:
        rf(); rs(); repair._resample_gate = old_gate; restore()
    print("  ✅ 证据门关：inconclusive 维持 v0.8.0 阻断（不引入新约束）")


def test_evidence_query_build():
    """查询构建：任务截断 ≤80 字 + 新增值（多值排序）"""
    q = repair._evidence_query("甲" * 100, {"2009"})
    assert len(q) == 80 + 1 + 4 and q.startswith("甲" * 80) and q.endswith("2009"), q
    q2 = repair._evidence_query("短任务", {"9", "10"})
    assert q2 == "短任务 10 9" or q2 == "短任务 9 10", q2  # sorted
    assert q2.split()[1:] == sorted(q2.split()[1:]), q2
    print("  ✅ 查询构建：≤80 字截断 + 新增值排序拼接")


def main():
    print("== 知识证据门 v1 离线测试（零 API/零网络）==")
    test_gate_supports_apply()
    test_gate_insufficient_blocks()
    test_gate_old_value_in_snippet_blocks()
    test_gate_error_conservative()
    test_gate_no_result_blocks()
    test_gate_off_not_called_not_blocked()
    test_default_no_gates_unchanged()
    test_resample_inconclusive_evidence_rescues()
    test_resample_conflict_not_overridden()
    test_resample_inconclusive_evidence_off_blocks()
    test_evidence_query_build()
    print("== 知识证据门全部通过 ✅ ==")


if __name__ == "__main__":
    main()
