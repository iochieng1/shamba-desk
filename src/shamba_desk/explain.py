"""Turn citation ids back into the evidence they stand for, so an officer can
answer a farmer's "why?" from the advisory alone."""

import json
from datetime import date


def rainfall_summary(result_json: str) -> str:
    try:
        daily = json.loads(result_json)["daily"]
        days, rain = daily["time"], daily.get("precipitation_sum") or daily.get("rain_sum")
    except (KeyError, TypeError, json.JSONDecodeError):
        return "weather result (no daily rainfall in it)"
    if not rain:
        return "weather result (no daily rainfall in it)"
    today = date.today().isoformat()
    past = [r or 0 for d, r in zip(days, rain) if d < today]
    ahead = [r or 0 for d, r in zip(days, rain) if d >= today]
    parts = []
    if past:
        parts.append(f"last {len(past)} days {sum(past):.0f} mm "
                     f"(last 7: {sum(past[-7:]):.0f} mm)")
    if ahead:
        parts.append(f"next {len(ahead)} days {sum(ahead):.0f} mm forecast "
                     f"(wettest day {max(ahead):.0f} mm)")
    return "Open-Meteo rainfall: " + "; ".join(parts)


def describe(conn, cite: str) -> str:
    cite = cite.strip()
    if cite.startswith("CALL-"):
        row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (int(cite[5:]),)).fetchone()
        if not row:
            return "missing call"
        args = json.loads(row["args"] or "{}")
        if row["server"] == "open-meteo":
            where = f"at {args.get('latitude')},{args.get('longitude')}"
            return f"{rainfall_summary(row['result'] or '')} {where}, fetched {row['started_at']}"
        return f"{row['tool']}({json.dumps(args)[:80]}) at {row['started_at']}"
    kind = cite.split("-")[0]
    if kind == "HH":
        r = conn.execute("SELECT * FROM households WHERE id = ?", (cite,)).fetchone()
        return (f"household {r['head_name']}, reached by {r['channel']}, stored: "
                f"{r['stored_produce'] or 'nothing recorded'}") if r else "missing"
    if kind == "PLOT":
        r = conn.execute("SELECT * FROM plots WHERE id = ?", (cite,)).fetchone()
        if not r:
            return "missing"
        planted = f"planted {r['planting_date']}" if r["planting_date"] else "not planted"
        return (f"{r['household_id']} plot {r['area_acres']} ac {r['crop']} ({r['variety']}), "
                f"{planted}, {r['stage']}, record from visit {r['last_visit']}")
    if kind == "PRICE":
        r = conn.execute("SELECT * FROM prices WHERE id = ?", (cite,)).fetchone()
        return (f"{r['crop']} at {r['market']} KES {r['price_kes']}/{r['unit']} on {r['date']} "
                f"({r['source']})") if r else "missing"
    if kind == "PEST":
        r = conn.execute("SELECT * FROM pest_alerts WHERE id = ?", (cite,)).fetchone()
        return (f"{r['pest']} on {r['crop']} in {r['ward']}, {r['severity']}, reported "
                f"{r['reported_on']} via {r['source']}") if r else "missing"
    return "unknown citation"
