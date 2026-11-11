"""MCP client: spawns the business server over stdio, discovers tools, calls them."""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from typing import Any, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .signals import failed


# Graph/as-of settings are forwarded to the server process; nothing else from the parent env leaks in.
FORWARD_ENV_PREFIXES = ("LOG_ON_GRAPH_", "LOG_ON_AS_OF", "NEO4J_")


def server_params(outbox: str | None = None, extra_env: dict[str, str] | None = None) -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if k.startswith(FORWARD_ENV_PREFIXES)}
    env |= {"PYTHONPATH": os.pathsep.join(filter(None, [os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                         os.environ.get("PYTHONPATH")]))}
    if outbox:
        env["LOG_ON_OUTBOX"] = outbox
    env |= {k: str(v) for k, v in (extra_env or {}).items()}
    return StdioServerParameters(command=sys.executable, args=["-m", "log_on.mcp_server.business_server"], env=env)


def _text(result) -> str:
    parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    return "\n".join(parts)


# Addressing fields must reach the tool intact; everything else is scrubbed on egress.
EGRESS_EXEMPT = {"recipient"}

_TEMPLATE = re.compile(r"\{\{\s*steps\[(\d+)\]\.output\s*\}\}")


def _resolve(args: dict, prior: list[dict]) -> dict:
    """Replace "{{steps[N].output}}" placeholders with earlier step outputs."""
    def sub(v):
        if isinstance(v, str):
            return _TEMPLATE.sub(lambda m: prior[int(m.group(1))]["output"] if int(m.group(1)) < len(prior) else "", v)
        return v
    return {k: sub(v) for k, v in args.items()}


async def _acall(calls: list[tuple[str, dict]], outbox: str | None,
                 egress_filter: Callable[[str], str] | None = None,
                 extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    async with stdio_client(server_params(outbox, extra_env)) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = {t.name: t.annotations for t in listed.tools}
            results = []
            for name, args in calls:
                if name not in tools:
                    results.append({"tool": name, "args": args, "ok": False, "output": f"tool '{name}' not offered by server"})
                    continue
                deps = [int(m.group(1)) for v in args.values() if isinstance(v, str) for m in _TEMPLATE.finditer(v)]
                bad = [i for i in deps if i < len(results) and failed(results[i])]
                if bad:
                    # Fail fast: never feed an error message into a later step (e.g. email a "No report named" body).
                    results.append({"tool": name, "args": args, "ok": False, "skipped": True,
                                    "output": f"skipped: depends on step {bad[0]}, which failed"})
                    continue
                args = _resolve(args, results)
                ann = tools[name]
                if egress_filter and ann is not None and ann.open_world_hint:
                    # Data leaving the system (email/report) is scrubbed before the call, not after.
                    args = {k: egress_filter(v) if isinstance(v, str) and k not in EGRESS_EXEMPT else v
                            for k, v in args.items()}
                t0 = time.time_ns()
                res = await session.call_tool(name, args)
                # started_ns/ended_ns feed the Phase 3 tool spans; the orchestrator pops them before state/audit.
                results.append({"tool": name, "args": args, "ok": not res.is_error, "output": _text(res),
                                "started_ns": t0, "ended_ns": time.time_ns()})
            return {"tools": tools, "results": results}


def call_tools(calls: list[tuple[str, dict]], outbox: str | None = None,
               egress_filter: Callable[[str], str] | None = None,
               extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """Synchronous wrapper so LangGraph sync nodes can use it."""
    return asyncio.run(_acall(calls, outbox, egress_filter, extra_env))


def list_tools(outbox: str | None = None, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    return asyncio.run(_acall([], outbox, extra_env=extra_env))["tools"]
