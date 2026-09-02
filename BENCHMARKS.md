# Benchmarks (measured)

Dates: 2 September 2026. Hardware: Apple Silicon, mlx-serve.
Default checkpoint now: `mlx-community/Qwen3.5-9B-MLX-4bit` (~6 GB). The 35B numbers below are the same-day predecessor, not the product default.
Command: `local-agent benchmark` / `local-agent benchmark sessions`.

These numbers were produced by a run of `local_agent/benchmark.py`, `local_agent/replay.py` and `tests/test_router.py`. They are not invented.
`interception_rate` compares local-agent output to the harness baseline (raw file or `rg`), **not** to billed Claude tokens.

Previous PROVE IT run (same day, before DIRECT/REDUCE): tiny repo 6.9 s / partial; log missed `InvoiceService`. Extract-only REDUCE and DIRECT slim landed later the same day.

## Routing Benchmarks

| Task | Expected tier | Actual tier | Correct | Latency |
| ---- | ------------- | ----------- | ------- | ------- |
| Tiny 297 B fixture, `invoice.read` | DIRECT | DIRECT | yes | 0.03 s (9B, slim) / 0.08 s (first 35B) |
| Explicit symbol `Store` (unit) | DIRECT | DIRECT | yes | n/a (unit) |
| 2.2 MB log, root cause | REDUCE | REDUCE | yes | 0.04 s extract-only / 4.1 s with 35B LLM |
| Large-repo `review this diff` (unit) | REDUCE | REDUCE | yes | n/a (unit) |
| Why expired contracts remain visible (unit) | AGENT | AGENT | yes | n/a (unit) |
| Screenshot + `repo://compare.py` (unit) | AGENT | AGENT | yes | n/a (unit) |
| Two images only (unit) | DIRECT | DIRECT | yes | n/a (unit) |
| Change the auth middleware (unit) | CLAUDE | CLAUDE | yes | n/a (unit) |

Harness routing accuracy on scored cases A+B: **2/2**. Unit routing cases: **8/8**. Combined routing accuracy on this set: **100%**.

`avoidable_local_llm_calls` (DIRECT that still called a local LLM): **0** on the tiny-repo harness and on the DIRECT unit tests.

## Results (context)

| Scenario | Raw context | Claude-visible | Direct avoided | Interception | Correct | Latency |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| A Repo, tier DIRECT (297 B fixture, slim) | 74 tok | 235 tok | 0 | 0% | yes (`require_permission`, `invoice.read`) | 0.03 s |
| A same, first 35B packet (pre-slim) | 74 tok | 383 tok | 0 | 0% | yes | 0.08 s |
| A same, baseline B `rg` | 74 tok | 51 tok | 23 | 31% | n/a (deterministic) | 0.014 s |
| B 2.2 MB log, REDUCE extract-only (9B resident, 0 LLM) | 560 953 tok | 866 tok | 560 087 | 99.8% | yes (`InvoiceService`, `null`) | 0.04 s |
| B same, REDUCE + 35B LLM (before extract-skip) | 560 953 tok | 1 006 tok | 559 947 | 99.8% | yes (`InvoiceService`, `null`) | 4.1 s |
| B same, baseline B `rg ERROR` | 560 953 tok | 1 319 tok | 559 634 | 99.8% | n/a | 0.005 s |
| C Two recette screenshots + repo, tier AGENT | 9 563 tok | 2 075 tok | 7 488 | 78.3% | yes (`DIV`, `HCP`) | 13.6 s |
| C same pair, pixel+OCR compare, no LLM | 9 563 tok | 1 400 tok | 8 163 | 85% | partial (`pixel`, not `DIV`) | 0.72 s |
| E Whitelisted failing check | 20 000 tok (assumed dump) | 86 tok | 19 914 | 99.6% | yes (`TypeError`, `total`) | 0.017 s |

Notes:

- Case A packet is still larger than the 297 B source (235 tok vs 74). The win is **not** compression: it is skipping the local LLM (6.9 s → 0.03 s) and keeping both identifiers.
- Case B no longer calls a local LLM when high-signal excerpts exist (0.04 s extract vs 4–5 s LLM). The planted cause stays in runtime evidence. `rg ERROR` remains ~800× faster. A 35B/9B sentence on that digest is optional, not the default.
- Case C with `image://` + `repo://` is routed AGENT (multi-source). Deterministic compare without a repo pointer is faster and smaller. AGENT got `DIV`/`HCP` where compare-only had `pixel` only.
- Case E baseline A (80 kB) is still an assumed PHPUnit-style dump.

## Quality evaluation (keyword recall, scale 0–4)

0 = wrong, 2 = partial, 4 = correct + the expected strings.

| Case | Expected | Hits | Recall | Score |
| --- | --- | --- | ---: | ---: |
| A DIRECT | `require_permission`, `invoice.read` | both | 100% | 4 |
| B REDUCE | `InvoiceService`, `null` | both | 100% | 4 |
| C AGENT | `DIV`, `HCP` | both | 100% | 4 |
| C compare no LLM | `DIV`, `pixel` | `pixel` | 50% | 2 |
| E `run_check` | `TypeError`, `total` | both | 100% | 4 |

Root cause on the log: **correct** (strings present in the packet). The 35B summary on REDUCE can still narrate the wrong worker-id story; the packet is not allowed to drop LOG-E excerpts. Do not treat the prose summary as the source of truth.

## Latency split

| Case | Routing | Deterministic | Local LLM | Total |
| --- | ---: | ---: | ---: | ---: |
| A DIRECT (slim, 9B resident unused) | 0.8 ms | ~20 ms (`rg`) | 0 | 0.03 s |
| B REDUCE extract-only | 0.1 ms | ~40 ms extract | 0 | 0.04 s |
| B REDUCE + 35B LLM (before skip) | 0.1 ms | 38 ms extract | 4.03 s | 4.1 s |
| C AGENT | n/a in row | tools inside 13.6 s | included | 13.6 s |
| C compare no LLM | n/a | 0.72 s | 0 | 0.72 s |
| E check | n/a | 17 ms | 0 | 0.017 s |

TTFT is not exposed by the mlx-serve HTTP complete call. `local_llm_s` is wall time of `complete` / `complete_chat`.

## Model Comparison

Same harness. 9B-only run after unloading the 35B: 2 September 2026, `mlx-community/Qwen3.5-9B-MLX-4bit` resident 5.95 GB, 35B `unloaded`.

| Model | A DIRECT | B REDUCE quality | B REDUCE LLM | Ping | First tool | RAM |
| ----- | -------- | ---------------- | -----------: | ---: | ---------- | --: |
| `Qwen3.6-35B-A3B-4bit` | 4/4, 0 LLM | keywords 4/4; prose invented a worker-id story | 4.03 s | n/a | 7 s, no tool (prior) | 20.4 GB |
| `Qwen3.5-9B-MLX-4bit` (alone) | 4/4, 0 LLM | keywords 4/4; prose named `InvoiceService.getTotal` / null | 4.90 s | 0.18 s | 1.88 s, `search_repo` | 5.95 GB |

With both models loaded, 9B ping was 0.44 s and REDUCE 5.13 s. Alone, ping drops to 0.18 s; REDUCE stays ~5 s (not a dual-load artifact). The 35B MoE is still slightly faster on the long log digest. The 9B is more accurate on that digest and ~3.4× lighter.

Not run on the 9B: recette AGENT vision.

## Real Session Replay

Reconstructed from artifacts still on disk, 2 September 2026, 9B loaded but **0 local LLM calls** on these three. jsonl transcripts are not interceptable (already in Claude). Not billed tokens.

| Session | Type | Raw external context | Claude-visible | Direct avoided | Quality |
| ------- | ---- | -------------------: | -------------: | -------------: | ------- |
| A Recette UI LYSI-5177 (2 annexes PNG, transcript a64d3fc5) | DIRECT | 35 142 tok | 511 tok | 34 631 | 4 (`pixel`, `SHA256`) |
| B Module lookup `route_task` (transcript a8ca4c10, two source files as proxy) | DIRECT | 10 820 tok | 202 tok | 10 618 | 4 |
| C Log incident (2.2 MB fixture, extract-only REDUCE) | REDUCE | 560 953 tok | 866 tok | 560 087 | 4 |

First session A run scored 0: DIRECT stored OCR `score=0.265…` and dropped the compare findings. After putting SHA256/pixel labels in the packet: quality 4, visible 437 → 511 tok, interception 98.5%.

A full 18 M token recette day was **not** replayed. Session B uses the two files the lookup needs, not the 2.5 MB jsonl. Do not scale these rows into a billed-session percentage.

Must remain Claude-visible: RECETTE.md / Jira already opened, architecture talk, this chat's instructions, expand on demand.
Non interceptable: the jsonl already in the thread, Claude's own reasoning.

## Context interception rate

```
(raw tokens of the source − Claude-visible packet tokens) / raw tokens
```

Baseline-dependent. Not billed Claude tokens. Optional exposure (`LOCAL_AGENT_COMPOUND_TURNS`) is a separate estimate and is not billed.

## Task offload / escalation

Scripted tests: DIRECT finishes without Claude except when tests FAIL or risk is HIGH (`needs_claude`). No verified-success rate on a 20-task live Claude mix.

Log REDUCE extract-only: 0 local LLM calls. Tiny DIRECT: 0 local LLM calls. A REDUCE task still calls the local LLM when the extract is not high-signal.

## False positives / negatives

- Log REDUCE prose can still invent a worker-id story when an LLM call runs. Extract-only skips that call. Treat LLM prose as **partial**, evidence as **correct**.
- Tiny DIRECT packet > source: no false identifiers in this run.
- AGENT vision: keyword hit on `DIV`/`HCP`; not a human recette sign-off.
- Session A first packet missed `pixel`/`SHA256` (quality 0). Fixed; remasured quality 4.

## Claude billed tokens

Not captured.

## What was not run

- Live Jira fetch inside this harness.
- Live `autonomy=patch` against mlx-serve (scripted in `tests/test_patch_workflow.py`).
- Sequential small-model vs 35B: **done** (9B alone). See Model Comparison.
- Full 18 M Claude recette session replay (still missing as a billed day).
- Houtini / delegate-local install.

## Product verdict (from these runs)

**Niche but useful.**

DIRECT is the tier that changed the product: the 297 B lookup no longer spends 6.9 s in a 35B that skips `rg`. REDUCE is the tier that saves the most Claude-visible tokens, and extract-only now skips the local LLM when high-signal excerpts exist (0.04 s). AGENT can correlate screenshot + repo but costs 13.6 s where pixel+OCR is 0.72 s.

The agent loop is **not** the default value. Use it when the task is actually multi-step.

Default local model: **Qwen3.5-9B-MLX-4bit**.

## Next five priorities

1. Replay a billed Claude Code day (eligible vs already-in-thread vs expand), not only on-disk artifacts.
2. Recette AGENT vision on the 9B (5662 pair was 13.6 s on the 35B).
3. DIRECT packet still 235 tok vs 74 raw: drop more wrapper or return rg lines only.
4. Commit + MCP restart (schema and `local_task` description changed earlier).
5. Slim the compare packet: session A is 511 tok with labels, 437 without. Keep the labels.
