#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · autoswitch.py — 自动审查总开关（pause / resume）
WebUI 后台管理 / CLI / hook 三处可拨；状态落 logs/auto/switch.json：
  {"enabled": true|false, "updated_at": "ISO", "by": "webui|cli|..."}
语义：
  enabled=false → hook 不点火、`mjc scan` 直接早退（0 花费、完全静音）
  文件缺失/损坏 → 默认 enabled=true（fail-open：开关本身故障不应悄悄停掉审查）
手动命令（auto/gate/review/judge-only）不受开关影响——只静音"自动扫描"这一路。
用法：
  python3 -m mjc.cli switch off      # 暂停自动审查
  python3 -m mjc.cli switch on       # 恢复
  python3 -m mjc.cli switch          # 查看状态
"""
import datetime
import json
import os

from mjc.paths import AUTO_LOG_DIR

SWITCH_PATH = os.path.join(AUTO_LOG_DIR, "switch.json")


def state():
    """当前开关状态（永不抛异常）。"""
    try:
        d = json.load(open(SWITCH_PATH, encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not a dict")
        return {
            "enabled": bool(d.get("enabled", True)),
            "updated_at": d.get("updated_at"),
            "by": d.get("by"),
        }
    except FileNotFoundError:
        return {"enabled": True, "updated_at": None, "by": None}
    except Exception:
        return {"enabled": True, "updated_at": None, "by": "corrupt->default"}


def is_enabled():
    return state()["enabled"]


def set_enabled(enabled, by=""):
    """写状态（原子：临时文件 + rename）。失败则抛出（调用方负责提示）。"""
    os.makedirs(os.path.dirname(SWITCH_PATH), exist_ok=True)
    d = {
        "enabled": bool(enabled),
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "by": by or "unknown",
    }
    tmp = SWITCH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, SWITCH_PATH)
    return state()


def cmd_switch(args):
    """CLI: mjc switch [on|off|status]"""
    action = getattr(args, "action", None) or "status"
    if action == "on":
        st = set_enabled(True, by="cli")
    elif action == "off":
        st = set_enabled(False, by="cli")
    else:
        st = state()
    print(json.dumps({"auto_switch": st}, ensure_ascii=False))
    return 0
