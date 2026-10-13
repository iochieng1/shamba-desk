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
