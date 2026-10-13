"""The eval harness itself, driven by a scripted model on recorded weather."""

import functools

import yaml
from langchain_core.messages import AIMessage

from shamba_desk import evals
from shamba_desk.agent import run_agent

from .fakes import ScriptedLLM, tool_call


async def test_unknown_cluster_task_passes_its_checks(tmp_path, monkeypatch):
    script = ['{"steps": ["look up the cluster"]}',
              AIMessage("", tool_calls=[tool_call("list_clusters", {})]),
              "CL-NAK is not one of our clusters; nothing drafted."]
    monkeypatch.setattr(evals, "run_agent",
                        functools.partial(run_agent, llm=ScriptedLLM(script=script)))
    tasks = yaml.safe_load((evals.EVAL_DIR / "tasks.yaml").read_text())
    task = next(t for t in tasks if t["id"].startswith("T08"))
    result = await evals.run_task(task, 1, tmp_path)
    assert result["passed"], result["checks"]


def test_retry_after_is_read_from_the_provider_message():
    assert evals._retry_after_s("Please try again in 41m27.888s. Need more") == 41 * 60 + 27.888 + 60
    assert evals._retry_after_s("try again in 1h2m3s") == 3723 + 60
    assert evals._retry_after_s("try again in 9.5s") == 69.5
    assert evals._retry_after_s("no hint") == 1800.0


def test_daily_quota_waits_until_a_whole_run_fits():
    msg = ("tokens per day (TPD): Limit 200000, Used 197801, Requested 3356. "
           "Please try again in 8m19.824s.")
    per_s = (3356 - 2199) / 499.824
    expected = (40000 - 2199) / per_s + 60
    assert abs(evals._retry_after_s(msg, 40000) - expected) < 1
    assert 4 * 3600 < expected < 5 * 3600  # ~4.5 h, not 8 min
    assert evals._retry_after_s(msg) == 499.824 + 60  # per-minute limits: use the hint
