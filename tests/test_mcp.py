#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_mcp.py — MCP stdio 服务器离线测试（零 API）
覆盖：initialize 握手 / tools/list / review 参数校验 / 成功路径（monkeypatch pipeline）/ 错误码。
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import mcp_server as M


class _FakePipeline:
    def resolve_screen_judge(self, *a):
        return None

    def run_review_once(self, *a, **k):
        return ({"final": "revise", "pass_votes": 0, "reject_votes": 0, "revise_votes": 2,
                 "debate_rounds": 1, "tokens": {"total": 123}, "cost_yuan": 0.001,
                 "rounds": [{"round": 1, "kind": "verifier", "opinions": [{
                     "judge_id": "verifier", "judge_display": "🔍 验证器", "verdict": "revise",
                     "confidence": 1.0, "issues": [{"type": "factual_error", "description": "求和错",
                                                    "suggestion": "改为 75", "judge": "verifier"}],
                     "final_reasoning": "确定性验算命中 1 处"}]}]},
                {"api_calls": 0, "verifier": True})


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    # 1) initialize
    r = M.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {}}})
    check("initialize 返回协议与工具能力", r["result"]["protocolVersion"] == "2024-11-05"
          and r["result"]["capabilities"].get("tools") == {})

    # 2) tools/list
    r = M.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    check("tools/list 含 review 工具", any(t["name"] == "review" for t in r["result"]["tools"]))

    # 3) ping
    r = M.handle_message({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    check("ping 正常", r.get("result") == {})

    # 4) 通知不回复
    check("initialized 通知无响应", M.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)

    # 5) 参数校验
    r = M.handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "review", "arguments": {"task": "x"}}})
    check("缺 output → 参数错误", r["error"]["code"] == -32602)

    # 6) 未知工具
    r = M.handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "hack", "arguments": {}}})
    check("未知工具 → -32602", r["error"]["code"] == -32602)

    # 7) 成功路径（monkeypatch pipeline + settings + build_pool，零 API、无 key 环境可用）
    old_pipe, old_st, old_bp = M.pipeline, M.settings, M.build_pool
    M.pipeline = _FakePipeline()
    M.settings = type("S", (), {"effective": lambda self: {"committee": ["a:m1", "b:m2"],
                                                      "screen_model": "a:m1", "screen_enabled": True}})()
    M.build_pool = lambda specs: [type("J", (), {"name": s})() for s in specs]
    try:
        r = M.handle_message({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                              "params": {"name": "review",
                                         "arguments": {"task": "t", "output": "o"}}})
        txt = r["result"]["content"][0]["text"]
        d = json.loads(txt)
        check("review 成功返回裁决 JSON", d["verdict"] == "revise" and d["api_calls"] == 0)
        check("issues 含验算命中", any(i["type"] == "factual_error" for i in d["issues"]))
    finally:
        M.pipeline, M.settings, M.build_pool = old_pipe, old_st, old_bp

    # 8) 坏 JSON
    check("坏 JSON → Parse error", M.handle_message("not json").get("error", {}).get("code") == -32700)

    print("== mcp 全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
