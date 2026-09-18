#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · context.py — context assembler (P5: shift from "multi-model voting" to "single strong model + context")

Findings (codecase arms A-F, 2026-09-18, measured):
  - Multi-model committee / falsifier / arbitration is a *net negative* for semantic-logic
    defects: adds cost without recall (13 calls / 6/12).
  - The recall ceiling is set by *context*: hand-written spec 12/12, code-only reading 6/12,
    runtime output complements it.
  - Single model + correct context = 2x recall, 1/13 calls, 1/25 latency.

This module assembles *auto-collectable* context into a review prompt, feeds it to a *single
strong model*, and uses that as the primary code-review path:
  - Auto-collect: py_compile status (deterministic, zero cost) + runtime output (demo / tests, if runnable).
  - The prompt explicitly enumerates two defect classes: A static contradiction (comment/doc/
    constant vs implementation), B runtime behavior anomaly.

Safety note: assemble_code_context only runs py_compile on the given code (no side effects).
Runtime collection runs only when the code contains `if __name__ == "__main__"` or the caller
passes run_cmd explicitly, and it is time-limited.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# Default single strong model for code review (overridable via settings.consensus.code_review_model)
DEFAULT_REVIEW_MODEL = "glm:glm-4-plus"

# Sandboxed runtime collection limits
SANDBOX_MEM_KB = 512 * 1024   # 512 MB virtual-memory cap
SANDBOX_CPU_S = 8             # CPU-time cap (seconds)
SANDBOX_WALL_S = 12           # wall-clock timeout (seconds)

CODE_REVIEW_TASK = (
    "Review this code and find defects (bugs). Check BOTH of the following classes, both required:\n"
    "[Class A: static contradiction] comment / docstring / constant declaration vs implementation "
    "mismatch — e.g. comment says 'free shipping over 200' but the constant is 199, docstring says "
    "'tax on the discounted amount' but the code uses the pre-discount subtotal, comment says '5 items' "
    "but the constant is 10, a condition that is self-contradictory and always false, etc. "
    "Compare each documented rule against the implementation.\n"
    "[Class B: runtime behavior anomaly] given the runtime output below, find behavior bugs — e.g. "
    "refund exceeds amount paid, a paid order fails to ship, delivery date contradicts the promised "
    "lead time, sort direction opposite to the semantics, etc."
)


def _run(cmd, timeout=180, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return None, str(e)[:200]


def _status_of(rc, out):
    """Normalize a run result to a status label: ok / exit_N / timeout / oom / error."""
    if rc == 124:
        return "timeout"
    if rc is None:
        return "error"
    low = (out or "").lower()
    if "memoryerror" in low or "cannot allocate memory" in low or "killed" in low:
        return "oom"
    return "ok" if rc == 0 else "exit_%d" % rc


def run_sandboxed(script_path, cwd, wall_s=SANDBOX_WALL_S, mem_kb=SANDBOX_MEM_KB):
    """Run a script in a sandbox, capturing stdout+stderr. Returns (status, output).

    Prefers bwrap (bubblewrap: zero-config, no root) with network isolation (--unshare-net),
    PID isolation, a read-only root (--ro-bind / /), a writable temp dir, ulimit memory/CPU caps,
    and a wall-clock timeout. Falls back to `timeout` + direct run when bwrap is absent
    (no memory/network isolation, degraded but usable).
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
    """Assemble auto-collectable context. Returns {compile_status, compile_detail, runtime_status, runtime_output}."""
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
    # Deterministic check: py_compile (zero LLM cost, no side effects)
    rc, out = _run([sys.executable, "-m", "py_compile", src], timeout=timeout)
    ctx["compile_status"] = "pass" if rc == 0 else "fail"
    ctx["compile_detail"] = (out or "")[:2000]
    # Runtime collection: explicit run_cmd wins, else auto-run only if the code has __main__ (in sandbox)
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
    """Assemble the review prompt: task + code + auto context + output constraints."""
    parts = [task or CODE_REVIEW_TASK]
    parts.append("【Code】\n" + (code_text or "")[:60000])
    if ctx.get("compile_status") != "unknown":
        line = "【Compile status】" + ctx["compile_status"]
        if ctx.get("compile_detail"):
            line += "\n" + ctx["compile_detail"][:1500]
        parts.append(line)
    if ctx.get("runtime_status") not in ("skipped", "unknown") and ctx.get("runtime_output"):
        parts.append("【Runtime output】\n" + ctx["runtime_output"])
    parts.append("Output strict JSON (no markdown code fence): "
                 "{\"issues\":[{\"func\":\"function name\",\"class\":\"A|B\",\"desc\":\"description\",\"fix\":\"correct behavior\"}]}"
                 "Only list what you are confident about, ordered by severity; empty issues array if none.")
    return "\n\n".join(parts)


def review_code_single(code_text, task=None, model=None, run_cmd=None, timeout=300):
    """Single strong model + context assembly, the primary code-review path (replaces the committee).

    Returns {verdict, issues, model, calls, latency_s, context, raw}. Does not raise.
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
    """Extract valid issues from parsed JSON (drop empty desc)."""
    if not parsed:
        return []
    return [i for i in (parsed.get("issues") or [])
            if isinstance(i, dict) and (i.get("desc") or "").strip()]


# ---- divide-by-zero false-positive filter (lightweight patch): LLM often misreads ternary/zero guards ----
DIV_ZERO_HINTS = ("除零", "除 0", "除数为零", "除以零", "division by zero", "divide by zero",
                  "zerodivisionerror", "divisionbyzero", "除以 0")

_DIV = re.compile(r"[A-Za-z_]\w*\s*/\s*[A-Za-z_0-9(]")
_GUARD = re.compile(r"if\s+\w+\s+else\b|if\s+not\s+\w+\s*:|except\s+ZeroDivisionError|"
                    r"if\s+\w+\s*(?:==|<=|!=|<)\s*0|or\s+(?:ZERO|0)\b", re.I)


def _all_divisions_guarded(code_text):
    """Whether every division in the code is guarded (ternary / zero-check / try-except / or-fallback). No division -> False."""
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
    """Suppress false positives: mark divide-by-zero claims whose divisions are all guarded as `guarded`.
    Dropped by default (prefer miss over false-positive); the original report is kept for audit. Returns (kept, guarded)."""
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
    """One sample: run a chat once, return the issues list (failure -> [])."""
    from mjc import providers
    from mjc.judge import extract_json
    prov, _, mdl = (model or DEFAULT_REVIEW_MODEL).partition(":")
    try:
        raw = providers.chat(prov, [{"role": "user", "content": prompt}],
                             model=(mdl or None), temperature=temperature, max_tokens=6000, timeout=timeout)
    except Exception:
        return []
    return _extract_issues(extract_json(raw))


VERIFY_PROMPT = ("You are a code review verifier. Independently judge whether the following 'suspected defect' "
                 "actually exists in the code (verdict=yes means a real defect, no means a false positive).\n"
                 "Suspected defect (function/location): {func}\nDescription: {desc}\n"
                 "Read the code carefully. In particular: is there any guard (if/else, ternary, try/except) "
                 "that makes this issue actually not trigger?\n"
                 "verdict=yes: cite the specific code line proving the defect is real; verdict=no: it is a false "
                 "positive (explain why). When unsure, answer no (prefer miss over false-positive).\n"
                 "Output strict JSON: {{\"verdict\":\"yes|no\",\"evidence\":\"cited code line\",\"reason\":\"one sentence\"}}.\n"
                 "【Code】\n{code}")


def _verify_issue(code_text, issue, model=None, timeout=120):
    """Lightweight verification: judge whether a suspected defect is real. Returns bool (yes=True). Failure/exception -> False (prefer miss)."""
    from mjc import providers
    from mjc.judge import extract_json
    func = (issue.get("func") or "").strip()
    desc = (issue.get("desc") or "").strip()
    if not desc:
        return False
    # Deterministic pre-filter: function name absent from the code -> directly no (zero calls, blocks the clearest hallucinations)
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
    """Flatten single-model variance: N low-temperature samples -> dedupe candidates -> per-item verification -> keep verified only.

    Context is assembled once; discovery stage samples N times (temp 0.2), verification stage does per-item
    yes/no (temp 0). Returns {verdict, issues, model, calls, latency_s, context, samples, candidates, verified_n}. Does not raise.
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
    # 1) N discovery samples
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
    # 2) per-item verification (verify once per description) + dedupe by function (merge same-function opinions)
    verified = []
    seen_func = set()
    for cand in sorted(merged.values(), key=lambda c: -c["count"]):
        if _verify_issue(code_text, cand, model=model):
            cand["verified"] = True
            func = cand.get("func") or ""
            if func and func in seen_func:
                continue  # dedupe by function (avoid listing the same defect with different wording)
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
