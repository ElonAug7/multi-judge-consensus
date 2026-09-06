#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mjc.mcp — MCP stdio 服务器别名入口（python3 -m mjc.mcp）"""
from mjc.mcp_server import handle_message, serve_stdio, TOOLS

__all__ = ["handle_message", "serve_stdio", "TOOLS"]

if __name__ == "__main__":
    serve_stdio()
