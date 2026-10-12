"""Actions only a named human officer can take. None of these are MCP tools:
the agent has no way to call them. Each one is written to the audit log
with the officer's name as the actor.
"""

import json

from .db import finish_call, log_call, now, tx


class GateError(PermissionError):
    pass


def _require_name(officer: str) -> str:
    officer = (officer or "").strip()
    if len(officer) < 3:
        raise GateError("A named officer is required (e.g. --officer 'Jane Wanjiku').")
    return officer


def _audited(conn, officer, tool, args, fn):
    call = log_call(conn, None, f"officer:{officer}", "officer-cli", tool, args)
    try:
        result = fn()
    except Exception as e:
        finish_call(conn, call, False, error=str(e))
        raise
    finish_call(conn, call, True, result=json.dumps(result, ensure_ascii=False))
    return result


def decide(conn, advisory_id: str, officer: str, approve: bool, note: str = "") -> dict:
    officer = _require_name(officer)

    def run():
        row = conn.execute("SELECT status FROM advisories WHERE id = ?", (advisory_id,)).fetchone()
        if not row:
            raise GateError(f"No advisory {advisory_id}.")
        if row["status"] != "pending_approval":
            raise GateError(f"{advisory_id} is {row['status']}, not pending_approval.")
        status = "approved" if approve else "rejected"
        conn.execute("UPDATE advisories SET status=?, decided_by=?, decided_at=?, "
                     "decision_note=? WHERE id=?", (status, officer, now(), note, advisory_id))
        return {"advisory_id": advisory_id, "status": status, "officer": officer}

    return _audited(conn, officer, "approve" if approve else "reject",
                    {"advisory_id": advisory_id, "note": note}, run)


def render_message(item: dict) -> str:
    """The farmer-facing text: the action plus its reason, short enough for SMS."""
    msg = item["action"].strip()
    if item.get("reason"):
        msg += f" Why: {item['reason'].strip()}"
    return msg[:459]  # three concatenated SMS segments


def queue_for_delivery(conn, advisory_id: str, officer: str) -> dict:
    """Write approved messages to the outbox. Delivery (SMS/USSD gateway or a
    printed visit sheet) is outside this system and is simulated here."""
    officer = _require_name(officer)

    def run():
        adv = conn.execute("SELECT * FROM advisories WHERE id = ?", (advisory_id,)).fetchone()
        if not adv:
            raise GateError(f"No advisory {advisory_id}.")
        queued = 0
        with tx(conn):  # all or nothing; the DB trigger enforces approval too
            for item in json.loads(adv["items"]):
                for hh in item["household_ids"]:
                    ch = conn.execute("SELECT channel FROM households WHERE id = ?",
                                      (hh,)).fetchone()["channel"]
                    conn.execute("INSERT INTO outbox (advisory_id, household_id, channel, "
                                 "message, queued_at, approved_by) VALUES (?,?,?,?,?,?)",
                                 (advisory_id, hh, ch, render_message(item), now(),
                                  adv["decided_by"]))
                    queued += 1
        return {"advisory_id": advisory_id, "queued": queued}

    return _audited(conn, officer, "queue_for_delivery", {"advisory_id": advisory_id}, run)
