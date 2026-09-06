#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_screen_toggle.py — 初筛开关语义回归（深审发现的 bug：后台关初筛仍回退默认候选）
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import pipeline
from mjc import settings as S


def run():
    real = S.PATH
    tmp = tempfile.mkdtemp()
    S.PATH = os.path.join(tmp, "settings.json")
    cfg = {
        "providers": S.DEFAULT_PROVIDERS, "model_tags": S.DEFAULT_TAGS,
        "tiers": S.DEFAULT_TIERS,
        "current": {"tier": "standard", "screen_enabled": False,
                    "screen_model": "glm:glm-4-flash", "screen_conf": 0.75},
        "key_status": {},
    }
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    try:
        json.dump(cfg, open(S.PATH, "w"))
        check("后台关闭初筛 → resolve 返回 None（不回退默认候选）", pipeline.resolve_screen_judge(None) is None)

        cfg["current"]["screen_enabled"] = True
        json.dump(cfg, open(S.PATH, "w"))
        j = pipeline.resolve_screen_judge(None)
        check("开启且有模型 → 用配置模型", j is not None and j.name == "glm:glm-4-flash")

        cfg["current"]["screen_model"] = None
        json.dump(cfg, open(S.PATH, "w"))
        j2 = pipeline.resolve_screen_judge(None)
        check("开启但无模型 → 允许默认候选回退", j2 is not None)

        check("override 参数优先于关闭语义", pipeline.resolve_screen_judge("glm:glm-4-flash") is not None)
    finally:
        S.PATH = real
    print("== screen toggle 全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
