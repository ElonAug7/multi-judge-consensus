#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · settings 层离线测试（零 API，mock providers.chat / has_key）
  python3 tests/test_settings.py

覆盖（P1.4 + P2.1 + 2026-09-12 修复）：
  - probe：TTL 缓存（5 分钟内复用 + force 跳过）、tried 长度截断折叠、错误分类 hint、无 key 短路
  - apply：committee 数组/字符串解析、screen_model 旧数组 bug 回归（列表→取首元素）、显式 null 语义
  - limits：长度门槛默认 1（全量送审）、apply 合并/持久化、非法值回退
  - 提示词回归：judge 含“前提核查/误判防线”（防幻觉漏检 + 防误杀）
  - set_key/clear_key：往返（临时目录 + monkeypatch PATH）、mask、自动探测触发
  - effective：按 key 过滤委员会、不足 2 个抛 ValueError、覆盖字段合并
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import settings
from mjc import providers as P

tmp = None  # 全局临时目录（模块内所有测试共用，按字母序跑无依赖）


def _patch_paths():
    """settings.json / keys.local.json 全指向临时目录，并清 providers key 缓存"""
    global tmp
    tmp = tempfile.mkdtemp(prefix="mjc-settings-")
    settings.PATH = os.path.join(tmp, "settings.json")
    settings.KEYS_PATH = os.path.join(tmp, "keys.local.json")
    P.KEY_PATH = settings.KEYS_PATH
    P.reload_keys()  # _KEYS=None


def _mk_chat(behavior, default="ok"):
    """behavior: model 名 → 返回值（str）或抛出的异常；default 覆盖未列模型。
    返回 (fake_chat, calls)"""
    calls = []
    def fake(name, messages, model=None, **kw):
        m = model or P.MODELS.get(name)
        calls.append(m)
        b = behavior.get(m, default)
        if isinstance(b, Exception):
            raise b
        return b
    return fake, calls


def _mk_has_key(pred):
    old = P.has_key
    P.has_key = pred
    return old


def test_probe_ttl_cache():
    ok_chat, calls = _mk_chat({"deepseek-v4-flash": "pong", "deepseek-chat": "pong"})
    old_chat, old_hk = P.chat, P.has_key
    P.chat, P.has_key = ok_chat, lambda n: True
    try:
        d = settings.load()
        d["key_status"].pop("deepseek", None)
        r1 = settings.probe("deepseek", data=d)
        assert r1["ok"] is True and r1.get("cached") is None, r1
        assert calls == ["deepseek-v4-flash"], f"首个成功即止: {calls}"
        # 5 分钟内第二次 → 直接复用缓存，不再调 chat
        r2 = settings.probe("deepseek", data=d)
        assert r2["ok"] is True and r2.get("cached") is True, r2
        assert calls == ["deepseek-v4-flash"], f"TTL 缓存不应再调 chat: {calls}"
        # force=True → 跳过缓存，真调一次（仍首个成功即止）
        r3 = settings.probe("deepseek", data=d, force=True)
        assert r3["ok"] is True and r3.get("cached") is None, r3
        assert calls == ["deepseek-v4-flash", "deepseek-v4-flash"], calls
        # 缓存过期（at 改到昨天同一时刻... 用超 5 分钟的秒数）→ 重新探测
        stale = dict(d["key_status"]["deepseek"])
        hh = int(stale["at"][:2]); stale["at"] = f"{(hh-1)%24:02d}:{stale['at'][3:5]}:{stale['at'][6:]}"
        d["key_status"]["deepseek"] = stale
        r4 = settings.probe("deepseek", data=d)
        assert r4.get("cached") is None and len(calls) == 3, calls
    finally:
        P.chat, P.has_key = old_chat, old_hk
    print("  ✅ probe TTL 缓存：5 分钟内复用 + force 跳过 + 过期重探")


def test_probe_tried_truncation_and_classify():
    # 造一个 6 模型厂商，全失败 → tried 只留最近 3 条 + 折叠说明
    fail_chat, calls = _mk_chat({}, default=RuntimeError("HTTP 503: busy"))
    old_chat, old_hk = P.chat, P.has_key
    P.chat, P.has_key = fail_chat, lambda n: True
    try:
        d = settings.load()
        d["providers"]["fakep"] = {"label": "假厂", "endpoint": "http://x", "models": [f"m{i}" for i in range(6)]}
        d["key_status"].pop("fakep", None)
        r = settings.probe("fakep", data=d)
        assert r["ok"] is False, r
        assert len(calls) == 6, calls
        assert len(r["tried"]) == settings.MAX_TRIED + 1, f"应截断为 {settings.MAX_TRIED}+折叠头: {r['tried']}"
        assert r["tried"][0].startswith("…另有 3 次失败未列"), r["tried"]
        assert r["tried"][-1].startswith("m5:"), r["tried"]  # 最近一条保留
        # 错误分类（直接单测分类器）
        assert "401" in settings._classify_error("x", "HTTP 401: Unauthorized")
        assert "403" in settings._classify_error("x", "HTTP 403: 未购买")
        assert "404" in settings._classify_error("x", "HTTP 404 model_not_found")
        assert "空响应" in settings._classify_error("x", "空响应 (attempt 1)")
        assert "429" in settings._classify_error("x", "HTTP 429: rate limit")
        assert "超时" in settings._classify_error("x", "timed out after 30s")
        assert "HTTP 500" in settings._classify_error("x", "HTTP 500: boom")  # 未知 → 原文
    finally:
        P.chat, P.has_key = old_chat, old_hk
    print("  ✅ probe tried 截断折叠 + 错误分类（401/403/404/空响应/429/超时/未知）")


def test_probe_no_key_short_circuit():
    old_chat, old_hk = P.chat, P.has_key
    P.chat, P.has_key = (lambda *a, **k: "pong"), (lambda n: False)
    try:
        d = settings.load()
        r = settings.probe("glm", data=d)
        assert r["ok"] is False and "未配置 key" in r["detail"] and "粘贴" in r["hint"], r
        assert "tried" not in r
    finally:
        P.chat, P.has_key = old_chat, old_hk
    print("  ✅ probe 无 key 短路：不调 chat、给粘贴提示")


def test_apply_split_and_array_bug():
    old_hk = P.has_key
    P.has_key = lambda n: True
    try:
        d = settings.load()
        d["current"] = {"tier": "standard", "committee": None, "screen_enabled": None,
                        "screen_model": None, "screen_conf": None, "degrade": None, "cache": None}
        # ① committee 字符串 CSV → 列表
        eff = settings.apply({"committee": "glm:glm-4-flash,deepseek:deepseek-v4-flash"}, data=d)
        assert eff["committee"] == ["glm:glm-4-flash", "deepseek:deepseek-v4-flash"], eff["committee"]
        # ② 旧数组 bug 回归：screen_model 传列表 → 取首元素，绝不落数组
        eff = settings.apply({"screen_model": ["glm:glm-4-flash"]}, data=d)
        assert eff["screen_model"] == "glm:glm-4-flash", eff["screen_model"]
        # ③ 显式 null：落盘为 None（关闭/回档位默认），不残留旧值
        settings.apply({"screen_model": None, "committee": None}, data=d)
        assert d["current"]["screen_model"] is None and d["current"]["committee"] is None
        eff = settings.effective(d)
        assert eff["committee"] == settings.DEFAULT_TIERS["standard"]["committee"], eff["committee"]
        assert eff["screen_model"] == "glm:glm-4-flash", eff  # 档位默认捡回（关闭靠 screen_enabled）
        # ④ 未知档位抛
        try:
            settings.apply({"tier": "nope"}, data=d)
            raise AssertionError("未知档位应抛 ValueError")
        except ValueError as e:
            assert "未知档位" in str(e)
    finally:
        P.has_key = old_hk
    print("  ✅ apply：CSV→列表、screen_model 数组回归、null 语义、未知档位报错")


def test_effective_filter():
    # 只有 glm 有 key → 委员会只剩 glm 系；不足 2 个抛
    old_hk = P.has_key
    P.has_key = lambda n: n == "glm"
    try:
        d = settings.load()
        eff = settings.effective(d)
        assert all(s.startswith("glm:") for s in eff["committee"]), eff["committee"]
        assert len(eff["committee"]) >= 2, eff["committee"]
        # 覆盖字段合并生效
        d2 = settings.load()
        d2["current"]["degrade"] = True
        d2["current"]["screen_model"] = "glm:glm-4-flash"
        eff2 = settings.effective(d2)
        assert eff2["degrade"] is True and eff2["screen_model"] == "glm:glm-4-flash"
        # 委员会里没有一家有 key → ValueError
        d3 = settings.load()
        P.has_key = lambda n: n == "dashscope"  # dashscope 不在 standard 委员会
        try:
            settings.effective(d3)
            raise AssertionError("委员会不足 2 应抛 ValueError")
        except ValueError as e:
            assert "不足 2 个" in str(e)
    finally:
        P.has_key = old_hk
    print("  ✅ effective：按 key 过滤、覆盖合并、不足 2 抛错")


def test_limits_config():
    old_hk = P.has_key
    P.has_key = lambda n: True  # 无 key 环境（CI）下 apply→effective 需委员会可用
    try:
        d = settings.load()
        # 默认值：1 ≈ 全量送审（防短答被门槛绕过）
        assert settings.limit("gate", d) == 1 and settings.limit("auto", d) == 1 and settings.limit("scan", d) == 1, \
            (settings.limit("gate", d), settings.limit("auto", d), settings.limit("scan", d))
        # apply 覆盖 + 持久化
        settings.apply({"limits": {"gate": 5, "scan": 30}}, data=d)
        assert settings.limit("gate", d) == 5 and settings.limit("scan", d) == 30 and settings.limit("auto", d) == 1
        d2 = settings.load()
        assert settings.limit("gate", d2) == 5, d2.get("limits")
        # 负数 clamp 到 0；未知名忽略
        settings.apply({"limits": {"gate": -3, "nope": 9}}, data=d)
        assert settings.limit("gate", d) == 0 and "nope" not in (d.get("limits") or {})
        # 非法类型 → 回退默认
        d["limits"]["scan"] = "abc"
        assert settings.limit("scan", d) == 1
    finally:
        P.has_key = old_hk
    print("  ✅ limits：默认 1、apply 合并/持久化、clamp、非法值回退")


def test_judge_prompt_guards():
    from mjc import judge
    assert "前提核查" in judge.JUDGE_PROMPT and "误判防线" in judge.JUDGE_PROMPT
    print("  ✅ judge 提示词：含前提核查 + 误判防线（2026-09-12 修复回归）")


def test_set_key_clear_key_roundtrip():
    ok_chat, calls = _mk_chat({"deepseek-v4-flash": "pong"})
    old_chat = P.chat
    P.chat = ok_chat  # has_key 走真 _load_keys（读 monkeypatched KEY_PATH）
    try:
        # 初始无 key 文件
        assert not os.path.exists(settings.KEYS_PATH)
        masked = settings.set_key("deepseek", "sk-test-1234567890abcdef")
        assert masked.startswith("sk-t") and masked.endswith("cdef") and "…" in masked, masked
        keys = json.load(open(settings.KEYS_PATH, encoding="utf-8"))
        assert keys.get("deepseek") == "sk-test-1234567890abcdef", keys
        # set_key 后自动探测一次（key_status 已 pop → 非缓存，真调）
        d = settings.load()
        assert d["key_status"].get("deepseek", {}).get("ok") is True
        assert len(calls) >= 1, calls
        # clear：真删 + 文件同步
        assert settings.clear_key("deepseek") is True
        assert settings.clear_key("deepseek") is False  # 第二次无内容
        keys = json.load(open(settings.KEYS_PATH, encoding="utf-8"))
        assert "deepseek" not in keys, keys
        # 空 key 拒绝
        try:
            settings.set_key("deepseek", "   ")
            raise AssertionError("空 key 应抛 ValueError")
        except ValueError:
            pass
        # 未知厂商拒绝
        try:
            settings.set_key("not-a-provider", "sk-x")
            raise AssertionError("未知厂商应抛 ValueError")
        except ValueError as e:
            assert "未知厂商" in str(e)
    finally:
        P.chat = old_chat
    print("  ✅ set_key/clear_key：往返落盘、mask、自动探测、空/未知厂商拒绝")


def main():
    print("== settings 层离线测试（零 API）==")
    _patch_paths()
    test_probe_ttl_cache()
    test_probe_tried_truncation_and_classify()
    test_probe_no_key_short_circuit()
    test_apply_split_and_array_bug()
    test_effective_filter()
    test_limits_config()
    test_judge_prompt_guards()
    test_set_key_clear_key_roundtrip()
    print("== settings 层全部通过 ✅ ==")


if __name__ == "__main__":
    main()
