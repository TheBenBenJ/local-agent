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
| Jira-only first tool `fetch_issue` (unit) | DIRECT | DIRECT | yes | n/a (unit) |
| Confluence-only first tool `fetch_page` (unit) | DIRECT | DIRECT | yes | n/a (unit) |
| Jira + repo (unit) | AGENT | AGENT | yes | n/a (unit) |
| Live Jira `jira://LYSI-5177` | DIRECT | DIRECT | yes | 0.5 s (0 LLM, HTTP fetch) |
| Live Confluence `confluence://1323499521` | DIRECT | DIRECT | yes | 0.3 s (0 LLM, HTTP fetch) |

Harness routing accuracy on scored cases A+B: **2/2**. Unit routing cases: **11/11**. Live Atlassian: **2/2**. Combined routing accuracy on this set: **100%**.

`avoidable_local_llm_calls` (DIRECT that still called a local LLM): **0** on the tiny-repo harness and on the DIRECT unit tests.

## Results (context)

| Scenario | Raw context | Claude-visible | Direct avoided | Interception | Correct | Latency |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| A Repo, tier DIRECT (297 B fixture, slim) | 74 tok | 101 tok | 0 | 0% | yes (`require_permission`, `invoice.read`) | 0.03 s |
| A same, first slim (absolute paths + wrapper) | 74 tok | 235 tok | 0 | 0% | yes | 0.03 s |
| A same, first 35B packet (pre-slim) | 74 tok | 383 tok | 0 | 0% | yes | 0.08 s |
| A same, baseline B `rg` | 74 tok | 51 tok | 23 | 31% | n/a (deterministic) | 0.014 s |
| B 2.2 MB log, REDUCE extract-only (9B resident, 0 LLM) | 560 953 tok | 866 tok | 560 087 | 99.8% | yes (`InvoiceService`, `null`) | 0.04 s |
| B same, REDUCE + 35B LLM (before extract-skip) | 560 953 tok | 1 006 tok | 559 947 | 99.8% | yes (`InvoiceService`, `null`) | 4.1 s |
| B same, baseline B `rg ERROR` | 560 953 tok | 1 319 tok | 559 634 | 99.8% | n/a | 0.005 s |
| C Two recette screenshots + repo, AGENT 9B | 9 563 tok | 1 832 tok | 7 731 | 80.8% | yes (`DIV`, `HCP`) | 5.7 s |
| C same pair, AGENT 35B (same day) | 9 563 tok | 2 075 tok | 7 488 | 78.3% | yes (`DIV`, `HCP`) | 13.6 s |
| C same pair, pixel+OCR compare, no LLM | 9 563 tok | 1 400 tok | 8 163 | 85% | partial (`pixel`, not `DIV`) | 0.72 s |
| E Whitelisted failing check | 20 000 tok (assumed dump) | 86 tok | 19 914 | 99.6% | yes (`TypeError`, `total`) | 0.017 s |
| Live Jira LYSI-5177, DIRECT `fetch_issue` | 2 000 tok (router estimate) | 172 tok (JSON packet) | n/a | n/a | yes (key + goal in packet; description stayed in the store) | 0.5 s |
| Live Confluence page 1323499521, DIRECT `fetch_page` | 2 000 tok (router estimate) | 105 tok (JSON packet) | n/a | n/a | yes (page id in packet; storage body stayed in the store) | 0.3 s |

Notes:

- Case A packet is still larger than the 297 B source (101 tok vs 74; `rg` is 51). The win is **not** compression: it is skipping the local LLM (6.9 s → 0.03 s) and keeping both identifiers. Absolute `/var/folders` paths were counted as the skipped `var/` directory, so the tiny fixture used to estimate 0 tokens.
- Case B no longer calls a local LLM when high-signal excerpts exist (0.04 s extract vs 4–5 s LLM). The planted cause stays in runtime evidence. `rg ERROR` remains ~800× faster. A 35B/9B sentence on that digest is optional, not the default.
- Case C with `image://` + `repo://` is routed AGENT (multi-source). On the 9B: 5.7 s, 3 local LLM calls, same `DIV`/`HCP` hits as the 35B at 13.6 s. Deterministic compare without a repo pointer remains faster (0.72 s) and smaller, but misses `DIV`.
- Case E baseline A (80 kB) is still an assumed PHPUnit-style dump.
- Live Jira: credentials from lysi `.claude/.env.local`, base `https://6tmgroup.atlassian.net`. Before the first-tool fix, jira-only DIRECT called `search_repo` and never fetched the issue. Interception is not scored: the router still estimates 8 kB / 2 000 tok, not the real issue payload.
- Live Confluence: same token, page id `1323499521`. DIRECT, 0 LLM, 0.3 s, 105 tok JSON. `"body"` did not appear in the packet. Same 8 kB estimate as Jira, not scored as interception.

## Quality evaluation (keyword recall, scale 0–4)

0 = wrong, 2 = partial, 4 = correct + the expected strings.

| Case | Expected | Hits | Recall | Score |
| --- | --- | --- | ---: | ---: |
| A DIRECT | `require_permission`, `invoice.read` | both | 100% | 4 |
| B REDUCE | `InvoiceService`, `null` | both | 100% | 4 |
| C AGENT 9B | `DIV`, `HCP` | both | 100% | 4 |
| C AGENT 35B | `DIV`, `HCP` | both | 100% | 4 |
| C compare no LLM | `DIV`, `pixel` | `pixel` | 50% | 2 |
| E `run_check` | `TypeError`, `total` | both | 100% | 4 |

Root cause on the log: **correct** (strings present in the packet). The 35B summary on REDUCE can still narrate the wrong worker-id story; the packet is not allowed to drop LOG-E excerpts. Do not treat the prose summary as the source of truth.

## Latency split

| Case | Routing | Deterministic | Local LLM | Total |
| --- | ---: | ---: | ---: | ---: |
| A DIRECT (slim, 9B resident unused) | 0.8 ms | ~20 ms (`rg`) | 0 | 0.03 s |
| B REDUCE extract-only | 0.1 ms | ~40 ms extract | 0 | 0.04 s |
| B REDUCE + 35B LLM (before skip) | 0.1 ms | 38 ms extract | 4.03 s | 4.1 s |
| C AGENT 9B | 0.1 ms | tools 1.45 s | 4.28 s (3 calls) | 5.7 s |
| C AGENT 35B | n/a in row | tools inside 13.6 s | included | 13.6 s |
| C compare no LLM | n/a | 0.72 s | 0 | 0.72 s |
| E check | n/a | 17 ms | 0 | 0.017 s |

TTFT is not exposed by the mlx-serve HTTP complete call. `local_llm_s` is wall time of `complete` / `complete_chat`.

## Model Comparison

Same harness. 9B-only run after unloading the 35B: 2 September 2026, `mlx-community/Qwen3.5-9B-MLX-4bit` resident 5.95 GB, 35B `unloaded`.

| Model | A DIRECT | B REDUCE | C AGENT recette | Ping | RAM |
| ----- | -------- | -------- | --------------- | ---: | --: |
| `Qwen3.6-35B-A3B-4bit` | 4/4, 0 LLM | keywords 4/4; prose invented a worker-id story; 4.03 s LLM | 4/4 `DIV`/`HCP`, 13.6 s | n/a | 20.4 GB |
| `Qwen3.5-9B-MLX-4bit` (alone) | 4/4, 0 LLM | keywords 4/4 extract-only 0.04 s (0 LLM). Prior LLM prose named `InvoiceService.getTotal` / null in 4.90 s | 4/4 `DIV`/`HCP`, 5.7 s, 3 calls | 0.18 s | 5.95 GB |

With both models loaded, 9B ping was 0.44 s and REDUCE 5.13 s. Alone, ping drops to 0.18 s. The 9B is the default: lighter, same recette keywords, faster AGENT loop. The 35B MoE was slightly faster only on a long log digest that extract-only no longer sends to an LLM.

## Real Session Replay

Reconstructed from artifacts still on disk, 2 September 2026, 9B loaded but **0 local LLM calls** on these three. jsonl transcripts are not interceptable (already in Claude). Not billed tokens.

| Session | Type | Raw external context | Claude-visible | Direct avoided | Quality |
| ------- | ---- | -------------------: | -------------: | -------------: | ------- |
| A Recette UI LYSI-5177 (2 annexes PNG, transcript a64d3fc5) | DIRECT | 35 142 tok | 254 tok | 34 888 | 4 (`pixel`, `SHA256`) |
| B Module lookup `route_task` (transcript a8ca4c10, two source files as proxy) | DIRECT | 10 929 tok | 96 tok | 10 833 | 4 |
| C Log incident (2.2 MB fixture, extract-only REDUCE) | REDUCE | 560 953 tok | 848 tok | 560 105 | 4 |

First session A run scored 0: DIRECT stored OCR `score=0.265…` and dropped the compare findings. Labels restored: quality 4. Wrapper slim then dropped duplicate evidence and path-prefixed OCR headers: 663 → 254 tok, interception 99.3%. Session B slim: 202 → 96 tok.

## Cursor jsonl (not billed)

Streamed 56 transcripts under the lysi Cursor `agent-transcripts` folder, 2 September 2026. Cursor jsonl stores **tool calls, not tool results**. File bodies billed at the time are missing. `eligible_read` reconstructs `Read` paths still on disk (limit × 200 chars, else file size).

| Scope | jsonl | Already in jsonl | Reconstructed Read | Reads / missing | Grep calls |
| ----- | ---: | ---------------: | -----------------: | --------------- | ---------: |
| This chat a8ca4c10 (2.6 MB, 1518 lines) | 2 613 055 B | 628 991 tok | 157 357 tok | 83 / 7 | 218 |
| Recette UI a64d3fc5 (1.1 MB, 640 lines) | 1 115 188 B | 268 721 tok | 575 671 tok | 134 / 69 | 420 |
| Folder (56 jsonl) | 7 901 707 B (~1.98 M tok) | 1 873 567 tok | 3 452 388 tok | 829 / 194 | 1 530 |

A billed 18 M token Claude day was **not** observed. The folder total is an on-disk jsonl census, not the API bill. Do not treat `eligible_read` as money saved: those bytes were not in the jsonl, and we do not know how much of each file Claude actually received.

Command: `local-agent --json benchmark transcript <file.jsonl>` / `local-agent --json benchmark day <folder>`.

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
- Extract-only REDUCE used to put `N high-signal / M signatures` in ROOT CAUSE (first stored row). It now prefers a ROOT_CAUSE / ERROR excerpt (`InvoiceService.getTotal called on null invoice`).
- Tiny DIRECT packet > source: no false identifiers in this run.
- AGENT vision: keyword hit on `DIV`/`HCP`; not a human recette sign-off.
- Session A first packet missed `pixel`/`SHA256` (quality 0). Fixed; remasured quality 4.

## Claude billed tokens

Not captured.

## What was not run

- Live Jira fetch: **done** 2 September 2026, `jira://LYSI-5177`. DIRECT, 0 local LLM, 0.5 s, JSON packet 689 B (~172 tok). `acceptance_criteria_verbatim` stayed in the evidence store.
- Live Confluence fetch: **done** 2 September 2026, `confluence://1323499521`. DIRECT, 0 local LLM, 0.3 s, JSON packet 423 B (~105 tok). Storage body stayed in the store.
- Live `autonomy=patch` against mlx-serve (scripted in `tests/test_patch_workflow.py`).
- Sequential small-model vs 35B: **done** (9B alone), including recette AGENT vision.
- Full 18 M Claude recette session as **billed** tokens (jsonl classifier exists; the API bill does not).
- Houtini / delegate-local install.

## Phrasing sensitivity on repo:// (2026-09-02)

Same file, same size (`local_agent/router.py`, ~3093 tokens). Only the wording of the mission changes.

| Mission wording | Tier | local_llm_calls | Latency | Status |
|---|---|---|---|---|
| `Where is the deterministic tier routing decided, and which function returns the initial action hint?` | reduce | 1 | 4.3 s | needs_claude |
| `Locate the definitions of route_task and initial_action_hint.` | direct | 0 | 0.0 s | locations 255 / 168 / 240 |

Mechanism, visible in the packet: the deterministic probe derives its patterns from the words of the mission. Naming the symbols produces `CODE-E5 1/1 matches for route_task` and `CODE-E6 2/2 matches for initial_action_hint`. An interrogative wording produces `CODE-E7 0/0 matches for Locate` in the good case, and in the bad case a false positive: `1/1 matches for Where` on line 40, which is the stopword list itself. That weak hit is not enough for DIRECT, the router falls through to REDUCE, spends one local LLM call and returns needs_claude with confidence 0.35.

The fallback is by design. The cost of the fallback is what this measures: 43x latency and 0 to 1 local LLM call on an identical source, decided by phrasing alone.

Commands:

    LOCAL_AGENT_REPO_ROOT=/Users/benjaminmille/.local-agent ./bin/local-agent --json task \
      "Where is the deterministic tier routing decided, and which function returns the initial action hint?" \
      --source "repo://local_agent/router.py"

    LOCAL_AGENT_REPO_ROOT=/Users/benjaminmille/.local-agent ./bin/local-agent --json task \
      "Locate the definitions of route_task and initial_action_hint." \
      --source "repo://local_agent/router.py"

Caller guidance already in the tool description ("If you already know a class, attribute or field name, grep") is confirmed by measurement, and extends to `local_task`: name the symbol in the mission when you know it.

The `Where` grep leak (fallback ignored `STOPWORDS`) was fixed after this measurement. The table above remains the cost of that leak. After the fix, an interrogative with no explicit symbol on a large file may still be REDUCE; the probe must not search `Where`.

## MCP freshness verified (2026-09-02)

`local_ping` with `repo=/Users/benjaminmille/.local-agent` returns `server.git_head 1f532ea4713f`, equal to the CLI. REDUCE locations came back relative (`var/bench.log:1-4`), and `jira://LYSI-5177` routed DIRECT to `fetch_issue` with 0 local LLM calls, 0.6 s, 150 visible tokens, title only, description kept in the store. Priority 2 below is closed for this client.

Jira credentials resolve from the target repo's `.claude/.env.local`, so `jira://` needs `repo` pointed at lysi. With `repo` on local-agent the packet returns `JIRA-E2 Jira is not configured` in DIRECT, 0 LLM.

## Product verdict (from these runs)

**Niche but useful.**

DIRECT is the tier that changed the product: the 297 B lookup no longer spends 6.9 s in a 35B that skips `rg`. REDUCE is the tier that saves the most Claude-visible tokens, and extract-only now skips the local LLM when high-signal excerpts exist (0.04 s). AGENT on the 9B correlates screenshot + repo in 5.7 s (was 13.6 s on the 35B); pixel+OCR without a repo pointer remains 0.72 s.

The agent loop is **not** the default value. Use it when the task is actually multi-step.

Default local model: **Qwen3.5-9B-MLX-4bit**.

## Next five priorities

1. Capture billed Claude tokens (usage API or export). Still not available: there is no supported public surface for Cursor/Claude Code bills. jsonl on disk is not the bill.
2. Done for this client: `local_ping` returns `server.git_head 1f532ea4713f`. Re-check after any tree that changes `version.py`.
3. Keep pixel/SHA256 labels in the compare packet. Session A is 254 tok with findings; do not strip the verdict to recover more.
4. Live `autonomy=patch` against mlx-serve (scripted only).
5. DIRECT 101 tok vs 74 raw (`rg` 51). Further cuts drop STATUS or the hit line. Accepted.
