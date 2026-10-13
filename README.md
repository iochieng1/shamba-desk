# shamba-desk

An agent that helps a sub-county agricultural extension office turn **rainfall, pest and price signals** into **household-level advisories**. It **drafts only**: a named officer approves every advisory before anything reaches a farmer, and every recommendation cites the record it came from.

Target office: **Kanduyi Sub-County Agriculture Office, Bungoma County, Kenya** (short-rains season). Workflow: signals in → sourced draft per household cluster → officer approves, rejects or defers → approved messages queued for SMS, USSD or visit.

> **Status: work in progress (hackathon build).** See [What is unfinished](#what-is-unfinished).

## Quick start

```bash
# 1. Hosted open weights (fast)
cp .env.example .env        # set SHAMBA_LLM_PROVIDER=groq and GROQ_API_KEY
uv run shamba-desk run CL-KHA

# 2. Fully local open weights (no data leaves the machine)
docker compose run --rm desk     # Ollama + qwen2.5:7b, first start downloads ~4.7 GB
```

Groq's free tier works (default model `qwen/qwen3.8-27b`): a run is kept under its 7,000 input-tokens-per-minute limit by trimming tool results and history. Its daily limit (200k tokens) allows about 6 runs a day, so `shamba-desk eval --wait-on-quota` waits for the quota instead of skipping tasks. The local Docker path needs a GPU or ~8 GB of free RAM; on a CPU-only laptop with less, a single model call can take minutes.

Needs Python 3.12 + [uv](https://docs.astral.sh/uv/) and Node 22+ (for the borrowed weather MCP server), or just Docker.

## What it does

```
plan ──> act ⇄ tools ──> verify ──(problems)──> act      (max 2 repairs, 30 tool calls)
                           │
                           v
                  gate: interrupt → named officer approves / rejects / defers
```

- **Plans** explicitly (a JSON plan stored with the run; falls back to a default plan if the model's plan is invalid).
- **Calls tools** on two MCP servers, through one audited hub.
- **Verifies with code, not the model grading itself:** a draft exists, every household is covered, rainfall was checked, households with missing or stale records get a follow-up visit instead of crop advice. Problems go back to the agent as concrete repair instructions.
- **Recovers:** failed tool calls come back as readable errors, transient failures are retried once, and tool calls a small model writes as text are salvaged.
- **Stops at the gate** (LangGraph `interrupt`). Nothing is sent and nothing is ordered by the agent.

How the parts fit, where each safety rule is enforced, and the trade-offs: [ARCHITECTURE.md](ARCHITECTURE.md).

## MCP servers

| Server | Built or borrowed | Tools |
|---|---|---|
| `extension-desk` (`src/shamba_desk/server.py`) | **Built** | read: `list_clusters`, `get_cluster_households`, `get_market_prices`, `get_pest_alerts` · write: `draft_advisory`, `schedule_followup`, `propose_input_request` |
| [`open-meteo-mcp-server@2.4.2`](https://github.com/cmer81/open-meteo-mcp) | **Borrowed** (MIT) | `weather_forecast` (16 others hidden from the agent) |

Why borrow the weather server: it already wraps Open-Meteo's forecast, archive, seasonal and ensemble APIs and is maintained upstream. Writing our own would add code without adding value. Its 10 KB schema is projected down to rainfall variables, so a 7B model can use it.

`extension-desk` works with any MCP client: `uv run shamba-desk-mcp` (stdio).

## Safety and defensibility, enforced in code

- **Citations are validated, not trusted.** Each advisory item must cite a `PLOT-`/`HH-` record for every targeted household. Planting advice must cite a successful rainfall call (`CALL-<n>`), selling advice a `PRICE-` record, and spraying advice a `PEST-` alert. Invalid drafts are rejected with reasons and nothing is saved.
- **Gate in the database:** a SQLite trigger refuses any outbox row unless the advisory was approved by a named officer. Approve, reject and send are officer CLI commands, not agent tools.
- **Audit log:** every tool call and model call is stored with inputs, outputs, timestamps and actor (`agent` or `officer:<name>`). `shamba-desk log`.
- **"Why?" view:** `shamba-desk show ADV-001` resolves every citation to its plot, price, pest report or rainfall total.
- **Cost per run:** tokens and USD per run. `shamba-desk runs`.
- **Prompt injection:** record text is treated as data. The agent has no tool that sends or orders, whatever a pest alert says.

## Commands

```
shamba-desk seed | clusters | config
shamba-desk run CL-KHA [--task "..."] [--defer] [--officer "Name"]
shamba-desk show ADV-001
shamba-desk approve|reject ADV-001 --officer "Name" [--note ...]
shamba-desk send ADV-001 --officer "Name"     # refused unless approved
shamba-desk log [--run RUN_ID] | runs
shamba-desk eval --repeat 3 [--only T04] [--wait-on-quota] [--live-weather]
shamba-desk weather record | info              # the recorded rainfall evals replay
```

## Evals and tests

- **Weather is recorded, so runs are comparable.** `shamba-desk weather record` stores 92 past and 16 forecast days of rainfall for every cluster in `evals/weather/cassette.json`. Evals and tests replay it (`SHAMBA_WEATHER=replay`), cutting out whatever window the model asks for and shifting dates to today, so the agent sees the same rain on any day and no network is needed. `--live-weather` uses Open-Meteo instead. Results say which weather they ran on.
- `evals/tasks.yaml`: 10 tasks (routine clusters, price spike, price tool failing once, weather server down, prompt injection in a pest alert, asked to order and send, unknown cluster, pressure to advise a household with no plot record, spraying request without an alert). Each runs N times on a fresh DB with deterministic checks. Pass rate, tool-call spread and cost are reported to `evals/results/`.
- `uv run pytest -m "not network"`: 31 offline tests: citation rules, the gate, weather replay, and end-to-end graph and eval-harness runs with a scripted model against the real `extension-desk` server on recorded weather.
- `uv run pytest`: also runs the graph test once against the live Open-Meteo server.

## Data

`data/` is **synthetic**: 30 households in 3 clusters (Khalaba, Musikoma, Marakaru/Tuuti wards), plots, stored produce, KAMIS-style prices and 2 pest alerts. Places are real; people and numbers are not. Two deliberate gaps (HH-007 has no plot record, HH-024's record is 210 days old) test that the agent does not invent advice. Rainfall is real Open-Meteo data (CC BY 4.0): live for `run`, recorded for evals and tests. `data/office_guidance.md` holds the agronomy rules of thumb and is editable by the office.

## What is unfinished

- **Eval results are partial.** On `qwen/qwen3.8-27b` (Groq free tier) the first tasks pass; the full suite is running at free-tier pace. Quota refusals are reported as "not run", never as passes or failures.
- **Not yet validated with an extension officer.** The guidance in `data/office_guidance.md` is generic and needs officer review.
- Docker Compose builds, pulls the model and starts the agent, but a full run has not finished on the CPU-only build machine (too slow; see Quick start).
- Prices and households are synthetic; delivery (SMS/USSD) is a simulated outbox.
- EVALS.md and the demo video are still to come.

## Licence

MIT. Weather data © Open-Meteo, CC BY 4.0.
