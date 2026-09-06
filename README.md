# Multi-Judge Consensus (MJC)

**A cross-vendor model committee that reviews agent-generated content before it ships.**

MJC assembles multiple LLMs from independent vendors (DeepSeek, Zhipu GLM, Alibaba Qwen, and others) into a
review committee. Each judge produces a structured opinion; a purely rule-based arbiter votes, and divergent
opinions trigger a bounded cross-debate. The result is an architecture-level defense against hallucination:
one model's blind spot rarely overlaps with another's.

Zero third-party dependencies (Python stdlib only). Bring your own API keys. Runs fully locally.

---

## What it does

- **Structured multi-judge review** — verdicts (`pass` / `revise` / `reject` / `need_human`), confidence, and
  itemized issues (`factual_error`, `logical_error`, `hallucination`, `style`) in machine-readable JSON.
- **Rule-based arbitration** — no LLM decides the final verdict. Majority vote; ties or high-confidence
  minority objections trigger cross-debate (≤ 2 rounds, stops on consensus).
- **Deterministic verifier** — date-span, percentage-base, and explicit-sum errors are checked by code,
  not by an LLM: zero cost, zero latency, no false positives by design.
- **Cost-tiered routing** — screen (1 call) → cheap pair → flagship committee. Escalation is monotonic;
  identical content re-reviews are free (cache).
- **Administration console** — a local Web UI for API keys, model catalog, tier presets, trust scores,
  live review streams, and per-issue disposition records.
- **Adapter surface** — MCP server, subprocess CLI with gate exit codes, HTTP API, and optional OpenClaw
  integration.

## How it works

```
content
  ├─ ⓪ verifier      deterministic checks (dates / percentages / sums) — 0 LLM cost
  ├─ ① cache         identical content → previous verdict, 0 calls
  ├─ ② screen        cheap judge (glm-4-flash); pass with conf ≥ threshold → done
  ├─ ③ committee     3 models review independently and in parallel
  ├─ ④ arbiter       pure rule vote: ≥2/3 pass → pass; disagreement → debate
  ├─ ⑤ debate ≤2 rds each judge sees the others' opinions, may change verdict
  └─ ⑥ verdict       result + token usage + cost estimate, fully logged
```

**Tier presets** (one-click in the admin console):

| Tier | Committee | Strategy |
|---|---|---|
| Economy | 2× budget models | cheapest, screen-first |
| Standard | 2 budget + 1 flagship | default (benchmark-verified) |
| Strict | flagship only, no screen | maximum rigor |

## Measured results

Adversarial benchmark v1 — 21 samples (cross-document contradictions, temporal hallucinations, numerical
traps, instruction deviation, plus clean controls), run with real API calls, 2026-09-06:

| Pipeline | Defect pass-through | False kills on clean content |
|---|---|---|
| No review (shipped as-is) | 100% (18/18) | — |
| Single-model self-check (glm-4-plus) | 11.1% (2/18 missed) | — |
| **MJC full pipeline** | **0% (18/18 caught)** | **0/3** |

Full-set recall 1.0 · precision 1.0 · F1 1.0. Per-category: 5/5, 5/5, 5/5, 3/3.
Layering: 1 deterministic catch by the verifier (0 LLM cost), 17 by committee + debate.

Reproduce: `python3 -m mjc.cli bench --set v1-full` (≈ ¥0.6 in API spend).
History is appended to `logs/bench-history.jsonl` for regression tracking.

## Getting started

> Step-by-step walkthrough for first-time users (key setup, first review, web console,
> agent integrations, cost table, troubleshooting): **[QUICKSTART.md](QUICKSTART.md)**

```bash
git clone https://github.com/ElonAug7/multi-judge-consensus.git
cd multi-judge-consensus

python3 -m mjc.cli setup      # ① enter API keys interactively (skippable; stored locally, 0600)
python3 -m mjc.cli webui      # ② admin console at http://127.0.0.1:8123

# ③ review a piece of content
python3 -m mjc.cli judge-only \
  --task "Summarize tomorrow's weather in Beijing" \
  --output "Beijing will have a heavy storm tomorrow"
```

Requirements: Python ≥ 3.9. No `pip install` needed. Without any keys you can still run the verifier,
the UI, and the offline test suites; with one vendor key the committee shrinks automatically
(recommended: two or more vendors).

Keys are read from environment variables (`MJC_DEEPSEEK_KEY`, `MJC_GLM_KEY`, `MJC_<PROVIDER>_KEY`) or
`keys.local.json` (gitignored, chmod 600). See `settings.example.json` for the configuration template.

## CLI

| Command | Purpose |
|---|---|
| `setup` | interactive first-run key configuration + connectivity probes |
| `doctor` | health check: keys, tier, trust scores, cache, cumulative usage |
| `judge-only --task --output` | review given text with the committee |
| `review --task` | generate → review → rewrite loop (≤ 3 rejections) |
| `auto --task --content [--kind code]` | single-shot review with memory context, JSON output |
| `gate --stage design\|code\|deliver --task --content` | stage gate; exit codes pass=0 / revise=2 / reject=3 |
| `bench [--set quick\|v1-full] [--ablation 0,1,2]` | red-team benchmark with history |
| `dispose --in <json>` | record per-issue dispositions (adopted / rejected with reason) |
| `webui` | local admin console (127.0.0.1:8123) |
| `mcp` | MCP stdio server |

## Integrating with other agents

**MCP** (Claude Desktop/Code, Cursor, Windsurf, Cline, …):

```bash
python3 -m mjc.mcp
# Claude Code:  claude mcp add mjc -- python3 -m mjc.mcp
```

The `review(task, output)` tool returns `{verdict, votes, issues, api_calls, tokens, cost_yuan}`.

**Subprocess CLI** — one-line JSON plus exit-code gate semantics; usable from any language or CI.

**HTTP** — `POST /api/review` with optional token auth (Dify/Coze/n8n custom tools, cross-machine).

**OpenClaw** — optional native integration: transcript scanning, coding stage gates, live review stream.

## Architecture

```
mjc/
├── providers.py     vendor registry (deepseek/glm/qwen/dashscope/doubao/kimi; add keys to enable)
├── judge.py         single reviewer: structured JSON, memory injection, same-vendor fallback
├── arbiter.py       vote/debate arbitration; per-opinion live events
├── pipeline.py      verifier → screen → committee; fault-tolerant degradation
├── verifier.py      deterministic checks (dates/percentages/sums), zero false positives by design
├── bench.py         adversarial benchmark runner (recall/precision/F1 + history)
├── webui.py         admin console (review / live tasks / settings)
├── mcp_server.py    MCP stdio server
└── settings.py      runtime config (provider registry, model catalog, tier presets)
tests/               10 offline suites (0 API calls; key-dependent cases skip gracefully)
samples/bench-v1.json   adversarial benchmark corpus
```

## Development

```bash
python3 tests/test_phase3.py     # pipeline/cache/degradation
python3 tests/test_verifier.py   # deterministic verifier
python3 tests/test_mcp.py        # MCP protocol
# …10 suites total, all offline. GitHub Actions runs them on Python 3.9/3.11/3.12.
```

## Security & notes

- Keys live only in environment variables or a local `keys.local.json` (0600, gitignored).
- Runtime config, logs, and review records stay local and are never committed.
- Cost figures are estimates from per-vendor price tables (¥/1K tokens), marked as approximations;
  token counts come from API usage fields.
- The benchmark measures worst-case interception on a constructed corpus, not a natural production
  distribution.

## License

GPL-3.0. Local invocation only; keys are the user's own.
