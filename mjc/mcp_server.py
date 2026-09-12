#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · mcp_server.py — MCP(Model Context Protocol) stdio 服务器（零依赖）
让任意 MCP 客户端（Claude Desktop/Code、Cursor、Windsurf、Cline…）把 MJC 审查当工具用：
  tools/call review {task, output, screen?, degrade?} → 委员会审查结果 JSON

协议：stdio + 换行分隔 JSON-RPC 2.0（initialize / notifications/initialized / tools/list / tools/call / ping）
运行：python3 -m mjc.mcp            （或 pip 安装后 mjc mcp）
配置示例（Claude Code）：claude mcp add mjc -- python3 -m mjc.mcp
"""
import json
import os
import sys

from mjc import pipeline, settings
from mjc.judge import build_pool

VERSION = "0.9.1"
PROTOCOL = "2024-11-05"
TOOLS = [{
    "name": "review",
    "description": "多模型共识审查：对 Agent 输出做幻觉/事实/逻辑错误交叉审查（验证器零成本先行，初筛+委员会+辩论）。"
                   "返回裁决 verdict(pass/revise/reject/need_human) 与逐条问题 issues。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Agent 被要求完成的原始任务"},
            "output": {"type": "string", "description": "待审查的 Agent 输出/代码说明"},
            "screen": {"type": "boolean", "description": "开初筛（默认跟后台配置）"},
            "degrade": {"type": "boolean", "description": "信任降级（默认关）"},
            "verbose": {"type": "boolean", "description": "true 返回完整 rounds；false 只返回摘要（默认）"},
        },
        "required": ["task", "output"],
    },
}]


def _rpc(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def _do_review(args):
    task = str(args.get("task") or "").strip()
    output = str(args.get("output") or "").strip()
    if not task or not output:
        raise ValueError("task 与 output 均必填")
    if len(output) > 20000 or len(task) > 4000:
        raise ValueError("内容超长（task≤4000，output≤20000）")
    eff = settings.effective()
    pool = build_pool(eff["committee"])
    if len(pool) < 2:
        raise ValueError("可用审查员 <2：请先配置 API key（MJC_<厂商>_KEY 或 keys.local.json）")
    screen_j = pipeline.resolve_screen_judge(None)
    use_screen = bool(screen_j) if args.get("screen") is None else bool(args.get("screen"))
    rec, meta = pipeline.run_review_once(
        task, output, pool,
        screen_judge=screen_j if use_screen else None,
        screen_conf=None, use_screen=use_screen,
        use_cache=True, use_degrade=bool(args.get("degrade")),
        trust_path=None,  # MCP 通道不动信任分（防并行污染），降级参数保留接口语义
        memory=None,
    )
    slim = {
        "verdict": rec.get("final"),
        "pass_votes": rec.get("pass_votes"), "reject_votes": rec.get("reject_votes"),
        "revise_votes": rec.get("revise_votes"), "debate_rounds": rec.get("debate_rounds", 0),
        "api_calls": meta.get("api_calls", 0),
        "tokens": rec.get("tokens"), "cost_yuan": rec.get("cost_yuan"),
        "verifier": meta.get("verifier", False),
        "issues": [{"type": i.get("type"), "judge": i.get("judge"),
                    "desc": i.get("description", "")[:200], "sug": i.get("suggestion", "")[:120]}
                   for rnd in (rec.get("rounds") or [])
                   for o in (rnd.get("opinions") or [])
                   for i in (o.get("issues") or [])][:10],
    }
    if args.get("verbose"):
        slim["rounds"] = rec.get("rounds")
    return slim


def handle_message(msg):
    """处理一条 JSON-RPC 消息 → 响应 dict（通知类返回 None）。纯函数便于测试。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc(None, error={"code": -32700, "message": "Parse error"})
    method = msg.get("method", "")
    mid = msg.get("id")
    if method == "initialize":
        return _rpc(mid, result={"protocolVersion": PROTOCOL,
                                 "capabilities": {"tools": {}},
                                 "serverInfo": {"name": "mjc", "version": VERSION}})
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _rpc(mid, result={})
    if mid is None:
        return None  # 其他通知忽略
    if method == "tools/list":
        return _rpc(mid, result={"tools": TOOLS})
    if method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        args = (msg.get("params") or {}).get("arguments") or {}
        if name != "review":
            return _rpc(mid, error={"code": -32602, "message": f"未知工具 {name}"})
        try:
            result = _do_review(args)
        except ValueError as e:
            return _rpc(mid, error={"code": -32602, "message": str(e)})
        except Exception as e:
            return _rpc(mid, error={"code": -32603, "message": f"审查失败: {e}"})
        return _rpc(mid, result={"content": [{"type": "text",
                                              "text": json.dumps(result, ensure_ascii=False, indent=1)}],
                                 "isError": False})
    return _rpc(mid, error={"code": -32601, "message": f"Method not found: {method}"})


def serve_stdio():
    """stdio 服务器主循环：换行分隔 JSON-RPC"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            sys.stdout.write(json.dumps(_rpc(None, error={"code": -32700, "message": "Parse error"}),
                                        ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        resp = handle_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve_stdio()
