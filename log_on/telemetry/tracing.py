"""Local-first OpenTelemetry tracing (roadmap ticket O-2).

One trace per graph invocation (`run.start` / `run.resume`), one child span per node execution, one
grand-child span per MCP tool call (real start/end times measured in the client) and span events for
routing decisions. Exporters are chosen with LOG_ON_TRACE_EXPORTERS (comma-separated):

  jsonl    (default) one JSON object per span in <runs>/traces.jsonl, read by `python -m log_on.trace`
  console  pretty JSON to stderr (OTel ConsoleSpanExporter)
  otlp     OTLP/HTTP to OTEL_EXPORTER_OTLP_ENDPOINT (Jaeger, Tempo, an OTel collector, Phoenix ...)
  langfuse OTLP/HTTP to a (self-hosted) Langfuse: LANGFUSE_HOST + LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY
  none     disable

Every string attribute is passed through the PII redactor when it is set (and again by the JSONL exporter), so traces are as safe to
share as the audit log. Each Orchestrator owns its own TracerProvider (no global state), which keeps
tests isolated and lets two orchestrators write to different files.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor,
                                            SpanExporter, SpanExportResult)
from opentelemetry.trace import Status, StatusCode

SERVICE = "log_on"


def _hex(i: int | None, width: int) -> str | None:
    return None if i is None else format(i, f"0{width}x")


class JsonlSpanExporter(SpanExporter):
    """Append finished spans to a JSONL file (thread-safe, redacted)."""

    def __init__(self, path: str | os.PathLike, redact: Callable[[str], str] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._redact = redact or (lambda s: s)
        self._lock = threading.Lock()

    def _clean(self, v: Any) -> Any:
        if isinstance(v, str):
            return self._redact(v)
        if isinstance(v, (list, tuple)):
            return [self._clean(x) for x in v]
        return v

    def to_dict(self, s: ReadableSpan) -> dict:
        ctx = s.get_span_context()
        return {
            "trace_id": _hex(ctx.trace_id, 32), "span_id": _hex(ctx.span_id, 16),
            "parent_span_id": _hex(s.parent.span_id, 16) if s.parent else None,
            "name": s.name, "kind": s.kind.name,
            "start_ns": s.start_time, "end_ns": s.end_time,
            "duration_ms": round(((s.end_time or 0) - (s.start_time or 0)) / 1e6, 3),
            "status": s.status.status_code.name, "status_description": s.status.description,
            "attributes": {k: self._clean(v) for k, v in (s.attributes or {}).items()},
            "events": [{"name": e.name, "ts_ns": e.timestamp,
                        "attributes": {k: self._clean(v) for k, v in (e.attributes or {}).items()}} for e in s.events],
            "service": (s.resource.attributes or {}).get("service.name", SERVICE),
        }

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = [json.dumps(self.to_dict(s), default=str) for s in spans]
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # nothing buffered
        return None


def _otlp_exporter(endpoint: str | None = None, headers: dict | None = None) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    return OTLPSpanExporter(endpoint=endpoint, headers=headers) if endpoint else OTLPSpanExporter(headers=headers)


def _langfuse_exporter() -> SpanExporter:
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not (pk and sk):
        raise RuntimeError("langfuse exporter needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    return _otlp_exporter(f"{host}/api/public/otel/v1/traces", {"Authorization": f"Basic {auth}"})


def exporter_names() -> list[str]:
    raw = os.environ.get("LOG_ON_TRACE_EXPORTERS", "jsonl")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


class RunTracer:
    """Thin wrapper around an OTel tracer with LOG_ON span conventions."""

    def __init__(self, path: str | os.PathLike | None = "runs/traces.jsonl", exporters: list[str] | None = None,
                 redact: Callable[[str], str] | None = None, service: str = SERVICE):
        self.redact = redact or (lambda s: s)
        self.path = Path(path) if path else None
        # log_on/__init__.py sets OTEL_SDK_DISABLED=true so third-party libraries (guardrails-ai) can't export
        # telemetry. Our provider is private (never registered globally) and only exports where configured,
        # so it is constructed with that switch lifted for the duration of __init__.
        saved = os.environ.pop("OTEL_SDK_DISABLED", None)
        try:
            self.provider = TracerProvider(resource=Resource.create({"service.name": service}))
        finally:
            if saved is not None:
                os.environ["OTEL_SDK_DISABLED"] = saved
        self.exporters: list[str] = []
        self.errors: list[str] = []
        for name in (exporters if exporters is not None else exporter_names()):
            try:
                if name == "none":
                    continue
                if name == "jsonl" and self.path is not None:
                    self.provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(self.path, self.redact)))
                elif name == "console":
                    self.provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
                elif name == "otlp":
                    self.provider.add_span_processor(BatchSpanProcessor(_otlp_exporter()))
                elif name == "langfuse":
                    self.provider.add_span_processor(BatchSpanProcessor(_langfuse_exporter()))
                else:
                    continue
                self.exporters.append(name)
            except Exception as exc:  # noqa: BLE001 - tracing must never break a run
                self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
        self.tracer = self.provider.get_tracer("log_on.orchestrator")
        self._root: trace.Span | None = None

    # -- attribute helpers ------------------------------------------------------------------------
    def _attr(self, v: Any) -> Any:
        if isinstance(v, bool) or isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            return self.redact(v)[:2000]
        if isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float, bool)) for x in v):
            return [self._attr(x) for x in v]
        return self.redact(json.dumps(v, default=str))[:2000]

    def set(self, span: trace.Span, **attrs: Any) -> None:
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k if k.startswith("log_on.") else f"log_on.{k}", self._attr(v))

    # -- spans ------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def run_span(self, run_id: str, kind: str, **attrs: Any) -> Iterator[trace.Span]:
        # Explicit empty Context: a fresh trace even if another library left a (non-sampled) span current.
        with self.tracer.start_as_current_span(f"run.{kind}", context=Context()) as span:
            self.set(span, run_id=run_id, **attrs)
            prev, self._root = self._root, span
            try:
                yield span
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
                raise
            finally:
                self._root = prev

    @contextlib.contextmanager
    def node_span(self, node: str, run_id: str, **attrs: Any) -> Iterator[trace.Span]:
        ctx = trace.set_span_in_context(self._root if self._root is not None else trace.INVALID_SPAN, Context())
        with self.tracer.start_as_current_span(f"node.{node}", context=ctx,
                                               record_exception=False, set_status_on_exception=False) as span:
            self.set(span, run_id=run_id, node=node, **attrs)
            yield span

    def tool_span(self, tool: str, run_id: str, start_ns: int | None, end_ns: int | None, **attrs: Any) -> None:
        """Record an MCP tool call that already happened (times measured by the client)."""
        span = self.tracer.start_span(f"tool.{tool}", start_time=start_ns)  # parent = current node span
        self.set(span, run_id=run_id, tool=tool, **attrs)
        if attrs.get("ok") is False:
            span.set_status(Status(StatusCode.ERROR, "tool returned an error"))
        span.end(end_time=end_ns)

    def decision(self, name: str, **attrs: Any) -> None:
        """A routing / policy decision as an event on the current run span."""
        if self._root is not None:
            self._root.add_event(name, {k: self._attr(v) for k, v in attrs.items() if v is not None})

    def flush(self) -> None:
        self.provider.force_flush()

    def shutdown(self) -> None:
        self.provider.shutdown()
