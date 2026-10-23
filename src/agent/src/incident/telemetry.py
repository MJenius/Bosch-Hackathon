# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Read-only diagnostic tools over Prometheus, OpenSearch (PPL) and Jaeger (API v3).

Every call is bounded (time window, row limits, timeouts) and recorded as a ToolCall.
"""

import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from .models import SERVICE_NAME_PATTERN, ToolCall

MAX_LOG_ROWS = 50
MAX_TRACES = 20
MAX_SERIES = 25
MAX_RANGE_POINTS = 60

_SERVICE_RE = re.compile(SERVICE_NAME_PATTERN)
# Jaeger/OTLP trace ids: 32 lowercase hex characters.
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class TelemetryConfig:
    prometheus_url: str
    jaeger_url: str
    jaeger_public_url: str
    opensearch_url: str
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> "TelemetryConfig":
        prom = os.getenv("PROMETHEUS_URL") or f"http://{os.getenv('PROMETHEUS_HOST', 'prometheus')}:{os.getenv('PROMETHEUS_PORT', '9090')}"
        jaeger = os.getenv("JAEGER_URL") or f"http://{os.getenv('JAEGER_HOST', 'jaeger')}:{os.getenv('JAEGER_UI_PORT', '16686')}"
        osearch = os.getenv("OPENSEARCH_URL") or f"http://{os.getenv('OPENSEARCH_HOST', 'opensearch')}:{os.getenv('OPENSEARCH_PORT', '9200')}"
        return cls(
            prometheus_url=prom.rstrip("/"),
            jaeger_url=jaeger.rstrip("/"),
            jaeger_public_url=os.getenv("JAEGER_PUBLIC_URL", "http://localhost:8080").rstrip("/"),
            opensearch_url=osearch.rstrip("/"),
            timeout_s=float(os.getenv("INCIDENT_TOOL_TIMEOUT_S", "10")),
        )


def _check_service(service: str | None) -> None:
    if service is not None and not _SERVICE_RE.match(service):
        raise ValueError(f"invalid service name: {service!r}")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ppl_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _attr(attrs: list[dict], key: str):
    for a in attrs or []:
        if a.get("key") == key:
            v = a.get("value", {})
            return next(iter(v.values()), None) if v else None
    return None


def _is_error(span: dict) -> bool:
    return span.get("status", {}).get("code") in (2, "STATUS_CODE_ERROR")


MetricName = Literal["error_ratio", "request_rate", "p95_latency_ms"]

_UNITS = {"error_ratio": "ratio", "request_rate": "req/s", "p95_latency_ms": "ms"}


def promql(metric: MetricName, window_s: int, service: str | None = None) -> str:
    """Fixed PromQL templates over the collector's spanmetrics connector output."""
    _check_service(service)
    sel = 'span_kind="SPAN_KIND_SERVER"' + (f',service_name="{service}"' if service else "")
    w = f"{max(window_s, 60)}s"
    if metric == "request_rate":
        return f"sum by (service_name) (rate(traces_span_metrics_calls_total{{{sel}}}[{w}]))"
    if metric == "error_ratio":
        return (
            f'sum by (service_name) (rate(traces_span_metrics_calls_total{{{sel},status_code="STATUS_CODE_ERROR"}}[{w}]))'
            f" / sum by (service_name) (rate(traces_span_metrics_calls_total{{{sel}}}[{w}]))"
        )
    if metric == "p95_latency_ms":
        return (
            "histogram_quantile(0.95, sum by (le, service_name) "
            f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{sel}}}[{w}])))"
        )
    raise ValueError(f"unknown metric {metric}")


@dataclass
class DiagnosticToolkit:
    config: TelemetryConfig
    client: httpx.AsyncClient
    calls: list[ToolCall] = field(default_factory=list)

    async def _run(self, tool, signal, reason, arguments, query, url, fn):
        call_id = f"call-{len(self.calls) + 1}"
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        status, error, result, truncated = "ok", None, None, False
        try:
            result, truncated = await fn()
            if not result:
                status = "empty"
        except httpx.HTTPStatusError as e:
            status, error = "error", f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        except httpx.HTTPError as e:
            status, error = "error", f"{type(e).__name__}: {e}"
        except (ValueError, KeyError, TypeError) as e:
            status, error = "error", f"{type(e).__name__}: {e}"
        call = ToolCall(
            id=call_id, tool=tool, signal=signal, reason=reason, arguments=arguments,
            query=query, backend_url=url, started_at=started,
            duration_ms=round((time.perf_counter() - t0) * 1000, 1),
            status=status, error=error, result=result, truncated=truncated,
        )
        self.calls.append(call)
        return call

    async def query_metric(self, metric: MetricName, start: datetime, end: datetime,
                           service: str | None = None, reason: str = "") -> ToolCall:
        """Instant PromQL query evaluated at window end, rate over the whole window."""
        q = promql(metric, int((end - start).total_seconds()), service)
        url = f"{self.config.prometheus_url}/api/v1/query"

        async def fn():
            r = await self.client.get(url, params={"query": q, "time": end.timestamp()}, timeout=self.config.timeout_s)
            r.raise_for_status()
            body = r.json()
            if body.get("status") != "success":
                raise ValueError(body.get("error", "prometheus query failed"))
            rows = []
            for s in body["data"]["result"]:
                v = float(s["value"][1])
                if v != v:  # NaN: no traffic in window
                    continue
                rows.append({"service": s["metric"].get("service_name"), "value": v,
                             "unit": _UNITS[metric],
                             "timestamp": datetime.fromtimestamp(float(s["value"][0]), timezone.utc).isoformat()})
            rows.sort(key=lambda x: x["value"], reverse=True)
            return rows[:MAX_SERIES], len(rows) > MAX_SERIES

        return await self._run("query_metric", "metrics", reason,
                               {"metric": metric, "service": service, "start": _iso(start), "end": _iso(end)}, q, url, fn)

    async def query_metric_range(self, metric: MetricName, service: str, start: datetime, end: datetime,
                                 reason: str = "") -> ToolCall:
        """Range query for one service; returns peak value and timestamp plus a bounded series."""
        _check_service(service)
        span = (end - start).total_seconds()
        step = max(30, int(span / MAX_RANGE_POINTS))
        # Spanmetrics are exported roughly once a minute, so rate windows need several samples.
        q = promql(metric, max(300, step * 4), service)
        url = f"{self.config.prometheus_url}/api/v1/query_range"

        async def fn():
            r = await self.client.get(url, params={"query": q, "start": start.timestamp(), "end": end.timestamp(), "step": step},
                                      timeout=self.config.timeout_s)
            r.raise_for_status()
            body = r.json()
            if body.get("status") != "success":
                raise ValueError(body.get("error", "prometheus query failed"))
            series = body["data"]["result"]
            if not series:
                return None, False
            pts = [(float(t), float(v)) for t, v in series[0]["values"] if float(v) == float(v)]
            if not pts:
                return None, False
            peak = max(pts, key=lambda p: p[1])
            return {
                "service": service, "unit": _UNITS[metric], "step_s": step,
                "peak": {"value": peak[1], "timestamp": datetime.fromtimestamp(peak[0], timezone.utc).isoformat()},
                "first": {"value": pts[0][1], "timestamp": datetime.fromtimestamp(pts[0][0], timezone.utc).isoformat()},
                "last": {"value": pts[-1][1], "timestamp": datetime.fromtimestamp(pts[-1][0], timezone.utc).isoformat()},
                "points": [[datetime.fromtimestamp(t, timezone.utc).isoformat(), v] for t, v in pts[-MAX_RANGE_POINTS:]],
            }, False

        return await self._run("query_metric_range", "metrics", reason,
                               {"metric": metric, "service": service, "start": _iso(start), "end": _iso(end), "step_s": step},
                               q, url, fn)

    async def _ppl(self, ppl: str):
        r = await self.client.post(f"{self.config.opensearch_url}/_plugins/_ppl", json={"query": ppl},
                                   timeout=self.config.timeout_s)
        r.raise_for_status()
        body = r.json()
        cols = [c["name"] for c in body.get("schema", [])]
        return [dict(zip(cols, row)) for row in body.get("datarows", [])]

    def _ppl_where(self, start, end, service, min_severity):
        clauses = [f"`@timestamp` >= '{_ppl_ts(start)}'", f"`@timestamp` <= '{_ppl_ts(end)}'",
                   f"severity.number >= {int(min_severity)}"]
        if service:
            clauses.append(f"resource.service.name = '{service}'")
        return "source=otel-logs-* | where " + " and ".join(clauses)

    async def count_logs_by_service(self, start: datetime, end: datetime, min_severity: int = 17,
                                    reason: str = "") -> ToolCall:
        """Count log records at or above a severity number (17 = ERROR, 13 = WARN) per service."""
        ppl = self._ppl_where(start, end, None, min_severity) + " | stats count() as n by resource.service.name"
        url = f"{self.config.opensearch_url}/_plugins/_ppl"

        async def fn():
            rows = await self._ppl(ppl)
            out = sorted(({"service": r.get("resource.service.name"), "count": r.get("n")} for r in rows),
                         key=lambda x: x["count"] or 0, reverse=True)
            return out[:MAX_SERIES], len(out) > MAX_SERIES

        return await self._run("count_logs_by_service", "logs", reason,
                               {"start": _iso(start), "end": _iso(end), "min_severity": min_severity}, ppl, url, fn)

    async def search_logs(self, service: str | None, start: datetime, end: datetime, min_severity: int = 17,
                          limit: int = 20, reason: str = "") -> ToolCall:
        """Most recent log records for a service in the window (bounded)."""
        _check_service(service)
        limit = max(1, min(int(limit), MAX_LOG_ROWS))
        ppl = (self._ppl_where(start, end, service, min_severity)
               + " | sort - `@timestamp` | fields `@timestamp`, severity.text, resource.service.name, body, traceId, spanId"
               + f" | head {limit}")
        url = f"{self.config.opensearch_url}/_plugins/_ppl"

        async def fn():
            rows = await self._ppl(ppl)
            out = [{
                "timestamp": r.get("@timestamp"), "severity": r.get("severity.text"),
                "service": r.get("resource.service.name"), "message": str(r.get("body") or "")[:500],
                "trace_id": r.get("traceId") or None, "span_id": r.get("spanId") or None,
            } for r in rows]
            return out, len(out) >= limit

        return await self._run("search_logs", "logs", reason,
                               {"service": service, "start": _iso(start), "end": _iso(end),
                                "min_severity": min_severity, "limit": limit}, ppl, url, fn)

    async def search_traces(self, service: str, start: datetime, end: datetime, limit: int = 20,
                            errors_only: bool = False, reason: str = "") -> ToolCall:
        """Fetch recent traces touching a service via Jaeger API v3 and summarise them."""
        _check_service(service)
        limit = max(1, min(int(limit), MAX_TRACES))
        url = f"{self.config.jaeger_url}/jaeger/ui/api/v3/traces"
        params = {"query.service_name": service, "query.num_traces": limit,
                  "query.start_time_min": _iso(start), "query.start_time_max": _iso(end)}
        if errors_only:
            params["query.attributes"] = '{"error":"true"}'
        query = f"GET {url}?" + "&".join(f"{k}={v}" for k, v in params.items())

        async def fn():
            r = await self.client.get(url, params=params, timeout=self.config.timeout_s)
            if r.status_code == 404:
                return [], False
            r.raise_for_status()
            traces = summarize_traces(r.json().get("result", {}).get("resourceSpans") or [])
            for t in traces:
                t["jaeger_url"] = f"{self.config.jaeger_public_url}/jaeger/ui/trace/{t['trace_id']}"
            traces.sort(key=lambda t: (len(t["error_spans"]) == 0, -t["duration_ms"]))
            return traces, False

        return await self._run("search_traces", "traces", reason,
                               {"service": service, "start": _iso(start), "end": _iso(end), "limit": limit,
                                "errors_only": errors_only},
                               query, url, fn)

    async def get_trace(self, trace_id: str, reason: str = "") -> ToolCall:
        """Fetch a single trace by id via Jaeger API v3."""
        if not _TRACE_ID_RE.match(trace_id or ""):
            raise ValueError(f"invalid trace id: {trace_id!r}")
        url = f"{self.config.jaeger_url}/jaeger/ui/api/v3/traces/{trace_id}"

        async def fn():
            r = await self.client.get(url, timeout=self.config.timeout_s)
            if r.status_code == 404:
                return [], False
            r.raise_for_status()
            traces = summarize_traces(r.json().get("result", {}).get("resourceSpans") or [])
            for t in traces:
                t["jaeger_url"] = f"{self.config.jaeger_public_url}/jaeger/ui/trace/{t['trace_id']}"
            return traces, False

        return await self._run("get_trace", "traces", reason, {"trace_id": trace_id}, f"GET {url}", url, fn)


def summarize_traces(resource_spans: list[dict]) -> list[dict[str, Any]]:
    """Group OTLP resourceSpans by trace and derive root, duration, error spans and parent-child edges."""
    spans_by_trace: dict[str, list[dict]] = defaultdict(list)
    for rs in resource_spans:
        svc = _attr(rs.get("resource", {}).get("attributes", []), "service.name")
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                start, end = int(sp.get("startTimeUnixNano", 0)), int(sp.get("endTimeUnixNano", 0))
                spans_by_trace[sp["traceId"]].append({
                    "span_id": sp.get("spanId"), "parent_span_id": sp.get("parentSpanId") or None,
                    "service": svc, "name": sp.get("name"), "start_ns": start,
                    "duration_ms": round((end - start) / 1e6, 3), "error": _is_error(sp),
                    "status_message": sp.get("status", {}).get("message"),
                })
    out = []
    for trace_id, spans in spans_by_trace.items():
        ids = {s["span_id"]: s for s in spans}
        roots = [s for s in spans if not s["parent_span_id"] or s["parent_span_id"] not in ids]
        root = min(roots or spans, key=lambda s: s["start_ns"])
        t0 = min(s["start_ns"] for s in spans)
        t1 = max(s["start_ns"] + s["duration_ms"] * 1e6 for s in spans)
        errors = [s for s in spans if s["error"]]
        error_parents = {s["parent_span_id"] for s in errors}
        # An error span with no erroring children is the deepest point the error was observed.
        leaves = [s for s in errors if s["span_id"] not in error_parents]
        deepest = max(leaves, key=lambda s: _depth(s, ids)) if leaves else None
        edges = sorted({(ids[s["parent_span_id"]]["service"], s["service"]) for s in spans
                        if s["parent_span_id"] in ids and ids[s["parent_span_id"]]["service"] != s["service"]})
        out.append({
            "trace_id": trace_id,
            "start": datetime.fromtimestamp(t0 / 1e9, timezone.utc).isoformat(),
            "duration_ms": round((t1 - t0) / 1e6, 3),
            "root_service": root["service"], "root_operation": root["name"],
            "span_count": len(spans),
            "services": sorted({s["service"] for s in spans if s["service"]}),
            "service_edges": [list(e) for e in edges][:20],
            "error_spans": [{k: s[k] for k in ("span_id", "parent_span_id", "service", "name", "duration_ms", "status_message")}
                            for s in errors][:10],
            "deepest_error_service": deepest["service"] if deepest else None,
            "slowest_spans": [{k: s[k] for k in ("service", "name", "duration_ms")}
                              for s in sorted(spans, key=lambda s: -s["duration_ms"])[:5]],
        })
    return out


def _depth(span: dict, ids: dict) -> int:
    d, seen = 0, set()
    while span["parent_span_id"] in ids and span["span_id"] not in seen:
        seen.add(span["span_id"])
        span = ids[span["parent_span_id"]]
        d += 1
    return d
