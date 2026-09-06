#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · page_html.py — Web 页面资产（HTML 模板 + 独立 JS）
P3 结构拆分：webui.py 只留 handler/server/路由；页面资产落 mjc/assets/：
  assets/page.html   页面模板（body 尾 <script src="/assets/page.js">，零外部 CDN）
  assets/page.js     全部前端逻辑（独立文件 → node --check 可直接查）
本模块只做加载。改动页面后无需动 Python（webui 重启即生效；服务端 no-store 防缓存）。
"""
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_HTML_PATH = os.path.join(_DIR, "assets", "page.html")
PAGE_JS_PATH = os.path.join(_DIR, "assets", "page.js")


def load_page():
    """读取 HTML 模板（服务启动/导入时调用一次）"""
    with open(PAGE_HTML_PATH, encoding="utf-8") as f:
        return f.read()


def load_page_js():
    """读取独立 JS 文件（服务启动/导入时调用一次）"""
    with open(PAGE_JS_PATH, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    # 自检：python3 -m mjc.page_html → 输出两文件行数（无需 API）
    print(f"page.html  {len(load_page())} chars")
    print(f"page.js    {len(load_page_js())} chars")
