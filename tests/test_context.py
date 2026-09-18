#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_context.py — P5 上下文装配器离线测试（零 API）
验证 mjc.context 的确定性部分：py_compile 采集、运行采集、提示组装。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc.context import (assemble_code_context, build_code_review_prompt, CODE_REVIEW_TASK,
                          run_sandboxed, _filter_guarded, _all_divisions_guarded)
import tempfile


GOOD = 'print("hello")\n'
BAD = 'def f(:\n    pass\n'
RUNNABLE = 'if __name__ == "__main__":\n    print("ran-ok")\n'
NET = 'import socket\nsocket.create_connection(("8.8.8.8", 53), timeout=3)\nprint("connected")\n'
SLEEP = 'import time\ntime.sleep(30)\nprint("slept")\n'


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    # assemble：编译状态
    c_good = assemble_code_context(GOOD, run_cmd=None)
    check("好代码 → compile pass", c_good["compile_status"] == "pass")
    c_bad = assemble_code_context(BAD, run_cmd=None)
    check("坏代码 → compile fail", c_bad["compile_status"] == "fail")

    # assemble：运行采集（含 __main__ 自动跑）
    c_run = assemble_code_context(RUNNABLE, run_cmd=None)
    check("含 __main__ → 自动运行并采集输出", c_run["runtime_status"] == "ok" and "ran-ok" in c_run["runtime_output"])

    # assemble：无 __main__ 不自动跑
    c_norun = assemble_code_context(GOOD, run_cmd=None)
    check("无 __main__ → 不自动运行", c_norun["runtime_status"] == "skipped")

    # 提示组装：含代码 + 编译状态 + 运行输出
    ctx = assemble_code_context(RUNNABLE, run_cmd=None)
    prompt = build_code_review_prompt(CODE_REVIEW_TASK, RUNNABLE, ctx)
    check("提示含代码", RUNNABLE.strip() in prompt)
    check("提示含编译状态", "Compile status" in prompt and "pass" in prompt)
    check("提示含运行输出", "ran-ok" in prompt)
    check("提示含输出约束", "issues" in prompt and "JSON" in prompt)

    # ---- run_sandboxed：网络隔离 + 超时 ----------------
    tmp = tempfile.mkdtemp(prefix="mjc_sbtest_")
    def _write(name, code):
        p = os.path.join(tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(code)
        return p
    st, out = run_sandboxed(_write("net.py", NET), tmp)
    check("沙箱：网络隔离（连外网失败）", st.startswith("exit_") and "connected" not in out)
    st2, out2 = run_sandboxed(_write("sleep.py", SLEEP), tmp, wall_s=3)
    check("沙箱：超时（sleep 30 → timeout）", st2 == "timeout")

    # ---- 除零假阳性过滤 ----
    GUARDED_DIV = 'avg = total / n if n else 0\n'
    UNGUARDED_DIV = 'avg = total / n\n'
    check("除法带三元保护 → 识别为 guarded", _all_divisions_guarded(GUARDED_DIV) is True)
    check("除法无保护 → 不识别为 guarded", _all_divisions_guarded(UNGUARDED_DIV) is False)
    claim = {"func": "f", "desc": "除零错误", "fix": ""}
    kept, guarded = _filter_guarded([claim], GUARDED_DIV)
    check("除零主张 + 除法已保护 → 过滤并标记 guarded", kept == [] and guarded and guarded[0].get("guarded") is True)
    kept2, _ = _filter_guarded([claim], UNGUARDED_DIV)
    check("除零主张 + 除法无保护 → 保留", len(kept2) == 1)

    print("== 上下文装配器全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
