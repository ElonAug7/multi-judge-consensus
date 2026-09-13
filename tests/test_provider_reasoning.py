#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_provider_reasoning.py — 推理模型预算陷阱的离线回归（v0.11.0e · 零 API）

背景（2026-09-13 实测踩坑）：deepseek-v4-flash 已变为**推理模型**，响应里
`content` 之外还有 `reasoning_content`。当 max_tokens 很小时（如探活用的 10），
预算被 reasoning 全部吃掉 → `content` 为空 → providers.chat 抛"[x] 空响应"，
把**好着的模型误判为下线**（我因此连续 4 轮误报 deepseek 故障）。
本测试锁定：① 含 content 正常返回；② 只有 reasoning_content 时，错误信息必须点明真因
（含 reasoning/max_tokens 字样），而不是含糊的"空响应"。
"""
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import providers  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    old_urlopen, old_keys = urllib.request.urlopen, providers._load_keys
    providers._load_keys = lambda: {"fake": "k"}
    old_ep = providers.endpoint_of
    providers.endpoint_of = lambda n: "https://example.invalid/v1"
    try:
        # ① 正常响应
        payload = {"choices": [{"message": {"role": "assistant", "content": "OK"}}], "usage": {}}
        urllib.request.urlopen = lambda req, timeout=None: _Resp(json.dumps(payload).encode())
        check("含 content → 正常返回", providers.chat("fake", [{"role": "user", "content": "x"}],
                                                model="m", max_tokens=512, retries=1) == "OK")

        # ② 只有 reasoning_content（预算被推理吃尽）
        payload = {"choices": [{"message": {"role": "assistant", "content": "",
                                            "reasoning_content": "让我想想……"}}], "usage": {}}
        urllib.request.urlopen = lambda req, timeout=None: _Resp(json.dumps(payload).encode())
        msg = ""
        try:
            providers.chat("fake", [{"role": "user", "content": "x"}], model="m",
                           max_tokens=10, retries=1)
        except Exception as e:  # noqa
            msg = str(e)
        check("只有 reasoning_content → 报错（不静默返回空）", "空响应" in msg)
        check("错误信息点明真因（reasoning / max_tokens）",
              "reasoning_content" in msg and "max_tokens" in msg)

        # ③ 报文里带当前预算，便于定位
        check("错误信息带出当前 max_tokens 值", "10" in msg)

        # ④ extra 字段合并进请求体（v0.11.0f：dashscope enable_search 等扩展参数）
        seen = {}
        payload = {"choices": [{"message": {"role": "assistant", "content": "OK"}}], "usage": {}}

        def _capture(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            return _Resp(json.dumps(payload).encode())

        urllib.request.urlopen = _capture
        providers.chat("fake", [{"role": "user", "content": "x"}], model="m", max_tokens=512,
                       retries=1, extra={"enable_search": True})
        check("extra 合并进请求体", seen["body"].get("enable_search") is True)
        check("extra 不破坏基础字段", seen["body"].get("model") == "m" and "messages" in seen["body"])
    finally:
        urllib.request.urlopen = old_urlopen
        providers._load_keys = old_keys
        providers.endpoint_of = old_ep

    print("== 推理模型预算陷阱（v0.11.0e）全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
