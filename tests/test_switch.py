#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_switch.py — 自动审查总开关（autoswitch）离线测试（0 API）
覆盖：状态读写/默认值/损坏兜底/原子写 + cmd_scan 开关门控 + admin_state 暴露
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import autoswitch

_ok = _fail = 0


def check(name, cond):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ✓ {name}")
    else:
        _fail += 1
        print(f"  ✗ {name}")


tmp = tempfile.mkdtemp(prefix="mjc-switch-")
orig_path = autoswitch.SWITCH_PATH
try:
    autoswitch.SWITCH_PATH = os.path.join(tmp, "sub", "switch.json")

    # ---- 状态读写 ----
    st = autoswitch.state()
    check("文件缺失 → 默认 enabled=True", st["enabled"] is True and st["updated_at"] is None)
    check("is_enabled 默认 True", autoswitch.is_enabled() is True)

    st = autoswitch.set_enabled(False, by="test")
    check("set off → enabled=False", st["enabled"] is False and autoswitch.is_enabled() is False)
    d = json.load(open(autoswitch.SWITCH_PATH, encoding="utf-8"))
    check("文件内容 enabled=False + by=test", d["enabled"] is False and d.get("by") == "test")
    check("updated_at 已写入", bool(d.get("updated_at")))
    check("目录自动创建（sub/）", os.path.isdir(os.path.dirname(autoswitch.SWITCH_PATH)))

    st = autoswitch.set_enabled(True, by="test")
    check("set on → enabled=True", autoswitch.is_enabled() is True)

    with open(autoswitch.SWITCH_PATH, "w", encoding="utf-8") as f:
        f.write("{broken json")
    check("损坏文件 → fail-open 默认开", autoswitch.is_enabled() is True)

    # ---- cmd_scan 门控 ----
    from mjc import autocheck
    from mjc import scan as scan_mod

    calls = {"n": 0}
    orig_cand = scan_mod.newest_candidate
    orig_locked = scan_mod.locked
    scan_mod.newest_candidate = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), (None, None))[1]
    scan_mod.locked = lambda: False
    try:
        class A:
            force = False
            no_memory = True

        autoswitch.set_enabled(False, by="test")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = autocheck.cmd_scan(A())
        check("暂停时 scan 早退：不调用扫描", calls["n"] == 0)
        check("暂停时输出 skipped=paused_by_switch", "paused_by_switch" in buf.getvalue())
        check("暂停时返回码 0", rc == 0)

        autoswitch.set_enabled(True, by="test")
        calls["n"] = 0
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = autocheck.cmd_scan(A())
        check("开启后 scan 恢复扫描（调用一次）", calls["n"] == 1)
        check("开启后返回码 0", rc == 0)
    finally:
        scan_mod.newest_candidate = orig_cand
        scan_mod.locked = orig_locked

    # ---- admin_state 暴露 ----
    autoswitch.set_enabled(True, by="test")
    from mjc import settings
    st_full = settings.admin_state()
    check("admin_state 含 auto_switch", isinstance(st_full.get("auto_switch"), dict)
          and st_full["auto_switch"].get("enabled") is True)
    check("admin_state 含 usage 摘要", isinstance(st_full.get("usage"), dict)
          and "reviews" in st_full["usage"])
finally:
    autoswitch.SWITCH_PATH = orig_path
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n== test_switch：{_ok} 通过 / {_fail} 失败 ==")
sys.exit(1 if _fail else 0)
