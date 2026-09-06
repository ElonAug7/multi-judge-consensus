# Quickstart

Get from zero to your first committee review in under two minutes.

## 1. Prerequisites

- Python ≥ 3.9 (stdlib only — no `pip install` needed)
- One or more vendor API keys (two or more recommended; both DeepSeek and Zhipu GLM
  offer free tiers): [DeepSeek](https://platform.deepseek.com), [Zhipu GLM](https://open.bigmodel.cn)

## 2. Install & configure

```bash
git clone https://github.com/ElonAug7/multi-judge-consensus.git
cd multi-judge-consensus

python3 -m mjc.cli setup    # paste your keys (Enter to skip any vendor)
python3 -m mjc.cli doctor   # health check: keys / tier / committee / usage
```

Keys are stored locally in `keys.local.json` (chmod 600, gitignored) or can be set as
environment variables: `MJC_DEEPSEEK_KEY`, `MJC_GLM_KEY`, `MJC_<PROVIDER>_KEY`.

## 3. Your first review

```bash
python3 -m mjc.cli judge-only \
  --task "Summarize tomorrow's weather in Beijing" \
  --output "Beijing will have a heavy storm tomorrow; bring an umbrella"
```

Expected output: verdict (`PASS` / `REVISE` / `REJECT`), per-judge votes, debate rounds,
token usage and a cost estimate.

Try a broken claim to see the deterministic verifier in action (zero API cost):

```bash
python3 -m mjc.cli gate --stage deliver --task "demo" \
  --content "Submitted Aug 31, merged Sep 5, took 4 days total."
# exit code 2 = blocked by the verifier (should be 5 days)
```

## 4. Web admin console

```bash
python3 -m mjc.cli webui
# open http://127.0.0.1:8123
```

Three pages: **Review** (paste & review any text), **Live Tasks** (every review session
with judge-by-judge opinions streaming in real time), **Settings** (API keys, model
catalog, one-click tier presets — Economy / Standard / Strict).

## 5. Plug MJC into your agent

**MCP** (Claude Desktop/Code, Cursor, Windsurf, Cline…):

```bash
python3 -m mjc.mcp
# Claude Code:  claude mcp add mjc -- python3 -m mjc.mcp
```

**CLI gate in CI** — non-zero exit blocks the pipeline:

```yaml
- run: python3 -m mjc.cli gate --stage deliver --task "$CI_TASK" --content "$(cat artifact.md)"
```

**HTTP** (Dify/Coze/n8n custom tools): `POST /api/review`
`{"task": "...", "output": "..."}` → `{record: {final, issues, tokens…}, meta: {…}}`.

## 6. Cost cheat sheet

| Path | Typical calls | Est. cost |
|---|---|---|
| Deterministic verifier catch | 0 | ¥0 |
| Clean short text (screen passes) | 1 | ≈ ¥0.002 |
| Content with issues (full committee) | 3–9 | ≈ ¥0.01–0.05 |
| Identical re-review (cache hit) | 0 | ¥0 |

Tier presets (Settings page): Economy (2× budget models) · Standard (2 budget + 1 flagship)
· Strict (flagship only, no screen).

## 7. Run the red-team benchmark

```bash
python3 -m mjc.cli bench --set quick      # 9 samples, ≈ ¥0.2
python3 -m mjc.cli bench --set v1-full    # 21 samples, ≈ ¥0.6 (recall/precision/F1 + history)
python3 -m mjc.cli bench --set quick --ablation 0,1,2   # debate-rounds gain curve
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `可用模型不足 2 个` / "not enough judges" | run `python3 -m mjc.cli setup`; one key is enough, two+ recommended |
| Probe says 401 | key invalid/expired — re-paste |
| Probe says 403 | account hasn't purchased the model/tier (e.g. Qwen token-plan) |
| Probe says 404 | model ID not on that endpoint — check the vendor's docs |
| Port 8123 busy | `python3 -m mjc.cli webui --port 9000` |
| Slow reviews | a vendor API is flaky; MJC retries, falls back to sibling models, and degrades gracefully |

Full architecture and design notes: [README.md](README.md).
