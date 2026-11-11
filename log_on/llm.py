"""Back-compat shim over the Phase 4 serving layer (`log_on.serving`).

`llm_settings()` / `chat_json()` keep their Phase 3 signatures, but every call now goes through
`log_on.serving.Provider`, so vLLM, llama.cpp, Ollama, OpenAI, xAI and the fake server are interchangeable.
Nothing here runs in the default offline mode (no provider configured -> rule-based components).
"""
from __future__ import annotations

from .serving import Provider, get_provider, make_provider


def llm_settings() -> dict | None:
    """Phase 3 shape ({api_key, base_url, model}) for the configured provider, or None."""
    p = get_provider()
    if p is None:
        return None
    return {"api_key": p.config.api_key, "base_url": p.config.base_url, "model": p.config.model}


def chat_json(system: str, user: str, *, api_key: str = "local", base_url: str, model: str, timeout: float = 30,
              provider: Provider | None = None) -> tuple[dict, dict]:
    prov = provider or make_provider("custom", base_url=base_url, model=model, api_key=api_key, timeout=timeout)
    return prov.chat_json(system, user)
