"""Citation rules and the approval gate. No model, no network."""

import sqlite3

import pytest

from shamba_desk import desk, officer
from shamba_desk.db import finish_call, log_call

ITEM = {"household_ids": ["HH-002"], "action": "Keep weeding.", "reason": "r",
        "citations": ["HH-002"]}


def weather_call(conn, run_id="r1", ok=True):
    cid = log_call(conn, run_id, "agent", "open-meteo", "weather_forecast", {})
    finish_call(conn, cid, ok, result="{}" if ok else None, error=None if ok else "timeout")
    return f"CALL-{cid}"


def problems(conn, **change):
    return desk.validate_items(conn, "CL-KHA", [{**ITEM, **change}], "r1")


def test_valid_item_has_no_problems(conn):
    assert problems(conn) == []


@pytest.mark.parametrize("cites, expected", [
    ([], "no citations"),
    (["HH-002", "PLOT-999"], "does not exist"),
    (["rainfall data"], "is not a citation"),
    (["CALL-999"], "does not exist in the audit log"),
])
def test_bad_citations_are_rejected(conn, cites, expected):
    assert any(expected in p for p in problems(conn, citations=cites))


def test_failed_or_foreign_calls_are_not_evidence(conn):
    failed = weather_call(conn, ok=False)
    foreign = weather_call(conn, run_id="other-run")
    assert any("failed tool call" in p for p in problems(conn, citations=["HH-002", failed]))
    assert any("different run" in p for p in problems(conn, citations=["HH-002", foreign]))


def test_household_must_be_in_cluster_and_grounded(conn):
    out = problems(conn, household_ids=["HH-002", "HH-003", "HH-011"])
    assert any("HH-011 is not a household in CL-KHA" in p for p in out)
    assert any("HH-003 is targeted but none" in p for p in out)


@pytest.mark.parametrize("action, needs", [
    ("Plant beans now.", "rainfall"),
    ("Delay planting until rain returns.", "rainfall"),
    ("Sell half of your stored beans this week.", "PRICE"),
    ("Spray the maize against armyworm.", "PEST"),
])
def test_actions_need_matching_evidence(conn, action, needs):
    assert any(needs in p for p in problems(conn, action=action))


def test_actions_pass_with_matching_evidence(conn):
    w = weather_call(conn)
    assert problems(conn, action="Plant beans now.", citations=["HH-002", w]) == []
    assert problems(conn, action="Sell half your beans.", citations=["HH-002", "PRICE-010"]) == []
    assert problems(conn, action="Scout maize plants twice a week.") == []


def test_draft_rejection_saves_nothing_and_redraft_supersedes(conn):
    with pytest.raises(desk.DeskError):
        desk.draft_advisory(conn, "CL-KHA", "s", [{**ITEM, "citations": []}], "r1")
    assert conn.execute("SELECT COUNT(*) FROM advisories").fetchone()[0] == 0
    a = desk.draft_advisory(conn, "CL-KHA", "s", [ITEM], "r1")
    b = desk.draft_advisory(conn, "CL-KHA", "s", [ITEM], "r1")
    status = dict(conn.execute("SELECT id, status FROM advisories").fetchall())
    assert status == {a["advisory_id"]: "superseded", b["advisory_id"]: "pending_approval"}


def test_input_request_is_never_an_order(conn):
    r = desk.propose_input_request(conn, "HH-002", "CAN", "1 bag", "top dress", ["HH-002"])
    assert r["status"] == "awaiting_officer"


def test_flags_for_missing_and_stale_records(conn):
    kha = {h["id"]: h["data_flags"] for h in desk.get_cluster_households(conn, "CL-KHA")["households"]}
    tuu = {h["id"]: h["data_flags"] for h in desk.get_cluster_households(conn, "CL-TUU")["households"]}
    assert kha["HH-007"][0].startswith("NO_PLOT_RECORD")
    assert tuu["HH-024"][0].startswith("STALE_RECORD")


# --- the gate -------------------------------------------------------------

def _draft(conn):
    return desk.draft_advisory(conn, "CL-KHA", "s", [ITEM], "r1")["advisory_id"]


def test_database_refuses_unapproved_messages(conn):
    adv = _draft(conn)
    with pytest.raises(sqlite3.IntegrityError, match="gate"):
        conn.execute("INSERT INTO outbox (advisory_id, household_id, channel, message) "
                     "VALUES (?, 'HH-002', 'sms', 'x')", (adv,))
    with pytest.raises(sqlite3.IntegrityError, match="gate"):
        officer.queue_for_delivery(conn, adv, "Some Officer")
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_approval_needs_a_name_and_happens_once(conn):
    adv = _draft(conn)
    with pytest.raises(officer.GateError, match="named officer"):
        officer.decide(conn, adv, "  ", approve=True)
    officer.decide(conn, adv, "Jane Wanjiku", approve=True)
    with pytest.raises(officer.GateError, match="not pending"):
        officer.decide(conn, adv, "Someone Else", approve=False)
    assert officer.queue_for_delivery(conn, adv, "Jane Wanjiku")["queued"] == 1
    row = conn.execute("SELECT approved_by FROM outbox").fetchone()
    assert row["approved_by"] == "Jane Wanjiku"
    actors = {r[0] for r in conn.execute("SELECT actor FROM audit_log")}
    assert "officer:Jane Wanjiku" in actors


def test_rejected_advisory_cannot_be_sent(conn):
    adv = _draft(conn)
    officer.decide(conn, adv, "Jane Wanjiku", approve=False, note="wrong stage")
    with pytest.raises(sqlite3.IntegrityError):
        officer.queue_for_delivery(conn, adv, "Jane Wanjiku")
