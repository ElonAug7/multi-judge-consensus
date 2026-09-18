#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arm F — 组合上下文 + 结构化提示：代码 + 运行输出，且显式要求查两类缺陷。

学习点（Arm E 暴露）：提示词偏向"看运行异常"→ 只抓运行时缺陷，漏掉 doc/常量 vs 实现的静态矛盾。
本臂：显式枚举两类缺陷（①注释/doc/常量 vs 实现不一致 ②运行输出里的行为异常），看能否取并集。
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.expanduser("~/.openclaw/workspace/multi-judge-consensus")
sys.path.insert(0, PROJ)

CODE = open(os.path.join(HERE, "order_engine.py"), encoding="utf-8").read()
comp = subprocess.run([sys.executable, "-m", "py_compile", os.path.join(HERE, "order_engine.py")],
                      capture_output=True, text=True)
COMPILE_STATUS = "通过" if comp.returncode == 0 else "失败:\n" + comp.stderr
demo = subprocess.run([sys.executable, os.path.join(HERE, "order_engine.py")],
                      capture_output=True, text=True, timeout=60)
DEMO_OUT = (demo.stdout or demo.stderr).strip()

TASK = ("审查这段电商订单结算引擎代码，找出其中的缺陷（bug）。请**同时**排查两类问题，缺一不可：\n"
        "【类 A：静态矛盾】注释/docstring/常量声明 与 代码实现不一致——例如注释写'满 200 元免运费'但常量是 199、"
        "docstring 写'按折后金额计税'但实现用折前小计、注释写'满 5 件'但常量是 10 等。逐条比对文档口径与实现。\n"
        "【类 B：运行时行为异常】下面附了真实运行输出，找出其中暴露的行为错误——例如退款金额超过实付、"
        "已支付订单发货失败、送达日期与承诺时效不符等。\n"
        "输出 JSON：{\"issues\":[{\"func\":\"函数名\",\"defect_id\":\"D?\",\"class\":\"A|B\",\"desc\":\"缺陷描述\",\"fix\":\"正确行为\"}]}"
        "只列有把握的，按严重程度排序。")

PROMPT = (TASK + "\n\n【代码 order_engine.py】\n" + CODE
          + "\n\n【编译状态】\n" + COMPILE_STATUS
          + "\n\n【运行输出（demo）】\n" + DEMO_OUT)


def main():
    from mjc import providers
    spec = "glm:glm-4-plus"
    prov, _, model = spec.partition(":")
    t0 = time.time()
    try:
        raw = providers.chat(prov, [{"role": "user", "content": PROMPT}],
                             model=model, temperature=0.2, max_tokens=8000, timeout=300)
    except Exception as e:
        print("ERROR:", e)
        return 1
    dt = time.time() - t0
    with open(os.path.join(HERE, "case_out", "F.json"), "w", encoding="utf-8") as f:
        json.dump({"arm": "F_combined", "spec": spec, "latency_s": round(dt, 2),
                   "raw": raw[:16000]}, f, ensure_ascii=False, indent=1)
    print("=== Arm F（组合上下文 + 结构化提示）===")
    print("latency:", round(dt, 1), "s")
    print(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
