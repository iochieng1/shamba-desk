# Architecture

shamba-desk is one Python package, one SQLite file and two MCP servers. The agent drafts; code checks; a named officer decides. This page explains how the parts fit and why they are split the way they are. For setup and commands see the [README](README.md).

## Components

```
                        officer (CLI: run, show, approve, reject, send, log)
                           │                                   │
                           v                                   v
   ┌──────────────── agent.py (LangGraph) ────────────────┐  officer.py
   │  plan ─> act ⇄ tools ─> verify ─> gate (interrupt)   │  approve / reject / queue
   └──────────────────────┬───────────────────────────────┘     │
                          │ every tool call                     │
                          v                                     │
                  hub.py  ToolHub                               │
                  audit row · CALL-<n> id · timeout ·           │
                  retry once · fault injection · replay         │
                     │                         │                │
            stdio MCP│                stdio MCP│ (or cassette)  │
                     v                         v                │
       server.py  extension-desk     open-meteo-mcp-server      │
       (built)    4 read, 3 write    (borrowed, MIT)            │
                     │                         │                │
                     v                         v                v
                 desk.py ──────────> var/shamba.db <──── outbox trigger
                 validation          (SQLite)
```

| Module | Role |
|---|---|
| `agent.py` | The LangGraph state machine: plan, act, tools, verify, gate. Owns prompts, budgets and run bookkeeping. |
| `hub.py` | The only path from the agent to a tool. Connects both MCP servers and shows the model a curated tool list. |
| `server.py` | The `extension-desk` MCP server: a thin FastMCP wrapper over `desk.py`. Usable from any MCP client. |
| `desk.py` | Plain functions over SQLite: reads, drafts, follow-ups, input notes, and **citation validation**. No model, no transport, so it is unit-tested directly. |
| `db.py` | Schema, seeding from `data/`, audit-log helpers, and the **outbox trigger**. |
| `officer.py` | Human actions: approve, reject, queue for delivery. Not exposed as agent tools. |
| `weather.py` | Recorded Open-Meteo responses for reproducible evals and tests. |
| `explain.py` | Resolves each citation back to the record or rainfall it stands for (`shamba-desk show`). |
| `config.py` | One OpenAI-compatible client config for Ollama, Groq or OpenRouter, with prices for cost per run. |
| `evals.py` | Runs `evals/tasks.yaml` N times on fresh databases and checks outcomes with code. |
| `cli.py` | The `shamba-desk` command. |

## One run, step by step

1. **Start.** `run_agent` creates a run id, opens a `ToolHub` (which starts both MCP servers over stdio) and inserts a `runs` row.
2. **Plan.** The model gets the task, the office guidance (`data/office_guidance.md`) and the tool list, and must reply with `{"steps": [...]}`. One retry; if it still is not valid JSON, a fixed fallback plan is used and the run records that it fell back. The plan is stored on the run.
3. **Act ⇄ tools.** The model calls tools until it stops asking. Each call goes through the hub, which writes an `audit_log` row *before* the call and finishes it after. The result handed back to the model starts with `[CALL-<n>]`, the audit row's id, which is what the model cites for weather. Tool calls a small model writes as text instead of in the API field are recovered when they parse. Budget: 30 tool calls.
4. **Verify.** Code, not the model, checks the database for this run: is there a pending draft, was rainfall checked, does every flagged household (no plot record, record older than 120 days) have a follow-up, is no flagged household given crop advice, is every household covered. Problems go back to `act` as concrete instructions. Budget: 2 repairs.
5. **Gate.** If a draft exists, the graph `interrupt`s. The caller's `decide` callback (the CLI prompt, or a test) returns approve, reject or defer with an officer name. Approve and reject go through `officer.decide`; defer leaves the draft pending.
6. **Finish.** Status, tool calls, repairs, tokens and USD cost are written to the run. Nothing has been sent: queueing messages is a separate officer command.

## Where the safety rules live

Each rule is enforced by code at the layer that can't be talked around. The prompt repeats the rules so the model gets them right first time, but nothing depends on the model obeying it.

| Rule | Enforced in | How |
|---|---|---|
| Every item cites evidence for every household | `desk.validate_items` | Each targeted household needs a cited `HH-` or `PLOT-` record that exists and belongs to it. |
| Planting, selling and spraying advice need the right evidence | `desk.validate_items` | Keyword families (`NEEDS_RAINFALL`, `NEEDS_PRICE`, `NEEDS_PEST`) require a successful weather `CALL-`, a `PRICE-` or a `PEST-` citation. The regexes are short and readable on purpose, so an officer can audit them. |
| Citations are real | `desk._check_citation` | Record ids must exist. `CALL-<n>` must be a *successful* audit row *from this run*. A failed call is not evidence. |
| Bad drafts are not saved | `desk.draft_advisory` | Validation runs before the insert. The rejection lists every problem so the model can repair it. |
| Missing or stale records get a visit, not advice | `agent.verify` | Checked against data flags computed from the database, not the model's reading of them. |
| Nothing reaches a farmer without a named officer | SQLite trigger `outbox_requires_approval` | Any outbox insert aborts unless the advisory is `approved` with a non-blank `decided_by`. This holds for every code path, including bugs in ours. |
| The agent cannot send, order or approve | `hub.py` + `server.py` | Those tools do not exist on any server the agent can reach. `propose_input_request` only records a note with status `awaiting_officer`. |
| Record text cannot steer the agent | Tool design | Pest alerts are labelled as unverified data, and since no tool can send or order, an injected instruction has nothing dangerous to call. Eval T06 tests this. |
| Everything is traceable | `audit_log` | Every tool call and model call, with inputs, outputs, timestamps and actor (`agent` or `officer:<name>`). |

## The tool hub

`ToolHub` sits between the graph and MCP so that auditing, citation ids and failure handling are the same for our server and the borrowed one.

- **Curated tools.** The borrowed server exposes 17 tools with a ~10 KB schema. The hub shows only `weather_forecast`, with its schema cut down to rainfall and temperature variables, a window and a timezone. Fewer knobs and a smaller prompt suit a 7B model. Arguments the hub fills itself (`run_id`) are hidden from the model.
- **Failures.** A transport failure or timeout (60 s) is retried once. A tool-level error, such as bad arguments, is not retried: it is the model's to fix. Either way the model gets a readable `ERROR [CALL-n] ...` that says the call cannot be cited.
- **Fault injection.** `SHAMBA_FAULTS=tool:once|always` makes a tool fail on purpose. Evals use it for the "price tool fails once" and "weather server down" tasks.
- **Weather replay.** With `SHAMBA_WEATHER=replay` the borrowed server is not started; `weather_forecast` is answered from `evals/weather/cassette.json`. The audit row, `CALL-n` id, server name and fault injection behave exactly as live, so citation rules and evals don't know the difference.

## Data

- **Seeded, relative dates.** `data/*.csv` stores dates as "days ago" and `db.seed` turns them into real dates at seed time, so the data is always current. Two deliberate gaps (HH-007 has no plot, HH-024's record is 210 days old) test that the agent asks for a visit instead of guessing.
- **Recorded weather.** `shamba-desk weather record` fetches 92 past and 16 forecast days of every rainfall variable per cluster. Replay cuts out the window the model asks for, snaps points to Open-Meteo's 0.1° grid (as the live server does) and shifts dates to today, so recorded rain lines up with the seeded "days ago" data on any day.
- **One SQLite file** holds reference data, runs, the audit log, drafts, decisions and the outbox. The MCP server process and the agent process share it via `SHAMBA_DB`. Evals give each run its own file.

## Model access

Every provider is reached through the OpenAI-compatible API (`langchain-openai`), so local Ollama, Groq and OpenRouter differ only in environment variables. `<think>` blocks from reasoning models are stripped before they reach the log or the next turn. Cost per run is tokens × the configured price. It is reported, not enforced.

## Evals and tests

- **Evals** (`evals/tasks.yaml`, `evals.py`): 10 scenarios, each run N times on a fresh database with replayed weather. Checks are code over the resulting database (was there a draft, are flagged households followed up, is there a rainfall citation, is the outbox empty...), never a model grading itself. Results go to `evals/results/` with pass rate, tool-call spread and cost.
- **Tests** (`tests/`): citation rules and the gate against the database; weather replay; and full graph runs with a scripted model (`tests/fakes.py`) against the real `extension-desk` server. All run offline; one variant also runs against live Open-Meteo (`-m network`).

## Design choices and trade-offs

- **Deterministic verifier over self-critique.** A small model grading its own draft tends to approve it. Checking the database is cheap, repeatable and explainable to an officer. The cost is that `verify` only catches what it was written to catch. Advice quality beyond the rules still needs the officer.
- **Gate in the schema, not only in the graph.** The LangGraph interrupt is the workflow; the trigger is the guarantee. If the graph, the CLI or a future web UI gets it wrong, the database still refuses.
- **Borrow the weather server.** Open-Meteo's MCP server is maintained upstream and covers forecast, archive and ensembles. Wrapping it costs one schema projection; rewriting it would add code to maintain without improving anything.
- **Keyword rules for evidence.** Regexes are blunt: they can miss a paraphrase ("put seed in the ground") or flag a negation. They are kept because an officer can read and change them, and the officer reviews every draft anyway.
- **Drafts replace drafts.** Each `draft_advisory` call in a run supersedes the previous one rather than patching it, so a repair is just "call it again with everything". Superseded drafts are kept for the audit trail.
- **SQLite and stdio.** Single office, single machine, no server to run. Moving to Postgres or HTTP MCP would change `db.py` and the hub's transport, not the agent.

## Not covered here

Delivery is a simulated outbox (no SMS or USSD gateway). Prices and households are synthetic. The office guidance has not yet been reviewed by an extension officer. See [What is unfinished](README.md#what-is-unfinished).
