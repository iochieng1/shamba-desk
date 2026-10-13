"""Model settings. Every provider is reached through the OpenAI-compatible API,
so switching between a local open-weights model (Ollama) and a hosted one is
an environment change, not a code change.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Prices are USD per million tokens, used only to report cost per run.
# Check them against the provider's page before quoting them.
# Local models: one slow attempt beats retries, because Ollama keeps working on
# a request the client gave up on. Groq's free tier refuses any request whose
# max_tokens exceeds its 1000 output-tokens-per-minute limit, so cap below it
# and retry 429s (the client waits for the retry-after header).
DEFAULTS = {"timeout": 180, "max_retries": 3, "max_tokens": None}
PRESETS = {
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b",
               "key_env": None, "price_in": 0.0, "price_out": 0.0,
               "timeout": 900, "max_retries": 0},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "qwen/qwen3.8-27b",
             "key_env": "GROQ_API_KEY", "price_in": 0.29, "price_out": 0.59,
             "max_retries": 8, "max_tokens": 900},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",
                   "model": "qwen/qwen-2.5-72b-instruct",
                   "key_env": "OPENROUTER_API_KEY", "price_in": 0.12, "price_out": 0.39},
}


@dataclass
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    price_in: float
    price_out: float
    temperature: float
    timeout: float = 180
    max_retries: int = 3
    max_tokens: int | None = None

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000


def load_llm_config() -> LLMConfig:
    load_dotenv()
    provider = os.environ.get("SHAMBA_LLM_PROVIDER", "ollama")
    preset = {**DEFAULTS, **PRESETS.get(provider, {"base_url": "", "model": "", "key_env": None,
                                                   "price_in": 0.0, "price_out": 0.0})}
    max_tokens = os.environ.get("SHAMBA_MAX_TOKENS", preset["max_tokens"])
    key_env = preset["key_env"]
    api_key = (os.environ.get("SHAMBA_LLM_API_KEY")
               or (os.environ.get(key_env) if key_env else None) or "not-needed")
    return LLMConfig(
        provider=provider,
        base_url=os.environ.get("SHAMBA_LLM_BASE_URL", preset["base_url"]),
        model=os.environ.get("SHAMBA_LLM_MODEL", preset["model"]),
        api_key=api_key,
        price_in=float(os.environ.get("SHAMBA_PRICE_IN_PER_M", preset["price_in"])),
        price_out=float(os.environ.get("SHAMBA_PRICE_OUT_PER_M", preset["price_out"])),
        temperature=float(os.environ.get("SHAMBA_TEMPERATURE", "0.2")),
        timeout=float(os.environ.get("SHAMBA_LLM_TIMEOUT", preset["timeout"])),
        max_retries=int(os.environ.get("SHAMBA_LLM_RETRIES", preset["max_retries"])),
        max_tokens=int(max_tokens) if max_tokens else None,
    )
