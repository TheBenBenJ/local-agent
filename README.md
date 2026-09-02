# local-agent

![Keep large context local. Send Claude only what matters.](docs/architecture.jpg)

## Why

Claude Code and Cursor should not do this:

```text
read huge.log
→ send huge.log to a local model
```

They should do this:

```text
local_task(task="Find the root cause.", sources=["log://var/bench.log"])
```

The MCP reads the file on disk. Claude receives an evidence packet (locations, hashes, ids), not the raw document. The same rule applies to repositories, screenshots, Jira, and Confluence.

If you already know a symbol name, put it in the mission. If `rg` is enough, use `rg`.

## How it works

The figure above is the product: task + source references in, evidence packet out. The router never calls a local LLM to decide which layer to use.

| Tier | When | Local LLM |
| --- | --- | --- |
| **DIRECT** | Tiny source, named symbol, OCR/compare, Jira/Confluence fetch | 0 |
| **REDUCE** | Large log, test dump, or single bulky file: deterministic high-signal extract | Only if the extract is not enough |
| **AGENT** | Genuinely multi-step or multi-source (screenshot + repo, ticket + repo) | Bounded loop, first tool before first LLM |
| **CLAUDE** | Auth, security, public API, architecture, high-risk change | Skip local; keep Claude |

DIRECT and REDUCE are the value. AGENT is not the default path. It is reserved for investigations that actually need several steps.

## Quick start

```bash
git clone https://github.com/TheBenBenJ/local-agent ~/.local-agent
~/.local-agent/install.sh
```

Requires Python 3.9+ (stdlib only), `ripgrep`, `git`, and an OpenAI-compatible local server (`mlx-serve` on Apple Silicon is what we measure against). Restart Claude Code / Cursor, then call `local_ping`.

```bash
~/.local-agent/bin/local-agent doctor
~/.local-agent/bin/local-agent ping
```

`ping` reports `server.git_head`. After you change MCP code, restart the client and check that `local_ping.server.git_head` matches the CLI.

Primary tool: `local_task` with `task` plus `sources` (`repo://`, `image://`, `log://`, `jira://`, `confluence://`). Drill down with `local_expand`. Fine-grained tools (`local_search`, `local_image`, `local_fix`, …) remain for a single already-identified step. Do not delegate twenty lines already in Claude's context.

## Routing tiers

**DIRECT** runs deterministic tools only (`rg`, OCR, pixel compare, `fetch_issue`). `local_llm_calls` must stay 0.

**REDUCE** extracts high-signal excerpts first. If those excerpts already answer the mission, there is no local LLM call. High-signal evidence is kept lossless: the packet is not allowed to drop the planted cause.

**AGENT** is a bounded local tool loop. Use it when the task is actually multi-source or multi-step. It is slower than DIRECT/REDUCE (measured 5.7 s vs tens of milliseconds).

**CLAUDE** is the high-risk exit: the router sets `needs_claude` and does not pretend to decide.

## Measured results

Numbers below come from [`BENCHMARKS.md`](BENCHMARKS.md) (2 September 2026, 9B alone). They are source-context interception, not Claude billing.

| Workload | Raw source | Claude-visible | Source interception | Local LLM | Latency |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2.2 MB log | 560,953 tok | ~848–866 tok | 99.8% | 0 | 0.04 s |
| 2 UI screenshots | 35,142 tok | 254 tok | 99.3% | 0 | n/a |
| module lookup | 10,929 tok | 96 tok | 99.1% | 0 | n/a |
| screenshot + repo AGENT | 9,563 tok | 1,832 tok | 80.8% | 3 calls | 5.7 s |
| tiny 297 B fixture | 74 tok | 101 tok | 0% | 0 | 0.03 s |

Source interception is not billed Claude token savings.

The 2.2 MB log kept `InvoiceService.getTotal` / null invoice in the packet, with 0 local LLM. The two screenshots kept `SHA256` and `pixel` in the packet; do not strip those labels to recover tokens. AGENT on the 9B found `DIV` / `HCP` on a recette pair (the previous 35B run was 13.6 s on the same kind of task).

The tiny fixture is the honest miss: the packet is larger than the source. `rg` can stay smaller (51 tok). The measured win there is skipping a local LLM (6.9 s → 0.03 s), not compression.

## What the numbers mean

Three different quantities. Do not mix them.

**A. Source interception** (measured per delegated file or pair):

```text
interception_rate =
(raw estimated tokens of delegated source
 - Claude-visible packet tokens)
 / raw estimated tokens of delegated source
```

Example: 2.2 MB log → 99.8% less of that source reached Claude. This is source-context interception. It is not Claude billing or subscription usage.

**B. Session impact** (depends on how much of the conversation is interceptable). In a recipe/ticket-heavy workflow we observed, the realistic session-level opportunity is roughly 2–7% of context, depending on whether screenshots and logs stay outside Claude. This is not a billing measurement. Above ~10% generally requires a session dominated by a large log, dump, or similarly large delegated input.

**C. Claude billing / subscription usage:** not measured. There is no supported public surface for Cursor/Claude Code bills. Prompt caching and compaction exist. Cursor jsonl stores tool calls, not tool results. Do not treat reconstructed `Read` sizes as money saved.

Optional "context exposure" (`one-shot × future turns`) is an estimate of later-turn exposure, not a bill. Detail: `BENCHMARKS.md`.

## When it helps

- large logs and dumps
- large test/check output
- screenshots / UI recipes
- unfamiliar repository exploration
- multi-source investigations not yet loaded into Claude
- Jira / Confluence pages whose body does not need to enter Claude verbatim

## When it doesn't

- tiny files (packet can exceed the source)
- already-known explicit symbols where plain `rg` is enough
- content already present in the Claude conversation
- architecture discussions
- high-risk auth/security/API decisions
- instructions Claude must follow verbatim

If `rg` is enough, use `rg`. The router is built to know the difference: this helps a lot on some inputs and little on others.

Name the symbol when you know it. An interrogative with no identifier on a large file is allowed to stay REDUCE. A fallback grep will not search `Where` / `What` / `Find` (those are stopwords). `Locate route_task and initial_action_hint` stays DIRECT.

## Evidence / progressive disclosure

Claude gets evidence IDs (`CODE-E12`, `LOG-E4`, `IMG-E2`, region ids like `a832b1c4-R1`) instead of every raw artifact. `local_expand` fetches the stored excerpt. Items carry provenance, hash, and stale detection. Image crops stay on disk until asked for. That is progressive disclosure, not a second copy of the file in the chat.

## Jira / Confluence

`jira://KEY` and `confluence://id` or `confluence://SPACE/Title` are DIRECT fetches: a short goal/title in the packet, body in the store, expand on demand. No extra semantic understanding is claimed.

Credentials are not stored in this repository. They come from the **target repo's** `.claude/.env.local` (`JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`) or from the environment / `~/.local-agent/local-agent.env` (`JIRA_BASE_URL`, `JIRA_TOKEN`, `JIRA_EMAIL`). `jira://` with `repo` pointed at a checkout that has no credentials correctly returns "not configured". Tokens never appear in `doctor` or reports.

## Model

Recommended default: **`mlx-community/Qwen3.5-9B-MLX-4bit`** (~6 GB, text + vision). Keep one model loaded. The 35B is not required for normal use; it remains a historical comparison in `BENCHMARKS.md`.

On the measured harness the 9B is much lighter, the first tool-use is faster, and extract-only REDUCE no longer needs a local LLM when high-signal excerpts exist. That is not a claim that the 9B is universally better.

Images: OCR and deterministic compare (hash, pixel grid) first. Optional local vision only when the grid has a hole. Most measured image gains come from that deterministic reduction, not from a VLM caption.

## Configuration

Root of the **client** repository, not this tool's checkout:

- default: `git rev-parse --show-toplevel` from the working directory
- override: `LOCAL_AGENT_REPO_ROOT=/path/to/client/repo`
- per call: MCP argument `repo` (absolute path). Cursor does not expand `${workspaceFolder}`.

To work on local-agent itself, point `repo` or `LOCAL_AGENT_REPO_ROOT` at this checkout.

Copy `local-agent.env.example` to `~/.local-agent/local-agent.env` (gitignored). Real environment variables win over the file. `MLX_*` names still work if `LOCAL_LLM_*` is unset.

| Variable | Default | Role |
| --- | --- | --- |
| `LOCAL_LLM_BASE_URL` | `http://127.0.0.1:11234/v1` | OpenAI-compatible endpoint |
| `LOCAL_LLM_MODEL` | `auto` | `auto` = already loaded model |
| `LOCAL_AGENT_REPO_ROOT` | current git root | Target repository |
| `LOCAL_AGENT_DIRECT_CONTEXT_THRESHOLD` | `2000` | Below this, DIRECT forbids a local LLM |
| `LOCAL_AGENT_COMPOUND_TURNS` | `25` | Exposure estimate factor; `0` disables. Not billing. |

Full list: `local-agent.env.example` and `local-agent config`.

## Safety

Paths resolve inside the repository root (images may be absolute files, still refuse secrets and >8 MB). `.git`, `node_modules`, `vendor`, `var`, dumps, and `*.env` / `*.pem` / `*secret*` are denied unless the path names that directory explicitly. Writes default to propose-then-apply with source hashes. No `git reset`, `git add`, or `git commit`. Project checks are a whitelist (`.local-agent.json` or a language preset). Output is clamped.

## CLI

```bash
~/.local-agent/bin/local-agent ping
~/.local-agent/bin/local-agent doctor
~/.local-agent/bin/local-agent task "Find the root cause." --source log://var/bench.log
~/.local-agent/bin/local-agent task "Locate route_task and initial_action_hint" --source repo://local_agent/router.py
~/.local-agent/bin/local-agent expand LOG-E1
~/.local-agent/bin/local-agent image-compare before.png after.png
```

`--json` prints the raw report.

## MCP tools

`local_task`, `local_expand`, `local_metrics`, `local_image_compare`, `local_search`, `local_analyze`, `local_review`, `local_fix`, `local_test_analysis`, `local_log_analysis`, `local_image`, `local_image_crop`, `local_diff_review`, `local_ping`. Every tool accepts optional `repo`. `local_ping` shows the effective root and `server.git_head`.

## Benchmarks

Measured runs, definitions, and limitations: [`BENCHMARKS.md`](BENCHMARKS.md).

```bash
python3 ~/.local-agent/tests/run_all.py
~/.local-agent/bin/local-agent benchmark all
```

## Limitations

- A local LLM call still costs seconds; AGENT is for background-ish work, not a chat ping-pong.
- No identifier in the mission plus a large file can stay REDUCE. Name the symbol when you know it.
- Claude billed tokens are not captured.
- Live `autonomy=patch` against mlx-serve is scripted in tests, not a live sandbox in this harness.
- `local_fix` rewrites whole files; keep it mechanical and under `LOCAL_AGENT_FIX_MAX_FILE_SIZE`.

## Development

Public version: **1.3.0** (`local_agent/version.py`, also `local_ping.server.version`). This release is functionally frozen: later work is for bugs, regressions, security, compatibility, or a measured low-cost win, not a feature roadmap.

Layout: `local_agent/` (router, agent loop, MCP, store, providers). Tests: `python3 tests/run_all.py`. After MCP code changes, restart the client and compare `local_ping.server.git_head` to `./bin/local-agent ping`.
