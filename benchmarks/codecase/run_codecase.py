#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码审查案例跑批：同一份含植入缺陷的代码，跑三个臂并记录可核验的量化数据。

臂设计（同一输入、同一时刻，唯一变量=审查方式）：
  A 确定性验证器   mjc.verifier.verify(text) —— 零 API、零延迟，只查数字/日期/求和类硬错误
  B 单模型自查     1 次 LLM 调用（默认 glm-4-plus），提示词="请审查这段代码"
  C MJC 全流水线   python3 -m mjc.cli gate --stage code（委员会+初筛+仲裁+证伪+确定性层）

产出：case_out/{arm}.json（原始结果）+ 控制台对比表。
判定与召回由主代理对照 GROUND_TRUTH.json 逐条人工核验，不在本脚本内自动打分
（自动关键词匹配会把"提到该函数"误当成"指出该缺陷"，宁可人工）。
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.expanduser("~/.openclaw/workspace/multi-judge-consensus")
CODE = os.path.join(HERE, "order_engine.py")
OUT = os.path.join(HERE, "case_out")

TASK = ("审查这段电商订单结算引擎代码（order_engine.py，约 500 行）。"
        "重点找：注释/文档与实现不符、数字或日期错误、逻辑矛盾与不可达分支、"
        "同一业务口径前后不一致。编译类错误不在范围内（代码本身可通过 py_compile）。")


def sh(cmd, timeout=600, **kw):
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    return p, time.time() - t0


def arm_a(code):
    """确定性验证器：零 API。"""
    sys.path.insert(0, PROJ)
    from mjc import verifier
    t0 = time.time()
    items = verifier.verify(code)
    return {"arm": "A_verifier", "calls": 0, "cost_yuan": 0.0,
            "latency_s": round(time.time() - t0, 2), "issues": items,
            "n_issues": len(items or [])}


def arm_b(code):
    """单模型自查：1 次调用（起点最低的对照）。"""
    sys.path.insert(0, PROJ)
    from mjc import providers
    spec = "glm:glm-4-plus"
    p, _, m = spec.partition(":")
    prompt = (TASK + "\n\n请逐条列出你发现的问题（JSON 数组，每条含 line_hint/type/desc），"
              "没有把握的不要写。\n\n【代码】\n" + code)
    t0 = time.time()
    err = None
    txt = ""
    try:
        txt = providers.chat(p, [{"role": "user", "content": prompt}], model=m,
                             temperature=0.2, max_tokens=8000, timeout=240)
    except Exception as e:  # noqa
        err = str(e)[:200]
    return {"arm": "B_single_model", "spec": spec, "calls": 1 if not err else 0,
            "cost_yuan": None, "latency_s": round(time.time() - t0, 2),
            "raw": txt[:20000], "error": err}


def arm_c(code):
    """MJC 全流水线：gate --stage code（退出码 pass=0/revise=2/reject=3）。"""
    cmd = [sys.executable, "-m", "mjc.cli", "gate", "--stage", "code",
           "--task", TASK, "--content", code]
    p, dt = sh(cmd, timeout=900, cwd=PROJ)
    line = ""
    for l in (p.stdout or "").strip().splitlines()[::-1]:
        if l.strip().startswith("{"):
            line = l.strip()
            break
    try:
        out = json.loads(line) if line else {"raw": (p.stdout or "")[-4000:]}
    except Exception:
        out = {"raw": (p.stdout or "")[-4000:]}
    out.update({"arm": "C_mjc_full", "exit_code": p.returncode, "latency_s": round(dt, 2)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="a,b,c")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    code = open(CODE, encoding="utf-8").read()
    print(f"代码: {CODE} · {len(code.splitlines())} 行 / {len(code)} 字符")
    print(f"任务: {TASK[:60]}…\n")
    res = {}
    for k in [x.strip() for x in a.arms.split(",") if x.strip()]:
        fn = {"a": arm_a, "b": arm_b, "c": arm_c}.get(k)
        if not fn:
            continue
        print(f"--- 臂 {k.upper()} 开跑 ---", flush=True)
        try:
            r = fn(code)
        except Exception as e:  # noqa
            r = {"arm": k, "error": str(e)[:300]}
        res[k.upper()] = r
        with open(os.path.join(OUT, f"{k.upper()}.json"), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        brief = {kk: vv for kk, vv in r.items() if kk not in ("issues", "raw")}
        print("   ", json.dumps(brief, ensure_ascii=False)[:400], flush=True)
        if r.get("issues"):
            for i, it in enumerate(r["issues"], 1):
                print(f"    [{i}] {json.dumps(it, ensure_ascii=False)[:150]}")
        if r.get("raw"):
            print("    raw 前 300 字:", r["raw"][:300].replace("\n", " "))
    with open(os.path.join(OUT, "all.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"\n原始结果: {OUT}")


if __name__ == "__main__":
    main()
