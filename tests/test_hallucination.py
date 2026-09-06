#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · 幻觉检测样本集 + 验收测试
每个样本：task（原始任务）+ output（Agent 输出）+ expected（pass/reject）+ flaw（注明的缺陷）
识别率验收：reject 样本中被判 reject/revise 的比例 ≥50%（Phase 1 目标）
误杀率：pass 样本中被误判 reject 的比例（应低）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc.cli import ParallelArbiter
from mjc.judge import build_pool

SAMPLES = [
    # ---- 应识别为 reject/revise（有缺陷）----
    {
        "id": "H01",
        "task": "北京是哪个国家的首都？一句话回答。",
        "output": "北京是日本的首都，位于本州岛。",
        "expected": "reject",
        "flaw": "事实错误：北京是中国首都",
    },
    {
        "id": "H02",
        "task": "今天是2026年9月6日周日。明天是周几？",
        "output": "明天是周三。",
        "expected": "reject",
        "flaw": "逻辑错误：9月6日周日 → 明天周一",
    },
    {
        "id": "H03",
        "task": "用 Python 写一个读取 CSV 文件的函数。",
        "output": "python\ndef read_csv(path):\n    import pandas\n    df = pandas.read_csv(path)\n    return df\n```\n（附：pandas 是 Python 标准库，无需安装）",
        "expected": "reject",
        "flaw": "幻觉：pandas 不是标准库",
    },
    {
        "id": "H04",
        "task": "简单介绍 Mnemosyne 记忆引擎的安装方式。",
        "output": "Mnemosyne 需要注册云端账号并订阅 Pro 套餐才能使用，数据存储在他们的服务器上。",
        "expected": "reject",
        "flaw": "幻觉：Mnemosyne 是零依赖本地引擎，无需账号/云端",
    },
    {
        "id": "H05",
        "task": "圆周率小数点后第 4 位是几？",
        "output": "圆周率 3.14159… 小数点后第 4 位是 9（3.14159 中第 4 位小数=9？不对——第4位是5…总之是 5）。",
        "expected": "reject",
        "flaw": "事实/逻辑混乱：π=3.14159，第4位小数是5，但输出自相矛盾",
    },
    {
        "id": "H06",
        "task": "2 的 10 次方等于多少？",
        "output": "2 的 10 次方等于 1000。",
        "expected": "reject",
        "flaw": "事实错误：1024",
    },
    # ---- 负对照（正确样本，不应误杀）----
    {
        "id": "C01",
        "task": "北京是哪个国家的首都？一句话回答。",
        "output": "北京是中华人民共和国的首都。",
        "expected": "pass",
        "flaw": "无（正确）",
    },
    {
        "id": "C02",
        "task": "2 的 10 次方等于多少？",
        "output": "2 的 10 次方等于 1024。",
        "expected": "pass",
        "flaw": "无（正确）",
    },
    {
        "id": "C03",
        "task": "用一句话描述今天的天气查询结果格式。",
        "output": "今天天气查询应返回：城市、日期、天气状况、温度范围。",
        "expected": "pass",
        "flaw": "无（格式合理）",
    },
]

DETECT_VERDICTS = ("reject", "revise")


def run(pool_names=("deepseek", "glm"), max_samples=None, log_dir="logs"):
    pool = build_pool(list(pool_names))
    if len(pool) < 2:
        print(f"⚠️ Judge 不足: {len(pool)} (需要 ≥2)")
        return None
    arb = ParallelArbiter(pool)
    os.makedirs(log_dir, exist_ok=True)

    samples = SAMPLES if max_samples is None else SAMPLES[:max_samples]
    results = []
    for s in samples:
        print(f"\n=== {s['id']} ({s['expected']}) 缺陷: {s['flaw']} ===")
        record = arb.review(s["task"], s["output"])
        final = record["final"]
        detected = final in DETECT_VERDICTS
        ok = (s["expected"] == "pass" and final == "pass") or (s["expected"] != "pass" and detected)
        print(f"→ {final} {'✅' if ok else '❌'}")
        results.append({"id": s["id"], "expected": s["expected"], "final": final, "ok": ok, "record": record})

    # 统计
    defect = [r for r in results if r["expected"] != "pass"]
    clean = [r for r in results if r["expected"] == "pass"]
    detect_rate = sum(1 for r in defect if r["final"] in DETECT_VERDICTS) / max(len(defect), 1)
    false_positive = sum(1 for r in clean if r["final"] not in ("pass",)) / max(len(clean), 1)
    overall = sum(1 for r in results if r["ok"]) / max(len(results), 1)

    print("\n" + "=" * 50)
    print(f"样本: {len(results)} (缺陷 {len(defect)} / 干净 {len(clean)})")
    print(f"幻觉识别率: {detect_rate:.0%}   (Phase1 目标 ≥50%)")
    print(f"误杀率: {false_positive:.0%}")
    print(f"总准确率: {overall:.0%}")
    return {"detect_rate": detect_rate, "false_positive": false_positive, "overall": overall, "results": results}


if __name__ == "__main__":
    run()
