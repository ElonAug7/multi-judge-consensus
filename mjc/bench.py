#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · bench.py — 对抗性基准集运行器（P 红队基准）
样本：samples/bench-v1.json（跨文档矛盾/时序幻觉/数值陷阱/指令偏离 + 干净对照）
指标：分猫 Recall（flawed 被拦比例）、对照误报率、整体 Precision/F1、token/费用/调用数
  caught = final ∈ (reject, revise, need_human)；verifier 命中单独记账（deterministic_caught）
运行：python3 -m mjc.cli bench [--set v1|quick] [--ids CD1,...] [--ablation 0,1,2] [--no-memory] [--json]
  quick = 每类前 2 条 flawed + 1 条对照（9 条）；默认当前后台档位；不写信任分/缓存/记忆。
历史：logs/bench-history.jsonl（ts/版本/配置快照/指标/成本），供 Recall-Precision 趋势追踪。
"""
import datetime
import json
import os
import time

from mjc import paths
from mjc.judge import build_pool
from mjc import pipeline, providers, settings

BENCH_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "bench-v1.json")
HISTORY = os.path.join(paths.LOG_DIR, "bench-history.jsonl")
QUICK_CATS = {"cross-doc": 2, "temporal": 2, "numerical": 2, "instruction": 2, "control": 1}


def load_samples():
    d = json.load(open(BENCH_FILE, encoding="utf-8"))
    return d.get("samples", [])


def select(ids=None, quick=False):
    samples = load_samples()
    if ids:
        return [s for s in samples if s["id"] in ids]
    if quick:
        out, used = [], {}
        for s in samples:
            n = used.get(s["cat"], 0)
            cap = QUICK_CATS.get(s["cat"], 1)
            if n < cap:
                out.append(s)
                used[s["cat"]] = n + 1
        return out
    return samples


def run_one(task, output, pool, use_screen, max_debate_rounds, single_judge=None):
    rec, meta = pipeline.run_review_once(
        task, output, pool,
        screen_judge=pipeline.resolve_screen_judge(None) if use_screen else None,
        screen_conf=None, use_screen=use_screen,
        use_cache=False, use_degrade=False, trust_path=None,  # 基准不污染信任分
        memory=None,  # 基准固定无记忆（可复现）；记忆对裁决的影响单独实验
        max_debate_rounds=max_debate_rounds,
    )
    single_final = None
    if single_judge is not None:
        try:
            op = single_judge.review(task, output)
            single_final = op.get("verdict")
        except Exception:
            single_final = "error"
    return rec, meta, single_final


def run_bench(ids=None, quick=False, use_screen=True, max_debate_rounds=2, verbose=True, compare_single=False):
    samples = select(ids, quick)
    try:
        eff = settings.effective()
        pool = build_pool(eff["committee"])
    except Exception as e:
        return {"error": f"档位/池不可用: {e}"}
    if len(pool) < 2:
        return {"error": "可用 Judge <2"}
    single_judge = None
    if compare_single:
        sp = build_pool(["glm:glm-4-plus"])
        single_judge = sp[0] if sp else pool[0]
    t0 = time.time()
    rows, totals = [], {"api": 0}
    per_cat = {}
    for s in samples:
        t = time.time()
        try:
            rec, meta, single_final = run_one(s["task"], s["output"], pool, use_screen,
                                              max_debate_rounds, single_judge)
            final = rec.get("final")
            caught = final in ("reject", "revise", "need_human")
            vf = bool(meta.get("verifier"))
            ok = caught if s["expected"] != "pass" else (final == "pass" and not vf)
            single_caught = single_final in ("reject", "revise", "need_human") if single_final else None
            rows.append({"id": s["id"], "cat": s["cat"], "expected": s["expected"], "final": final,
                         "caught": caught, "verifier": vf, "ok": ok,
                         "single_final": single_final, "single_caught": single_caught,
                         "debate": bool(rec.get("debate_rounds")),
                         "api": meta.get("api_calls", 0), "secs": round(time.time() - t, 1),
                         "tokens": (rec.get("tokens") or {}).get("total", 0),
                         "cost": rec.get("cost_yuan")})
            totals["api"] += meta.get("api_calls", 0)
        except Exception as e:
            rows.append({"id": s["id"], "cat": s["cat"], "error": str(e)[:120]})
    # 汇总
    cats = {}
    for r in rows:
        if "error" in r:
            continue
        c = cats.setdefault(r["cat"], {"n": 0, "caught": 0, "ok": 0, "verifier": 0})
        c["n"] += 1
        if r["expected"] != "pass":
            c["caught"] += 1 if r["caught"] else 0
        c["ok"] += 1 if r["ok"] else 0
        c["verifier"] += 1 if r["verifier"] else 0
    flawed = [r for r in rows if "error" not in r and r["expected"] != "pass"]
    ctrls = [r for r in rows if "error" not in r and r["expected"] == "pass"]
    nf, nc = len(flawed), len(ctrls)
    recall = (sum(1 for r in flawed if r["caught"]) / nf) if nf else None
    single_recall = None
    sf = [r for r in flawed if r.get("single_caught") is not None]
    if sf:
        single_recall = sum(1 for r in sf if r["single_caught"]) / len(sf)
        # 同口径：只统计单模型也审过的样本，便于对照
        rec_same = sum(1 for r in sf if r["caught"]) / len(sf) if sf else None
    else:
        rec_same = None
    false_alarm = (sum(1 for r in ctrls if r["caught"] or r["verifier"]) / nc) if nc else None
    tp = sum(1 for r in flawed if r["caught"])
    flagged = sum(1 for r in flawed if r["caught"]) + sum(1 for r in ctrls if r["caught"] or r["verifier"])
    precision = (tp / flagged) if flagged else None
    f1 = (2 * precision * recall / (precision + recall)) if (precision is not None and recall and precision + recall > 0) else None
    toks = sum(r.get("tokens", 0) for r in rows)
    cost = sum(r.get("cost", 0) or 0 for r in rows)
    report = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "set": "quick" if quick else ("ids" if ids else "v1-full"),
        "samples": len(rows), "flawed": nf, "controls": nc,
        "recall": round(recall, 3) if recall is not None else None,
        "false_alarm": round(false_alarm, 3) if false_alarm is not None else None,
        "precision": round(precision, 3) if precision is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
        "deterministic_caught": sum(1 for r in flawed if r.get("verifier")),
        "single_recall": round(single_recall, 3) if single_recall is not None else None,
        "mjc_recall_same": round(rec_same, 3) if rec_same is not None else None,
        "single_model": single_judge.name if single_judge else None,
        "debate_caught": sum(1 for r in flawed if r.get("caught") and r.get("debate")),
        "api_calls": totals["api"], "tokens": toks, "cost_yuan": round(cost, 5),
        "elapsed_s": round(time.time() - t0, 1),
        "config": {"tier": eff.get("tier"), "committee": eff["committee"],
                   "screen": use_screen, "max_debate_rounds": max_debate_rounds},
        "per_cat": {k: v for k, v in cats.items()},
        "rows": rows,
    }
    with open(HISTORY, "a", encoding="utf-8") as f:
        slim = {k: v for k, v in report.items() if k != "rows"}
        slim["rows"] = [{"id": r["id"], "final": r.get("final"), "ok": r.get("ok"),
                          "single_final": r.get("single_final"), "single_caught": r.get("single_caught"),
                          "verifier": r.get("verifier"), "debate": r.get("debate")} for r in rows]
        f.write(json.dumps(slim, ensure_ascii=False) + "\n")
    if verbose:
        _print(report)
    return report


def _print(r):
    print(f"== 基准 {r['set']} | 样本 {r['samples']}（缺陷 {r['flawed']} / 对照 {r['controls']}）==")
    print(f"Recall(缺陷拦截率): {r['recall']} | 对照误报率: {r['false_alarm']} | Precision: {r['precision']} | F1: {r['f1']}")
    if r.get("single_recall") is not None:
        print(f"对照: 单模型({r['single_model']})同集 Recall {r['single_recall']} vs MJC 委员会 {r['mjc_recall_same']}（放行率 {round(1 - r['single_recall'], 3)} vs {round(1 - r['mjc_recall_same'], 3)}）")
    print(f"其中确定性验证器拦截: {r['deterministic_caught']} 条 | API {r['api_calls']} · tokens {r['tokens']} · ≈¥{r['cost_yuan']}")
    for cat, c in (r.get("per_cat") or {}).items():
        print(f"  {cat}: 缺陷 {c['caught']}/{c['n']} 拦截 · 判定正确 {c['ok']}/{c['n']} · 验证器 {c['verifier']}")
    for row in r["rows"]:
        if "error" in row:
            print(f"  !! {row['id']} ERROR {row['error']}")
        elif not row.get("ok"):
            print(f"  ✗ {row['id']} 期望 {row['expected']} 实得 {row.get('final')}{' (verifier)' if row.get('verifier') else ''}")


if __name__ == "__main__":
    import sys
    run_bench(quick=True)
