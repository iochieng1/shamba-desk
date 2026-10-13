"""ToolHub: one place where the agent's tool calls go through.

It connects to both MCP servers (ours and the borrowed Open-Meteo one),
shows the model a small, curated set of tools, and wraps every call with:
an audit-log row (inputs, outputs, timestamps), a CALL-<n> evidence id the
model can cite, a timeout, one retry for transient upstream failures, and
optional fault injection for evals (SHAMBA_FAULTS). With SHAMBA_WEATHER=replay
the weather tool answers from a recorded cassette instead (see weather.py).
"""

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import weather
from .db import ROOT, connect, db_path, finish_call, log_call

OPEN_METEO_PACKAGE = os.environ.get("SHAMBA_OPEN_METEO_PKG", "open-meteo-mcp-server@2.4.2")
CALL_TIMEOUT_S = 60
MAX_RESULT_CHARS = 7000
HIDDEN_ARGS = {"run_id"}  # filled by the hub, never by the model

# The borrowed weather tool's real schema is ~10 KB of enums. A small model
# does better with a projection: same tool, same server, fewer knobs.
RAIN_VARS = ["precipitation_sum", "rain_sum", "precipitation_probability_max",
             "precipitation_hours", "et0_fao_evapotranspiration",
             "temperature_2m_max", "temperature_2m_min"]
WEATHER_FORECAST_SCHEMA = {
    "type": "object",
    "properties": {
        "latitude": {"type": "number"},
        "longitude": {"type": "number"},
        "daily": {"type": "array", "items": {"type": "string", "enum": RAIN_VARS},
                  "description": "Daily variables, e.g. ['precipitation_sum']."},
        "past_days": {"type": "integer", "minimum": 0, "maximum": 92,
                      "description": "Include this many recent days (modelled observations)."},
        "forecast_days": {"type": "integer", "minimum": 1, "maximum": 16},
        "timezone": {"type": "string", "description": "Use 'Africa/Nairobi'."},
    },
    "required": ["latitude", "longitude", "daily"],
}
BORROWED_TOOLS = {
    "weather_forecast": {
        "schema": WEATHER_FORECAST_SCHEMA,
        "description": "Daily rainfall and temperature for a point: recent days (past_days) "
                       "and forecast (forecast_days). Source: Open-Meteo (CC BY 4.0).",
    },
}


def _slim(schema):
    """Drop schema keys a model does not need (pydantic adds a title to every field)."""
    if isinstance(schema, dict):
        return {k: _slim(v) for k, v in schema.items() if k not in ("title", "default")
                or isinstance(v, dict) and k == "title"}
    if isinstance(schema, list):
        return [_slim(v) for v in schema]
    return schema


def _compact_weather(text: str) -> str:
    """Keep the rainfall series and its units; drop server metadata."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    keep = ("latitude", "longitude", "timezone", "daily_units", "daily")
    return json.dumps({k: data[k] for k in keep if k in data}, separators=(",", ":"))


@dataclass
class Fault:
    mode: str  # "once" or "always"
    fired: int = 0


@dataclass
class ToolHub:
    run_id: str
    tools: dict = field(default_factory=dict)  # name -> (server, session, spec)
    faults: dict = field(default_factory=dict)
    cassette: dict | None = None
    _stack: AsyncExitStack | None = None

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        env = {**os.environ, "SHAMBA_DB": str(db_path())}
        desk = StdioServerParameters(command=sys.executable,
                                     args=["-m", "shamba_desk.server"], env=env)
        meteo = StdioServerParameters(command="npx", args=["-y", OPEN_METEO_PACKAGE], env=env)
        (ROOT / "var").mkdir(exist_ok=True)
        errlog = self._stack.enter_context(open(ROOT / "var" / "mcp-servers.log", "a"))
        servers = [("extension-desk", desk), ("open-meteo", meteo)]
        if weather.mode() == "replay":
            self.cassette = weather.load()
            servers.pop()
            for tool in BORROWED_TOOLS:
                self.tools[tool] = ("open-meteo", None, self._borrowed_spec(tool))
        for name, params in servers:
            read, write = await self._stack.enter_async_context(stdio_client(params, errlog))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            for tool in (await session.list_tools()).tools:
                spec = self._spec(name, tool)
                if spec:
                    self.tools[tool.name] = (name, session, spec)
        for part in filter(None, os.environ.get("SHAMBA_FAULTS", "").split(",")):
            tool, _, mode = part.partition(":")
            self.faults[tool.strip()] = Fault(mode.strip() or "once")
        self.conn = connect()
        return self

    async def __aexit__(self, *exc):
        self.conn.close()
        await self._stack.aclose()

    @staticmethod
    def _spec(server: str, tool) -> dict | None:
        if server == "open-meteo":
            if tool.name not in BORROWED_TOOLS:
                return None  # 16 other tools exist; the agent does not need them
            return ToolHub._borrowed_spec(tool.name)
        schema = _slim(json.loads(json.dumps(tool.inputSchema)))
        for arg in HIDDEN_ARGS:
            schema.get("properties", {}).pop(arg, None)
            if arg in schema.get("required", []):
                schema["required"].remove(arg)
        return {"type": "function", "function": {"name": tool.name,
                                                 "description": " ".join(
                                                     (tool.description or "").split()),
                                                 "parameters": schema}}

    @staticmethod
    def _borrowed_spec(name: str) -> dict:
        borrowed = BORROWED_TOOLS[name]
        return {"type": "function",
                "function": {"name": name, "description": borrowed["description"],
                             "parameters": borrowed["schema"]}}

    def specs(self) -> list[dict]:
        return [spec for _, _, spec in self.tools.values()]

    def server_of(self, name: str) -> str | None:
        return self.tools[name][0] if name in self.tools else None

    async def call(self, name: str, args: dict) -> tuple[bool, str, int]:
        """Run one tool call. Returns (ok, text for the model, call id)."""
        server = self.server_of(name) or "unknown"
        call_id = log_call(self.conn, self.run_id, "agent", server, name, args)
        tag = f"CALL-{call_id}"
        if name not in self.tools:
            msg = f"Unknown tool {name!r}. Available: {sorted(self.tools)}."
            finish_call(self.conn, call_id, False, error=msg)
            return False, f"ERROR [{tag}] {msg}", call_id
        _, session, _ = self.tools[name]
        if server == "extension-desk" and name in ("draft_advisory", "schedule_followup",
                                                  "propose_input_request"):
            args = {**args, "run_id": self.run_id}

        error, text = None, ""
        for attempt in (1, 2):
            fault = self.faults.get(name)
            try:
                if fault and (fault.mode == "always" or fault.fired == 0):
                    fault.fired += 1
                    raise TimeoutError("injected fault: upstream did not respond")
                if session is None:  # replayed weather
                    try:
                        text, error = weather.replay(args, self.cassette), None
                    except weather.WeatherReplayError as e:
                        text = error = str(e)
                    break
                result = await asyncio.wait_for(session.call_tool(name, args), CALL_TIMEOUT_S)
                text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
                error = text if result.isError else None
                break  # a tool-level error is the model's to fix; do not retry it
            except (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError) as e:
                error = f"{type(e).__name__}: {e}"
                if attempt == 1 and fault is None:
                    await asyncio.sleep(1.5)
                    continue
                break
        if error:
            finish_call(self.conn, call_id, False, error=error)
            return False, (f"ERROR [{tag}] {error}\n(This call failed and cannot be cited. "
                           f"Fix the arguments, try another source, or record the gap.)"), call_id
        finish_call(self.conn, call_id, True, result=text)
        if name in BORROWED_TOOLS:
            text = _compact_weather(text)  # the audit log keeps the full result
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n... [truncated; full result in audit log]"
        return True, f"[{tag}] (cite as {tag})\n{text}", call_id


async def record_weather(points: list[tuple[float, float]]) -> dict[str, dict]:
    """Ask the live Open-Meteo server for the widest window at each point, for
    weather.save(). Fails loudly: a cassette with holes would make evals lie."""
    params = StdioServerParameters(command="npx", args=["-y", OPEN_METEO_PACKAGE],
                                   env=dict(os.environ))
    (ROOT / "var").mkdir(exist_ok=True)
    recorded = {}
    with open(ROOT / "var" / "mcp-servers.log", "a") as errlog:
        async with (stdio_client(params, errlog) as (read, write),
                    ClientSession(read, write) as session):
            await session.initialize()
            for lat, lon in points:
                key = weather.grid_key(lat, lon)
                if key in recorded:
                    continue
                result = await asyncio.wait_for(session.call_tool(
                    "weather_forecast", weather.record_args(lat, lon, RAIN_VARS)),
                    CALL_TIMEOUT_S)
                text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
                if result.isError:
                    raise RuntimeError(f"weather_forecast failed at {key}: {text}")
                recorded[key] = json.loads(text)
    return recorded
