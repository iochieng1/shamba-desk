"""Command line for the officer and the developer.

    shamba-desk seed                  reset the database from data/
    shamba-desk run CL-KHA            run the agent on one cluster (stops at the officer gate)
    shamba-desk show ADV-001          the advisory with every citation resolved
    shamba-desk approve ADV-001 --officer "Name"   /  reject ...
    shamba-desk send ADV-001 --officer "Name"      queue approved messages (simulated)
    shamba-desk log [--run RUN_ID]    the audit log
    shamba-desk runs                  past runs with tool calls, tokens and cost
    shamba-desk eval --repeat 3       the eval suite on recorded weather (see EVALS.md)
    shamba-desk weather record        record rainfall for every cluster, for replay
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import date

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from . import db, officer
from .agent import run_agent
from .config import load_llm_config
from .explain import describe

console = Console()


def _short(value, n=110) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def show_advisory(conn, adv_id: str) -> None:
    adv = conn.execute("SELECT * FROM advisories WHERE id = ?", (adv_id,)).fetchone()
    if not adv:
        console.print(f"[red]No advisory {adv_id}[/]")
        return
    console.rule(f"[bold]{adv['id']}[/] · {adv['cluster_id']} · {adv['status']}")
    console.print(f"[bold]Summary:[/] {adv['summary']}")
    for i, item in enumerate(json.loads(adv["items"]), 1):
        console.print(f"\n[bold cyan]{i}. {', '.join(item['household_ids'])}[/]")
        console.print(f"   [bold]Do:[/] {item['action']}")
        console.print(f"   [bold]Why:[/] {item.get('reason', '')}")
        for c in item["citations"]:
            console.print(f"   [dim]{c}[/] {describe(conn, c)}")
    fus = conn.execute("SELECT * FROM followups WHERE run_id = ?", (adv["run_id"],)).fetchall()
    if fus:
        console.print("\n[bold]Follow-up visits:[/]")
        for f in fus:
            console.print(f"   {f['id']} {f['household_id']} by {f['due_date']}: {f['reason']}")
    reqs = conn.execute("SELECT * FROM input_requests WHERE run_id = ?",
                        (adv["run_id"],)).fetchall()
    for r in reqs:
        console.print(f"   [yellow]{r['id']} input note (not an order)[/] {r['household_id']}: "
                      f"{r['quantity']} {r['item']} ({r['status']})")
    if adv["decided_by"]:
        console.print(f"\n[bold]{adv['status'].title()} by {adv['decided_by']}[/] at "
                      f"{adv['decided_at']} {adv['decision_note'] or ''}")
    console.rule()


def emit(kind: str, p) -> None:
    if kind == "start":
        console.print(f"[bold]Run {p['run_id']}[/] · model {p['model']} ({p['provider']})")
    elif kind == "plan":
        console.print(f"\n[bold magenta]PLAN[/] [dim]({p['source']})[/]")
        for i, s in enumerate(p["steps"], 1):
            console.print(f"  {i}. {s}")
        console.print()
    elif kind == "think":
        console.print(f"[dim italic]model: {_short(p, 200)}[/]")
    elif kind == "call":
        console.print(f"[bold blue]→ {p['name']}[/]({_short(p['args'])})")
    elif kind == "result":
        mark = "[green]✓[/]" if p["ok"] else "[red]✗[/]"
        body = p["text"].split("\n", 1)[-1] if p["ok"] else p["text"]
        console.print(f"  {mark} {_short(body, 150)}")
    elif kind == "note":
        console.print(f"[yellow]! {p}[/]")
    elif kind == "verify":
        if p["problems"]:
            console.print("\n[bold yellow]VERIFIER[/] sends the agent back:")
            for prob in p["problems"]:
                console.print(f"  [yellow]- {prob}[/]")
            console.print()
        else:
            extra = f" (caveats: {', '.join(p['caveats'])})" if p.get("caveats") else ""
            console.print(f"\n[bold green]VERIFIER[/] passed{extra}")
    elif kind == "decision":
        console.print(f"[bold]GATE[/] {p['advisory_id']} → {p['status']}")
    elif kind == "end":
        console.print(f"\n[bold]Done[/] status={p['status']} tool_calls={p['tool_calls']} "
                      f"repairs={p['repairs']} llm_calls={p['llm_calls']} "
                      f"tokens={p['tokens_in']}+{p['tokens_out']} "
                      f"cost=${p['cost_usd']:.4f} time={p['seconds']}s")


def cmd_run(args) -> int:
    conn = db.connect()

    def decide(info):
        show_advisory(conn, info["advisory_id"])
        if args.defer or not sys.stdin.isatty():
            console.print("[bold]GATE[/] left for an officer: "
                          f"shamba-desk approve {info['advisory_id']} --officer \"Name\"")
            return {"action": "defer"}
        console.print("[bold]GATE[/] Nothing reaches a farmer without a named officer.")
        action = Prompt.ask("Decision", choices=["approve", "reject", "defer"], default="defer")
        if action == "defer":
            return {"action": "defer"}
        name = args.officer or Prompt.ask("Officer name")
        note = Prompt.ask("Note (optional)", default="")
        return {"action": action, "officer": name, "note": note}

    summary = asyncio.run(run_agent(args.cluster, args.task, decide=decide, emit=emit))
    if summary["final_message"]:
        console.print(f"[bold]Agent's last message:[/] {summary['final_message']}")
    return 0 if not summary["status"].startswith("error") else 1


def cmd_log(args) -> int:
    conn = db.connect()
    q, params = "SELECT * FROM audit_log", []
    if args.run:
        q += " WHERE run_id = ?"
        params.append(args.run)
    rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", params + [args.limit]).fetchall()
    t = Table("id", "started", "actor", "server", "tool", "ok", "args", "error/result")
    for r in reversed(rows):
        t.add_row(f"CALL-{r['id']}", (r["started_at"] or "")[11:19], r["actor"], r["server"],
                  r["tool"], "✓" if r["ok"] else "✗", _short(r["args"] or "", 50),
                  _short(r["error"] or r["result"] or "", 60))
    console.print(t)
    return 0


def cmd_runs(args) -> int:
    conn = db.connect()
    t = Table("run", "cluster", "model", "status", "calls", "repairs", "tokens", "cost $")
    for r in conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 20"):
        t.add_row(r["id"], r["cluster_id"], r["model"], _short(r["status"] or "", 40),
                  str(r["steps"]), str(r["repairs"]),
                  f"{r['prompt_tokens']}+{r['completion_tokens']}", f"{r['cost_usd']:.4f}")
    console.print(t)
    return 0


def cmd_decide(args, approve: bool) -> int:
    conn = db.connect()
    try:
        r = officer.decide(conn, args.advisory, args.officer, approve, args.note or "")
    except officer.GateError as e:
        console.print(f"[red]{e}[/]")
        return 1
    console.print(f"{r['advisory_id']} {r['status']} by {r['officer']}")
    return 0


def cmd_send(args) -> int:
    conn = db.connect()
    try:
        r = officer.queue_for_delivery(conn, args.advisory, args.officer)
    except (officer.GateError, sqlite3.IntegrityError) as e:
        console.print(f"[red]Refused: {e}[/]")
        return 1
    console.print(f"Queued {r['queued']} messages for {r['advisory_id']} (simulated outbox):")
    for m in conn.execute("SELECT * FROM outbox WHERE advisory_id = ?", (args.advisory,)):
        console.print(f"  {m['household_id']} via {m['channel']}: {m['message']}")
    return 0


def cmd_weather(args) -> int:
    from . import weather
    if args.action == "info":
        try:
            c = weather.load()
        except FileNotFoundError as e:
            console.print(f"[yellow]{e}[/]")
            return 1
        console.print(f"{weather.cassette_path()}: recorded {c['recorded_on']}, points "
                      f"{', '.join(sorted(c['points']))}; replay mode is "
                      f"{weather.mode()} (SHAMBA_WEATHER)")
        return 0
    from .desk import list_clusters
    from .hub import record_weather
    if not db.db_path().exists():
        db.seed()
    clusters = list_clusters(db.connect())
    points = asyncio.run(record_weather([(c["lat"], c["lon"]) for c in clusters]))
    first = next(iter(points.values()))["daily"]["time"]
    recorded_on = date.fromisoformat(first[weather.RECORD_PAST_DAYS])
    path = weather.save(points, recorded_on)
    console.print(f"Recorded {len(points)} points for {recorded_on} to {path}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="shamba-desk", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="reset the database from data/")
    sub.add_parser("clusters", help="list clusters")
    r = sub.add_parser("run", help="run the agent on a cluster")
    r.add_argument("cluster")
    r.add_argument("--task", help="override the default task text")
    r.add_argument("--officer", default=os.environ.get("SHAMBA_OFFICER"))
    r.add_argument("--defer", action="store_true", help="do not prompt at the gate")
    s = sub.add_parser("show", help="show an advisory with its evidence")
    s.add_argument("advisory")
    for name in ("approve", "reject"):
        a = sub.add_parser(name)
        a.add_argument("advisory")
        a.add_argument("--officer", required=True)
        a.add_argument("--note")
    s = sub.add_parser("send", help="queue an approved advisory (simulated delivery)")
    s.add_argument("advisory")
    s.add_argument("--officer", required=True)
    lg = sub.add_parser("log", help="audit log")
    lg.add_argument("--run")
    lg.add_argument("--limit", type=int, default=60)
    sub.add_parser("runs", help="past runs with cost")
    sub.add_parser("config", help="show the model configuration")
    e = sub.add_parser("eval", help="run the eval suite")
    e.add_argument("--repeat", type=int, default=3)
    e.add_argument("--only", nargs="*")
    e.add_argument("--wait-on-quota", action="store_true",
                   help="when the provider's quota is reached, wait and retry instead of "
                        "skipping (free tiers: the suite may take days)")
    e.add_argument("--live-weather", action="store_true",
                   help="call Open-Meteo instead of replaying the recorded cassette")
    w = sub.add_parser("weather", help="recorded weather for reproducible evals")
    w.add_argument("action", choices=["record", "info"])
    args = p.parse_args(argv)

    if args.cmd not in ("seed", "config", "eval", "weather") and not db.db_path().exists():
        console.print(f"[dim]No database yet; seeding {db.db_path()}[/]")
        db.seed()
    if args.cmd == "seed":
        console.print(f"Seeded {db.seed()}")
        return 0
    if args.cmd == "clusters":
        from .desk import list_clusters
        for c in list_clusters(db.connect()):
            console.print(f"{c['id']}  {c['name']} ({c['ward']}), {c['households']} households, "
                          f"market {c['nearest_market']}")
        return 0
    if args.cmd == "config":
        cfg = load_llm_config()
        key = "set" if cfg.api_key != "not-needed" else "not set"
        console.print(f"provider={cfg.provider} model={cfg.model} base_url={cfg.base_url} "
                      f"api_key={key} price=${cfg.price_in}/${cfg.price_out} per M tokens")
        return 0
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "show":
        show_advisory(db.connect(), args.advisory)
        return 0
    if args.cmd in ("approve", "reject"):
        return cmd_decide(args, args.cmd == "approve")
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "log":
        return cmd_log(args)
    if args.cmd == "runs":
        return cmd_runs(args)
    if args.cmd == "eval":
        from .evals import run_suite
        os.environ.setdefault("SHAMBA_WEATHER", "live" if args.live_weather else "replay")
        return asyncio.run(run_suite(args.repeat, args.only, args.wait_on_quota))
    if args.cmd == "weather":
        return cmd_weather(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
