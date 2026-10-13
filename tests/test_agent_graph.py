"""End-to-end graph test with a scripted model and the real extension-desk MCP
server. Weather is replayed from the cassette; the live variant needs network
(npx fetches the Open-Meteo MCP server, which calls Open-Meteo)."""

import pytest
from langchain_core.messages import AIMessage

from shamba_desk import officer
from shamba_desk.agent import run_agent
from shamba_desk.config import LLMConfig

from .fakes import ScriptedLLM, call_id, tool_call

CFG = LLMConfig("scripted", "", "scripted", "", 0.0, 0.0, 0.0)
OTHERS = ["HH-001", "HH-002", "HH-003", "HH-004", "HH-005", "HH-006", "HH-008", "HH-009",
          "HH-010"]


def draft(households, extra_cites=()):
    def reply(messages):
        w = call_id(messages, "weather_forecast")
        return AIMessage("", tool_calls=[tool_call("draft_advisory", {
            "cluster_id": "CL-KHA", "summary": "Short rains week advisory",
            "items": [{"household_ids": households,
                       "action": "Keep weeding and scout maize whorls for armyworm twice a week.",
                       "reason": "Rain is steady this week.",
                       "citations": households + [w, *extra_cites]}]})])
    return reply


@pytest.mark.parametrize("weather", ["replay", pytest.param("live", marks=pytest.mark.network)])
async def test_agent_repairs_after_verifier_and_stops_at_gate(conn, monkeypatch, weather):
    monkeypatch.setenv("SHAMBA_WEATHER", weather)
    script = [
        '{"steps": ["read households", "check rain", "draft"]}',
        AIMessage("", tool_calls=[
            tool_call("get_cluster_households", {"cluster_id": "CL-KHA"}),
            tool_call("weather_forecast", {"latitude": 0.59, "longitude": 34.58,
                                           "daily": ["precipitation_sum"], "past_days": 14,
                                           "forecast_days": 7, "timezone": "Africa/Nairobi"}),
        ]),
        draft(["HH-001"]),            # incomplete on purpose
        "Drafted.",                   # verifier should push back here
        AIMessage("", tool_calls=[tool_call("schedule_followup", {
            "household_id": "HH-007", "reason": "No plot record", "due_in_days": 7,
            "citations": ["HH-007"]})]),
        draft(OTHERS),
        "Done: advisory drafted for officer review.",
    ]
    decisions = []

    def decide(info):
        decisions.append(info)
        return {"action": "approve", "officer": "Test Officer", "note": "ok"}

    summary = await run_agent("CL-KHA", cfg=CFG, llm=ScriptedLLM(script=script), decide=decide)

    assert summary["repairs"] == 1, summary
    assert summary["status"] == "drafted -> approved", summary
    assert decisions and decisions[0]["advisory_id"] == summary["advisory_id"]
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    adv = conn.execute("SELECT * FROM advisories WHERE id = ?",
                       (summary["advisory_id"],)).fetchone()
    assert adv["decided_by"] == "Test Officer"
    # the first, incomplete draft was superseded, not deleted
    assert conn.execute("SELECT COUNT(*) FROM advisories WHERE status = 'superseded'"
                        ).fetchone()[0] == 1
    # every tool call and model call is in the audit log for this run
    rows = conn.execute("SELECT tool, ok FROM audit_log WHERE run_id = ?",
                        (summary["run_id"],)).fetchall()
    assert {"plan", "act", "weather_forecast", "draft_advisory"} <= {r["tool"] for r in rows}
    assert officer.queue_for_delivery(conn, summary["advisory_id"], "Test Officer")["queued"] == 9


async def test_unknown_cluster_stops_without_drafting(conn):
    script = ['{"steps": ["look up cluster"]}',
              AIMessage("", tool_calls=[tool_call("get_cluster_households",
                                                  {"cluster_id": "CL-XXX"})]),
              "That cluster does not exist; nothing drafted."]
    summary = await run_agent("CL-XXX", cfg=CFG, llm=ScriptedLLM(script=script))
    assert summary["status"] == "stopped_unknown_cluster"
    assert conn.execute("SELECT COUNT(*) FROM advisories").fetchone()[0] == 0
