# Code-review case study

A reproducible benchmark for MJC's **code-review** path: a ~500-line Python module with **12 defects
injected on purpose** inside MJC's declared scope (docstring-vs-implementation mismatches, numeric/date
errors, dead or unreachable branches), plus **4 correct-but-suspicious decoys** to measure false positives.
Compile/runtime errors are excluded by design — those belong to the test suite.

Three arms are run on the identical input: the deterministic verifier (no API), a single-model self-check,
and the full MJC pipeline. Scoring is done by hand against the sealed ground truth, because keyword matching
would count "mentions the function" as "found the defect".

## Reproduce

```bash
cd benchmarks/codecase
wc -l order_engine.py          # 518 lines / 14,044 chars, compiles clean
cat GROUND_TRUTH.json          # sealed defect list + scoring rules
python3 run_codecase.py --arms a,b,c   # ~8 minutes; writes case_out/
```

Results land in `case_out/` (`A.json`, `B.json`, `C.json`, `SCORE.json`).
See `REPORT.zh.md` for the full write-up, including where MJC is weak.

## Headline (one run, 2026-09-13)

| Arm | API calls | Latency | Cost | Defects found | False positives | Output usable |
|---|---|---|---|---|---|---|
| Deterministic verifier | 0 | 0.0s | 0 | 0/12 | 0 | yes |
| Single-model self-check | 1 | 88s | ~0.21 | 4/12 | n/a | no |
| **MJC full pipeline** | **7** | **346s** | **0.29192** | **6/12** | **0** | **yes** |

Sample size is one module with constructed defects — treat the numbers as directional, not as an accuracy
guarantee.
