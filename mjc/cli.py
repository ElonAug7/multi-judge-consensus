#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · cli.py — 命令行调试界面
用法：
  python3 -m mjc.cli doctor                                   # 检查 providers/key 可用性
  python3 -m mjc.cli judge-only --task "..." --output "..."  # 委员会直接审文本（可 --degrade / --no-cache）
  python3 -m mjc.cli review --task "..."                     # 生成 → 初筛/委员会审查 → 打回循环
  python3 -m mjc.cli webui                                   # 启动本地 Web 界面（默认 127.0.0.1:8123）

Phase 3 成本优化（均可在 review/judge-only 上生效）：
  P3.1 初筛   review 默认开（--no-screen 关闭；模型 MJC_SCREEN_MODEL 或 --screen-model）
  P3.2 缓存   默认开（--no-cache 关闭；命中零 API 调用）
  P3.3 降级   --degrade 开启（连续一致≥5 次的 Judge 本轮降频，分歧自动升级回委员会）

向后兼容：ParallelArbiter/parallel_review 自 arbiter 移入后仍在 mjc.cli 可见（旧测试 import 不受影响）。
P3 拆分：auto_review/cmd_auto/cmd_scan 已移入 mjc.autocheck（本文件顶层再导出，旧 import 不破）；
BASE_DIR/LOG_DIR/TRUST_PATH/AUTO_LOG_DIR 移入 mjc.paths（本文件顶层再导出）。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import providers, cache, trust
from mjc.judge import build_pool
from mjc import pipeline
# 向后兼容再导出（tests/test_hallucination.py、test_debate.py 从此处 import）
from mjc.arbiter import Arbiter, ParallelArbiter, parallel_review, append_debate_log
from mjc.paths import BASE_DIR, LOG_DIR, TRUST_PATH, AUTO_LOG_DIR
# P3 拆分：自动审查移入 autocheck.py（顶层再导出 → 旧 import 不破）
from mjc.autocheck import auto_review, cmd_auto, cmd_scan
# 三票池（Phase 2 定案）：deepseek-v4-flash + glm-4-flash（便宜）+ glm-4-plus（精审）
DEFAULT_POOL = ("deepseek:deepseek-v4-flash", "glm:glm-4-flash", "glm:glm-4-plus")


def cmd_doctor(args=None):
    print("可用 providers:", providers.available_providers())
    for p in providers.available_providers():
        print(f"  {p}: 模型 {providers.MODELS[p]} — key 就绪 ✓")
    if not providers.available_providers():
        print("⚠️ 无可用 key！检查 keys.local.json 或 MJC_*_KEY 环境变量")
    try:
        from mjc import settings
        st = settings.admin_state()
        cur = st["current"]
        print(f"\n当前档位: {cur.get('tier_label', cur.get('tier', '?'))}")
        print(f"  委员会: {' + '.join(cur.get('committee') or ['(不可用)'])}")
        print(f"  初筛: {'开' if cur.get('screen_enabled') else '关'} "
              f"(model={cur.get('screen_model') or '—'}, conf={cur.get('screen_conf')})")
        print(f"  降级: {cur.get('degrade')} | 缓存: {cur.get('cache')}")
    except Exception as e:
        print(f"\n⚠️ 后台设置读取失败: {e}")
    print(f"\n信任分（{TRUST_PATH}）:")
    print(trust.describe(path=TRUST_PATH))
    print(f"审查缓存条目: {cache.count()}")
    try:
        import json as _json
        up = os.path.join(LOG_DIR, "usage.json")
        if os.path.exists(up):
            u = _json.load(open(up, encoding="utf-8"))
            print(f"累计用量: {u.get('reviews', 0)} 次审查 · {u.get('tokens', 0)} tokens · "
                  f"≈¥{u.get('cost_yuan', 0)} · 非 pass {u.get('nonpass', 0)} 次")
    except Exception:
        pass


def _pool_from(args):
    if args.pool is None or not args.pool:
        try:
            from mjc import settings
            args.pool = settings.effective()["committee"]
        except Exception as e:
            print(f"⚠️ 后台设置不可用（{e}）→ 用内置默认池", file=sys.stderr)
            args.pool = list(DEFAULT_POOL)
    pool = build_pool(list(args.pool))
    if len(pool) < 2:
        print(f"⚠️ Judge 不足: {len(pool)}（需要 ≥2）可用池: {args.pool}", file=sys.stderr)
        sys.exit(1)
    return pool


def _tri(flag_off, flag_on=False):
    """三态：True/False/None(跟设置)"""
    if flag_off:
        return False
    if flag_on:
        return True
    return None


def _print_result(record, meta, arb=None):
    """统一的人类可读输出（含 Phase 3 标记）"""
    marks = []
    if meta.get("cache_hit"):
        marks.append("缓存命中(0 API)")
    if meta.get("screened"):
        marks.append(("初筛放行" if meta.get("screen_passed") else "初筛未过→升级委员会")
                     + f"(screen_calls={meta.get('screen_calls', 0)})")
    if meta.get("degraded"):
        marks.append(f"降级跳过 {meta['degraded']}" + ("→分歧升级" if meta.get("escalated") else " 双 Judge 终局"))
    head = f"最终裁决: {record['final'].upper()}"
    if marks:
        head += "  [" + " | ".join(marks) + "]"
    print(head)
    toks = record.get("tokens") or {}
    cost = record.get("cost_yuan")
    print(f"票型: 通过={record.get('pass_votes')} 拒绝={record.get('reject_votes')} "
          f"修订={record.get('revise_votes')} | 辩论轮数: {record.get('debate_rounds', 0)} | "
          f"耗时: {record.get('elapsed_s')}s | API 调用: {meta.get('api_calls', '?')}"
          + (f" | tokens: {toks.get('total', 0)} (入{toks.get('prompt', 0)}/出{toks.get('completion', 0)})"
             + (f" | ≈¥{cost}" if cost is not None else "") if toks else ""))
    if record.get("screened"):
        op = record.get("screen_opinion") or {}
        print(f"  [初筛 {record.get('screen_model')}] pass conf={op.get('confidence')} "
              f"{op.get('final_reasoning', '')[:120]}")
        return
    for rnd in record.get("rounds") or []:
        print(f"—— 第{rnd['round']}轮({rnd['kind']}) ——")
        for o in rnd.get("opinions") or []:
            issues = o.get("issues") or []
            issue_summary = "; ".join(f"[{i.get('type')}] {i.get('description', '')[:60]}" for i in issues[:3])
            print(f"  {o.get('judge_display', o.get('judge_id'))}: {o.get('verdict')} "
                  f"(conf={o.get('confidence')}) {issue_summary}")


def cmd_judge_only(args):
    pool = _pool_from(args)
    screen_j = pipeline.resolve_screen_judge(args.screen_model) if getattr(args, "screen", False) else None
    os.makedirs(LOG_DIR, exist_ok=True)
    log = os.path.join(LOG_DIR, f"judge-{int(time.time())}.jsonl")
    record, meta = pipeline.run_review_once(
        args.task, args.output, pool,
        screen_judge=screen_j, screen_conf=pipeline.resolve_screen_conf(args.screen_conf),
        use_screen=bool(screen_j), use_cache=_tri(args.no_cache),
        use_degrade=_tri(args.no_degrade, args.degrade), trust_path=TRUST_PATH,
        debate_log_dir=LOG_DIR, log_path=log,
    )
    if args.json:
        print(json.dumps({"record": record, "meta": meta}, ensure_ascii=False, indent=1))
    else:
        _print_result(record, meta)
        print(f"(日志: {log})")


def cmd_review(args):
    """完整 review：Agent(生成模型) 产出 → 初筛/委员会审查 → 打回重写循环"""
    pool = _pool_from(args)
    screen_on = not args.no_screen  # review 默认开初筛（Phase 3 成本优化），--no-screen 关闭
    screen_j = pipeline.resolve_screen_judge(args.screen_model) if screen_on else None
    if screen_on and screen_j is None:
        print("⚠️ 无可用初筛 Judge（后台设置/glm-4-flash 均无 key）→ 直接走委员会", file=sys.stderr)
    screen_conf = pipeline.resolve_screen_conf(args.screen_conf)
    use_cache = _tri(args.no_cache)
    use_degrade = _tri(args.no_degrade, args.degrade)

    gen_provider = args.gen_provider or pool[0].provider
    gen_model = args.gen_model or providers.MODELS[gen_provider]
    os.makedirs(LOG_DIR, exist_ok=True)
    cache.set_cache_dir(os.path.join(LOG_DIR, "cache"))

    gen_sys = "你是 Agent，负责完成用户任务。输出直接可用、准确、不编造事实。"
    output = None
    feedback_all = ""
    attempt = 0
    summary_meta = {"screen_calls": 0, "committee_calls": 0, "api_calls": 0,
                    "cache_hit": False, "screened": 0, "screen_passed": 0, "degraded": 0}

    while attempt < args.max_rewrite:
        attempt += 1
        if output is None:
            print(f"[生成] {providers.display_name(gen_provider, gen_model)} 正在产出…")
            output = providers.chat(gen_provider, [
                {"role": "system", "content": gen_sys},
                {"role": "user", "content": args.task},
            ], model=gen_model, temperature=0.4, max_tokens=2000)
            print(f"[生成] 完成，{len(output)} 字符\n")
        elif feedback_all:
            print(f"[重写 #{attempt}] 已按反馈修改\n")
            gen_messages = [
                {"role": "system", "content": gen_sys + "\n你的上一版输出被审查委员会打回，请根据反馈修改。不要解释，直接输出修改后的完整内容。"},
                {"role": "user", "content": args.task},
                {"role": "assistant", "content": output},
                {"role": "user", "content": "审查反馈：\n" + feedback_all},
            ]
            output = providers.chat(gen_provider, gen_messages, model=gen_model, temperature=0.3, max_tokens=2000)

        log = os.path.join(LOG_DIR, f"review-{int(time.time())}.jsonl")
        label = "初筛+委员会" if screen_j else "委员会"
        print(f"[审查 #{attempt}] {label}（{len(pool)} Judge）…")
        record, meta = pipeline.run_review_once(
            args.task, output, pool,
            screen_judge=screen_j, screen_conf=screen_conf,
            use_screen=bool(screen_j), use_cache=use_cache,
            use_degrade=use_degrade, trust_path=TRUST_PATH,
            debate_log_dir=LOG_DIR, log_path=log,
        )
        _print_result(record, meta)
        print()
        # 汇总统计
        for k in ("screen_calls", "committee_calls", "api_calls"):
            summary_meta[k] = summary_meta.get(k, 0) + meta.get(k, 0)
        summary_meta["cache_hit"] = summary_meta["cache_hit"] or meta.get("cache_hit")
        summary_meta["screened"] += 1 if meta.get("screened") else 0
        summary_meta["screen_passed"] += 1 if meta.get("screen_passed") else 0
        summary_meta["degraded"] += 1 if meta.get("degraded") else 0

        if record["final"] == "pass":
            print("✅ 通过审查。最终输出：\n")
            print(output)
            break
        elif record["final"] == "reject":
            feedback_all = collect_feedback(record)
            print(f"❌ 被拒绝（{record['reject_votes']} 票），打回重写…\n")
            output = None
        elif record["final"] == "revise":
            feedback_all = collect_feedback(record)
            print(f"⚠️ 需修改（{record['revise_votes']} 票），打回修订…\n")
            output = None
        else:
            print("⛔ 委员会无共识（need_human），停止。建议人工审查。")
            print("最终输出：\n")
            print(output)
            break
    else:
        if output is not None:
            print(f"⛔ 达到最大打回次数（{args.max_rewrite}），强制输出并标记低置信度：\n")
            print(output)

    print("-" * 50)
    print(f"本轮统计: API 调用 {summary_meta['api_calls']}（初筛 {summary_meta['screen_calls']} + "
          f"委员会 {summary_meta['committee_calls']}）| 缓存命中 {summary_meta['cache_hit']} | "
          f"初筛放行 {summary_meta['screen_passed']}/{summary_meta['screened']} | 降级跳过 {summary_meta['degraded']} 次")


def collect_feedback(record):
    """收集所有 Judge 的 issues → 反馈文本"""
    lines = []
    for rnd in record.get("rounds") or []:
        for o in rnd.get("opinions") or []:
            if o.get("verdict") in ("reject", "revise"):
                for i in (o.get("issues") or [])[:4]:
                    desc = i.get("description", "")
                    sug = i.get("suggestion", "")
                    lines.append(f"- {o.get('judge_display')}: {desc}" + (f" 建议: {sug}" if sug else ""))
    return "\n".join(lines[:12])


def cmd_webui(args):
    from mjc.webui import main as webui_main
    webui_main(args.host, args.port)


def cmd_bench(args):
    """红队基准运行器（真 API；默认 quick 子集控制成本）"""
    from mjc import bench
    ids = [x.strip() for x in (args.ids or "").split(",") if x.strip()] or None
    rounds_list = [int(x) for x in (args.ablation or "").split(",") if x.strip().isdigit()] if args.ablation else [2]
    out = []
    for mr in rounds_list:
        rep = bench.run_bench(ids=ids, quick=(args.set == "quick" and not ids),
                              use_screen=not args.no_screen, max_debate_rounds=mr,
                              compare_single=args.compare,
                              verbose=not args.json and len(rounds_list) == 1)
        out.append(rep)
    if args.json:
        print(json.dumps(out if len(out) > 1 else out[0], ensure_ascii=False, indent=1))
    elif len(rounds_list) > 1:
        print("\n== 辩论轮数消融 ==")
        for mr, rep in zip(rounds_list, out):
            print(f"  轮数上限 {mr}: Recall {rep.get('recall')} | F1 {rep.get('f1')} | API {rep.get('api_calls')}")
    return 0


def cmd_gate(args):
    """阶段闸门（P1 实时）：设计/写码/交付检查点审查。
    事件写 live 流（WebUI 实时动画）；裁决 pass→0 / revise→2 / reject·need_human→3 / 错误→1。
    用法：python3 -m mjc.cli gate --stage deliver --task "<原始任务>" --content "<产物文本>" [--id mytask]"""
    import datetime
    import hashlib
    from mjc import autocheck, live
    content = (args.content or "").strip()
    if not content:
        print(json.dumps({"error": "content 必填"}, ensure_ascii=False))
        return 1
    task = (args.task or "").strip()[:400]
    tid = args.id or (hashlib.sha1((task + "|" + args.stage).encode()).hexdigest()[:8])
    stage = args.stage or "deliver"
    live.append("stage_start", tid, stage=stage, task=task[:200])
    try:
        code, out = autocheck.auto_review(
            content, channel=f"gate:{stage}", task=task, kind="code",
            no_memory=args.no_memory, min_len=20,
            emit=lambda ev: live.append(ev["kind"], tid,
                                        **{k: v for k, v in ev.items() if k not in ("kind", "ts_ms", "at", "task_id")}),
            task_id=tid,
        )
    except Exception as e:
        print(json.dumps({"error": f"审查失败: {e}"}, ensure_ascii=False))
        return 1
    if out.get("skipped"):
        print(json.dumps(out, ensure_ascii=False))
        return 1
    out["task_id"] = tid
    out["stage"] = stage
    verdict = out.get("verdict", "error")
    live.append("stage_done", tid, stage=stage, verdict=verdict, api_calls=out.get("api_calls", 0), sha=out.get("sha"))
    print(json.dumps(out, ensure_ascii=False))
    # 闸门语义：非 pass 即阻断（agent 需修复后重跑本 stage）
    if verdict == "pass":
        return 0
    if verdict == "revise":
        return 2
    if verdict in ("reject", "need_human"):
        return 3
    return 1


def cmd_dispose(args):
    """对一次 auto 审查的 issues 逐条记录处置（采纳/不采纳+理由）→ logs/auto/dispositions.jsonl。
    用法：--in <json>，内容 {entry: <auto 日文件行>, decisions: [{idx, adopted, action?, note}]}"""
    import json as _json
    try:
        data = _json.load(open(args.in_file, encoding="utf-8"))
    except Exception as e:
        print(_json.dumps({"error": f"读输入失败: {e}"}, ensure_ascii=False))
        return 1
    try:
        from mjc import dispositions
        rec = dispositions.append(data.get("entry") or {}, data.get("decisions") or [])
    except ValueError as e:
        print(_json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1
    print(_json.dumps({"ok": True, "at": rec["at"],
                        "n_issues": len(rec["review"]["issues"]),
                        "adopted": sum(1 for d in rec["decisions"] if d["adopted"])},
                       ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Multi-Judge Consensus CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_doctor = sub.add_parser("doctor", help="检查 key/模型可用性/信任分/缓存")
    p_doctor.set_defaults(fn=cmd_doctor)

    p_judge = sub.add_parser("judge-only", help="审查给定文本（委员会；可开初筛/降级）")
    p_judge.add_argument("--task", required=True)
    p_judge.add_argument("--output", required=True)
    p_judge.add_argument("--pool", default=None, help="委员会（逗号分隔 provider:model）；默认=后台当前档位")
    p_judge.add_argument("--screen", action="store_true", help="P3.1 开初筛（pass+高置信免委员会）")
    p_judge.add_argument("--screen-model", default=None)
    p_judge.add_argument("--screen-conf", type=float, default=None, help="默认=后台设置")
    p_judge.add_argument("--degrade", action="store_true", help="P3.3 信任降级（开）")
    p_judge.add_argument("--no-degrade", action="store_true", help="强制关降级（默认跟后台设置）")
    p_judge.add_argument("--no-cache", action="store_true", help="P3.2 关缓存（默认跟后台设置）")
    p_judge.add_argument("--json", action="store_true")
    p_judge.set_defaults(fn=cmd_judge_only)

    p_review = sub.add_parser("review", help="生成 → 初筛/委员会审查 → 打回循环")
    p_review.add_argument("--task", required=True)
    p_review.add_argument("--pool", default=None, help="委员会（逗号分隔 provider:model）；默认=后台当前档位")
    p_review.add_argument("--gen-provider", default=None)
    p_review.add_argument("--gen-model", default=None)
    p_review.add_argument("--max-rewrite", type=int, default=3)
    p_review.add_argument("--no-screen", action="store_true", help="P3.1 关初筛（默认开）")
    p_review.add_argument("--screen-model", default=None)
    p_review.add_argument("--screen-conf", type=float, default=None, help="默认=后台设置")
    p_review.add_argument("--degrade", action="store_true", help="P3.3 信任降级（开）")
    p_review.add_argument("--no-degrade", action="store_true", help="强制关降级（默认跟后台设置）")
    p_review.add_argument("--no-cache", action="store_true", help="P3.2 关缓存（默认跟后台设置）")
    p_review.set_defaults(fn=cmd_review)

    p_auto = sub.add_parser("auto", help="自动审查（hook 扫描 / 代码交付工作流）：内容+记忆 → 委员会 → logs/auto/")
    p_auto.add_argument("--in", dest="in_file", default=None, help="事件 JSON {content,channel,task,kind}")
    p_auto.add_argument("--content", default=None, help="直接给文本")
    p_auto.add_argument("--task", default=None, help="原始任务（记忆检索/审查用）")
    p_auto.add_argument("--kind", default=None, help="message（默认）| code（代码任务收尾审查）")
    p_auto.add_argument("--no-memory", action="store_true", help="跳过记忆上下文检索")
    p_auto.set_defaults(fn=cmd_auto)

    p_scan = sub.add_parser("scan", help="转录扫描（webchat 自动审查触发源）：找最新未审终稿并审查")
    p_scan.add_argument("--force", action="store_true", help="跳过 150 字门控")
    p_scan.add_argument("--no-memory", action="store_true", help="跳过记忆上下文检索")
    p_scan.set_defaults(fn=cmd_scan)

    p_bench = sub.add_parser("bench", help="红队基准集：四类对抗样本 Recall/Precision/F1 + 成本追踪（真 API）")
    p_bench.add_argument("--set", default="quick", choices=["quick", "v1-full", "ids"], help="quick=9 条子集(默认)；v1-full=全 21 条")
    p_bench.add_argument("--ids", default=None, help="逗号分隔样本 ID（如 CD1,NM2,CL1）")
    p_bench.add_argument("--no-screen", action="store_true", help="关初筛（全委员会）")
    p_bench.add_argument("--ablation", default=None, help="辩论轮数消融：逗号分隔 0,1,2（每档全跑一遍）")
    p_bench.add_argument("--compare", action="store_true", help="每条缺陷样本同时跑单模型(glm-4-plus)对照票")
    p_bench.add_argument("--json", action="store_true")
    p_bench.set_defaults(fn=cmd_bench)

    p_gate = sub.add_parser("gate", help="阶段闸门：设计/写码/交付检查点审查（实时事件流）——非 pass 退出码阻断")
    p_gate.add_argument("--stage", required=True, choices=["design", "code", "deliver"])
    p_gate.add_argument("--task", required=True)
    p_gate.add_argument("--content", required=True)
    p_gate.add_argument("--id", default=None, help="任务会话 id（默认 task+stage 哈希）")
    p_gate.add_argument("--no-memory", action="store_true")
    p_gate.set_defaults(fn=cmd_gate)

    p_dispose = sub.add_parser("dispose", help="记录审查意见处置（主 agent 逐条答复）→ dispositions.jsonl（WebUI 纠正过程可视化）")
    p_dispose.add_argument("--in", dest="in_file", required=True, help="JSON 文件 {entry, decisions:[{idx,adopted,action?,note}]}")
    p_dispose.set_defaults(fn=cmd_dispose)

    p_web = sub.add_parser("webui", help="本地 Web 界面（默认 127.0.0.1:8123）")
    p_web.add_argument("--host", default=os.environ.get("MJC_WEBUI_HOST", "127.0.0.1"))
    p_web.add_argument("--port", type=int, default=int(os.environ.get("MJC_WEBUI_PORT", "8123")))
    p_web.set_defaults(fn=cmd_webui)

    args = ap.parse_args()
    if hasattr(args, "pool") and args.pool:
        args.pool = [x.strip() for x in args.pool.split(",") if x.strip()]
    if hasattr(args, "screen_model") and args.screen_model:
        args.screen_model = args.screen_model.strip()
    rc = args.fn(args)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
