#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · webui HTTP API 离线测试（零 API）
  python3 tests/test_webui_api.py

覆盖（P2.2）：
  - 鉴权：token 缺失/错误 → 401（含 query token 通过路径）
  - 路由：GET / 页面 200 html、未知路径 404（GET/POST）
  - GET /api/state 结构（无 key 时 current.error 语义）、/api/health
  - /api/review 参数校验：task/output 缺失 400、key 不足 400（无真实调用）
  - /api/review 成功路径（monkeypatch pipeline/build_pool/effective）：_slim 字段裁剪、
    task/output 截断、no_cache/degrade 透传、pool CSV 解析
  - /api/admin/keys：provider/action 校验 400、未知厂商 400；set→自动探测→probe cached、clear
  - /api/admin/apply：未知档位 400、未知模型 400、screen_conf 非法 400、合法 patch 200 落盘、
    screen_model 数组兼容（旧 bug 回归）
全部通过临时目录隔离 settings/keys（monkeypatch PATH），providers.chat 全程 mock —— 零真实 API。
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import ThreadingHTTPServer

from mjc import providers as P
from mjc import webui

TOKEN = "test-token-abc"
tmp = None
srv = None
base = None
_chat_calls = []


def _chat_fake(provider, messages, model=None, **kw):
    """离线替身：任何调用直接成功（probe 只看是否抛异常）"""
    _chat_calls.append((provider, model))
    return "pong"


def _tmp_has_key(name):
    """has_key 替身：只认临时 keys.local.json（屏蔽 env / ~/.openclaw/keys 真 key 兜底）"""
    try:
        return name in json.load(open(webui.settings.KEYS_PATH, encoding="utf-8"))
    except Exception:
        return False


_old_has_key = None


def _patch_paths():
    """settings.json / keys.local.json → 临时目录；webui 与 providers 指向同一份；
    has_key 换成 tmp 文件感知谓词（真实现会从 env / bigmodel.key 捡回真 key，破坏零 API 隔离）"""
    global tmp, _old_has_key
    tmp = tempfile.mkdtemp(prefix="mjc-webui-")
    webui.settings.PATH = os.path.join(tmp, "settings.json")
    webui.settings.KEYS_PATH = os.path.join(tmp, "keys.local.json")
    P.KEY_PATH = webui.settings.KEYS_PATH
    P.reload_keys()  # 清 _KEYS 缓存
    _old_has_key = P.has_key
    P.has_key = _tmp_has_key


def _start_server():
    global srv, base
    os.environ["MJC_WEBUI_TOKEN"] = TOKEN
    srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def _req(method, path, body=None, token=True, query_token=None):
    r = urllib.request.Request(base + path, method=method)
    if body is not None:
        r.data = json.dumps(body).encode("utf-8")
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("X-MJC-Token", TOKEN)
    if query_token:
        r.add_header("X-MJC-Token", query_token)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            ct = resp.headers.get("Content-Type", "")
            raw = resp.read()
            if "application/json" in ct:
                return resp.status, json.loads(raw or b"{}"), ct
            return resp.status, raw.decode("utf-8", "replace"), ct
    except urllib.error.HTTPError as e:
        ct = e.headers.get("Content-Type", "")
        try:
            raw = e.read()
            body = json.loads(raw or b"{}") if "application/json" in ct else raw.decode("utf-8", "replace")
            return e.code, body, ct
        except Exception:
            return e.code, {}, ct


# ---------- 鉴权 ----------

def test_unauth_401():
    st, d, _ = _req("GET", "/api/state", token=False)
    assert st == 401 and "error" in d, (st, d)
    st, d, _ = _req("GET", "/api/health", token=False)
    assert st == 401, st
    st, d, _ = _req("POST", "/api/review", body={"task": "t", "output": "o"}, token=False)
    assert st == 401, st
    st, d, _ = _req("POST", "/api/admin/keys", body={"action": "set"}, token=False)
    assert st == 401, st
    # 错误 query token（urlopen 不带 header，query 鉴权路径）→ 401
    st, d, _ = _req("GET", "/api/state?token=wrong", token=False)
    assert st == 401, st
    print("  ✅ 401：token 缺失/错误（含 query 错误 token）全部拦截")


def test_query_token_allowed():
    st, d, _ = _req("GET", "/api/state?token=" + TOKEN, token=False)
    assert st == 200, (st, d)
    print("  ✅ query token 鉴权路径通过（200）")


def test_index_and_404():
    st, html, ct = _req("GET", "/")
    assert st == 200 and "text/html" in ct and "Multi-Judge Consensus" in html, (st, ct)
    # P3 拆分：页面 JS 独立文件走 /assets/page.js（外部 script 引用）
    st, js, ct = _req("GET", "/assets/page.js")
    assert st == 200 and "javascript" in ct and "window.onerror" in js, (st, ct)
    st, _, _ = _req("GET", "/assets/nope.js")
    assert st == 404, st
    st, d, _ = _req("GET", "/api/nope")
    assert st == 404, st
    st, d, _ = _req("POST", "/api/nope", body={})
    assert st == 404, st
    print("  ✅ 路由：/ 页面 200 html、GET/POST 未知路径 404")


# ---------- state / health ----------

def test_state_structure():
    st, d, _ = _req("GET", "/api/state")
    assert st == 200, (st, d)
    assert isinstance(d["version"], str) and d["version"]
    assert isinstance(d["providers"], list) and d["providers"], "providers 不应为空"
    for p in d["providers"]:
        for k in ("name", "label", "endpoint", "has_key", "models", "note"):
            assert k in p, f"provider 缺 {k}: {p}"
        assert p["has_key"] is False, "临时目录下不应有任何 key"
        assert p["key_masked"] is None
        assert all(":" in m for m in p["models"]), p["models"]
    assert isinstance(d["catalog"], list) and d["catalog"]
    for m in d["catalog"]:
        for k in ("spec", "provider", "model", "tag", "has_key"):
            assert k in m, m
    assert isinstance(d["tiers"], dict) and "standard" in d["tiers"]
    for tid, t in d["tiers"].items():
        assert isinstance(t["committee"], list) and len(t["committee"]) >= 2, (tid, t)
    cur = d["current"]
    assert cur.get("tier") == "standard"
    assert "error" in cur and "不足" in cur["error"], f"无 key 时 current 应带 error: {cur}"
    assert isinstance(d["cache_entries"], int)
    assert "trust" in d
    print("  ✅ /api/state 结构完整（providers/catalog/tiers/current.error/trust）")


def test_health():
    st, d, _ = _req("GET", "/api/health")
    assert st == 200 and d.get("ok") is True and d.get("app") == "mjc"
    assert isinstance(d.get("providers"), list) and isinstance(d.get("cache_entries"), int)
    print("  ✅ /api/health 结构")


# ---------- /api/review ----------

def test_review_param_validation():
    # task/output 缺失 → 400，不触 pipeline
    st, d, _ = _req("POST", "/api/review", body={})
    assert st == 400 and "task 与 output 均必填" in d.get("error", ""), (st, d)
    st, d, _ = _req("POST", "/api/review", body={"task": "x", "output": ""})
    assert st == 400, (st, d)
    st, d, _ = _req("POST", "/api/review", body={"task": "", "output": "y"})
    assert st == 400, (st, d)
    # 参数齐但无 key（临时目录）→ effective 抛 → Judge 不足 400，仍零调用
    st, d, _ = _req("POST", "/api/review", body={"task": "t", "output": "o"})
    assert st == 400 and "Judge 不足" in d.get("error", ""), (st, d)
    assert _chat_calls == [], "参数校验路径不应触发任何模型调用"
    print("  ✅ /api/review 校验：task/output 必填 400、Judge 不足 400（零调用）")


def test_review_success_mocked():
    calls = {}

    def fake_run(task, output, pool, **kw):
        calls["task"], calls["output"], calls["pool"], calls["kw"] = task, output, pool, kw
        record = {"final": "pass", "pass_votes": 3, "reject_votes": 0, "revise_votes": 0,
                  "rounds": [{"round": 1, "kind": "committee", "opinions": [
                      {"judge_id": "glm:glm-4-flash", "judge_display": "glm-4-flash", "verdict": "pass",
                       "confidence": 0.9, "issues": [], "final_reasoning": "看起来没问题",
                       "secret_extra": "不应出现在响应里"}]}]}
        return record, {"api_calls": 3, "cache_hit": False}

    old_run, old_bp, old_eff = webui.pipeline.run_review_once, webui.build_pool, webui.settings.effective
    webui.pipeline.run_review_once = fake_run
    webui.build_pool = lambda specs: list(specs) if specs else []
    webui.settings.effective = lambda data=None: {"committee": ["glm:glm-4-flash", "glm:glm-4-plus"],
                                                  "degrade": False, "cache": True}
    try:
        st, d, _ = _req("POST", "/api/review",
                        body={"task": "写个排序", "output": "已实现", "screen": False})
        assert st == 200, (st, d)
        assert calls["task"] == "写个排序" and calls["output"] == "已实现"
        assert calls["pool"] == ["glm:glm-4-flash", "glm:glm-4-plus"], calls["pool"]
        assert calls["kw"]["use_cache"] is True and calls["kw"]["use_degrade"] is False
        # _slim 裁剪：secret_extra 被剔除，白名单字段保留
        op = d["record"]["rounds"][0]["opinions"][0]
        assert op.get("final_reasoning") == "看起来没问题" and "secret_extra" not in op, op
        assert d["meta"]["api_calls"] == 3
        # 显式 no_cache/degrade → 透传；task 超长截断
        st, d, _ = _req("POST", "/api/review",
                        body={"task": "T" * 5000, "output": "O" * 25000, "screen": False,
                              "no_cache": True, "degrade": True})
        assert st == 200, (st, d)
        assert len(calls["task"]) == webui.MAX_TASK == 4000 and calls["task"] == "T" * 4000
        assert len(calls["output"]) == webui.MAX_OUTPUT == 20000 and calls["output"] == "O" * 20000
        assert calls["kw"]["use_cache"] is False and calls["kw"]["use_degrade"] is True
        # pool CSV 字符串 → 拆列表（webui 路由层解析）
        st, d, _ = _req("POST", "/api/review",
                        body={"task": "t", "output": "o", "pool": "glm:glm-4-flash,glm:glm-4-plus",
                              "screen": False})
        assert st == 200 and calls["pool"] == ["glm:glm-4-flash", "glm:glm-4-plus"], calls["pool"]
    finally:
        webui.pipeline.run_review_once, webui.build_pool, webui.settings.effective = old_run, old_bp, old_eff
    print("  ✅ /api/review 成功：_slim 裁剪、task/output 截断(4000/20000)、no_cache/degrade/pool 透传")


# ---------- /api/admin/keys + probe ----------

def test_admin_keys_validation():
    # 缺 provider → 400；action 非法 → 400
    st, d, _ = _req("POST", "/api/admin/keys", body={"action": "set"})
    assert st == 400 and "provider 必填" in d.get("error", ""), (st, d)
    st, d, _ = _req("POST", "/api/admin/keys", body={"action": "bogus", "provider": "glm"})
    assert st == 400 and "set/clear" in d.get("error", ""), (st, d)
    # 未知厂商 → settings 层 ValueError → 400
    st, d, _ = _req("POST", "/api/admin/keys", body={"action": "set", "provider": "nope", "key": "sk-x"})
    assert st == 400 and "未知厂商" in d.get("error", ""), (st, d)
    print("  ✅ /api/admin/keys 校验：provider/action/未知厂商 400")


def test_admin_keys_set_probe_clear():
    global _chat_calls
    _chat_calls = []
    old_chat = P.chat
    P.chat = _chat_fake
    try:
        # set → 自动探测 1 次（首个模型成功即止）+ masked 返回
        st, d, _ = _req("POST", "/api/admin/keys",
                        body={"action": "set", "provider": "glm", "key": "sk-test-abcdef1234567890"})
        assert st == 200 and d.get("ok") is True and d["masked"].startswith("sk-t"), (st, d)
        assert len(_chat_calls) == 1 and _chat_calls[0][0] == "glm", _chat_calls
        keys = json.load(open(webui.settings.KEYS_PATH, encoding="utf-8"))
        assert keys.get("glm") == "sk-test-abcdef1234567890", keys
        # state 里 key 已掩码、has_key True、key_status ok
        st, d, _ = _req("GET", "/api/state")
        g = next(p for p in d["providers"] if p["name"] == "glm")
        assert g["has_key"] is True and g["key_masked"].startswith("sk-t"), g
        assert g["status"]["ok"] is True, g["status"]
        # probe（非 force）→ set 刚探过，5 分钟 TTL → cached:true，不再调
        n = len(_chat_calls)
        st, d, _ = _req("POST", "/api/admin/probe", body={"provider": "glm"})
        assert st == 200 and d.get("ok") is True and d.get("cached") is True, (st, d)
        assert len(_chat_calls) == n, "TTL 缓存不应再调 chat"
        # probe force:true → 跳过缓存真探 1 次
        st, d, _ = _req("POST", "/api/admin/probe", body={"provider": "glm", "force": True})
        assert st == 200 and d.get("ok") is True and d.get("cached") is None, (st, d)
        assert len(_chat_calls) == n + 1, _chat_calls
        # clear → 200 removed:true；再 probe → 未配置 key
        st, d, _ = _req("POST", "/api/admin/keys", body={"action": "clear", "provider": "glm"})
        assert st == 200 and d.get("removed") is True, (st, d)
        assert not os.path.exists(webui.settings.KEYS_PATH) or "glm" not in json.load(
            open(webui.settings.KEYS_PATH, encoding="utf-8"))
        st, d, _ = _req("POST", "/api/admin/probe", body={"provider": "glm"})
        assert st == 200 and d.get("ok") is False and "未配置 key" in d.get("detail", ""), (st, d)
    finally:
        P.chat = old_chat
    print("  ✅ keys set→自动探测→TTL cached→force 真探→clear 全链路（mock chat，零真实 API）")


# ---------- /api/admin/apply ----------

def _seed_two_keys():
    """造两家有 key 的厂商（绕过 HTTP，直接 settings 层 + mock chat）→ apply 的 effective 能过"""
    old_chat = P.chat
    P.chat = _chat_fake
    try:
        webui.settings.set_key("glm", "sk-glm-1111222233334444")
        webui.settings.set_key("deepseek", "sk-ds-1111222233334444")
    finally:
        P.chat = old_chat


def test_admin_apply():
    _seed_two_keys()
    # 合法 patch → 200 且落盘（tier 显式）
    st, d, _ = _req("POST", "/api/admin/apply", body={"tier": "standard"})
    assert st == 200, (st, d)
    # committee CSV 字符串 → 200；落盘为列表
    st, d, _ = _req("POST", "/api/admin/apply",
                    body={"committee": "glm:glm-4-flash,deepseek:deepseek-v4-flash"})
    assert st == 200, (st, d)
    st, d, _ = _req("GET", "/api/state")
    assert d["current"]["committee"] == ["glm:glm-4-flash", "deepseek:deepseek-v4-flash"], d["current"]
    # screen_model 数组（旧 bug 输入）→ 取首元素，state 里为字符串
    st, d, _ = _req("POST", "/api/admin/apply", body={"screen_model": ["glm:glm-4-flash"]})
    assert st == 200, (st, d)
    st, d, _ = _req("GET", "/api/state")
    assert d["current"]["screen_model"] == "glm:glm-4-flash", d["current"]
    # 校验错误：未知档位 / 未知模型 / screen_conf 非法 → 400
    st, d, _ = _req("POST", "/api/admin/apply", body={"tier": "nope"})
    assert st == 400 and "未知档位" in d.get("error", ""), (st, d)
    st, d, _ = _req("POST", "/api/admin/apply", body={"committee": ["glm:no-such-model"]})
    assert st == 400 and "未知模型" in d.get("error", ""), (st, d)
    st, d, _ = _req("POST", "/api/admin/apply", body={"screen_conf": "abc"})
    assert st == 400 and "0-1" in d.get("error", ""), (st, d)
    # 回滚档位默认（显式 null → 不落残留）
    st, d, _ = _req("POST", "/api/admin/apply", body={"screen_model": None, "committee": None})
    assert st == 200, (st, d)
    st, d, _ = _req("GET", "/api/state")
    assert "glm:glm-4-flash" in d["current"]["committee"], "null → 回档位默认（standard 含 glm 系）"
    print("  ✅ /api/admin/apply：合法 200 落盘、CSV/数组/null 语义、三类校验 400")


def main():
    print("== webui HTTP API 离线测试（零 API，mock providers.chat）==")
    _patch_paths()
    _start_server()
    try:
        test_unauth_401()
        test_query_token_allowed()
        test_index_and_404()
        test_state_structure()      # 须在写 key 前跑（断言无 key 语义）
        test_health()
        test_review_param_validation()  # 同上：无 key → Judge 不足
        test_review_success_mocked()
        test_admin_keys_validation()
        test_admin_keys_set_probe_clear()
        test_admin_apply()
    finally:
        srv.shutdown()
        srv.server_close()
        os.environ.pop("MJC_WEBUI_TOKEN", None)
        P.has_key = _old_has_key
        P.reload_keys()
    print("== webui HTTP API 全部通过 ✅ ==")


if __name__ == "__main__":
    main()
