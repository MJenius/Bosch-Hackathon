# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Evidence model and observation report schema with programmatic validation."""

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Lowercase service names made of letters, digits and dashes, e.g. "product-catalog".
SERVICE_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"

MAX_WINDOW = timedelta(hours=6)

Signal = Literal["metrics", "logs", "traces"]


class IncidentInput(BaseModel):
    incident_id: str | None = Field(default=None, max_length=128)
    description: str = Field(default="", max_length=4000)
    service: str | None = Field(default=None, pattern=SERVICE_NAME_PATTERN)
    start: datetime | None = None
    end: datetime | None = None
    lookback_minutes: int = Field(default=15, ge=1, le=360)

    @model_validator(mode="after")
    def _check(self):
        if not self.incident_id and not self.description.strip():
            raise ValueError("incident_id or description is required")
        return self

    def window(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        now = now or datetime.now(timezone.utc)
        end = _utc(self.end) if self.end else now
        start = _utc(self.start) if self.start else end - timedelta(minutes=self.lookback_minutes)
        if start >= end:
            raise ValueError("start must be before end")
        if end - start > MAX_WINDOW:
            start = end - MAX_WINDOW
        return start, end


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class ToolCall(BaseModel):
    id: str
    tool: str
    signal: Signal
    reason: str
    arguments: dict[str, Any]
    query: str
    backend_url: str
    started_at: datetime
    duration_ms: float
    status: Literal["ok", "empty", "error"]
    error: str | None = None
    result: Any = None
    truncated: bool = False


class Evidence(BaseModel):
    id: str
    tool_call_id: str
    signal: Signal
    service: str | None
    timestamp: datetime | None
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class MetricAnomaly(BaseModel):
    service: str
    metric: str
    value: float
    unit: str
    timestamp: datetime
    threshold: float
    evidence_id: str


class LogEvent(BaseModel):
    timestamp: datetime | None
    service: str | None
    severity: str | None
    message: str
    trace_id: str | None = None
    span_id: str | None = None
    evidence_id: str


class TraceRef(BaseModel):
    trace_id: str
    root_service: str | None
    root_operation: str | None
    duration_ms: float
    span_count: int
    error_spans: list[dict[str, Any]]
    deepest_error_service: str | None
    jaeger_url: str
    evidence_id: str


class Finding(BaseModel):
    kind: Literal["observation", "hypothesis"]
    statement: str
    evidence_ids: list[str] = Field(min_length=1)


class ObservationReport(BaseModel):
    schema_version: Literal["incident-zero/observation/v1"] = "incident-zero/observation/v1"
    mode: Literal["telemetry-only", "llm-agent"]
    incident_id: str
    description: str
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    initial_symptoms: list[str]
    affected_services: list[str]
    suspected_services: list[str]
    metric_anomalies: list[MetricAnomaly]
    log_events: list[LogEvent]
    trace_refs: list[TraceRef]
    findings: list[Finding]
    unknowns: list[str]
    symptom_summary: str
    llm_narrative: str | None = None
    evidence: list[Evidence]
    tool_calls: list[ToolCall]

    @field_validator("tool_calls")
    @classmethod
    def _needs_calls(cls, v):
        if not v:
            raise ValueError("report must record at least one tool call")
        return v

    @model_validator(mode="after")
    def _references_resolve(self):
        calls = {c.id: c for c in self.tool_calls}
        evidence_ids = set()
        for ev in self.evidence:
            call = calls.get(ev.tool_call_id)
            if call is None:
                raise ValueError(f"evidence {ev.id} references unknown tool call {ev.tool_call_id}")
            if call.status == "error":
                raise ValueError(f"evidence {ev.id} cites failed tool call {call.id}")
            evidence_ids.add(ev.id)
        refs = [f.evidence_ids for f in self.findings]
        refs += [[x.evidence_id] for x in (*self.metric_anomalies, *self.log_events, *self.trace_refs)]
        for ids in refs:
            missing = set(ids) - evidence_ids
            if missing:
                raise ValueError(f"unresolved evidence references: {sorted(missing)}")
        if not set(self.suspected_services) <= set(self.affected_services):
            raise ValueError("suspected_services must be a subset of affected_services")
        return self
