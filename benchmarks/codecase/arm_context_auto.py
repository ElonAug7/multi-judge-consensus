#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arm E — 自动装配上下文（诚实臂）：不用手写规格，只用真实可自动采集的上下文。

自动采集：
  1) py_compile 状态（确定性校验）
  2) 运行 order_engine.py 的 demo 输出（运行时行为）
单模型 glm-4-plus + 上述自动上下文。测：不喂手写答案纸，召回能从 6/12 推到多少？
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

# 1) 确定性校验：py_compile
comp = subprocess.run([sys.executable, "-m", "py_compile", os.path.join(HERE, "order_engine.py")],
                      capture_output=True, text=True)
COMPILE_STATUS = "通过（无语法错误）" if comp.returncode == 0 else "失败:\n" + comp.stderr

# 2) 运行 demo（自动采集运行时行为）
demo = subprocess.run([sys.executable, os.path.join(HERE, "order_engine.py")],
                      capture_output=True, text=True, timeout=60)
DEMO_OUT = demo.stdout.strip() or demo.stderr.strip()

TASK = ("审查这段电商订单结算引擎代码，找出其中的缺陷（bug）。"
        "下面附上该模块的编译状态与一次真实运行输出（demo 自测片段）。"
        "请结合代码实现与实际运行行为，找出代码缺陷：函数名、错误内容、正确行为应是什么。"
        "特别留意运行输出里出现的异常现象（如发货失败、退款金额异常、时效不一致等）。"
        "只列有把握的，按严重程度排序。")

PROMPT = (TASK + "\n\n【代码 order_engine.py】\n" + CODE
          + "\n\n【编译状态】\n" + COMPILE_STATUS
          + "\n\n【运行输出（demo）】\n" + DEMO_OUT
          + "\n\n请输出 JSON：{\"issues\":[{\"func\":\"函数名\",\"defect_id\":\"D?（若能对应）\",\"desc\":\"缺陷描述\",\"fix\":\"正确行为\"}]}")


def main():
    from mjc import providers
    spec = "glm:glm-4-plus"
    prov, _, model = spec.partition(":")
    t0 = time.time()
    try:
        raw = providers.chat(prov, [{"role": "user", "content": PROMPT}],
                             model=model, temperature=0.2, max_tokens=6000, timeout=300)
    except Exception as e:
        print("ERROR:", e)
        return 1
    dt = time.time() - t0
    out = {"arm": "E_auto_context", "spec": spec, "latency_s": round(dt, 2),
           "compile": COMPILE_STATUS, "demo_out": DEMO_OUT, "raw": raw[:12000]}
    with open(os.path.join(HERE, "case_out", "E.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("=== Arm E（自动装配上下文，单模型）===")
    print("latency:", round(dt, 1), "s")
    print("--- demo 输出 ---")
    print(DEMO_OUT)
    print("--- 模型审查结果 ---")
    print(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
