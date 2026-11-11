"""Minimal OpenAI-compatible JSON chat client shared by the Phase 3 verifier and reviser.

Returns (parsed_json, usage). `usage` uses the endpoint's own token counts when present, else estimates.
Only used when LOG_ON_LLM_API_KEY (or OPENAI_API_KEY) is set; nothing here runs in the default offline mode.
"""
from __future__ import annotations

import json
import os
import urllib.request

from .telemetry.cost import est_tokens


def llm_settings() -> dict | None:
    key = os.getenv("LOG_ON_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    return {"api_key": key, "base_url": os.getenv("LOG_ON_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            "model": os.getenv("LOG_ON_LLM_MODEL", "gpt-4o-mini")}


def chat_json(system: str, user: str, *, api_key: str, base_url: str, model: str, timeout: float = 30) -> tuple[dict, dict]:
    body = json.dumps({"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(f"{base_url}/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    content = data["choices"][0]["message"]["content"]
    u = data.get("usage") or {}
    usage = {"model": model, "calls": 1,
             "prompt_tokens": int(u.get("prompt_tokens") or est_tokens(system + user)),
             "completion_tokens": int(u.get("completion_tokens") or est_tokens(content)),
             "estimated": not bool(u)}
    return json.loads(content), usage
