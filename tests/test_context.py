"""Keeping the conversation small enough for rate-limited models."""

import json

from langchain_core.messages import AIMessage, HumanMessage

from shamba_desk.agent import (OLD_PROSE_CHARS, _accepted_complete_draft, _elide_old_drafts,
                               _shorten_old_prose)
from shamba_desk.server import _households_view, _prices_view

from .fakes import tool_call


def test_only_the_latest_draft_keeps_its_arguments():
    first = AIMessage("", tool_calls=[tool_call("draft_advisory", {"items": ["a"]}, 0)])
    second = AIMessage("", tool_calls=[tool_call("draft_advisory", {"items": ["b"]}, 1)])
    out = _elide_old_drafts([HumanMessage("task"), first, second])
    assert out[1].tool_calls[0]["args"] == {"note": "earlier draft, replaced by a later call"}
    assert out[2].tool_calls[0]["args"] == {"items": ["b"]}


def test_old_commentary_is_shortened_but_the_latest_is_kept():
    long = "x" * (OLD_PROSE_CHARS * 2)
    out = _shorten_old_prose([AIMessage(long), HumanMessage("ok"), AIMessage(long)])
    assert len(out[0].content) < len(long) and out[0].content.endswith("[…]")
    assert out[2].content == long


def test_complete_draft_detection():
    done = '[CALL-9] (cite as CALL-9)\n' + json.dumps({"uncovered_households": []})
    partial = '[CALL-9] (cite as CALL-9)\n' + json.dumps({"uncovered_households": ["HH-002"]})
    assert _accepted_complete_draft("draft_advisory", True, done)
    assert not _accepted_complete_draft("draft_advisory", True, partial)
    assert not _accepted_complete_draft("draft_advisory", False, done)
    assert not _accepted_complete_draft("schedule_followup", True, done)


def test_model_views_keep_ids_and_flags_but_drop_delivery_fields(conn):
    from shamba_desk import desk
    view = _households_view(desk.get_cluster_households(conn, "CL-KHA"))
    text = json.dumps(view)
    assert "head_name" not in text and "channel" not in text and '""' not in text
    hh7 = next(h for h in view["households"] if h["id"] == "HH-007")
    assert hh7["data_flags"] and hh7["plots"] == []
    assert all(p["id"].startswith("PLOT-") for h in view["households"] for p in h["plots"])
    prices = _prices_view(desk.get_market_prices(conn, "beans", "Bungoma"))
    assert prices["unit"] == "KES/kg" and prices["trends"]
    assert set(prices["records"][0]) == {"id", "market", "date", "price_kes"}
