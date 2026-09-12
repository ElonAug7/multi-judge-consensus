#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_verifier.py — 确定性验证器离线测试（零 API）
正样本：日期差错 / 百分比基数错 / 求和错（今早 08:36 演练里委员会漏/慢的那类）
负样本：正确数字不误报 / 歧义输入跳过（缺基数、跨 2 月无年份、非显式算式、容差内）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import verifier


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    # ---- 正样本：必须抓到 ----
    issues = verifier.verify("改动 8 月 31 日提交，9 月 5 日完成验收，历时 4 天。")
    check("日期错：8/31→9/5 写 4 天被抓住（应 5）", any("应为 5 天" in i["description"] for i in issues))

    issues = verifier.verify("每次审查 API 调用从 3.2 次降到 0.9 次，节省约 28%。")
    check("百分比错：3.2→0.9 写省 28% 被抓住（应 71.9%）", any("71.9" in i["description"] for i in issues))

    issues = verifier.verify("采购合计 18+12+20+25=85 万元。")
    check("求和错：18+12+20+25 写 85 被抓住（应 75）", any("75.0" in i["description"] for i in issues))

    issues = verifier.verify("时间线：2026-08-31 提交，2026-09-05 合并，历时 4 天。")
    check("ISO 日期错被抓住", any("应为 5 天" in i["description"] for i in issues))

    issues = verifier.verify("从 100 元涨到 150 元，增长 50%；18+12+20+25=75；8 月 1 日到 8 月 10 日共 9 天。")
    check("正确数字零误报", issues == [])

    # ---- 负样本/歧义：必须跳过 ----
    check("缺基数百分比跳过", verifier.verify("成本下降了 25%，效果显著。") == [])
    check("无算式数字跳过", verifier.verify("共 4 个模块，约 2000 行代码。") == [])
    check("跨 2 月无年份跳过（平闰歧义）", verifier.verify("2 月 28 日到 3 月 1 日，历时 2 天。") == [])
    check("有年份跨闰年精确算", verifier.verify("2024-02-28 到 2024-03-01，历时 2 天。") == [])
    check("无历时关键词跳过", verifier.verify("8 月 31 日提交，9 月 5 日合并。") == [])
    check("非显式求和跳过", verifier.verify("四项采购合计约 85 万。") == [])
    check("转述引用跳过（文中写/应为 上下文）", verifier.verify("改动摘要里写了 18+12+20+25=85 作为演示，应为 75。") == [])
    check("空文本", verifier.verify("") == [])

    # pipeline 集成：verifier 命中时 judge 不被调用（离线假 pool）
    import json
    import tempfile
    from unittest import mock
    from mjc import pipeline, paths as _paths

    # 运行数据隔离：usage.json 等写临时目录（不碰真 logs/）
    _old_log_dir = _paths.LOG_DIR
    _paths.LOG_DIR = tempfile.mkdtemp(prefix="mjc-verifier-")

    class BoomJudge:
        name = "boom:judge"
        display = "Boom（测试）"
        def review(self, *a, **k):
            raise AssertionError("验证器命中后不应再调 LLM")

    with mock.patch.dict("os.environ", {}, clear=False):
        rec, meta = pipeline.run_review_once(
            "测试任务", "从 3.2 次降到 0.9 次，节省约 28%。", [BoomJudge(), BoomJudge()],
            use_verifier=True, use_screen=False, use_cache=False, trust_path=None)
    check("pipeline 集成：命中→revise 且 0 调用", rec["final"] == "revise" and meta["api_calls"] == 0 and meta["verifier"])

    with mock.patch.dict("os.environ", {"MJC_VERIFIER": "0"}, clear=False):
        class QuietJudge:
            name = "quiet:judge"
            display = "Quiet（测试）"
            def review(self, *a, **k):
                return {"verdict": "pass", "confidence": 1.0, "issues": [], "final_reasoning": "ok"}
        rec2, meta2 = pipeline.run_review_once(
            "测试任务", "从 3.2 次降到 0.9 次，节省约 28%。", [QuietJudge(), QuietJudge()],
            use_verifier=True, use_screen=False, use_cache=False, trust_path=None)
    # env 关验证器后应走委员会（QuietJudge 被调用且最终 pass）
    check("env MJC_VERIFIER=0 可关闭（走委员会）", not meta2.get("verifier") and rec2["final"] == "pass")

    _paths.LOG_DIR = _old_log_dir
    print("== verifier 全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
