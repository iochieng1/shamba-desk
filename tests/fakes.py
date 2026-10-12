"""A scripted chat model so the graph can be tested without a real LLM."""

import re

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


class ScriptedLLM(BaseChatModel):
    """Returns the next scripted reply. A reply may be a callable that gets the
    messages, so it can cite CALL ids produced earlier in the run."""

    script: list
    _i: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        if callable(reply):
            reply = reply(messages)
        if isinstance(reply, str):
            reply = AIMessage(reply)
        return ChatResult(generations=[ChatGeneration(message=reply)])


def call_id(messages, tool: str) -> str:
    """The CALL-<n> id of the last successful result from `tool`."""
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == tool and not m.content.startswith("ERROR"):
            return re.search(r"CALL-\d+", m.content).group(0)
    raise AssertionError(f"no successful {tool} result in messages")


def tool_call(name: str, args: dict, n: int = 0) -> dict:
    return {"name": name, "args": args, "id": f"tc_{name}_{n}", "type": "tool_call"}
