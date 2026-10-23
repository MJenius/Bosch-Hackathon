# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Checkpoint 1 (Eyes) additions and Checkpoint 2 (Brain) diagnosis."""

import asyncio
from datetime import datetime, timezone
import pytest
import httpx
from pydantic import ValidationError

from src.incident.models import IncidentInput, ObservationReport, Evidence, Finding, ToolCall, MetricAnomaly, TraceRef
from src.incident.diagnosis_models import DiagnosisReport, CausalStep, AlternativeHypothesis, RecommendedAction
from src.incident.diagnosis import rule_based_diagnose, diagnose
from src.incident.telemetry import DiagnosticToolkit, TelemetryConfig

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
START = datetime(2026, 10, 23, 11, 45, 0, tzinfo=timezone.utc)
END = NOW
CFG = TelemetryConfig(
    prometheus_url="http://prom.mock",
    jaeger_url="http://jaeger.mock",
    jaeger_public_url="http://public.mock",
    opensearch_url="http://os.mock",
)


def _dummy_observation_report(service: str = "payment") -> ObservationReport:
    call1 = ToolCall(
        id="call-1",
        tool="query_metric",
        signal="metrics",
        reason="test",
        arguments={"metric": "error_ratio"},
        query="promql",
        backend_url="http://prom.mock",
        started_at=NOW,
        duration_ms=10.0,
        status="ok",
        result=[{"service": service, "value": 0.5, "unit": "ratio", "timestamp": NOW.isoformat()}],
    )
    call2 = ToolCall(
        id="call-2",
        tool="search_traces",
        signal="traces",
        reason="test",
        arguments={"service": "frontend"},
        query="jaeger",
        backend_url="http://jaeger.mock",
        started_at=NOW,
        duration_ms=15.0,
        status="ok",
        result=[{
            "trace_id": "0123456789abcdef0123456789abcdef",
            "start": NOW.isoformat(),
            "duration_ms": 120.0,
            "root_service": "frontend",
            "root_operation": "placeOrder",
            "span_count": 5,
            "services": ["frontend", "checkout", service],
            "service_edges": [["frontend", "checkout"], ["checkout", service]],
            "error_spans": [{"span_id": "sp1", "parent_span_id": None, "service": service, "name": "charge", "duration_ms": 50.0, "status_message": "failed"}],
            "deepest_error_service": service,
            "slowest_spans": [],
        }],
    )
    ev1 = Evidence(
        id="ev-1",
        tool_call_id="call-1",
        signal="metrics",
        service=service,
        timestamp=NOW,
        summary=f"error_ratio for {service} = 0.5",
        data={},
    )
    ev2 = Evidence(
        id="ev-2",
        tool_call_id="call-2",
        signal="traces",
        service="frontend",
        timestamp=NOW,
        summary=f"trace error deepest in {service}",
        data={},
    )
    anom = MetricAnomaly(
        service=service,
        metric="error_ratio",
        value=0.5,
        unit="ratio",
        timestamp=NOW,
        threshold=0.05,
        evidence_id="ev-1",
    )
    tref = TraceRef(
        trace_id="0123456789abcdef0123456789abcdef",
        root_service="frontend",
        root_operation="placeOrder",
        duration_ms=120.0,
        span_count=5,
        error_spans=[{"span_id": "sp1", "service": service}],
        deepest_error_service=service,
        jaeger_url="http://public.mock/jaeger/ui/trace/0123456789abcdef0123456789abcdef",
        evidence_id="ev-2",
    )
    findings = [
        Finding(kind="observation", statement=f"{service} error ratio 0.5", evidence_ids=["ev-1"]),
        Finding(kind="hypothesis", statement=f"deepest error in {service}", evidence_ids=["ev-2"]),
    ]
    return ObservationReport(
        schema_version="incident-zero/observation/v1",
        mode="telemetry-only",
        incident_id="INC-TEST",
        description=f"Test incident for {service}",
        window_start=START,
        window_end=END,
        generated_at=NOW,
        initial_symptoms=["checkout failure"],
        affected_services=[service, "checkout", "frontend"],
        suspected_services=[service],
        metric_anomalies=[anom],
        log_events=[],
        trace_refs=[tref],
        findings=findings,
        unknowns=[],
        symptom_summary=f"Error ratio high in {service}",
        evidence=[ev1, ev2],
        tool_calls=[call1, call2],
    )


def test_diagnosis_schema_validation():
    diag = DiagnosisReport(
        incident_id="INC-100",
        description="Checkout broken",
        diagnosis_status="confirmed",
        root_cause="Payment failure",
        confidence=0.85,
        affected_services=["payment", "checkout"],
        symptoms=["HTTP 500 on checkout"],
        causal_chain=[
            CausalStep(step_number=1, service="payment", description="Payment rejected charge", evidence_ids=["ev-1"])
        ],
        supporting_evidence_ids=["ev-1"],
        contradicting_evidence_ids=[],
        alternative_hypotheses=[
            AlternativeHypothesis(hypothesis="Checkout bug", rejection_reason="Traces show error in payment", evaluated_evidence_ids=["ev-1"])
        ],
        unknowns=[],
        recommended_actions=[
            RecommendedAction(action="Check payment logs", target_service="payment", safety_assessment="Safe inspection", is_automated_execution=False)
        ],
    )
    assert diag.schema_version == "incident-zero/diagnosis/v1"
    assert diag.confidence == 0.85


def test_diagnosis_validation_rejects_high_confidence_without_evidence():
    with pytest.raises(ValidationError):
        DiagnosisReport(
            incident_id="INC-101",
            description="No evidence but high confidence",
            diagnosis_status="confirmed",
            root_cause="Unsubstantiated guess",
            confidence=0.95,
            supporting_evidence_ids=[],  # Violates confidence > 0.7 requirement
        )


def test_diagnosis_validation_rejects_confirmed_status_with_low_confidence():
    with pytest.raises(ValidationError):
        DiagnosisReport(
            incident_id="INC-102",
            description="Confirmed with low confidence",
            diagnosis_status="confirmed",
            root_cause="Guess",
            confidence=0.4,
            supporting_evidence_ids=["ev-1"],
        )


def test_rule_based_diagnose_identifies_root_cause_and_causal_chain():
    obs = _dummy_observation_report("payment")
    diag = rule_based_diagnose(obs)
    assert diag.diagnosis_status in ("confirmed", "probable")
    assert "payment" in diag.root_cause.lower()
    assert "ev-1" in diag.supporting_evidence_ids or "ev-2" in diag.supporting_evidence_ids
    assert len(diag.causal_chain) >= 2
    assert diag.causal_chain[0].service == "payment"
    assert any(step.service in ("checkout", "frontend") for step in diag.causal_chain[1:])
    assert diag.recommended_actions[0].is_automated_execution is False


def test_diagnose_cleans_hallucinated_evidence_ids():
    obs = _dummy_observation_report("cart")
    diag = asyncio.run(diagnose(obs, use_llm=False))
    # All cited evidence IDs must exist in obs
    valid_ids = {e.id for e in obs.evidence}
    for eid in diag.supporting_evidence_ids:
        assert eid in valid_ids
    for step in diag.causal_chain:
        for eid in step.evidence_ids:
            assert eid in valid_ids


def test_diagnose_handles_empty_evidence_as_insufficient():
    call = ToolCall(
        id="call-empty",
        tool="query_metric",
        signal="metrics",
        reason="test",
        arguments={},
        query="q",
        backend_url="http://prom",
        started_at=NOW,
        duration_ms=5.0,
        status="empty",
        result=[],
    )
    empty_obs = ObservationReport(
        schema_version="incident-zero/observation/v1",
        mode="telemetry-only",
        incident_id="INC-EMPTY",
        description="Empty run",
        window_start=START,
        window_end=END,
        generated_at=NOW,
        initial_symptoms=["Nothing reported"],
        affected_services=[],
        suspected_services=[],
        metric_anomalies=[],
        log_events=[],
        trace_refs=[],
        findings=[],
        unknowns=["No anomalies detected"],
        symptom_summary="Healthy",
        evidence=[],
        tool_calls=[call],
    )
    diag = asyncio.run(diagnose(empty_obs, use_llm=False))
    assert diag.diagnosis_status == "insufficient_evidence"
    assert diag.confidence <= 0.4
    assert diag.supporting_evidence_ids == []


def test_get_change_history_tool():
    kit = DiagnosticToolkit(CFG, httpx.AsyncClient())
    call = asyncio.run(kit.get_change_history(START, END, limit=3))
    assert call.tool == "get_change_history"
    assert call.signal == "changes"
    assert call.status in ("ok", "empty")
    if call.status == "ok":
        assert len(call.result) <= 3
        assert "commit_hash" in call.result[0]
        assert call.result[0]["deployment_confirmed"] is False
