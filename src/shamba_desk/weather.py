"""Recorded weather, so eval runs see the same rainfall on any day.

SHAMBA_WEATHER picks where weather_forecast answers come from:
  live    (default) the borrowed Open-Meteo MCP server
  replay  a cassette recorded earlier; no network, no npx

`shamba-desk weather record` asks the live server once per cluster for the
widest window (92 past days, 16 forecast days, every rainfall variable).
Replay cuts each request out of that window, so it works whatever window or
variables the model asks for. Dates are shifted by the days since recording,
so "yesterday" stays yesterday, matching the seeded data (stored as days ago).
The server snaps points to a 0.1 degree grid; replay looks points up the same way.
"""

import json
import os
from datetime import date, timedelta
from pathlib import Path

from .db import ROOT

CASSETTE = ROOT / "evals" / "weather" / "cassette.json"
RECORD_PAST_DAYS = 92
RECORD_FORECAST_DAYS = 16


class WeatherReplayError(Exception):
    pass


def mode() -> str:
    m = os.environ.get("SHAMBA_WEATHER", "live").strip().lower()
    if m not in ("live", "replay"):
        raise ValueError(f"SHAMBA_WEATHER must be 'live' or 'replay', not {m!r}")
    return m


def cassette_path() -> Path:
    return Path(os.environ.get("SHAMBA_WEATHER_CASSETTE", CASSETTE))


def grid_key(lat: float, lon: float) -> str:
    return f"{round(float(lat), 1):.1f},{round(float(lon), 1):.1f}"


def record_args(lat: float, lon: float, daily: list[str]) -> dict:
    return {"latitude": lat, "longitude": lon, "daily": daily,
            "past_days": RECORD_PAST_DAYS, "forecast_days": RECORD_FORECAST_DAYS,
            "timezone": "Africa/Nairobi"}


def save(points: dict[str, dict], recorded_on: date, path: Path | None = None) -> Path:
    """points: grid key -> parsed Open-Meteo response for record_args()."""
    path = path or cassette_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"recorded_on": recorded_on.isoformat(),
            "source": "Open-Meteo via open-meteo-mcp-server (CC BY 4.0)",
            "points": points}
    path.write_text(json.dumps(body, indent=1) + "\n")
    return path


def load(path: Path | None = None) -> dict:
    path = path or cassette_path()
    if not path.exists():
        raise FileNotFoundError(f"No weather cassette at {path}. "
                                f"Record one with `shamba-desk weather record`.")
    return json.loads(path.read_text())


def replay(args: dict, cassette: dict, today: date | None = None) -> str:
    """Answer one weather_forecast call from the cassette, as the server would."""
    try:
        key = grid_key(args["latitude"], args["longitude"])
    except (KeyError, TypeError, ValueError):
        raise WeatherReplayError("latitude and longitude are required numbers")
    point = cassette["points"].get(key)
    if point is None:
        raise WeatherReplayError(f"No recorded weather near ({args['latitude']}, "
                                 f"{args['longitude']}). Recorded points: "
                                 f"{sorted(cassette['points'])}.")
    past = int(args.get("past_days", 0))
    ahead = int(args.get("forecast_days", 7))
    if not (0 <= past <= RECORD_PAST_DAYS and 1 <= ahead <= RECORD_FORECAST_DAYS):
        raise WeatherReplayError(f"past_days must be 0-{RECORD_PAST_DAYS} and forecast_days "
                                 f"1-{RECORD_FORECAST_DAYS}")
    wanted = args.get("daily") or []
    if isinstance(wanted, str):
        wanted = [wanted]
    daily = point["daily"]
    missing = [v for v in wanted if v not in daily]
    if missing:
        raise WeatherReplayError(f"Unknown daily variables {missing}. "
                                 f"Available: {sorted(k for k in daily if k != 'time')}.")

    shift = ((today or date.today()) - date.fromisoformat(cassette["recorded_on"])).days
    start = daily["time"].index(cassette["recorded_on"]) - past
    stop = start + past + ahead
    times = [(date.fromisoformat(t) + timedelta(days=shift)).isoformat()
             for t in daily["time"][start:stop]]
    out = {k: v for k, v in point.items() if k not in ("daily", "daily_units",
                                                       "generationtime_ms")}
    out["daily_units"] = {k: v for k, v in point["daily_units"].items()
                          if k == "time" or k in wanted}
    out["daily"] = {"time": times, **{v: daily[v][start:stop] for v in wanted}}
    return json.dumps(out, separators=(",", ":"))
