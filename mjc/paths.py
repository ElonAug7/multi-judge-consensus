#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · paths.py — 公共路径常量（P3 结构拆分：消除 cli.py 被 webui/settings 反依赖）
  BASE_DIR    仓库根
  LOG_DIR     logs/（debate/review/auto/cache 全在这下面）
  TRUST_PATH  logs/trust.json
  AUTO_LOG_DIR logs/auto/（自动审查日文件）
mjc.cli 保留同名模块级名字再导出 → 旧 import（from mjc.cli import LOG_DIR, TRUST_PATH）不破。
本模块零依赖，可被任何模块顶层引用（无循环 import 风险）。
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
TRUST_PATH = os.path.join(LOG_DIR, "trust.json")
AUTO_LOG_DIR = os.path.join(LOG_DIR, "auto")
