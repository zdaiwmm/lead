"""Metadata-only telemetry. Prompts, cookies, raw URLs and form fields are excluded."""

import json
import os
import re
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from functools import wraps

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .config import STATE, env

SAFE_ATTRIBUTES = {
    "http.request.method",
    "http.route",
    "http.response.status_code",
    "lead.operation",
    "error.type",
    "gen_ai.provider.name",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
}
TRACEPARENT = re.compile(r"^00-[a-f0-9]{32}-[a-f0-9]{16}-[a-f0-9]{2}$")


class MetadataFileExporter(SpanExporter):
    def __init__(self, service):
        self.path = STATE / "telemetry" / (service + ".jsonl")
        self.lock = threading.Lock()

    def export(self, spans):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.lock:
                if self.path.exists() and self.path.stat().st_size > 10 * 1024**2:
                    previous = self.path.with_suffix(".previous.jsonl")
                    os.replace(self.path, previous)
                with self.path.open("a") as stream:
                    os.chmod(self.path, 0o600)
                    for span in spans:
                        stream.write(
                            json.dumps(
                                {
                                    "name": span.name[:180],
                                    "trace_id": format(span.context.trace_id, "032x"),
                                    "span_id": format(span.context.span_id, "016x"),
                                    "parent_id": format(span.parent.span_id, "016x")
                                    if span.parent
                                    else None,
                                    "start_ns": span.start_time,
                                    "end_ns": span.end_time,
                                    "status": span.status.status_code.name,
                                    "attributes": {
                                        k: v
                                        for k, v in (span.attributes or {}).items()
                                        if k in SAFE_ATTRIBUTES
                                    },
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
            return SpanExportResult.SUCCESS
        except OSError:
            return SpanExportResult.FAILURE


_providers = {}
_lock = threading.Lock()


def provider(service):
    with _lock:
        if service not in _providers:
            current = TracerProvider(
                resource=Resource.create({"service.name": service})
            )
            current.add_span_processor(
                BatchSpanProcessor(
                    MetadataFileExporter(service),
                    max_queue_size=1024,
                    schedule_delay_millis=1000,
                    max_export_batch_size=128,
                )
            )
            endpoint = env().get("LEAD_OTLP_TRACES_ENDPOINT")
            if endpoint:
                from urllib.parse import urlsplit

                value = urlsplit(endpoint)
                local = env().get(
                    "LEAD_DEPLOYMENT_MODE"
                ) != "production" and value.hostname in ("127.0.0.1", "localhost")
                if (
                    value.username
                    or value.password
                    or value.scheme != "https"
                    and not (local and value.scheme == "http")
                ):
                    raise ValueError("遥测接收地址不可用")
                current.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5))
                )
            _providers[service] = current
        return _providers[service]


@contextmanager
def span(operation, service="lead-worker", parent=None, attributes=None):
    context = (
        TraceContextTextMapPropagator().extract({"traceparent": parent})
        if parent and TRACEPARENT.fullmatch(parent)
        else None
    )
    with (
        provider(service)
        .get_tracer("lead")
        .start_as_current_span(
            operation,
            context=context,
            record_exception=False,
            set_status_on_exception=False,
            attributes={
                k: v for k, v in (attributes or {}).items() if k in SAFE_ATTRIBUTES
            },
        ) as current
    ):
        try:
            yield current
        except BaseException as error:
            current.set_status(Status(StatusCode.ERROR))
            current.set_attribute("error.type", type(error).__name__)
            raise


def trace_parent():
    carrier = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier.get("traceparent")


def trace_headers():
    parent = trace_parent()
    return {"traceparent": parent} if parent else {}


def trace_id():
    context = trace.get_current_span().get_span_context()
    return (
        format(context.trace_id, "032x")
        if context.is_valid
        else __import__("uuid").uuid4().hex
    )


def flush():
    return all(p.force_flush(timeout_millis=5000) for p in list(_providers.values()))


def traced(operation):
    def decorate(fn):
        @wraps(fn)
        async def run(*args, **kwargs):
            with span(operation, attributes={"lead.operation": operation}):
                return await fn(*args, **kwargs)

        return run

    return decorate


class HTTPMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = defaultdict(
            lambda: {"count": 0, "sum": 0.0, "buckets": defaultdict(int)}
        )
        self.bounds = (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 15.0, 60.0)

    def observe(self, method, route, status, elapsed):
        method = (
            method
            if method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
            else "OTHER"
        )
        # Labels are route templates. Arbitrary paths cannot grow the label set.
        if route == "/api/events":
            return
        key = (method, route, str(status))
        with self.lock:
            row = self.rows[key]
            row["count"] += 1
            row["sum"] += elapsed
            for bound in self.bounds:
                if elapsed <= bound:
                    row["buckets"][bound] += 1

    def render(self):
        rows = [
            "# TYPE lead_http_requests_total counter",
            "# TYPE lead_http_duration_seconds histogram",
        ]
        with self.lock:
            for (method, route, status), value in self.rows.items():
                labels = (
                    "method="
                    + json.dumps(method)
                    + ",route="
                    + json.dumps(route)
                    + ",status="
                    + json.dumps(status)
                )
                rows.append(f"lead_http_requests_total{{{labels}}} {value['count']}")
                rows.append(
                    f"lead_http_duration_seconds_sum{{{labels}}} {value['sum']}"
                )
                rows.append(
                    f"lead_http_duration_seconds_count{{{labels}}} {value['count']}"
                )
                for bound in self.bounds:
                    rows.append(
                        f'lead_http_duration_seconds_bucket{{{labels},le="{bound}"}} {value["buckets"][bound]}'
                    )
                rows.append(
                    f'lead_http_duration_seconds_bucket{{{labels},le="+Inf"}} {value["count"]}'
                )
        return "\n".join(rows) + "\n"


HTTP = HTTPMetrics()


class TelemetryMiddleware:
    def __init__(self, app, service):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status = 500
        headers = dict(scope.get("headers", []))
        parent = headers.get(b"traceparent", b"").decode("ascii", errors="ignore")[:100]
        method = (
            scope["method"]
            if scope["method"]
            in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
            else "OTHER"
        )
        with span("HTTP", self.service, parent=parent) as current:

            async def observed(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    current.update_name(
                        method + " " + getattr(scope.get("route"), "path", "unmatched")
                    )
                    current.set_attributes(
                        {
                            "http.request.method": method,
                            "http.route": getattr(
                                scope.get("route"), "path", "unmatched"
                            ),
                            "http.response.status_code": status,
                        }
                    )
                    if status >= 500:
                        current.set_status(Status(StatusCode.ERROR))
                await send(message)

            try:
                await self.app(scope, receive, observed)
            finally:
                HTTP.observe(
                    scope["method"],
                    getattr(scope.get("route"), "path", "unmatched"),
                    status,
                    time.monotonic() - started,
                )
