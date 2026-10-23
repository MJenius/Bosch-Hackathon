# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Incident Zero tools, workflow and report validation (mocked HTTP; no live backends).

Run from src/agent:  python -m pytest tests -q
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from src.incident.models import Evidence, Finding, IncidentInput, ObservationReport, ToolCall
from src.incident.telemetry import DiagnosticToolkit, TelemetryConfig, promql, summarize_traces
from src.incident.workflow import build_report, extract_service, investigate

NOW = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
START, END = NOW - timedelta(minutes=15), NOW
CFG = TelemetryConfig("http://prom", "http://jaeger", "http://localhost:8080", "http://os", timeout_s=1)


def otlp_trace(trace_id, spans):
    """spans: (span_id, parent, service, name, start_ms, dur_ms, error)"""
    by_svc = {}
    for sid, parent, svc, name, st, dur, err in spans:
        by_svc.setdefault(svc, []).append({
            "traceId": trace_id, "spanId": sid, "parentSpanId": parent or "", "name": name,
            "startTimeUnixNano": str(int(NOW.timestamp() * 1e9) + st * 1_000_000),
            "endTimeUnixNano": str(int(NOW.timestamp() * 1e9) + (st + dur) * 1_000_000),
            "status": {"code": 2, "message": "boom"} if err else {},
        })
    return [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": s}}]},
             "scopeSpans": [{"spans": sp}]} for s, sp in by_svc.items()]


TRACE = otlp_trace("a" * 32, [
    ("1", None, "frontend-proxy", "GET", 0, 100, True),
    ("2", "1", "frontend", "GET /api", 5, 90, True),
    ("3", "2", "recommendation", "ListRecommendations", 10, 80, True),
    ("4", "3", "product-catalog", "ListProducts", 12, 5, False),
])


def prom_vector(rows):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"service_name": s}, "value": [END.timestamp(), str(v)]} for s, v in rows]}}


def make_handler(fail=()):
    def handler(req: httpx.Request):
        path = req.url.path
        if any(f in str(req.url) for f in fail):
            return httpx.Response(503, text="backend down")
        if path == "/api/v1/query":
            q = req.url.params["query"]
            if "histogram_quantile" in q:
                return httpx.Response(200, json=prom_vector([("recommendation", 2500.0), ("cart", 10.0)]))
            if "STATUS_CODE_ERROR" in q:
                return httpx.Response(200, json=prom_vector([("recommendation", 0.4), ("cart", "NaN"), ("otelcol-contrib", 0.9)]))
            return httpx.Response(200, json=prom_vector([("recommendation", 3.0), ("cart", 1.0)]))
        if path == "/api/v1/query_range":
            t = START.timestamp()
            return httpx.Response(200, json={"status": "success", "data": {"result": [
                {"metric": {}, "values": [[t, "0.1"], [t + 60, "0.9"], [t + 120, "NaN"], [t + 180, "0.3"]]}]}})
        if path == "/_plugins/_ppl":
            q = json.loads(req.content)["query"]
            if "stats count()" in q:
                return httpx.Response(200, json={"schema": [{"name": "n"}, {"name": "resource.service.name"}],
                                                 "datarows": [[12, "frontend"], [3, "recommendation"]]})
            return httpx.Response(200, json={"schema": [{"name": n} for n in ("@timestamp", "severity.text", "resource.service.name", "body", "traceId", "spanId")],
                                             "datarows": [["2026-10-23 09:55:01.123456789", "ERROR", "recommendation", "failed to fetch", "a" * 32, "3"]]})
        if path.startswith("/jaeger/ui/api/v3/traces"):
            return httpx.Response(200, json={"result": {"resourceSpans": TRACE}})
        return httpx.Response(404)
    return handler


def kit(fail=()):
    return DiagnosticToolkit(CFG, httpx.AsyncClient(transport=httpx.MockTransport(make_handler(fail))))


def run(coro):
    return asyncio.run(coro)


def test_promql_rejects_injection():
    with pytest.raises(ValueError):
        promql("error_ratio", 900, 'x"} or vector(1)')
    assert 'service_name="cart"' in promql("request_rate", 900, "cart")


def test_query_metric_normalizes_and_drops_nan():
    k = kit()
    call = run(k.query_metric("error_ratio", START, END, reason="t"))
    assert call.status == "ok"
    assert [r["service"] for r in call.result] == ["otelcol-contrib", "recommendation"]
    assert call.result[1]["unit"] == "ratio" and call.query.startswith("sum by")


def test_range_reports_peak_with_timestamp():
    call = run(kit().query_metric_range("error_ratio", "recommendation", START, END))
    assert call.result["peak"]["value"] == 0.9
    assert call.result["peak"]["timestamp"].startswith("2026-10-23T09:46")


def test_query_failure_is_recorded_not_raised():
    k = kit(fail=("prom",))
    call = run(k.query_metric("request_rate", START, END))
    assert call.status == "error" and "503" in call.error


def test_connection_error_is_recorded():
    def boom(req):
        raise httpx.ConnectError("refused")
    k = DiagnosticToolkit(CFG, httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    call = run(k.search_logs("cart", START, END))
    assert call.status == "error" and "ConnectError" in call.error


def test_empty_results_marked_empty():
    k = DiagnosticToolkit(CFG, httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"schema": [], "datarows": []}))))
    assert run(k.search_logs("cart", START, END)).status == "empty"


def test_log_limit_is_bounded():
    call = run(kit().search_logs("recommendation", START, END, limit=10_000))
    assert call.arguments["limit"] == 50 and "head 50" in call.query
    assert call.result[0]["trace_id"] == "a" * 32


def test_trace_summary_parent_child_and_deepest_error():
    t = summarize_traces(TRACE)[0]
    assert t["root_service"] == "frontend-proxy"
    assert t["deepest_error_service"] == "recommendation"
    assert ["recommendation", "product-catalog"] in t["service_edges"]
    assert len(t["error_spans"]) == 3 and t["span_count"] == 4


def test_extract_service_prefers_longest_match():
    assert extract_service("errors in product-catalog", {"product-catalog", "catalog"}) == "product-catalog"
    assert extract_service("nothing here", {"cart"}) is None


def test_investigate_end_to_end_with_mocks():
    inp = IncidentInput(incident_id="INC-1", description="Shoppers see recommendation failures")
    r = run(investigate(inp, kit(), use_llm=False, now=NOW))
    assert r.mode == "telemetry-only"
    assert "recommendation" in r.affected_services
    assert "otelcol-contrib" not in r.affected_services
    assert r.suspected_services == ["recommendation"]
    assert any(f.kind == "hypothesis" for f in r.findings)
    assert r.trace_refs and r.trace_refs[0].jaeger_url.endswith("a" * 32)
    assert r.log_events[0].timestamp.year == 2026
    ids = {e.id for e in r.evidence}
    assert all(set(f.evidence_ids) <= ids for f in r.findings)
    assert any("Root cause is not established" in u for u in r.unknowns)


def test_investigate_reports_unavailable_backends():
    inp = IncidentInput(description="checkout slow", service="checkout")
    r = run(investigate(inp, kit(fail=("jaeger", "/_plugins")), use_llm=False, now=NOW))
    failed = [c for c in r.tool_calls if c.status == "error"]
    assert failed and all(any(c.id in u for u in r.unknowns) for c in failed)
    assert any("No trace data" in u for u in r.unknowns)
    assert r.affected_services[0] == "checkout"


def _call(status="ok"):
    return ToolCall(id="call-1", tool="query_metric", signal="metrics", reason="", arguments={}, query="q",
                    backend_url="u", started_at=NOW, duration_ms=1, status=status)


def _report(**kw):
    base = dict(mode="telemetry-only", incident_id="x", description="", window_start=START, window_end=END,
                generated_at=NOW, initial_symptoms=[], affected_services=[], suspected_services=[],
                metric_anomalies=[], log_events=[], trace_refs=[], findings=[], unknowns=[], symptom_summary="",
                evidence=[], tool_calls=[_call()])
    base.update(kw)
    return ObservationReport(**base)


def test_validation_rejects_dangling_evidence_reference():
    with pytest.raises(ValidationError):
        _report(findings=[Finding(kind="observation", statement="s", evidence_ids=["ev-9"])])


def test_validation_rejects_evidence_from_failed_call():
    ev = Evidence(id="ev-1", tool_call_id="call-1", signal="metrics", service=None, timestamp=None, summary="s")
    with pytest.raises(ValidationError):
        _report(tool_calls=[_call("error")], evidence=[ev])


def test_validation_requires_tool_calls_and_subset():
    with pytest.raises(ValidationError):
        _report(tool_calls=[])
    with pytest.raises(ValidationError):
        _report(suspected_services=["cart"])


def test_finding_requires_evidence():
    with pytest.raises(ValidationError):
        Finding(kind="observation", statement="s", evidence_ids=[])


def test_input_validation():
    with pytest.raises(ValidationError):
        IncidentInput()
    with pytest.raises(ValidationError):
        IncidentInput(description="x", service="Bad Name;")
    s, e = IncidentInput(description="x", start=NOW - timedelta(days=2), end=NOW).window()
    assert e - s == timedelta(hours=6)


def test_build_report_with_only_failures_is_valid():
    calls = [_call("error").model_copy(update={"error": "down"})]
    r = build_report(IncidentInput(description="x"), calls, START, END, "telemetry-only", [])
    assert r.evidence == [] and any("down" in u for u in r.unknowns)
