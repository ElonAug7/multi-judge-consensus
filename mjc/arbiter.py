#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · arbiter.py — 仲裁器：收集意见 → 多数投票 → 分歧触发辩论 → 最终裁决
规则：
  - ≥2/3 pass      → 通过（进入下一阶段）
  - ≥2/3 reject    → 拒绝（打回 Agent 重写）
  - 1:1:1 或高置信少数派反对 → 触发第二轮辩论（交叉审查）
  - 辩论后仍无共识 → 标记 need_human（需人工审查）
  - 最大辩论 2 轮；单次审查任务最大打回 3 次（由 cli 层控制）
"""
import concurrent.futures
import json
import os
import time
import datetime


# P1 异议保护（dissent guard，v0.10 无幻觉引擎）：
#   这些 issue 类型属"阻塞性异议"——即便多数票 pass，也不得直接放行。
#   修因：csqa-07 实测 votes={pass:2,revise:1} → pass，其中 glm-flash 标了 factual_error
#   却投 pass，glm-plus 标 factual_error 投 revise——纯票型裁决把异议票压掉了。
BLOCKING_ISSUE_TYPES = ("factual_error", "hallucination", "logical_error")


def dissent_config(data=None):
    """读取 settings.consensus.dissent_guard（**默认启用**）。
    返回 {enabled, types, min_conf}；读不到配置时走内置默认（宁严勿漏）。"""
    try:
        from mjc import settings as _settings
        cfg = ((_settings.load(data).get("consensus") or {}).get("dissent_guard") or {})
    except Exception:
        cfg = {}
    enabled = cfg.get("enabled", True)
    types = cfg.get("blocking_types") or list(BLOCKING_ISSUE_TYPES)
    try:
        min_conf = float(cfg.get("min_conf", 0.0))
    except (TypeError, ValueError):
        min_conf = 0.0
    return {"enabled": bool(enabled), "types": tuple(types), "min_conf": min_conf}


def detect_blocking_dissent(opinions, guard):
    """扫描各裁定：命中阻塞性 issue 类型（事实/幻觉/逻辑错）→ 返回异议摘要列表。
    只看 issue 类型，**不看该裁判自己的 verdict**（自相矛盾也要拦）。"""
    if not (guard and guard.get("enabled")):
        return []
    types = set(guard.get("types") or BLOCKING_ISSUE_TYPES)
    min_conf = float(guard.get("min_conf") or 0.0)
    hits = []
    for o in opinions or []:
        if not isinstance(o, dict):
            continue
        try:
            conf = float(o.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        bad = sorted({i.get("type") for i in (o.get("issues") or [])
                      if isinstance(i, dict) and i.get("type") in types})
        if bad:
            hits.append({"judge": o.get("judge_id"), "verdict": o.get("verdict"),
                         "confidence": o.get("confidence"), "types": bad})
    return hits


def append_debate_log(record, log_dir):
    """辩论发生时（debate_rounds>0）把完整记录（含每轮意见）追加到 log_dir/debate-*.jsonl"""
    if not (log_dir and record.get("debate_rounds")):
        return
    try:
        os.makedirs(log_dir, exist_ok=True)
        fn = os.path.join(log_dir, f"debate-{int(time.time())}.jsonl")
        with open(fn, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _opinion_event(j, op, round_no):
    """单票实时事件（轻量：只带前 2 条 issue 摘要，供动画）"""
    its = []
    for i in (op.get("issues") or [])[:2]:
        its.append({"type": i.get("type"), "desc": (i.get("description") or "")[:110]})
    return {"kind": "opinion", "round": round_no, "judge": j.name, "display": j.display,
            "verdict": op.get("verdict", "error"), "confidence": op.get("confidence"),
            "issues": its, "n_issues": len(op.get("issues") or [])}


def parallel_review(pool, user_task, agent_output, others=None, timeout=120, emit=None, round_no=1):
    """并发跑所有 Judge 的 review（保持 pool 顺序返回；调用失败降级为 error 意见不中断他人）
    emit: 每票落定立即回调（实时事件流），不阻塞收集。"""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(pool)) as ex:
        futs = {ex.submit(j.review, user_task, agent_output, others): j for j in pool}
        for fut in concurrent.futures.as_completed(futs):
            j = futs[fut]
            try:
                op = fut.result()
            except Exception as e:
                op = {"verdict": "error", "confidence": 0, "issues": [], "final_reasoning": f"调用失败: {e}", "judge_id": j.name, "judge_display": j.display}
            results[j.name] = op
            if emit:
                try:
                    emit(_opinion_event(j, op, round_no))
                except Exception:
                    pass
    return [results[j.name] for j in pool]


class Arbiter:
    def __init__(self, pool, max_debate_rounds=2, min_pass=2, debate_log_dir=None, emit=None):
        self.emit = emit
        self.pool = pool            # [Judge, ...]
        self.max_debate_rounds = max_debate_rounds
        self.min_pass = min_pass    # 通过所需票数
        self.debate_log_dir = debate_log_dir  # 非空 → 辩论记录自动落盘 logs/debate-*.jsonl（P2.3）
        self.dissent_guard = dissent_config()  # P1 异议保护配置
        self.log = []               # 完整辩论记录（仲裁日志）

    @staticmethod
    def decide(verdicts, min_pass=2, opinions=None, guard=None):
        # error 票：Judge 调用失败/解析失败时的降级产物（verdict='error'，见 parallel_review 异常分支），
        # 不参与任何票型计数——池退化由多数票兜底；全 error 时 final=need_human（无票可达 min_pass）。
        """票型裁决（单轮基线 & 辩论后最终裁决共用同一规则）。
        P1 异议保护：若任一裁定标了 factual_error/hallucination/logical_error，
        则多数 pass **不得直接放行** → 降级为 revise（交给修复链 + 证据核验）。"""
        from collections import Counter
        c = Counter(verdicts)
        n = len(verdicts)
        pass_votes = c.get("pass", 0)
        reject_votes = c.get("reject", 0)
        revise_votes = c.get("revise", 0)
        if pass_votes >= min_pass:
            if detect_blocking_dissent(opinions, guard):
                return "revise"
            return "pass"
        if reject_votes >= min_pass:
            return "reject"
        if n - c.get("error", 0) >= 2 and (revise_votes >= 2 or (revise_votes == 1 and pass_votes >= 1)):
            return "revise"
        return "need_human"

    # ---------- 工具 ----------
    def _count(self, verdicts):
        from collections import Counter
        return Counter(verdicts)

    def _needs_debate(self, verdicts, opinions):
        """分歧判定：不一致 或 高置信少数派反对"""
        c = self._count(verdicts)
        n = len(verdicts)
        if n < 3:
            return False
        # 全票一致无需辩论
        if len(c) == 1:
            return False
        # 2:1 且少数派置信度高（≥0.7）→ 辩论（少数派可能发现了真问题）
        for v, cnt in c.items():
            if cnt == 1:
                minority = [o for o in opinions if o.get("verdict") == v]
                if minority and minority[0].get("confidence", 0) >= 0.7:
                    return True
        # 1:1:1 全分散 → 辩论
        if len(c) == 3:
            return True
        return False

    # ---------- 主流程 ----------
    def review(self, user_task, agent_output, log_path=None):
        """执行完整审查流程（含辩论），返回最终裁决 + 全程日志。"""
        start = time.time()
        round_opinions = {}
        rounds = []

        # 第 1 轮：独立审查（并行调用在 cli 层做并发，这里串行也支持）
        r1 = []
        for _rn, j in enumerate(self.pool, 1):
            try:
                op = j.review(user_task, agent_output)
            except Exception as e:
                op = {"verdict": "error", "confidence": 0, "issues": [], "final_reasoning": f"调用失败: {e}", "judge_id": j.name, "judge_display": j.display}
            r1.append(op)
            if self.emit:
                try:
                    self.emit(_opinion_event(j, op, 1))
                except Exception:
                    pass
        verdicts = [o.get("verdict", "error") for o in r1]
        round1_decision = self.decide(verdicts, self.min_pass, r1, self.dissent_guard)  # 单轮基线（P2.5 对比用）
        round_opinions[1] = r1
        rounds.append({"round": 1, "kind": "independent", "opinions": r1})

        debate_round = 0
        final = None
        while self._needs_debate(verdicts, r1) and debate_round < self.max_debate_rounds:
            debate_round += 1
            # 第 2 轮：交叉辩论——每个 Judge 看其他所有人的意见
            others = {}
            for i, j in enumerate(self.pool):
                others[j.name] = [o for k, o in enumerate(r1) if k != i]
            r2 = []
            for _rn, j in enumerate(self.pool, 1):
                try:
                    op = j.review(user_task, agent_output, other_opinions=others.get(j.name, []))
                except Exception as e:
                    op = {"verdict": "error", "confidence": 0, "issues": [], "final_reasoning": f"调用失败: {e}", "judge_id": j.name, "judge_display": j.display}
                r2.append(op)
                if self.emit:
                    try:
                        self.emit(_opinion_event(j, op, debate_round + 1))
                    except Exception:
                        pass
            verdicts = [o.get("verdict", "error") for o in r2]
            r1 = r2  # 下一轮辩论基于最新意见
            rounds.append({"round": debate_round + 1, "kind": "debate", "opinions": r2})

        # 最终投票（与单轮基线同一套 decide 规则）
        c = self._count(verdicts)
        n = len(verdicts)
        pass_votes = c.get("pass", 0)
        reject_votes = c.get("reject", 0)
        revise_votes = c.get("revise", 0)
        dissent_hits = detect_blocking_dissent(r1, self.dissent_guard)
        final = self.decide(verdicts, self.min_pass, r1, self.dissent_guard)

        elapsed = time.time() - start
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "user_task": user_task[:500],
            "agent_output": agent_output[:2000],
            "final": final,
            "round1_decision": round1_decision,  # 无辩论时的等价裁决（P2.5 单轮基线）
            "votes": dict(c),
            "pass_votes": pass_votes,
            "reject_votes": reject_votes,
            "revise_votes": revise_votes,
            "debate_rounds": debate_round,
            "elapsed_s": round(elapsed, 1),
            "dissent": {"enabled": bool(self.dissent_guard.get("enabled")),
                        "triggered": bool(dissent_hits), "hits": dissent_hits},
            "rounds": rounds,
        }
        self.log.append(record)
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception:
                pass
        append_debate_log(record, self.debate_log_dir)  # P2.3：辩论记录自动落盘
        return record

    def summary(self, record):
        """人类可读摘要"""
        lines = [
            f"最终裁决: {record['final'].upper()}",
            f"票型: pass={record['pass_votes']} reject={record['reject_votes']} revise={record['revise_votes']} | 辩论轮数: {record['debate_rounds']} | 耗时: {record['elapsed_s']}s",
        ]
        # 列出各 Judge 意见
        for rnd in record["rounds"]:
            lines.append(f"—— 第{rnd['round']}轮({rnd['kind']}) ——")
            for o in rnd["opinions"]:
                issues = o.get("issues") or []
                issue_summary = "; ".join(
                    f"[{i.get('type')}] {i.get('description', '')[:60]}" for i in issues[:3]
                )
                lines.append(
                    f"  {o.get('judge_display', o.get('judge_id'))}: {o.get('verdict')} "
                    f"(conf={o.get('confidence')}) {issue_summary}"
                )
        return "\n".join(lines)


class ParallelArbiter(Arbiter):
    """并发版仲裁：第 1/2 轮各 Judge 并行调用（自 cli 移入 arbiter，消除循环导入）
    与串行 Arbiter 同一套 decide/_needs_debate/落盘规则，仅调用方式为并发。"""

    def review(self, user_task, agent_output, log_path=None):
        start = time.time()
        rounds = []

        r1 = parallel_review(self.pool, user_task, agent_output, emit=getattr(self, 'emit', None), round_no=1)
        verdicts = [o.get("verdict", "error") for o in r1]
        round1_decision = self.decide(verdicts, self.min_pass, r1, self.dissent_guard)  # 单轮基线（P2.5 对比用）
        rounds.append({"round": 1, "kind": "independent", "opinions": r1})

        debate_round = 0
        while self._needs_debate(verdicts, r1) and debate_round < self.max_debate_rounds:
            debate_round += 1
            others = {}
            for i, j in enumerate(self.pool):
                others[j.name] = [o for k, o in enumerate(r1) if k != i]
            r2 = parallel_review(self.pool, user_task, agent_output, others=others,
                                    emit=getattr(self, 'emit', None), round_no=debate_round + 1)
            verdicts = [o.get("verdict", "error") for o in r2]
            r1 = r2
            rounds.append({"round": debate_round + 1, "kind": "debate", "opinions": r2})

        from collections import Counter
        c = Counter(verdicts)
        dissent_hits = detect_blocking_dissent(r1, self.dissent_guard)
        final = self.decide(verdicts, self.min_pass, r1, self.dissent_guard)
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "user_task": user_task[:500],
            "agent_output": agent_output[:2000],
            "final": final,
            "round1_decision": round1_decision,
            "votes": dict(c),
            "pass_votes": c.get("pass", 0),
            "reject_votes": c.get("reject", 0),
            "revise_votes": c.get("revise", 0),
            "debate_rounds": debate_round,
            "elapsed_s": round(time.time() - start, 1),
            "dissent": {"enabled": bool(self.dissent_guard.get("enabled")),
                        "triggered": bool(dissent_hits), "hits": dissent_hits},
            "rounds": rounds,
        }
        self.log.append(record)
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception:
                pass
        append_debate_log(record, self.debate_log_dir)  # P2.3
        return record
