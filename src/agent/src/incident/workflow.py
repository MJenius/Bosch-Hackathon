# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Incident observation workflow: plan tool calls, collect evidence, build a validated report.

Two planners share the same toolkit and report builder:
  * telemetry-only: a fixed, rule-based sequence of read-only queries (no LLM involved).
  * llm-agent: a LangChain/LangGraph agent selects the read-only tools itself.
The report is always derived from recorded tool results, never from the LLM's text.
"""

import os
import re
from collections import Counter
from datetime import datetime, timezone

from .models import (
    Evidence, Finding, IncidentInput, LogEvent, MetricAnomaly, ObservationReport, ToolCall, TraceRef,
)
from .telemetry import DiagnosticToolkit

ERROR_RATIO_THRESHOLD = float(os.getenv("INCIDENT_ERROR_RATIO_THRESHOLD", "0.05"))
P95_LATENCY_THRESHOLD_MS = float(os.getenv("INCIDENT_P95_THRESHOLD_MS", "1000"))
THRESHOLDS = {"error_ratio": ERROR_RATIO_THRESHOLD, "p95_latency_ms": P95_LATENCY_THRESHOLD_MS}

# Telemetry pipeline components, not application services under investigation.
NON_APP_SERVICES = {"otelcol-contrib", "otel-collector", "load-generator", "telemetry-docs"}


def extract_service(description: str, known: set[str]) -> str | None:
    """Return the longest known service name mentioned in the description, if any."""
    text = description.lower()
    hits = [s for s in known if re.search(rf"(?<![a-z0-9-]){re.escape(s)}(?![a-z0-9-])", text)]
    return max(hits, key=len) if hits else None


async def plan_telemetry_only(inp: IncidentInput, kit: DiagnosticToolkit, start: datetime, end: datetime) -> list[str]:
    """Rule-based investigation. Returns notes about planning decisions for the audit trail."""
    notes = []
    err = await kit.query_metric("error_ratio", start, end, reason="Overview: server-span error ratio for all services")
    rate = await kit.query_metric("request_rate", start, end, reason="Overview: request volume for all services")
    lat = await kit.query_metric("p95_latency_ms", start, end, reason="Overview: p95 server latency for all services")

    known = {r["service"] for c in (err, rate, lat) if c.status == "ok" for r in c.result if r["service"]}
    target = inp.service
    if target:
        notes.append(f"Service '{target}' supplied with the incident.")
    else:
        target = extract_service(inp.description, known)
        if target:
            notes.append(f"Service '{target}' mentioned in the incident description.")
    latency_lead = False
    if not target:
        def top(call, thr):
            rows = [r for r in (call.result or []) if r["service"] not in NON_APP_SERVICES and r["value"] > thr]
            return rows[0]["service"] if call.status == "ok" and rows else None
        target = top(err, ERROR_RATIO_THRESHOLD)
        if target:
            notes.append(f"No service given; '{target}' has the highest error ratio above {ERROR_RATIO_THRESHOLD}.")
        else:
            target = top(lat, P95_LATENCY_THRESHOLD_MS)
            latency_lead = bool(target)
            if target:
                notes.append(f"No service given; '{target}' has the highest p95 latency above {P95_LATENCY_THRESHOLD_MS} ms.")
    if not target:
        notes.append("No service given and no metric exceeded thresholds; investigating logs only.")

    await kit.count_logs_by_service(start, end, reason="Which services emitted ERROR-or-worse logs in the window")
    if not target:
        await kit.search_logs(None, start, end, reason="Most recent ERROR logs across all services")
        return notes

    await kit.query_metric_range("error_ratio", target, start, end, reason=f"Error ratio timeline for {target} (peak + timestamp)")
    await kit.query_metric_range("p95_latency_ms", target, start, end, reason=f"p95 latency timeline for {target}")
    await kit.search_logs(target, start, end, min_severity=13, reason=f"WARN+ logs for {target}")
    tr = await kit.search_traces(target, start, end, errors_only=True, reason=f"Error traces involving {target}")
    if tr.status != "ok":
        notes.append(f"No error traces found for '{target}'; sampling recent traces instead.")
        tr = await kit.search_traces(target, start, end, reason=f"Recent traces involving {target}")

    if tr.status == "ok":
        deepest = Counter(t["deepest_error_service"] for t in tr.result if t["deepest_error_service"])
        if deepest:
            lead, n = deepest.most_common(1)[0]
            if lead != target:
                notes.append(f"Follow-up: {n} error trace(s) show errors deepest in '{lead}'; querying it.")
                await kit.query_metric_range("error_ratio", lead, start, end, reason=f"Follow-up: error ratio timeline for {lead}")
                await kit.search_logs(lead, start, end, min_severity=13, reason=f"Follow-up: WARN+ logs for {lead}")
        elif latency_lead and tr.result:
            slow = Counter(t["slowest_spans"][0]["service"] for t in tr.result if t["slowest_spans"])
            lead = slow.most_common(1)[0][0] if slow else None
            if lead and lead != target:
                notes.append(f"Follow-up: slowest spans most often in '{lead}'; querying its latency.")
                await kit.query_metric_range("p95_latency_ms", lead, start, end, reason=f"Follow-up: p95 latency for {lead}")
    return notes


SYSTEM_PROMPT = """You are Incident Zero, an on-call SRE assistant in OBSERVATION mode (Checkpoint 1).
You have READ-ONLY telemetry tools over Prometheus (metrics), OpenSearch (logs) and Jaeger (traces).
Investigate the incident: start with an overview of metrics, pick the most relevant service, then
inspect its logs and traces, and follow up on any downstream service the traces point at.
Rules: cite tool call ids for every claim; separate OBSERVATIONS (seen in data) from HYPOTHESES;
list UNKNOWNS; never claim a definitive root cause unless the data proves it; do not propose or
perform remediation. Keep the final answer under 250 words. Use at most 10 tool calls."""


async def plan_llm(inp: IncidentInput, kit: DiagnosticToolkit, start: datetime, end: datetime) -> str:
    """Let an LLM select the read-only tools. Returns the LLM's narrative text."""
    from langchain.agents import create_agent
    from langchain.tools import tool
    from src.agents.llm import ChatLLM

    import functools

    s, e = start, end

    def safe(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except ValueError as exc:
                return f"invalid tool input: {exc}"
        return wrapper

    @tool
    @safe
    async def query_metric(metric: str, service: str | None = None) -> str:
        """Instant metric for all services or one. metric: error_ratio | request_rate | p95_latency_ms."""
        c = await kit.query_metric(metric, s, e, service, reason="LLM-selected")
        return _brief(c)

    @tool
    @safe
    async def query_metric_range(metric: str, service: str) -> str:
        """Timeline (peak, first, last) of a metric for one service over the incident window."""
        return _brief(await kit.query_metric_range(metric, service, s, e, reason="LLM-selected"))

    @tool
    @safe
    async def count_error_logs_by_service() -> str:
        """Count of ERROR-or-worse log records per service in the incident window."""
        return _brief(await kit.count_logs_by_service(s, e, reason="LLM-selected"))

    @tool
    @safe
    async def search_logs(service: str | None = None, min_severity: int = 17, limit: int = 20) -> str:
        """Recent logs (severity number >= min_severity; 13=WARN, 17=ERROR) for a service or all services."""
        return _brief(await kit.search_logs(service, s, e, min_severity, limit, reason="LLM-selected"))

    @tool
    @safe
    async def search_traces(service: str, limit: int = 20, errors_only: bool = False) -> str:
        """Recent Jaeger traces involving a service (optionally only traces with errors), with error spans and the deepest erroring service."""
        return _brief(await kit.search_traces(service, s, e, limit, errors_only, reason="LLM-selected"))

    agent = create_agent(ChatLLM(), tools=[query_metric, query_metric_range, count_error_logs_by_service,
                                           search_logs, search_traces], system_prompt=SYSTEM_PROMPT)
    msg = (f"Incident {inp.incident_id or '(no id)'}: {inp.description or '(no description)'}\n"
           f"Known affected service: {inp.service or 'unknown'}\nWindow: {start.isoformat()} to {end.isoformat()}")
    result = await agent.ainvoke({"messages": [{"role": "user", "content": msg}]},
                                 config={"recursion_limit": int(os.getenv("GRAPH_RECURSION_LIMIT", "25"))})
    return result["messages"][-1].content


def _brief(call: ToolCall) -> str:
    import json
    body = {"tool_call_id": call.id, "status": call.status, "error": call.error, "query": call.query,
            "result": call.result}
    return json.dumps(body, default=str)[:6000]


def llm_configured() -> bool:
    return os.getenv("INCIDENT_LLM_ENABLED", "False").lower() == "true"


async def investigate(inp: IncidentInput, kit: DiagnosticToolkit, use_llm: bool | None = None,
                      now: datetime | None = None) -> ObservationReport:
    start, end = inp.window(now)
    use_llm = llm_configured() if use_llm is None else use_llm
    narrative, notes = None, []
    if use_llm:
        try:
            narrative = await plan_llm(inp, kit, start, end)
        except Exception as e:  # noqa: BLE001 - LLM failures must not hide telemetry
            notes.append(f"LLM planner failed ({type(e).__name__}: {str(e)[:200]}); fell back to telemetry-only planner.")
            use_llm = False
    if not use_llm or not kit.calls:
        notes += await plan_telemetry_only(inp, kit, start, end)
    return build_report(inp, kit.calls, start, end, "llm-agent" if narrative else "telemetry-only", notes, narrative)


def build_report(inp: IncidentInput, calls: list[ToolCall], start: datetime, end: datetime, mode: str,
                 notes: list[str], narrative: str | None = None) -> ObservationReport:
    evidence: list[Evidence] = []
    anomalies: list[MetricAnomaly] = []
    logs: list[LogEvent] = []
    traces: list[TraceRef] = []
    findings: list[Finding] = []
    unknowns: list[str] = []
    affected: dict[str, None] = {}
    suspected: dict[str, None] = {}

    def add_ev(call, service, ts, summary, data) -> str:
        ev = Evidence(id=f"ev-{len(evidence) + 1}", tool_call_id=call.id, signal=call.signal, service=service,
                      timestamp=_parse_ts(ts), summary=summary, data=data)
        evidence.append(ev)
        return ev.id

    for c in calls:
        if c.status == "error":
            unknowns.append(f"{c.tool} ({c.id}) failed: {c.error}")
            continue
        if c.status == "empty":
            unknowns.append(f"{c.tool} ({c.id}) returned no data for {c.arguments}")
            continue
        metric = c.arguments.get("metric")
        if c.tool == "query_metric":
            thr = THRESHOLDS.get(metric)
            for r in c.result:
                if thr is not None and r["value"] > thr and r["service"] not in NON_APP_SERVICES:
                    eid = add_ev(c, r["service"], r["timestamp"],
                                 f"{metric} for {r['service']} = {r['value']:.4g} {r['unit']} (threshold {thr})", r)
                    anomalies.append(MetricAnomaly(service=r["service"], metric=metric, value=r["value"], unit=r["unit"],
                                                   timestamp=_parse_ts(r["timestamp"]), threshold=thr, evidence_id=eid))
                    affected[r["service"]] = None
                    findings.append(Finding(kind="observation", evidence_ids=[eid],
                                            statement=f"{r['service']} {metric} was {r['value']:.4g} {r['unit']} over the window "
                                                      f"(above threshold {thr})."))
        elif c.tool == "query_metric_range":
            r = c.result
            pk = r["peak"]
            eid = add_ev(c, r["service"], pk["timestamp"],
                         f"{metric} for {r['service']} peaked at {pk['value']:.4g} {r['unit']} at {pk['timestamp']} "
                         f"(first {r['first']['value']:.4g}, last {r['last']['value']:.4g})",
                         {k: r[k] for k in ("peak", "first", "last", "step_s", "unit")})
            thr = THRESHOLDS.get(metric)
            if thr is not None and pk["value"] > thr:
                anomalies.append(MetricAnomaly(service=r["service"], metric=metric, value=pk["value"], unit=r["unit"],
                                               timestamp=_parse_ts(pk["timestamp"]), threshold=thr, evidence_id=eid))
                affected[r["service"]] = None
                findings.append(Finding(kind="observation", evidence_ids=[eid],
                                        statement=f"{r['service']} {metric} peaked at {pk['value']:.4g} {r['unit']} at {pk['timestamp']}."))
        elif c.tool == "count_logs_by_service":
            ids = [add_ev(c, r["service"], None, f"{r['count']} log records >= severity {c.arguments['min_severity']} from {r['service']}", r)
                   for r in c.result[:10]]
            listing = ", ".join(f"{r['service']}={r['count']}" for r in c.result[:10])
            findings.append(Finding(kind="observation", evidence_ids=ids,
                                    statement=f"Services emitting ERROR+ logs in the window: {listing}."))
        elif c.tool == "search_logs":
            ids = []
            for r in c.result[:10]:
                eid = add_ev(c, r["service"], r["timestamp"], f"[{r['severity']}] {r['service']}: {r['message'][:200]}", r)
                ids.append(eid)
                logs.append(LogEvent(timestamp=_parse_ts(r["timestamp"]), service=r["service"], severity=r["severity"],
                                     message=r["message"], trace_id=r["trace_id"], span_id=r["span_id"], evidence_id=eid))
            svc = c.arguments.get("service")
            if ids:
                findings.append(Finding(kind="observation", evidence_ids=ids,
                                        statement=f"{len(c.result)} WARN/ERROR log record(s) returned for {svc or 'all services'}"
                                                  f"{' (limit reached)' if c.truncated else ''}; latest: {c.result[0]['message'][:160]!r}."))
                if svc and any((r["severity"] or "").upper() in ("ERROR", "FATAL", "CRITICAL") for r in c.result):
                    affected[svc] = None
        elif c.tool in ("search_traces", "get_trace"):
            errs = [t for t in c.result if t["error_spans"]]
            chosen = errs[:5] or sorted(c.result, key=lambda t: -t["duration_ms"])[:2]
            ids = []
            for t in chosen:
                eid = add_ev(c, t["root_service"], t["start"],
                             f"trace {t['trace_id']} root {t['root_service']}/{t['root_operation']} {t['duration_ms']} ms, "
                             f"{len(t['error_spans'])} error span(s), deepest error in {t['deepest_error_service']}", t)
                ids.append(eid)
                traces.append(TraceRef(trace_id=t["trace_id"], root_service=t["root_service"], root_operation=t["root_operation"],
                                       duration_ms=t["duration_ms"], span_count=t["span_count"], error_spans=t["error_spans"],
                                       deepest_error_service=t["deepest_error_service"], jaeger_url=t["jaeger_url"], evidence_id=eid))
            svc = c.arguments.get("service") or "trace"
            findings.append(Finding(kind="observation", evidence_ids=ids,
                                    statement=f"{len(errs)} of {len(c.result)} sampled traces for {svc} contained error spans."))
            deepest = Counter(t["deepest_error_service"] for t in errs if t["deepest_error_service"])
            if deepest:
                lead, n = deepest.most_common(1)[0]
                lead_ids = [traces[-len(chosen) + i].evidence_id for i, t in enumerate(chosen) if t["deepest_error_service"] == lead]
                affected[lead] = None
                suspected[lead] = None
                findings.append(Finding(kind="hypothesis", evidence_ids=lead_ids or ids,
                                        statement=f"In {n} error trace(s) the deepest erroring span belongs to {lead}; errors may "
                                                  f"originate in or below {lead}. This is a lead, not a confirmed root cause."))
    if inp.service:
        affected = {inp.service: None, **affected}
    for s in [s for s in affected if s in NON_APP_SERVICES]:
        affected.pop(s)
        suspected.pop(s, None)
    unknowns.append("Root cause is not established at Checkpoint 1; findings describe symptoms and leads only.")
    if not any(c.signal == "traces" and c.status == "ok" for c in calls):
        unknowns.append("No trace data was retrieved, so request paths could not be inspected.")

    initial = [x for x in [inp.description.strip() or None,
                           f"Reported service: {inp.service}" if inp.service else None] if x] or [f"Incident {inp.incident_id}"]
    report = ObservationReport(
        mode=mode, incident_id=inp.incident_id or f"adhoc-{start.strftime('%Y%m%dT%H%M%SZ')}",
        description=inp.description, window_start=start, window_end=end, generated_at=datetime.now(timezone.utc),
        initial_symptoms=initial + [f"Planner note: {n}" for n in notes],
        affected_services=list(affected), suspected_services=list(suspected),
        metric_anomalies=anomalies, log_events=logs, trace_refs=traces, findings=findings, unknowns=unknowns,
        symptom_summary=_summary(affected, anomalies, logs, traces, suspected),
        llm_narrative=narrative, evidence=evidence, tool_calls=calls,
    )
    return ObservationReport.model_validate(report.model_dump())


def _summary(affected, anomalies, logs, traces, suspected) -> str:
    parts = []
    if anomalies:
        parts.append("Metric anomalies: " + "; ".join(
            f"{a.service} {a.metric}={a.value:.3g}{a.unit if a.unit != 'ratio' else ''} @ {a.timestamp:%H:%M:%SZ}"
            for a in anomalies[:5]) + ".")
    else:
        parts.append("No metric exceeded the configured thresholds.")
    err_traces = [t for t in traces if t.error_spans]
    if err_traces:
        parts.append(f"{len(err_traces)} error trace(s) cited, e.g. {err_traces[0].trace_id}.")
    if logs:
        parts.append(f"{len(logs)} WARN/ERROR log record(s) cited.")
    parts.append(f"Affected services (observed): {', '.join(affected) or 'none identified'}.")
    if suspected:
        parts.append(f"Leads to investigate (unconfirmed): {', '.join(suspected)}.")
    return " ".join(parts)


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    s = str(ts).replace(" ", "T")
    if "." in s:
        head, frac = s.split(".", 1)
        tz = ""
        for sep in ("+", "Z", "-"):
            if sep in frac:
                i = frac.index(sep)
                frac, tz = frac[:i], frac[i:]
                break
        s = f"{head}.{frac[:6]}{tz}"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
