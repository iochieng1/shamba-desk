"""SQLite storage shared by the MCP server, the agent and the officer CLI.

The approval gate lives here, in the schema, not in a prompt: a trigger
refuses any outbox row whose advisory has not been approved by a named
officer. No code path, agent or human, can queue a farmer message around it.
"""

import csv
from contextlib import contextmanager
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DEFAULT_DB = ROOT / "var" / "shamba.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (
    id TEXT PRIMARY KEY, name TEXT, ward TEXT, sub_county TEXT, county TEXT,
    lat REAL, lon REAL, agro_zone TEXT, nearest_market TEXT
);
CREATE TABLE IF NOT EXISTS households (
    id TEXT PRIMARY KEY, cluster_id TEXT REFERENCES clusters(id), head_name TEXT,
    phone_masked TEXT, channel TEXT, language TEXT
);
CREATE TABLE IF NOT EXISTS plots (
    id TEXT PRIMARY KEY, household_id TEXT REFERENCES households(id), area_acres REAL,
    crop TEXT, variety TEXT, planting_date TEXT, stage TEXT, soil TEXT,
    last_visit TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    id TEXT PRIMARY KEY, market TEXT, crop TEXT, unit TEXT, price_kes REAL,
    date TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS pest_alerts (
    id TEXT PRIMARY KEY, ward TEXT, pest TEXT, crop TEXT, severity TEXT,
    reported_on TEXT, source TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT, cluster_id TEXT, task TEXT,
    model TEXT, base_url TEXT, status TEXT, plan TEXT, steps INTEGER DEFAULT 0,
    repairs INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, final_message TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, actor TEXT, server TEXT,
    tool TEXT, args TEXT, result TEXT, ok INTEGER, error TEXT,
    started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS advisories (
    id TEXT PRIMARY KEY, run_id TEXT, cluster_id TEXT, status TEXT, summary TEXT,
    items TEXT, created_at TEXT, decided_by TEXT, decided_at TEXT, decision_note TEXT
);
CREATE TABLE IF NOT EXISTS followups (
    id TEXT PRIMARY KEY, run_id TEXT, household_id TEXT, reason TEXT, due_date TEXT,
    citations TEXT, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS input_requests (
    id TEXT PRIMARY KEY, run_id TEXT, household_id TEXT, item TEXT, quantity TEXT,
    reason TEXT, citations TEXT, status TEXT, created_at TEXT,
    decided_by TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, advisory_id TEXT REFERENCES advisories(id),
    household_id TEXT, channel TEXT, message TEXT, queued_at TEXT, approved_by TEXT
);
CREATE TRIGGER IF NOT EXISTS outbox_requires_approval
BEFORE INSERT ON outbox
WHEN (SELECT status FROM advisories WHERE id = NEW.advisory_id) IS NOT 'approved'
  OR (SELECT decided_by FROM advisories WHERE id = NEW.advisory_id) IS NULL
  OR trim((SELECT decided_by FROM advisories WHERE id = NEW.advisory_id)) = ''
BEGIN
    SELECT RAISE(ABORT, 'gate: advisory is not approved by a named officer');
END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    return Path(os.environ.get("SHAMBA_DB", DEFAULT_DB))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _days_ago(n: str) -> str:
    return (date.today() - timedelta(days=int(n))).isoformat() if n else ""


def seed(path: Path | str | None = None, data_dir: Path = DATA_DIR) -> Path:
    """Recreate the database from the CSVs in data/. Destroys previous state."""
    path = Path(path) if path else db_path()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = connect(path)

    def rows(name):
        with open(data_dir / name, newline="") as f:
            return list(csv.DictReader(f))

    with tx(conn):
        for r in rows("clusters.csv"):
            conn.execute("INSERT INTO clusters VALUES (?,?,?,?,?,?,?,?,?)",
                         (r["id"], r["name"], r["ward"], r["sub_county"], r["county"],
                          float(r["lat"]), float(r["lon"]), r["agro_zone"], r["nearest_market"]))
        for r in rows("households.csv"):
            conn.execute("INSERT INTO households VALUES (?,?,?,?,?,?)",
                         (r["id"], r["cluster_id"], r["head_name"], r["phone_masked"],
                          r["channel"], r["language"]))
        for r in rows("plots.csv"):
            conn.execute("INSERT INTO plots VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (r["id"], r["household_id"], float(r["area_acres"]), r["crop"],
                          r["variety"], _days_ago(r["planted_days_ago"]), r["stage"],
                          r["soil"], _days_ago(r["last_visit_days_ago"]), r["notes"]))
        for r in rows("prices.csv"):
            conn.execute("INSERT INTO prices VALUES (?,?,?,?,?,?,?)",
                         (r["id"], r["market"], r["crop"], r["unit"], float(r["price_kes"]),
                          _days_ago(r["days_ago"]), r["source"]))
        for r in rows("pest_alerts.csv"):
            conn.execute("INSERT INTO pest_alerts VALUES (?,?,?,?,?,?,?,?)",
                         (r["id"], r["ward"], r["pest"], r["crop"], r["severity"],
                          _days_ago(r["reported_days_ago"]), r["source"], r["text"]))
    conn.close()
    return path


def next_id(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return f"{prefix}-{n + 1:03d}"


def log_call(conn, run_id, actor, server, tool, args) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log (run_id, actor, server, tool, args, started_at) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, actor, server, tool, json.dumps(args, ensure_ascii=False), now()))
    return cur.lastrowid


def finish_call(conn, call_id: int, ok: bool, result: str | None = None,
                error: str | None = None) -> None:
    conn.execute("UPDATE audit_log SET ok=?, result=?, error=?, ended_at=? WHERE id=?",
                 (int(ok), result, error, now(), call_id))


@contextmanager
def tx(conn: sqlite3.Connection):
    """Explicit transaction (the connection runs in autocommit mode)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
