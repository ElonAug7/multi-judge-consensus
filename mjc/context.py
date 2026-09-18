#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · context.py — 上下文装配器（P5：架构从「多模型投票」转向「单强模型 + 上下文」）

学习结论（codecase A–F 臂，2026-09-18，实测）：
  - 多模型委员会/证伪者/仲裁对语义逻辑缺陷是**负资产**：加成本不加召回（13 调用 / 6/12）。
  - 召回天花板由「上下文」决定：手写规格 12/12，纯读代码 6/12，运行输出与之**互补**。
  - 单模型 + 正确上下文 = 2× 召回、1/13 调用、1/25 延迟。

本模块把**可自动采集**的上下文装配成审阅提示，交给**单强模型**深挖，作为代码审查主路径：
  - 自动采集：py_compile 编译状态（确定性、零成本）+ 运行输出（demo / 测试，若可跑）。
  - 提示词显式枚举两类缺陷：A 静态矛盾（注释/doc/常量 vs 实现）、B 运行时行为异常。

安全说明：assemble_code_context 只对传入代码做 py_compile（无副作用）；
运行采集仅在「代码含 `if __name__ == "__main__"`」或调用方显式传 run_cmd 时执行，且限时。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# 默认代码审查单强模型（settings.consensus.code_review_model 可覆盖）
DEFAULT_REVIEW_MODEL = "glm:glm-4-plus"

# 沙箱运行采集限制
SANDBOX_MEM_KB = 512 * 1024   # 512 MB 虚拟内存上限
SANDBOX_CPU_S = 8             # CPU 时间上限（秒）
SANDBOX_WALL_S = 12           # 墙钟超时（秒）

CODE_REVIEW_TASK = (
    "审查这段代码，找出其中的缺陷（bug）。请**同时**排查两类问题，缺一不可：\n"
    "【类 A：静态矛盾】注释 / docstring / 常量声明 与 代码实现不一致——例如注释写「满 200 元免运费」但常量是 199、"
    "docstring 写「按折后金额计税」但实现用折前小计、注释写「满 5 件」但常量是 10、状态判断条件自相矛盾恒为假等。"
    "逐条比对文档口径与实现。\n"
    "【类 B：运行时行为异常】结合下方运行输出，找出暴露的行为错误——例如退款金额超过实付、"
    "已支付订单发货失败、送达日期与承诺时效不符、排序方向与语义相反等。"
)


def _run(cmd, timeout=180, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return None, str(e)[:200]


def _status_of(rc, out):
    """把运行结果归一为状态标签：ok / exit_N / timeout / oom / error。"""
    if rc == 124:
        return "timeout"
    if rc is None:
        return "error"
    low = (out or "").lower()
    if "memoryerror" in low or "cannot allocate memory" in low or "killed" in low:
        return "oom"
    return "ok" if rc == 0 else "exit_%d" % rc


def run_sandboxed(script_path, cwd, wall_s=SANDBOX_WALL_S, mem_kb=SANDBOX_MEM_KB):
    """在沙箱里跑脚本，捕获 stdout+stderr。返回 (status, output)。

    优先 bwrap（bubblewrap，零配置、无需 root）：网络隔离(--unshare-net)、PID 隔离、
    只读根文件系统(--ro-bind / /)、可写临时目录、ulimit 内存/CPU 上限、wall 超时。
    无 bwrap 时回退 timeout + 直接运行（无内存/网络隔离，降级可用）。
    """
    bwrap = shutil.which("bwrap")
    if bwrap:
        inner = ('ulimit -v "$1" 2>/dev/null; ulimit -t "$2" 2>/dev/null; '
                 'cd "$3" && exec python3 "$4"')
        cmd = ["timeout", str(wall_s), bwrap,
               "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
               "--bind", cwd, cwd, "--unshare-net", "--unshare-pid", "--die-with-parent",
               "bash", "-c", inner, "_", str(mem_kb), str(SANDBOX_CPU_S), cwd, script_path]
    else:
        cmd = ["timeout", str(wall_s), sys.executable, script_path]
    rc, out = _run(cmd, timeout=wall_s + 5, cwd=cwd)
    return _status_of(rc, out), out


def assemble_code_context(code_text, run_cmd=None, timeout=180):
    """装配可自动采集的上下文。返回 {compile_status, compile_detail, runtime_status, runtime_output}。"""
    ctx = {"compile_status": "unknown", "compile_detail": "",
           "runtime_status": "skipped", "runtime_output": ""}
    tmp = tempfile.mkdtemp(prefix="mjc_ctx_")
    src = os.path.join(tmp, "review_target.py")
    try:
        with open(src, "w", encoding="utf-8") as f:
            f.write(code_text)
    except Exception as e:
        ctx["compile_detail"] = str(e)[:200]
        return ctx
    # 确定性校验：py_compile（零 LLM 成本，无副作用）
    rc, out = _run([sys.executable, "-m", "py_compile", src], timeout=timeout)
    ctx["compile_status"] = "pass" if rc == 0 else "fail"
    ctx["compile_detail"] = (out or "")[:2000]
    # 运行采集：显式 run_cmd 优先，否则「含 __main__」才自动跑（沙箱内）
    if run_cmd:
        rc, out = _run(run_cmd, timeout=timeout, cwd=tmp)
        ctx["runtime_status"] = _status_of(rc, out)
    elif "__main__" in code_text:
        ctx["runtime_status"], out = run_sandboxed(src, tmp)
    else:
        ctx["runtime_status"], out = "skipped", ""
    ctx["runtime_output"] = (out or "")[:3000]
    return ctx


def build_code_review_prompt(task, code_text, ctx):
    """组装审阅提示：任务 + 代码 + 自动上下文 + 输出约束。"""
    parts = [task or CODE_REVIEW_TASK]
    parts.append("【代码】\n" + (code_text or "")[:60000])
    if ctx.get("compile_status") != "unknown":
        line = "【编译状态】" + ctx["compile_status"]
        if ctx.get("compile_detail"):
            line += "\n" + ctx["compile_detail"][:1500]
        parts.append(line)
    if ctx.get("runtime_status") not in ("skipped", "unknown") and ctx.get("runtime_output"):
        parts.append("【运行输出】\n" + ctx["runtime_output"])
    parts.append("输出严格 JSON（不要 markdown 代码块）："
                 "{\"issues\":[{\"func\":\"函数名\",\"class\":\"A|B\",\"desc\":\"缺陷描述\",\"fix\":\"正确行为\"}]}"
                 "只列有把握的，按严重程度排序；没有则 issues 为空数组。")
    return "\n\n".join(parts)


def review_code_single(code_text, task=None, model=None, run_cmd=None, timeout=300):
    """单强模型 + 上下文装配 的代码审查主路径（替代多模型委员会）。

    返回 {verdict, issues, model, calls, latency_s, context, raw}。不抛出。
    """
    from mjc import providers
    from mjc.judge import extract_json
    ctx = assemble_code_context(code_text, run_cmd=run_cmd)
    if model is None:
        try:
            from mjc import settings as _s
            model = (_s.load().get("consensus") or {}).get("code_review_model") or DEFAULT_REVIEW_MODEL
        except Exception:
            model = DEFAULT_REVIEW_MODEL
    prompt = build_code_review_prompt(task, code_text, ctx)
    prov, _, mdl = (model or DEFAULT_REVIEW_MODEL).partition(":")
    t0 = time.time()
    try:
        raw = providers.chat(prov, [{"role": "user", "content": prompt}],
                             model=(mdl or None), temperature=0.2, max_tokens=6000, timeout=timeout)
    except Exception as e:
        return {"verdict": "error", "issues": [], "model": model, "calls": 0,
                "latency_s": round(time.time() - t0, 2), "context": ctx, "error": str(e)[:200]}
    dt = time.time() - t0
    issues = _extract_issues(extract_json(raw))
    issues, guarded = _filter_guarded(issues, code_text)
    verdict = "revise" if issues else "pass"
    return {"verdict": verdict, "issues": issues, "model": model, "calls": 1,
            "latency_s": round(dt, 2),
            "context": {"compile": ctx["compile_status"], "runtime": ctx["runtime_status"]},
            "guarded": guarded, "raw": raw}


def _extract_issues(parsed):
    """从解析后的 JSON 里抽取有效 issues（过滤空 desc）。"""
    if not parsed:
        return []
    return [i for i in (parsed.get("issues") or [])
            if isinstance(i, dict) and (i.get("desc") or "").strip()]


# ---- 除零假阳性过滤（轻量补丁）：LLM 常读不懂三元/判零保护，误报除零 ----
DIV_ZERO_HINTS = ("除零", "除 0", "除数为零", "除以零", "division by zero", "divide by zero",
                  "zerodivisionerror", "divisionbyzero", "除以 0")

_DIV = re.compile(r"[A-Za-z_]\w*\s*/\s*[A-Za-z_0-9(]")
_GUARD = re.compile(r"if\s+\w+\s+else\b|if\s+not\s+\w+\s*:|except\s+ZeroDivisionError|"
                    r"if\s+\w+\s*(?:==|<=|!=|<)\s*0|or\s+(?:ZERO|0)\b", re.I)


def _all_divisions_guarded(code_text):
    """代码里的除法是否都带保护（三元/判零/try-except/或兜底）。无除法 → False。"""
    lines = code_text.splitlines()
    idxs = [i for i, l in enumerate(lines) if _DIV.search(l)]
    if not idxs:
        return False
    for i in idxs:
        window = "\n".join(lines[max(0, i - 1): i + 2])
        if not _GUARD.search(window):
            return False
    return True


def _filter_guarded(issues, code_text):
    """压假阳性：把「除零类」意见中、代码里除法均带保护的条目标记为 guarded。
    默认剔除（宁漏勿误报），保留原始报告供审计。返回 (kept, guarded)。"""
    kept, guarded = [], []
    for it in issues:
        desc = (it.get("desc") or "") + " " + (it.get("fix") or "")
        if any(h in desc.lower() for h in DIV_ZERO_HINTS) and _all_divisions_guarded(code_text):
            it["guarded"] = True
            guarded.append(it)
        else:
            kept.append(it)
    return kept, guarded


def _sample_issues(prompt, model, timeout=300, temperature=0.2):
    """单次采样：跑一次 chat，返回 issues 列表（失败→[]）。"""
    from mjc import providers
    from mjc.judge import extract_json
    prov, _, mdl = (model or DEFAULT_REVIEW_MODEL).partition(":")
    try:
        raw = providers.chat(prov, [{"role": "user", "content": prompt}],
                             model=(mdl or None), temperature=temperature, max_tokens=6000, timeout=timeout)
    except Exception:
        return []
    return _extract_issues(extract_json(raw))


VERIFY_PROMPT = ("你是代码审查复核员，独立判断下面这条「疑似缺陷」是否**真实存在**于代码中（verdict=yes 表示真缺陷，no 表示误报）。\n"
                 "疑似缺陷（函数/位置）：{func}\n问题描述：{desc}\n"
                 "请逐字核对代码，特别注意：是否有防御代码（if/else、三元保护、try/except）使该问题实际不会触发？\n"
                 "verdict=yes：能引用**具体代码行**证明缺陷真实存在；verdict=no：是误报（说明原因）。拿不准填 no（宁漏勿误报）。\n"
                 "输出严格 JSON：{{\"verdict\":\"yes|no\",\"evidence\":\"引用的代码行\",\"reason\":\"一句话\"}}。\n"
                 "【代码】\n{code}")


def _verify_issue(code_text, issue, model=None, timeout=120):
    """轻量验证：判断某条疑似缺陷是否真实存在。返回 bool（yes=True）。失败/异常→False（宁漏勿误报）。"""
    from mjc import providers
    from mjc.judge import extract_json
    func = (issue.get("func") or "").strip()
    desc = (issue.get("desc") or "").strip()
    if not desc:
        return False
    # 确定性预筛：函数名在代码里根本不存在 → 直接否（零调用，拦截最明显的幻觉）
    if func and func not in code_text:
        return False
    model = model or DEFAULT_REVIEW_MODEL
    prov, _, mdl = model.partition(":")
    prompt = VERIFY_PROMPT.format(func=func or "?", desc=desc[:300], code=(code_text or "")[:60000])
    try:
        raw = providers.chat(prov, [{"role": "user", "content": prompt}],
                             model=(mdl or None), temperature=0.0, max_tokens=600, timeout=timeout)
    except Exception:
        return False
    parsed = extract_json(raw) or {}
    return str(parsed.get("verdict", "no")).strip().lower() == "yes"


def review_code_stable(code_text, task=None, model=None, run_cmd=None, n_samples=3, timeout=300):
    """压平单模型方差：N 次低温采样 → 去重合并候选 → 逐条独立验证 → 只留验证通过。

    上下文只装配一次；发现阶段 N 次采样（低温 0.2），验证阶段逐条 yes/no（温度 0）。
    返回 {verdict, issues, model, calls, latency_s, context, samples, candidates, verified_n}。不抛出。
    """
    n = max(1, int(n_samples or 3))
    t0 = time.time()
    ctx = assemble_code_context(code_text, run_cmd=run_cmd)
    if model is None:
        try:
            from mjc import settings as _s
            model = (_s.load().get("consensus") or {}).get("code_review_model") or DEFAULT_REVIEW_MODEL
        except Exception:
            model = DEFAULT_REVIEW_MODEL
    prompt = build_code_review_prompt(task, code_text, ctx)
    # 1) N 次发现
    merged = {}
    for _ in range(n):
        for it in _sample_issues(prompt, model, timeout=timeout, temperature=0.2):
            func = (it.get("func") or "").strip()
            desc = (it.get("desc") or "").strip()
            if not desc:
                continue
            key = (func, desc[:80])
            if key not in merged:
                merged[key] = {"func": func, "class": it.get("class"), "desc": desc,
                               "fix": (it.get("fix") or "").strip(), "count": 0}
            merged[key]["count"] += 1
    # 2) 逐条验证（同描述只验一次）+ 同函数去重（同一函数的多个意见合并为一个）
    verified = []
    seen_func = set()
    for cand in sorted(merged.values(), key=lambda c: -c["count"]):
        if _verify_issue(code_text, cand, model=model):
            cand["verified"] = True
            func = cand.get("func") or ""
            if func and func in seen_func:
                continue  # 同函数去重（避免同一缺陷多种措辞重复列出）
            if func:
                seen_func.add(func)
            verified.append(cand)
        else:
            cand["verified"] = False
    calls = n + len(merged)
    verified, guarded = _filter_guarded(verified, code_text)
    verdict = "revise" if verified else "pass"
    return {"verdict": verdict, "issues": verified, "model": model, "calls": calls,
            "latency_s": round(time.time() - t0, 2),
            "context": {"compile": ctx["compile_status"], "runtime": ctx["runtime_status"]},
            "samples": n, "candidates": len(merged), "verified_n": len(verified),
            "guarded": guarded}
