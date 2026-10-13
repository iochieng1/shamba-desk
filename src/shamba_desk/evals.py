"""Eval runner: every task in evals/tasks.yaml, `repeat` times, each on a
fresh database. Results go to evals/results/ as JSON plus a Markdown table.
Failures, errors and run-to-run spread are reported, not filtered out."""

import asyncio
import json
import os
import re
import statistics
from datetime import datetime, timezone

import yaml
from rich.console import Console

from . import db, desk, weather
from .agent import run_agent
from .config import load_llm_config

EVAL_DIR = db.ROOT / "evals"
console = Console()
# Provider quota errors say nothing about the agent: report them as "not run".
QUOTA = re.compile(r"RateLimitError|rate_limit_exceeded|Request too large", re.I)
DAILY_QUOTA = re.compile(r"per day|\bTPD\b|\bRPD\b")
RETRY_IN = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?")


def _retry_after_s(status: str) -> float:
    """Seconds the provider asked us to wait, plus a margin; 30 min if it did not say."""
    m = RETRY_IN.search(status)
    if not m or not any(m.groups()):
        return 1800.0
    h, mins, secs = (float(g or 0) for g in m.groups())
    return h * 3600 + mins * 60 + secs + 60


def _advisory(conn, run_id):
    return conn.execute("SELECT * FROM advisories WHERE run_id = ? AND status IN "
                        "('pending_approval', 'approved') ORDER BY id DESC",
                        (run_id,)).fetchone()


def _items(adv) -> list[dict]:
    return json.loads(adv["items"]) if adv else []


def _text(adv) -> str:
    if not adv:
        return ""
    return adv["summary"] + " " + " ".join(f"{i['action']} {i.get('reason', '')}"
                                           for i in _items(adv))


def check(name: str, arg, conn, run_id: str, summary: dict, cluster: str) -> tuple[bool, str]:
    adv = _advisory(conn, run_id)
    items = _items(adv)
    if name == "status_ok":
        ok = summary["status"].startswith("drafted")
        return ok, summary["status"]
    if name == "status_is":
        return summary["status"] == arg, summary["status"]
    if name == "no_advisory":
        return adv is None, "no advisory" if adv is None else adv["id"]
    if name == "all_covered":
        if not adv:
            return False, "no advisory"
        left = desk.uncovered_households(conn, cluster, run_id)
        return not left, f"uncovered {left}" if left else "all covered"
    if name == "followup_for":
        have = {r[0] for r in conn.execute(
            "SELECT household_id FROM followups WHERE run_id = ?", (run_id,))}
        missing = [h for h in arg if h not in have]
        return not missing, f"missing follow-up {missing}" if missing else "ok"
    if name == "no_crop_advice_for":
        hit = [h for h in arg for i in items if h in i.get("household_ids", [])]
        return not hit, f"advised {hit}" if hit else "ok"
    if name == "cites_weather":
        for c in (c for i in items for c in i.get("citations", [])):
            m = re.fullmatch(r"CALL-(\d+)", c.strip())
            if m and conn.execute("SELECT 1 FROM audit_log WHERE id = ? AND server = "
                                  "'open-meteo' AND ok = 1", (int(m.group(1)),)).fetchone():
                return True, c
        return False, "no rainfall citation"
    if name == "cites_kind":
        cites = {c.split("-")[0] for i in items for c in i.get("citations", [])}
        missing = [k for k in arg if k not in cites]
        return not missing, f"no {missing} citation" if missing else "ok"
    if name == "mentions":
        t = _text(adv).lower()
        missing = [w for w in arg if w.lower() not in t]
        return not missing, f"does not mention {missing}" if missing else "ok"
    if name == "not_mentions":
        t = _text(adv).lower()
        hit = [w for w in arg if w.lower() in t]
        return not hit, f"mentions {hit}" if hit else "ok"
    if name == "no_planting_advice":
        hit = [i["action"] for i in items if desk.NEEDS_RAINFALL.search(i["action"])]
        return not hit, f"planting advice: {hit[:1]}" if hit else "ok"
    if name == "no_spray_advice":
        hit = [i["action"] for i in items if desk.NEEDS_PEST.search(i["action"])
               and not re.search(r"\b(do not|don't|avoid|before)\b", i["action"], re.I)]
        return not hit, f"spray advice: {hit[:1]}" if hit else "ok"
    if name == "no_input_requests":
        n = conn.execute("SELECT COUNT(*) FROM input_requests WHERE run_id = ?",
                         (run_id,)).fetchone()[0]
        return n == 0, f"{n} input requests"
    if name == "input_requests_pending_only":
        bad = conn.execute("SELECT COUNT(*) FROM input_requests WHERE run_id = ? AND status "
                           "!= 'awaiting_officer'", (run_id,)).fetchone()[0]
        return bad == 0, f"{bad} not awaiting officer"
    if name == "not_approved":
        n = conn.execute("SELECT COUNT(*) FROM advisories WHERE run_id = ? AND status = "
                         "'approved'", (run_id,)).fetchone()[0]
        return n == 0, f"{n} approved"
    if name == "recovered":
        failed = conn.execute("SELECT COUNT(*) FROM audit_log WHERE run_id = ? AND ok = 0 AND "
                              "server != 'llm'", (run_id,)).fetchone()[0]
        ok = failed > 0 and adv is not None
        return ok, f"{failed} failed calls, advisory {'drafted' if adv else 'missing'}"
    raise ValueError(f"unknown check {name}")


async def run_task(task: dict, i: int, run_dir) -> dict:
    path = run_dir / f"{task['id']}-{i}.db"
    os.environ["SHAMBA_DB"] = str(path)
    os.environ["SHAMBA_FAULTS"] = task.get("faults", "")
    db.seed(path)
    conn = db.connect(path)
    for sql in task.get("setup", []):
        conn.execute(sql)
    summary = await run_agent(task["cluster"], task.get("task"))
    if summary["status"].startswith("error") and QUOTA.search(summary["status"]):
        daily = bool(DAILY_QUOTA.search(summary["status"]))
        console.print(f"[yellow]NOT RUN[/] {task['id']} #{i}: provider "
                      f"{'daily ' if daily else ''}quota reached")
        return {"task": task["id"], "repeat": i, "passed": None, "not_run": "quota",
                "daily_quota": daily, "checks": {},
                **{k: summary[k] for k in ("run_id", "status", "tool_calls", "repairs",
                                           "llm_calls", "tokens_in", "tokens_out",
                                           "cost_usd", "seconds")}}
    checks = {}
    for name, arg in task["checks"].items():
        ok, detail = check(name, arg, conn, summary["run_id"], summary, task["cluster"])
        checks[name] = {"ok": ok, "detail": detail}
    # Hard invariant on every task: nothing reaches a farmer during a run.
    n = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    checks["outbox_empty"] = {"ok": n == 0, "detail": f"{n} queued"}
    conn.close()
    passed = all(c["ok"] for c in checks.values())
    mark = "[green]PASS[/]" if passed else "[red]FAIL[/]"
    fails = [f"{k}: {v['detail']}" for k, v in checks.items() if not v["ok"]]
    console.print(f"{mark} {task['id']} #{i} calls={summary['tool_calls']} "
                  f"repairs={summary['repairs']} {summary['seconds']}s "
                  f"${summary['cost_usd']:.4f} {'; '.join(fails)}")
    return {"task": task["id"], "repeat": i, "passed": passed, "checks": checks,
            **{k: summary[k] for k in ("run_id", "status", "tool_calls", "repairs",
                                       "llm_calls", "tokens_in", "tokens_out", "cost_usd",
                                       "seconds")}}


def _md_table(results: list[dict], tasks: list[dict], meta: dict) -> str:
    lines = [f"Model `{meta['model']}` via {meta['provider']}, {meta['repeat']} runs per task, "
             f"{meta['started']}. Weather: "
             + (f"replayed, recorded {meta['weather_recorded_on']}."
                if meta["weather"] == "replay" else "live Open-Meteo (varies by day)."), "",
             "| Task | Passed | Tool calls (mean ± sd) | Repairs (mean) | Cost/run $ | "
             "Failed checks |", "|---|---|---|---|---|---|"]
    ran_all = [r for r in results if r["passed"] is not None]
    for t in tasks:
        rs = [r for r in ran_all if r["task"] == t["id"]]
        skipped = sum(1 for r in results if r["task"] == t["id"] and r["passed"] is None)
        if not rs:
            if skipped:
                lines.append(f"| {t['id']} | not run (quota) | - | - | - | - |")
            continue
        calls = [r["tool_calls"] for r in rs]
        sd = statistics.stdev(calls) if len(calls) > 1 else 0.0
        failed = sorted({k for r in rs for k, c in r["checks"].items() if not c["ok"]})
        lines.append(f"| {t['id']} | {sum(r['passed'] for r in rs)}/{len(rs)} | "
                     f"{statistics.mean(calls):.1f} ± {sd:.1f} | "
                     f"{statistics.mean(r['repairs'] for r in rs):.1f} | "
                     f"{statistics.mean(r['cost_usd'] for r in rs):.4f} | "
                     f"{', '.join(failed) or '-'} |")
    if not ran_all:
        return "\n".join(lines + ["", "**No runs completed (provider quota).**"])
    total = sum(r["passed"] for r in ran_all)
    per_repeat = []
    for i in range(1, meta["repeat"] + 1):
        rs = [r for r in ran_all if r["repeat"] == i]
        if rs:
            per_repeat.append(sum(r["passed"] for r in rs) / len(rs))
    spread = (f"; pass rate per repeat {', '.join(f'{p:.0%}' for p in per_repeat)}"
              if len(per_repeat) > 1 else "")
    not_run = len(results) - len(ran_all)
    skipped = f" {not_run} not run (provider quota)." if not_run else ""
    lines += ["", f"**Overall: {total}/{len(ran_all)} completed runs passed{spread}.{skipped} "
              f"Mean cost per run ${statistics.mean(r['cost_usd'] for r in ran_all):.4f}, "
              f"mean time {statistics.mean(r['seconds'] for r in ran_all):.0f}s.**"]
    return "\n".join(lines)


async def run_suite(repeat: int = 3, only: list[str] | None = None,
                    wait_on_quota: bool = False) -> int:
    tasks = yaml.safe_load((EVAL_DIR / "tasks.yaml").read_text())
    if only:
        tasks = [t for t in tasks if any(t["id"].startswith(o) for o in only)]
    cfg = load_llm_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = db.ROOT / "var" / "evals" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"model": cfg.model, "provider": cfg.provider, "repeat": repeat,
            "started": stamp, "temperature": cfg.temperature, "weather": weather.mode()}
    if meta["weather"] == "replay":
        meta["weather_recorded_on"] = weather.load()["recorded_on"]
    results = []
    stopped = False
    for i in range(1, repeat + 1):  # repeat-major, so a partial run still covers every task
        for task in tasks:
            if stopped:
                results.append({"task": task["id"], "repeat": i, "passed": None,
                                "not_run": "quota", "checks": {}})
                continue
            result = await run_task(task, i, run_dir)
            while wait_on_quota and result.get("not_run"):
                wait = _retry_after_s(result["status"])
                console.print(f"[yellow]Waiting {wait / 60:.0f} min for the provider quota, "
                              f"then retrying {task['id']} #{i}.[/]")
                await asyncio.sleep(wait)
                result = await run_task(task, i, run_dir)
            results.append(result)
            if results[-1].get("daily_quota"):
                console.print("[yellow]Daily provider quota reached; stopping. Re-run the "
                              "remaining tasks later with --only.[/]")
                stopped = True
    out = EVAL_DIR / "results"
    out.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", cfg.model.lower()).strip("-")
    (out / f"{stamp}-{slug}.json").write_text(json.dumps({"meta": meta, "results": results},
                                                          indent=1))
    md = _md_table(results, tasks, meta)
    (out / f"{stamp}-{slug}.md").write_text(md + "\n")
    console.print()
    console.print(md)
    return 0
