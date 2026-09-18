#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arm D — 上下文注入对照：给审阅者喂「业务规格测试 + 运行失败输出」，测召回是否突破 6/12。

假设：语义/逻辑缺陷的召回天花板由「缺业务上下文」决定，而非「Judge 数量/厂商多样性」。
本臂：单模型（glm-4-plus）+ 附规格测试断言（=正确业务口径）+ 失败输出，看能否接近 12/12。
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
SPEC = open(os.path.join(HERE, "spec_tests.py"), encoding="utf-8").read()

# 运行 spec_tests 拿失败输出
p = subprocess.run([sys.executable, os.path.join(HERE, "spec_tests.py")],
                   capture_output=True, text=True)
FAIL_OUT = p.stdout.strip()

TASK = ("审查这段电商订单结算引擎代码，找出其中的缺陷（bug）。"
        "下面附了一份由测试组按业务需求文档编写的「业务规格测试」及其运行结果——"
        "规格测试断言的是**正确的业务口径**，当前全部失败。"
        "请结合规格测试断言与代码实现，逐条指出代码缺陷：函数名、错误内容、正确行为应是什么。"
        "只列有把握的，按严重程度排序。")

PROMPT = (TASK + "\n\n【代码 order_engine.py】\n" + CODE
          + "\n\n【业务规格测试 spec_tests.py】\n" + SPEC
          + "\n\n【规格测试运行结果】\n" + FAIL_OUT
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
    out = {"arm": "D_context", "spec": spec, "latency_s": round(dt, 2), "raw": raw[:12000]}
    with open(os.path.join(HERE, "case_out", "D.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("=== Arm D（上下文注入，单模型）===")
    print("latency:", round(dt, 1), "s")
    print(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
