"""MCP client: spawns the business server over stdio, discovers tools, calls them."""
from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def server_params(outbox: str | None = None) -> StdioServerParameters:
    env = {"PYTHONPATH": os.pathsep.join(filter(None, [os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                         os.environ.get("PYTHONPATH")]))}
    if outbox:
        env["LOG_ON_OUTBOX"] = outbox
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
                 egress_filter: Callable[[str], str] | None = None) -> dict[str, Any]:
    async with stdio_client(server_params(outbox)) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = {t.name: t.annotations for t in listed.tools}
            results = []
            for name, args in calls:
                if name not in tools:
                    results.append({"tool": name, "args": args, "ok": False, "output": f"tool '{name}' not offered by server"})
                    continue
                args = _resolve(args, results)
                ann = tools[name]
                if egress_filter and ann is not None and ann.open_world_hint:
                    # Data leaving the system (email/report) is scrubbed before the call, not after.
                    args = {k: egress_filter(v) if isinstance(v, str) and k not in EGRESS_EXEMPT else v
                            for k, v in args.items()}
                res = await session.call_tool(name, args)
                results.append({"tool": name, "args": args, "ok": not res.is_error, "output": _text(res)})
            return {"tools": tools, "results": results}


def call_tools(calls: list[tuple[str, dict]], outbox: str | None = None,
               egress_filter: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Synchronous wrapper so LangGraph sync nodes can use it."""
    return asyncio.run(_acall(calls, outbox, egress_filter))


def list_tools(outbox: str | None = None) -> dict[str, Any]:
    return asyncio.run(_acall([], outbox))["tools"]
