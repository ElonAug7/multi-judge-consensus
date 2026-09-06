#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · pipeline.py — 审查流水线编排（Phase 3：成本优化三件套的组装层）
一次"审查调用"的统一入口 run_review_once()，按顺序应用：

  P3.1 初筛（screen）：最便宜 Judge 先看一遍
       verdict==pass 且 confidence>=screen_conf → 直接放行（screened 记录，零委员会调用）
       否则 → 升级委员会（3 Judge 并行 + 分歧辩论）
  P3.2 缓存（cache）：相同 task+output+池+模式 → 直接复用上次完整 record（0 API 调用）
  P3.3 信任降级（degrade）：trust.json 里连续一致 streak≥5 的 Judge 本轮跳过
       → 剩余 2 Judge 快速裁决：一致 pass/reject 直接终局；分歧(need_human/revise)再升级满委员会
  P3.4 供 webui 复用同一入口（保证 CLI 与 Web 行为一致）

返回 (record, meta)：
  record: 与 arbiter 产物同构（含 final/round1_decision/votes/rounds…；screen 快速通道为合成记录）
  meta:   {cache_hit, screened, screen_passed, screen_calls, committee_calls,
           api_calls, degraded(跳过者名或 None), escalated(降级分歧后是否升级)}
"""
import datetime
import json
import os
import time

from . import cache, trust
from .judge import build_pool, Judge

# P3.1 初筛候选：glm-4-flash（可用且便宜）。qwen3-turbo 是设计目标（更便宜），
# 但 2026-09-06 实测 qwen key 欠费/端点 403 不可用 → 不列入自动候选，
# 待 key 可用后经 env MJC_SCREEN_MODEL=qwen:qwen3-turbo 显式启用。
SCREEN_CANDIDATES = ("glm:glm-4-flash",)
DEFAULT_SCREEN_CONF = 0.75  # 初筛放行置信阈值（pass 且 conf≥此值才敢省掉委员会）
DEFAULT_CACHE = True


def resolve_screen_judge(override=None):
    """初筛 Judge：override 参数 > 后台设置(screen_enabled/screen_model) > env MJC_SCREEN_MODEL > 默认候选
    后台明确关闭初筛（screen_enabled=false）→ 直接 None；只有设置不可读或未配模型时才允许候选回退。"""
    if override:
        p = build_pool([override])
        return p[0] if p else None
    try:
        from mjc import settings
        eff = settings.effective()
        if not eff.get("screen_enabled", True):
            return None  # 后台明确关闭初筛：不偷偷回退默认候选
        spec = eff.get("screen_model") or os.environ.get("MJC_SCREEN_MODEL", "")
        if spec:
            p = build_pool([spec])
            return p[0] if p else None
        for cand in SCREEN_CANDIDATES:
            p = build_pool([cand])
            if p:
                return p[0]
        return None
    except Exception:
        # 设置层不可读：回退 env/默认候选（保持可用性）
        spec = os.environ.get("MJC_SCREEN_MODEL", "")
        if spec:
            p = build_pool([spec])
            return p[0] if p else None
        for cand in SCREEN_CANDIDATES:
            p = build_pool([cand])
            if p:
                return p[0]
        return None


def resolve_screen_conf(conf):
    """初筛放行置信阈值：显式参数 > 后台设置 > 默认 0.75"""
    if conf is not None:
        return conf
    try:
        from mjc import settings
        eff = settings.effective()
        return float(eff.get("screen_conf", DEFAULT_SCREEN_CONF))
    except Exception:
        return DEFAULT_SCREEN_CONF


def _key(task, output, pool_names, mode, extra=None):
    opts = {"mode": mode}
    if extra:
        opts.update(extra)
    return cache.cache_key(task, output, pool_names, opts)


def _write_log(record, log_path):
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _mk_screen_record(task, output, opinion, start, screen_conf):
    return {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "user_task": (task or "")[:500],
        "agent_output": (output or "")[:2000],
        "final": "pass",
        "round1_decision": "pass",
        "votes": {"pass": 1},
        "pass_votes": 1, "reject_votes": 0, "revise_votes": 0,
        "debate_rounds": 0,
        "elapsed_s": round(time.time() - start, 1),
        "screened": True,                       # P3.1 标记
        "screen_model": opinion.get("judge_display") or opinion.get("judge_id"),
        "screen_conf_used": screen_conf,
        "screen_opinion": opinion,
        "rounds": [{"round": 1, "kind": "screen", "opinions": [opinion]}],
    }


def _calls_of(record):
    """从 record 反推实际 Judge 调用次数（每轮 opinions 数之和）"""
    return sum(len(r.get("opinions") or []) for r in (record.get("rounds") or []))


def _committee(task, output, pool, mode, use_cache, log_path, debate_log_dir,
               key_extra=None, emit=None, max_debate_rounds=None):
    """委员会仲裁（带缓存）：返回 (record, cache_hit, calls)"""
    from .arbiter import ParallelArbiter
    pool_names = sorted(j.name for j in pool)
    key = _key(task, output, pool_names, mode, key_extra)
    if use_cache:
        hit = cache.get(key)
        if hit:
            return hit, True, 0
    arb = ParallelArbiter(pool, debate_log_dir=debate_log_dir, emit=emit,
                         max_debate_rounds=max_debate_rounds if max_debate_rounds is not None else 2)
    record = arb.review(task, output)  # 不在此传 log_path：统一由本层 _write_log 控制
    if use_cache:
        cache.put(key, record)
    return record, False, _calls_of(record)



def _accum_usage(rec, st):
    """累计统计：logs/usage.json（reviews/tokens/cost_yuan/非 pass 次数），尽力而为"""
    try:
        import json as _json
        from mjc import paths as _paths
        up = os.path.join(_paths.LOG_DIR, "usage.json")
        d = {}
        if os.path.exists(up):
            try:
                d = _json.load(open(up, encoding="utf-8"))
            except Exception:
                d = {}
        toks = (rec.get("tokens") or {}).get("total", 0)
        d["reviews"] = d.get("reviews", 0) + 1
        d["tokens"] = d.get("tokens", 0) + toks
        d["cost_yuan"] = round(d.get("cost_yuan", 0.0) + (rec.get("cost_yuan") or 0.0), 5)
        if rec.get("final") not in ("pass", None):
            d["nonpass"] = d.get("nonpass", 0) + 1
        with open(up, "w", encoding="utf-8") as f:
            _json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
def run_review_once(task, output, pool, screen_judge=None, screen_conf=None,
                    use_screen=True, use_cache=None, use_degrade=None,
                    trust_path=None, debate_log_dir=None, log_path=None,
                    memory=None, use_verifier=True, emit=None, max_debate_rounds=None):
    """
    一次审查（任务+产出+池固定）。详见模块 docstring。
    pool: [Judge,...]（≥2）；screen_judge: 初筛 Judge 或 None（关闭初筛）。
    screen_conf/use_cache/use_degrade 为 None 时读后台设置（settings.json）。
    memory: [{text,source,date}] 背景记忆（memctx 产物）→ 注入全体 Judge。
    """
    screen_conf = resolve_screen_conf(screen_conf)
    if use_cache is None or use_degrade is None:
        try:
            from mjc import settings
            eff = settings.effective()
            if use_cache is None:
                use_cache = bool(eff.get("cache", True))
            if use_degrade is None:
                use_degrade = bool(eff.get("degrade", False))
        except Exception:
            use_cache = DEFAULT_CACHE if use_cache is None else use_cache
            use_degrade = False if use_degrade is None else use_degrade
    stats = {"cache_hit": False, "committee_cached": False, "screened": False,
             "screen_passed": False, "screen_calls": 0, "committee_calls": 0,
             "api_calls": 0, "degraded": None, "escalated": False, "verifier": False}
    start = time.time()
    pool_names = sorted(j.name for j in pool)
    if not use_cache:
        stats["cache_off"] = True
    # 背景记忆透传（挂到 Judge 对象，arbiter 内部调用零改动）
    if memory:
        for j in list(pool) + ([screen_judge] if screen_judge else []):
            j.memory = memory
    _t0 = time.time()

    def _emit(ev):
        if emit:
            try:
                emit(ev)
            except Exception:
                pass

    def _ret(rec, st):
        # 用量与费用（估算）：从调用开始时间窗聚合，先算好再随 final 事件出去
        toks_total, cost = 0, None
        try:
            from mjc import providers as _P
            use = _P.usage_since(_t0)
            pt = sum(v["prompt"] for v in use.values())
            ct = sum(v["completion"] for v in use.values())
            cost = 0.0
            for spec, v in use.items():
                nm, _, mdl = spec.partition(":")
                ip, op = _P.price_of(nm, mdl or None)
                cost += v["prompt"] / 1000.0 * ip + v["completion"] / 1000.0 * op
            rec["tokens"] = {"prompt": pt, "completion": ct, "total": pt + ct}
            rec["cost_yuan"] = round(cost, 5)
            toks_total = pt + ct
            _accum_usage(rec, st)
        except Exception:
            pass
        _emit({"kind": "final", "verdict": rec.get("final"), "votes": rec.get("votes"),
               "debate_rounds": rec.get("debate_rounds", 0), "api_calls": st.get("api_calls", 0),
               "verifier": st.get("verifier", False),
               "tokens": toks_total, "cost_yuan": cost})
        return rec, st

    _emit({"kind": "start", "pool": [j.name for j in pool],
           "judges": [{"name": j.name, "display": j.display} for j in pool],
           "screen": bool(screen_judge and use_screen), "verifier": bool(use_verifier)})


    # ---- ⓪ 确定性验证器（零 LLM）：数值/日期/求和 100% 验算命中 → 直接 revise，跳过全部 LLM ----
    if use_verifier and os.environ.get("MJC_VERIFIER") != "0":
        try:
            from mjc import verifier
            vissues = verifier.verify(output)
        except Exception:
            vissues = []
        if vissues:
            import datetime as _dt
            rec = {
                "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                "user_task": (task or "")[:500],
                "agent_output": (output or "")[:2000],
                "final": "revise",
                "round1_decision": "revise",
                "votes": {"revise": 1}, "pass_votes": 0, "reject_votes": 0, "revise_votes": 1,
                "debate_rounds": 0, "elapsed_s": round(time.time() - start, 3),
                "rounds": [{"round": 1, "kind": "verifier", "opinions": [{
                    "judge_id": "verifier", "judge_display": "🔍 验证器",
                    "verdict": "revise", "confidence": 1.0,
                    "issues": [{k: v for k, v in i.items() if k != "certain"} for i in vissues],
                    "final_reasoning": f"确定性验算命中 {len(vissues)} 处（零 LLM 成本，零延迟）",
                }]}],
            }
            stats["verifier"] = True
            stats["api_calls"] = 0
            _emit({"kind": "verifier_hit", "n": len(vissues),
                   "issues": [{"type": i.get("type"), "desc": (i.get("description") or "")[:120]} for i in vissues[:3]]})
            _write_log(rec, log_path)
            return _ret(rec, stats)

    # ---- ① P3.1 初筛：pass+高置信 → 直接放行（零委员会调用）----
    if use_screen and screen_judge is not None and len(pool) >= 2:
        key = _key(task, output, pool_names, "screen",
                   {"screen": screen_judge.name, "conf": screen_conf})
        cached_op = None
        if use_cache:
            hit = cache.get(key)
            if hit is not None:
                cached_op = hit.get("screen_opinion")
                if hit.get("_screen_pass"):
                    # 整条流水线完全缓存：0 调用直接放行
                    rec = _mk_screen_record(task, output, cached_op, start, screen_conf)
                    stats["cache_hit"] = True
                    stats["screened"] = True
                    stats["screen_passed"] = True
                    return _ret(rec, stats)
                # 上次初筛未放行（结果已缓存）→ 免初筛调用，直接落入委员会
        if cached_op is not None:
            op = cached_op
            _emit({"kind": "opinion", "round": 0, "judge": screen_judge.name,
                   "display": screen_judge.display + "（初筛·缓存）", "verdict": op.get("verdict"),
                   "confidence": op.get("confidence"), "issues": [], "n_issues": 0})
        else:
            try:
                op = screen_judge.review(task, output)
                stats["screen_calls"] = 1
                stats["api_calls"] = 1
                _emit({"kind": "opinion", "round": 0, "judge": screen_judge.name,
                       "display": screen_judge.display + "（初筛）", "verdict": op.get("verdict"),
                       "confidence": op.get("confidence"), "issues": [], "n_issues": 0})
            except Exception as _e:
                # 初筛 Judge 故障（如空响应/超时）→ 跳过初筛直接走委员会，不阻塞审查
                op = {"verdict": "screen_error", "confidence": 0, "issues": [],
                      "final_reasoning": f"初筛 Judge 调用失败，本次跳过初筛: {str(_e)[:150]}"}
                if log_path:
                    try:
                        with open(log_path, "a", encoding="utf-8") as _f:
                            _f.write(json.dumps({"event": "screen_judge_error", "judge": screen_judge.name,
                                                 "err": str(_e)[:200]}, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
        if op.get("verdict") == "screen_error":
            stats["screened"] = False  # 故障不算初筛拦截，直接落入委员会
        else:
            stats["screened"] = True
        if op.get("verdict") == "pass" and float(op.get("confidence") or 0) >= screen_conf:
            stats["screen_passed"] = True
            rec = _mk_screen_record(task, output, op, start, screen_conf)
            if use_cache:
                cache.put(key, {"_screen_pass": True, "screen_opinion": op})
            _write_log(rec, log_path)
            return _ret(rec, stats)
        if use_cache:
            cache.put(key, {"_screen_pass": False, "screen_opinion": op})
        # 初筛未过 → 落入委员会（committee 结论单独缓存于 mode=full/deg2）

    # ---- ② P3.3 信任降级判定 ----
    skip = None
    if use_degrade and len(pool) >= 3:
        data = trust.load(trust_path)
        cands = [n for n in trust.eligible_degrade(data, path=trust_path) if n in pool_names]
        if cands:
            skip = max(cands, key=lambda n: data["judges"][n].get("streak", 0))
            stats["degraded"] = skip
            active = [j for j in pool if j.name != skip]
            rec, hit, calls = _committee(task, output, active, f"deg2:{skip}",
                                         use_cache, None, debate_log_dir, emit=emit,
                                         max_debate_rounds=max_debate_rounds)
            stats["committee_cached"] = hit
            stats["committee_calls"] += calls
            if rec.get("final") in ("pass", "reject"):
                stats["cache_hit"] = stats["committee_cached"] or (stats["screen_calls"] == 0 and stats["screened"])
                stats["api_calls"] = stats["screen_calls"] + stats["committee_calls"]
                _finish(rec, stats, trust_path, log_path)
                return _ret(rec, stats)
# 双 Judge 分歧/需改 → 升级满委员会（含被降级的 Judge）
            stats["escalated"] = True

    # ---- ③ 委员会（3 Judge 并行；分歧自动辩论 ≤2 轮）----
    rec, hit, calls = _committee(task, output, pool, "full",
                                 use_cache, None, debate_log_dir, emit=emit,
                                 max_debate_rounds=max_debate_rounds)
    stats["committee_cached"] = hit
    stats["committee_calls"] += calls
    stats["cache_hit"] = stats["committee_cached"] or (stats["screen_calls"] == 0 and stats["screened"])
    stats["api_calls"] = stats["screen_calls"] + stats["committee_calls"]
    _finish(rec, stats, trust_path, log_path)
    return _ret(rec, stats)


def _finish(record, stats, trust_path, log_path):
    """收尾：信任分更新（仅委员会共识记录；screen 快速通道不参与）+ 日志落盘"""
    if (trust_path and not stats.get("committee_cached")
            and (record.get("rounds") or []) and record["rounds"][0].get("kind") != "screen"):
        trust.update_with_record(record, path=trust_path)
    if not stats.get("committee_cached"):
        _write_log(record, log_path)
