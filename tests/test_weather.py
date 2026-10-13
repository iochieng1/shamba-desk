"""Weather record/replay: the cassette answers like the live server would."""

import json
from datetime import date

import pytest

from shamba_desk import weather
from shamba_desk.hub import ToolHub

DAYS = ["2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
CASSETTE = {"recorded_on": "2026-10-13", "points": {"0.6,34.6": {
    "latitude": 0.5975, "longitude": 34.5611, "timezone": "Africa/Nairobi",
    "generationtime_ms": 3.1,
    "daily_units": {"time": "iso8601", "precipitation_sum": "mm", "rain_sum": "mm"},
    "daily": {"time": DAYS, "precipitation_sum": [1.0, 2.0, 3.0, 4.0, 5.0],
              "rain_sum": [0.5, 1.5, 2.5, 3.5, 4.5]}}}}
KHALABA = {"latitude": 0.5905, "longitude": 34.5822, "daily": ["precipitation_sum"]}


def replay(args, today="2026-10-13"):
    return json.loads(weather.replay(args, CASSETTE, today=date.fromisoformat(today)))


def test_replay_cuts_the_requested_window_and_variables():
    out = replay({**KHALABA, "past_days": 2, "forecast_days": 2})
    assert out["daily"] == {"time": DAYS[0:4], "precipitation_sum": [1.0, 2.0, 3.0, 4.0]}
    assert set(out["daily_units"]) == {"time", "precipitation_sum"}
    assert "generationtime_ms" not in out


def test_replay_defaults_match_open_meteo():
    out = replay({**KHALABA, "forecast_days": 3})  # past_days defaults to 0
    assert out["daily"]["time"] == DAYS[2:5]


def test_replay_shifts_dates_to_today():
    out = replay({**KHALABA, "past_days": 1, "forecast_days": 1}, today="2026-11-01")
    assert out["daily"] == {"time": ["2026-10-31", "2026-11-01"], "precipitation_sum": [2.0, 3.0]}


@pytest.mark.parametrize("args, message", [
    ({"latitude": 1.2, "longitude": 35.0, "daily": ["rain_sum"]}, "No recorded weather"),
    ({**KHALABA, "daily": ["snowfall_sum"]}, "Unknown daily variables"),
    ({**KHALABA, "past_days": 93}, "past_days must be"),
    ({"daily": ["rain_sum"]}, "latitude and longitude"),
])
def test_replay_errors_read_like_tool_errors(args, message):
    with pytest.raises(weather.WeatherReplayError, match=message):
        weather.replay(args, CASSETTE, today=date(2026, 10, 13))


def test_grid_key_matches_the_servers_rounding():
    assert weather.grid_key(0.5905, 34.5822) == "0.6,34.6"
    assert weather.grid_key(0.5512, 34.5243) == "0.6,34.5"


def test_committed_cassette_covers_every_cluster(conn):
    from shamba_desk.desk import list_clusters
    cassette = weather.load()
    for c in list_clusters(conn):
        assert weather.grid_key(c["lat"], c["lon"]) in cassette["points"], c["id"]


async def test_hub_replays_weather_with_audit_and_faults(conn, monkeypatch):
    monkeypatch.setenv("SHAMBA_FAULTS", "")
    async with ToolHub("R-test") as hub:
        assert "weather_forecast" in {s["function"]["name"] for s in hub.specs()}
        ok, text, call_id = await hub.call("weather_forecast", {**KHALABA, "past_days": 14})
        assert ok and text.startswith(f"[CALL-{call_id}]")
        ok, text, _ = await hub.call("weather_forecast", {**KHALABA, "latitude": 3.0})
        assert not ok and "No recorded weather" in text
    rows = conn.execute("SELECT server, ok FROM audit_log WHERE run_id = 'R-test'").fetchall()
    assert [(r["server"], r["ok"]) for r in rows] == [("open-meteo", 1), ("open-meteo", 0)]

    monkeypatch.setenv("SHAMBA_FAULTS", "weather_forecast:always")
    async with ToolHub("R-fault") as hub:
        ok, text, _ = await hub.call("weather_forecast", KHALABA)
        assert not ok and "injected fault" in text
