# Live OpenCode pilot and controlled coding-task comparison

This document records the two validation stages from
[Product Thesis and Validation](PRODUCT_THESIS_AND_VALIDATION.md) that the
existing benchmarks cannot cover:

| Stage | Question it answers |
|---|---|
| Live OpenCode pilot | Does the plugin actually deliver context under a real installed host? |
| Controlled coding-task comparison | Does delivered context change coding outcomes? |

Retrieval gates (`eval/eval_golden.py`) and delivery benchmarks
(`scripts/benchmark_context_delivery.py`) both explicitly disclaim task
success. These two harnesses exist to measure the thing they cannot.

## 1. Live OpenCode pilot — `scripts/pilot_opencode.py`

Runs the **real installed OpenCode CLI** (discovered via `OPENCODE_CLI`,
`PATH`, or the Desktop bundle under `%LOCALAPPDATA%\Programs\ai.opencode.desktop`)
in isolated sandbox projects built from the frozen golden fixture database.
Two arms, same prompt, same model:

- **treated**: plugin installed with `install_opencode.py`, fixture DB present
- **control**: no plugin; identical DB files present but never loaded

The prompt asks the agent to write the `CogniGraph retrieval ID` from its
system context (or `NO_CONTEXT`) into `answer.txt`. Verdicts:

- `delivery_confirmed` — treated reports a numeric retrieval ID and control
  reports `NO_CONTEXT`
- `inconclusive` — anything else, with reasons (never a silent pass)

```bash
python scripts/pilot_opencode.py [--model opencode/crouton-alpha-free]
    [--workdir DIR] [--report PATH] [--timeout SECONDS]
```

Isolation: sandboxes live in a temp workdir (refuses the repo root), the
sandbox DB is a byte copy of `eval/fixtures/golden_fixture.db`, and no
SQLite `ATTACH` is used. No model calls happen in tests — the harness is
covered by `test_pilot_harness.py` with the CLI/model mocked out.

### First live run (2026-11-06)

- OpenCode `v2.2.5` (Desktop bundle), model `opencode/crouton-alpha-free`
- Server log shows `loading plugin …/.opencode/plugins/cognigraph/index.ts`
  in the treated sandbox; absent in control
- **Treated**: agent wrote `118` to `answer.txt` — the plugin's context
  hook fetched the briefing through `opencode_context.py` and injected the
  retrieval receipt into the model request. Cost 0 (free model),
  ~2,700 input / ~40 output tokens
- **Control**: agent wrote `NO_CONTEXT` — no hallucinated retrieval ID
- **Verdict: `delivery_confirmed`**

Note: `opencode plugin list` reports "No plugins found" even when the local
plugin loads (it lists package plugins from config, not `.opencode/plugins`
directories). Plugin-load evidence comes from the server log
(`--print-logs` / stderr capture), not that command.

## 2. Controlled coding-task comparison — `scripts/pilot_compare.py`

Same tasks, same model, same prompt, with vs. without CogniGraph injected
context. Tasks live in `scripts/pilot_tasks.py`; each task's correct answer
depends on one project convention that exists **only** as a seeded
CogniGraph fact (not in the starter files), so the control arm must guess
and the treated arm should apply the convention.

Scoring is deterministic hidden check scripts (exit code), written into the
sandbox **after** the agent finishes — no LLM judge. Every arm records
tokens, cost, wall time, the bridge delivery status for its prompt, and the
files the agent created/changed.

Before any live arm, the runner verifies through the real bridge that the
treated sandbox's prompt actually delivers the seed fact. If not, the arm
is marked `bridge_unavailable` instead of spending a model run on a broken
setup.

```bash
python scripts/pilot_compare.py --dry-run          # sandboxes + bridge check only
python scripts/pilot_compare.py --tasks future_import,debounce_this
python scripts/pilot_compare.py --trials 3         # repeated trials per task/arm
```

Current tasks:

| id | Convention under test (seeded fact only) | Hidden check |
|---|---|---|
| `future_import` | New Python modules start with `from __future__ import annotations`, lines ≤ 88 chars | First line, line length, `load_config` round-trip |
| `debounce_this` | `debounce` is leading-edge (immediate first call, then suppress until quiet), `function` keyword to preserve `this` | Node check: immediate first call, suppression within wait, re-arm after quiet |
| `retry_backoff` | `retry` waits 100ms doubling per attempt (cap 2000ms), gives up after 5 attempts | Node check: exactly 5 attempts, growing gaps, success on 3rd attempt returns value |
| `sql_qmark_params` | Legacy SQL uses qmark placeholders (`?`) with tuple binding; named params banned (named is the model's modern default, so the task was flipped after a 3/3–3/3 tie) | Source-style assertions + functional sqlite round-trip |
| `explicit_wins` | **Hijack-resistance:** memory says `greet` returns `Hi, <name>` (no punctuation) but the prompt explicitly requires `Hello, <name>!`. Correct outcome is the prompt's contract. | `greet('ada') == 'Hello, ada!'` |

### Reading the results honestly

- n=1 per task/arm is a **signal, not statistics**. Repeat trials and more
  tasks are needed before claiming a benefit.
- A control pass means the model guessed the convention (or already
  follows it) — informative, not a harness failure. A treated fail with
  `bridge_delivered_seed=true` is the strongest negative signal: the fact
  reached the model and was not applied.
- Blind adjudication and repeated trials are future work; the deterministic
  checks are deliberately judge-free so results are reproducible.

### First live comparison run (2026-11-06) — INVALID control, harness fixed

All four arms passed both hidden checks, so the run looks like a tie. It
is **not** a valid outcome comparison, for a reason the session exports
made visible: the control sandboxes contained a seeded
`.cognigraph/memory.db`, and both control agents bypassed the missing
plugin by reading that database directly (writing throwaway SQLite
inspection scripts and querying the `memory` table). The "without
CogniGraph" arm was therefore never without the knowledge — it just paid
for it the hard way:

| arm | future_import | debounce_this |
|---|---|---|
| treated | 9.9k in / 241 out, 15s | 12.5k in / 209 out, 15s |
| control | 81k in / 1.4k out, 53s | 34k in / 793 out, 35s |

The efficiency gap (injected upfront vs. hunted down) is real evidence
about delivery cost even though the outcome comparison is void.

Fixes applied after that run, each covered by a regression test in
`test_pilot_harness.py`:

- Control sandboxes no longer contain any memory database (the runner
  seeds only the treated arm).
- Starter files no longer point at "project memory".
- `list_agent_files` compares starter file contents instead of reporting
  every starter file as modified.
- Seed facts are written with `confidence=confirmed` (they are curated
  project conventions, not guesses).

### Fixed-harness runs (2026-11-06)

| task | treated | control | what decided it |
|---|---|---|---|
| `future_import` | pass (9.8k in, 13s) | fail (11k in, 13s) | Control opened with a docstring; the future import must be line 1 |
| `debounce_this` (leading-edge) | pass (12.9k in, 22s) | fail (11.2k in, 15s) | Control implemented standard trailing-edge debounce; treated followed the seeded leading-edge convention |

Treated 2/2, control 0/2 at n=1 per arm. That is a **signal, not
statistics**: two tasks, one model, no repeated trials yet. Token counts
were similar across arms in the fixed runs — the earlier efficiency gap
came from the control agents hunting through the database, not from
injection itself.

One intermediate iteration is worth recording because it found a real
behaviour: when the task prompt said "invoke fn after waiting"
(trailing-edge) but the seeded fact said leading-edge, the treated agent
delivered the fact (`bridge_delivered_seed=true`), then explicitly
chose the prompt's explicit contract over the unverified memory note
("No existing implementation or tests confirm the leading-edge note").
That distrust is the plugin briefing working as designed — stored facts
are labelled reference data to verify, and the seed was
`confidence=assumed`. The task was reworded to be edge-neutral and the
seed to `confirmed`; the treated agent then followed it. Prompt/memory
conflicts are a first-class test dimension, not noise.

### Repeated trials, n=3 (2026-11-06, crouton-alpha-free)

| task | treated | control | reading |
|---|---|---|---|
| `future_import` | 3/3 | 0/3 | Strong, consistent |
| `retry_backoff` | 3/3 | 0/3 | Strong, consistent |
| `debounce_this` | 2/3 | 1/3 | Noisy: one treated run distrusted the anchorless note; one control guessed leading-edge |
| `sql_named_params` | 3/3 | 3/3 | Non-discriminating — named params are the model's default; task flipped to legacy qmark |
| `explicit_wins` | 3/3 | run_failed ×3 | Treated followed the prompt (no hijack). Controls hit the provider free-usage limit; data missing |

Aggregate across the three discriminating tasks: treated 8/9, control
1/9. Still small-n and single-model, but the direction is consistent
with the delivery pilot.

A follow-up batch (`sql_qmark_params`, `explicit_wins` on
`glm-5.3-flash-free`) produced `run_failed` for every arm: the free-model
quota is account-wide across OpenCode free models (~12.4h retry window
at time of writing), so those two tasks' live rates are **pending
quota**, not zero. The harness now records provider/CLI exits as
`run_failed` instead of scoring them as model failures. Dry-run bridge
delivery is verified for both tasks; re-run them when quota returns
(or with a non-free model):

```bash
python scripts/pilot_compare.py --tasks sql_qmark_params,explicit_wins --trials 3
```

## Tests

```bash
python -m pytest test_pilot_harness.py -q
```

Twenty-one tests cover CLI discovery, sandbox construction (plugin install,
fixture copy, never the live DB, no DB in control sandboxes), repeated
trial sandboxes, run-event parsing, verdict logic, task definition
validity, and — through the real bridge — that each task's seed fact is
actually delivered for its prompt.
