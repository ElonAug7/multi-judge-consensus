#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_live.py — 实时事件流离线测试（零 API）
覆盖：append/read_after 增量语义、同毫秒防重、坏行容错、task 会话摘要、归档重建。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import live


def run():
    tmp = tempfile.mkdtemp()
    old_path = live.PATH
    live.PATH = os.path.join(tmp, "live.jsonl")
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    # 无文件
    evs, last = live.read_after(0)
    check("无文件空流", evs == [] and last == 0)

    # append 基本
    live.append("stage_start", "t1", stage="deliver")
    live.append("opinion", "t1", judge="a", verdict="pass")
    evs, last = live.read_after(0)
    check("append 两条可读", len(evs) == 2 and last > 0)

    # 增量
    evs2, _ = live.read_after(last)
    check("after=last 只拿新的", evs2 == [])

    # 坏行容错
    with open(live.PATH, "a", encoding="utf-8") as f:
        f.write("{bad\n")
    live.append("final", "t1", verdict="revise")
    evs3, _ = live.read_after(0)
    check("坏行跳过不崩", len(evs3) == 3)

    # ts_ms 严格递增（同毫秒防重）
    tss = [e["ts_ms"] for e in evs3]
    check("ts_ms 严格递增", all(a < b for a, b in zip(tss, tss[1:])))

    # task_sessions 摘要（只收最近）
    live.append("opinion", "t2", judge="b")
    sess = live.task_sessions()
    check("task 会话摘要含最新", any(x["task_id"] == "t2" for x in sess))

    # 归档重建（MAX_LINES 小值模拟）
    old_max = live.MAX_LINES
    live.MAX_LINES = 5
    for i in range(8):
        live.append("opinion", f"t{i}", judge="j")
    evs4, _ = live.read_after(0)
    check("超限归档重建（新文件从最新继续）", len(evs4) <= 8 and os.path.exists(live.PATH + ".bak"))
    live.MAX_LINES = old_max

    live.PATH = old_path
    print("== live 全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
