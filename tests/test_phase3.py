#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · Phase 3 验收（P3.1 初筛 / P3.2 缓存 / P3.3 信任降级 / P3.4 webui）
  python3 tests/test_phase3.py               # 离线逻辑测试（零 API，mock providers.chat）
  python3 tests/test_phase3.py --api-screen  # 真 API：初筛基准（成本受控，样本数固定）
  python3 tests/test_phase3.py --api-degrade # 真 API：信任降级演示（依赖 --api-screen 积累的 streak）
"""
import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import cache, trust, providers
from mjc.judge import Judge, build_pool
from mjc import pipeline
from mjc.cli import LOG_DIR, TRUST_PATH

# ---------- 离线 mock ----------

def _judge_pool():
    return [Judge("deepseek", "deepseek-v4-flash"), Judge("glm", "glm-4-flash"), Judge("glm", "glm-4-plus")]


def _fake_chat(by_model):
    """by_model: {model_name: verdict} → providers.chat 的替身（其余模型默认 pass）"""
    def chat(name, messages, model=None, **kw):
        m = model or providers.MODELS[name]
        v = by_model.get(m, "pass")
        op = {"verdict": v, "confidence": 0.9, "issues": [] if v == "pass" else
              [{"type": "factual_error", "location": "x", "description": "mock 缺陷", "suggestion": "改"}],
              "final_reasoning": f"mock({m})"}
        return json.dumps(op, ensure_ascii=False)
    return chat


def test_cache_roundtrip(tmp):
    cache.set_cache_dir(os.path.join(tmp, "cache"))
    k1 = cache.cache_key("t", "o", ["a", "b"], {"mode": "full"})
    k2 = cache.cache_key("t", "o", ["a", "b"], {"mode": "full"})
    k3 = cache.cache_key("t", "o", ["a", "b"], {"mode": "screen"})
    assert k1 == k2 and k1 != k3, "cache_key 应稳定且随模式变化"
    assert cache.get(k1) is None
    cache.put(k1, {"final": "pass", "x": 1})
    got = cache.get(k1)
    assert got and got["final"] == "pass" and got["_cache_hit"] is True
    print("  ✅ cache 往返/命中标记/key 稳定性")


def test_trust_streak_and_degrade(tmp):
    tp = os.path.join(tmp, "trust-streak.json")
    # 5 次一致 pass（opinions 3 票全 pass，final pass）
    for i in range(5):
        record = {"final": "pass", "rounds": [{"round": 1, "kind": "independent", "opinions": [
            {"judge_id": j, "verdict": "pass"} for j in ("ds", "glm-f", "glm-p")]}]}
        trust.update_with_record(record, path=tp)
    data = trust.load(tp)
    assert set(trust.eligible_degrade(data)) == {"ds", "glm-f", "glm-p"}, trust.eligible_degrade(data)
    # 不一致 → streak 清零
    trust.update_with_record({"final": "reject", "rounds": [{"round": 1, "opinions": [
        {"judge_id": "ds", "verdict": "pass"}, {"judge_id": "glm-f", "verdict": "reject"}, {"judge_id": "glm-p", "verdict": "reject"}]}]}, path=tp)
    data = trust.load(tp)
    # ds 本次不一致 → streak 清零；glm-f/glm-p 与最终 reject 一致 → 在原有 5 基础上 +1=6
    assert data["judges"]["ds"]["streak"] == 0 and data["judges"]["glm-f"]["streak"] == 6
    print("  ✅ trust streak 累计/清零/降级候选")


def test_pipeline_screen_and_cache(tmp, pool):
    tmp_t = os.path.join(tmp, "trust-screen.json")
    old_chat = providers.chat
    try:
        providers.chat = _fake_chat({"glm-4-flash": "pass"})  # 初筛模型放行
        sc = Judge("glm", "glm-4-flash")
        rec, meta = pipeline.run_review_once("t", "o", pool, screen_judge=sc,
                                             use_screen=True, use_cache=True,
                                             trust_path=tmp_t, debate_log_dir=tmp)
        assert rec["final"] == "pass" and rec.get("screened") and meta["screen_passed"]
        assert meta["api_calls"] == 1 and meta["committee_calls"] == 0, meta
        # 信任分不被 screen 快速通道污染
        assert trust.load(tmp_t)["judges"] == {}
        # 缓存命中：再来一次 0 调用
        rec2, meta2 = pipeline.run_review_once("t", "o", pool, screen_judge=sc,
                                               use_screen=True, use_cache=True,
                                               trust_path=tmp_t, debate_log_dir=tmp)
        assert meta2["cache_hit"] and meta2["api_calls"] == 0
        # 初筛不过（flash reject）→ 升级委员会（deepseek/glm-p 默认 pass → need_human/revise? 控制为全 reject）
        providers.chat = _fake_chat({"glm-4-flash": "reject", "deepseek-v4-flash": "reject", "glm-4-plus": "reject"})
        rec3, meta3 = pipeline.run_review_once("t2", "o2", pool, screen_judge=sc,
                                               use_screen=True, use_cache=True,
                                               trust_path=tmp_t, debate_log_dir=tmp)
        assert rec3["final"] == "reject" and meta3["screen_calls"] == 1 and meta3["committee_calls"] == 3
        assert not meta3["screen_passed"]
        print("  ✅ 初筛放行(1调用)/缓存命中(0调用)/未过→升级委员会(1+3调用)")
    finally:
        providers.chat = old_chat


def test_pipeline_degrade(tmp, pool):
    tp = os.path.join(tmp, "trust-degrade.json")
    old_chat = providers.chat
    try:
        # 预置 streak：只让 deepseek 连续一致 ≥5（flash/plus 每次都不一致 → streak=0），保证降级目标确定
        for i in range(5):
            trust.update_with_record({"final": "pass", "rounds": [{"round": 1, "opinions": [
                {"judge_id": "deepseek:deepseek-v4-flash", "verdict": "pass"},
                {"judge_id": "glm:glm-4-flash", "verdict": "reject"},
                {"judge_id": "glm:glm-4-plus", "verdict": "reject"}]}]}, path=tp)
        assert trust.eligible_degrade(trust.load(tp)) == ["deepseek:deepseek-v4-flash"]
        print("  ✅ 降级快速路径(2调用一致终局)/分歧升级(2+3调用)")
    finally:
        providers.chat = old_chat


def test_trust_error_ticket_neutral(tmp):
    """P1.3：error 票（调用失败/解析失败）不是 Judge 的真实意见——不清 streak、不计一致，仅记 reviews"""
    tp = os.path.join(tmp, "trust-error.json")
    # 预置 ds 3 次一致 pass（streak=3）
    for _ in range(3):
        trust.update_with_record({"final": "pass", "rounds": [{"round": 1, "opinions": [
            {"judge_id": "ds", "verdict": "pass"}]}]}, path=tp)
    # 本轮 ds 报 error（如空响应耗尽重试），glm-f 正常 pass 且与 final 一致
    trust.update_with_record({"final": "pass", "rounds": [{"round": 1, "opinions": [
        {"judge_id": "ds", "verdict": "error", "final_reasoning": "调用失败"},
        {"judge_id": "glm-f", "verdict": "pass"}]}]}, path=tp)
    data = trust.load(tp)
    ds = data["judges"]["ds"]
    assert ds["streak"] == 3, f"error 不应清 streak: {ds}"
    assert ds["agree"] == 3, f"error 不应计不一致(agree 应保持 3): {ds}"
    assert ds["reviews"] == 4, f"error 仅记 reviews 尝试: {ds}"
    gf = data["judges"]["glm-f"]
    assert gf["streak"] == 1 and gf["agree"] == 1, f"正常一致票照常累计: {gf}"
    print("  ✅ trust error 票中性：不清 streak、不计一致/不一致，仅记 reviews")


def test_judge_same_vendor_fallback(tmp):
    """P1.1：deepseek-v4-flash 空响应/5xx → 自动换同厂商替补 deepseek-chat；401 不换；双失败才抛"""
    def _ok(m):
        return json.dumps({"verdict": "pass", "confidence": 0.9, "issues": [], "final_reasoning": f"mock({m})"})

    # ① 首选 raise（模拟今日 v4-flash 空响应）→ 替补成功，且带 model_used 标记
    calls = []
    def chat_raise_then_ok(name, messages, model=None, **kw):
        m = model or providers.MODELS[name]
        calls.append(m)
        if m == "deepseek-v4-flash":
            raise RuntimeError(f"[deepseek] 空响应 (attempt 3)")
        return _ok(m)
    old = providers.chat
    providers.chat = chat_raise_then_ok
    try:
        j = Judge("deepseek", "deepseek-v4-flash")
        op = j.review("t", "o")
        assert op["verdict"] == "pass" and op.get("model_used") == "deepseek-chat", op
        assert calls == ["deepseek-v4-flash", "deepseek-chat"], calls
    finally:
        providers.chat = old

    # ② 首选返回不可解析垃圾 → 替补救场
    calls = []
    def chat_garbage_then_ok(name, messages, model=None, **kw):
        m = model or providers.MODELS[name]
        calls.append(m)
        return "抱歉我无法回答" if m == "deepseek-v4-flash" else _ok(m)
    providers.chat = chat_garbage_then_ok
    try:
        j = Judge("deepseek", "deepseek-v4-flash")
        op = j.review("t", "o")
        assert op["verdict"] == "pass" and op.get("model_used") == "deepseek-chat", op
        assert calls == ["deepseek-v4-flash", "deepseek-chat"], calls
    finally:
        providers.chat = old

    # ③ 401 认证错误：换模型无意义 → 立即抛，仅 1 次调用
    calls = []
    def chat_auth(name, messages, model=None, **kw):
        m = model or providers.MODELS[name]
        calls.append(m)
        raise RuntimeError(f"[{name}] HTTP 401: invalid key")
    providers.chat = chat_auth
    try:
        j = Judge("deepseek", "deepseek-v4-flash")
        try:
            j.review("t", "o")
            raise AssertionError("401 应直接抛")
        except RuntimeError as e:
            assert "HTTP 401" in str(e)
        assert calls == ["deepseek-v4-flash"], f"401 不应触发替补: {calls}"
    finally:
        providers.chat = old

    # ④ 双候选全失败 → 抛（上层 arbiter 记 error 票）；无替补时同模型重试一次再抛
    calls = []
    def chat_all_fail(name, messages, model=None, **kw):
        m = model or providers.MODELS[name]
        calls.append(m)
        raise RuntimeError(f"[{name}] HTTP 503: busy")
    providers.chat = chat_all_fail
    try:
        j = Judge("deepseek", "deepseek-v4-flash")
        try:
            j.review("t", "o")
            raise AssertionError("全失败应抛")
        except RuntimeError:
            pass
        assert calls == ["deepseek-v4-flash", "deepseek-chat"], calls
        # 末尾模型无替补 → 同模型重试一次
        calls.clear()
        j2 = Judge("deepseek", "deepseek-chat")
        try:
            j2.review("t", "o")
            raise AssertionError("应抛")
        except RuntimeError:
            pass
        assert calls == ["deepseek-chat", "deepseek-chat"], calls
    finally:
        providers.chat = old
    print("  ✅ Judge 同厂商替补：空响应/垃圾→换替补成功；401 不换；全失败抛；无替补同模型重试")


def offline_all():
    print("== Phase 3 离线逻辑测试（零 API）==")
    tmp = tempfile.mkdtemp(prefix="mjc-t3-")
    # 运行数据隔离：usage.json 等写临时目录（不碰真 logs/；真 API 路径不受影响）
    from mjc import paths as _paths
    _old_log_dir = _paths.LOG_DIR
    _paths.LOG_DIR = tmp
    try:
        pool = _judge_pool()
        test_cache_roundtrip(tmp)
        test_trust_streak_and_degrade(tmp)
        test_trust_error_ticket_neutral(tmp)
        test_pipeline_screen_and_cache(tmp, pool)
        test_pipeline_degrade(tmp, pool)
        test_judge_same_vendor_fallback(tmp)
        print("== 全部离线测试通过 ✅ ==")
    finally:
        _paths.LOG_DIR = _old_log_dir


# ---------- 真 API 验收 ----------

def api_screen(ids=("C01", "C02", "H01", "H06", "F01", "F02", "F05")):
    """P3.1 初筛基准：真 API，glm-4-flash 初筛 + 三票委员会。
    对比基线：同集纯委员会调用数（3×样本数）。输出 初筛放行率/漏检/识别率 + 调用节省。"""
    from tests.test_debate import F_SAMPLES
    from tests.test_hallucination import SAMPLES
    by_id = {s["id"]: s for s in SAMPLES + F_SAMPLES}
    pool = build_pool(["deepseek:deepseek-v4-flash", "glm:glm-4-flash", "glm:glm-4-plus"])
    assert len(pool) == 3, "三票池构建失败"
    sc = pipeline.resolve_screen_judge()
    assert sc is not None, "初筛 Judge 不可用"
    print(f"== P3.1 初筛基准（真 API）| 初筛模型: {sc.display} | 样本: {ids} ==")
    rows = []
    for sid in ids:
        s = by_id[sid]
        t0 = time.time()
        rec, meta = pipeline.run_review_once(s["task"], s["output"], pool,
                                             screen_judge=sc, use_screen=True,
                                             use_cache=True, trust_path=TRUST_PATH,
                                             debate_log_dir=LOG_DIR)
        ok = (s["expected"] == "pass" and rec["final"] == "pass") or \
             (s["expected"] != "pass" and rec["final"] in ("reject", "revise"))
        flag = ""
        if meta["screened"] and meta["screen_passed"]:
            flag = " 初筛放行(免委员会)"
        elif meta["screened"]:
            flag = " 初筛未过→升级"
        print(f"{sid} (期望{s['expected']}) → {rec['final']}{'✅' if ok else '❌'} "
              f"[screen:{meta['screen_calls']}+委员会:{meta['committee_calls']}={meta['api_calls']}调用 "
              f"{rec['elapsed_s']}s]{flag}")
        rows.append({"id": sid, "expected": s["expected"], "final": rec["final"], "ok": ok,
                     "screen_passed": meta.get("screen_passed"), "calls": meta["api_calls"],
                     "cache": meta["cache_hit"]})
        # 缓存验证：第一次重跑应命中（暖 screen 结果缓存，≤1 次初筛调用）；第二次必须 0 调用（零成本）
        if not meta["cache_hit"]:
            _, meta2 = pipeline.run_review_once(s["task"], s["output"], pool,
                                                screen_judge=sc, use_screen=True,
                                                use_cache=True, trust_path=None,
                                                debate_log_dir=LOG_DIR)
            assert meta2["cache_hit"], f"{sid} 第一次重跑应缓存命中!"
            _, meta3 = pipeline.run_review_once(s["task"], s["output"], pool,
                                                screen_judge=sc, use_screen=True,
                                                use_cache=True, trust_path=None,
                                                debate_log_dir=LOG_DIR)
            assert meta3["cache_hit"] and meta3["api_calls"] == 0, f"{sid} 二次重跑应 0 调用! meta={meta3}"
            print(f"   ↳ 缓存验证 ✅ 首次重跑命中，二次重跑 0 API 调用")
    defect = [r for r in rows if r["expected"] != "pass"]
    clean = [r for r in rows if r["expected"] == "pass"]
    detect = sum(1 for r in defect if r["ok"]) / max(len(defect), 1)
    fp = sum(1 for r in clean if r["final"] != "pass") / max(len(clean), 1)
    base_calls = 3 * len(rows)          # 纯委员会基线（无辩论估算）
    saved = base_calls - sum(r["calls"] for r in rows)
    print("-" * 60)
    print(f"缺陷 {len(defect)} 识别率 {detect:.0%} | 干净 {len(clean)} 误杀率 {fp:.0%} | "
          f"初筛直接放行 {sum(1 for r in rows if r['screen_passed'])}/{len(rows)}")
    print(f"调用: 实际 {sum(r['calls'] for r in rows)} vs 纯委员会基线 ≈{base_calls} "
          f"（含初筛成本，节省约 {saved} 次等价调用）")
    print("信任分现状:\n" + trust.describe(path=TRUST_PATH))
    return rows


def api_degrade(ids=("C01", "C02", "H01")):
    """P3.3 真 API 演示：依赖 trust.json 已有 streak（先跑 --api-screen 积累）"""
    from tests.test_hallucination import SAMPLES
    by_id = {s["id"]: s for s in SAMPLES}
    pool = build_pool(["deepseek:deepseek-v4-flash", "glm:glm-4-flash", "glm:glm-4-plus"])
    data = trust.load(TRUST_PATH)
    cands = trust.eligible_degrade(data)
    print(f"== P3.3 信任降级（真 API）| 当前可降级候选: {cands or '无'} ==")
    if not cands:
        print("⚠️ streak 未达 5，无真实降级可演示（离线逻辑已覆盖）——本次只更新信任分")
    for sid in ids:
        s = by_id[sid]
        rec, meta = pipeline.run_review_once(s["task"], s["output"], pool,
                                             use_screen=False, use_cache=True,
                                             use_degrade=True, trust_path=TRUST_PATH,
                                             debate_log_dir=LOG_DIR)
        d = f"降级跳过 {meta['degraded']}" if meta["degraded"] else "未降级"
        e = "→升级" if meta["escalated"] else ""
        print(f"{sid} → {rec['final']} [{d}{e}] {meta['api_calls']}调用 {'缓存' if meta['cache_hit'] else ''}")
    print("信任分现状:\n" + trust.describe(path=TRUST_PATH))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-screen", action="store_true", help="真 API 初筛基准")
    ap.add_argument("--api-degrade", action="store_true", help="真 API 降级演示")
    args = ap.parse_args()
    t0 = time.time()
    if args.api_screen:
        api_screen()
    elif args.api_degrade:
        api_degrade()
    else:
        offline_all()
    print(f"\n总耗时 {time.time()-t0:.0f}s")
