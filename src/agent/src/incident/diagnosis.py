# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint 2 Brain: Root-cause reasoning engine consuming Eyes observation reports."""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from .diagnosis_models import (
    AlternativeHypothesis,
    CausalStep,
    DiagnosisReport,
    DiagnosisStatus,
    RecommendedAction,
)
from .models import ObservationReport

SYSTEM_BRAIN_PROMPT = """You are Incident Zero's Brain (Checkpoint 2).
You are an expert SRE on-call root cause analysis agent for a distributed microservices architecture (OpenTelemetry Astronomy Shop).
You are given a validated ObservationReport (Checkpoint 1 - Eyes) containing real telemetry (metric anomalies, error logs, distributed traces, and change history).

Your task:
1. Infer the most likely root cause from actual evidence.
2. Formulate a step-by-step causal chain explaining the propagation from failure to user symptoms.
3. Assign a realistic confidence score (0.0 to 1.0) and status ('confirmed', 'probable', or 'insufficient_evidence').
4. Cite supporting and contradicting evidence IDs (e.g. 'ev-1', 'ev-2') strictly from the report. NEVER hallucinate evidence IDs.
5. Consider alternative hypotheses and provide reasons for rejection.
6. Note any missing evidence or unknowns.
7. Recommend safe recovery/investigation actions (read-only or approved human recovery, NO automated remediation).

You must respond ONLY with valid JSON matching this schema:
{
  "diagnosis_status": "confirmed" | "probable" | "insufficient_evidence",
  "root_cause": "string explaining root cause",
  "confidence": 0.0 to 1.0,
  "affected_services": ["service1", "service2"],
  "symptoms": ["symptom1"],
  "causal_chain": [
    {"step_number": 1, "service": "service_name", "description": "what happened", "evidence_ids": ["ev-1"]}
  ],
  "supporting_evidence_ids": ["ev-1"],
  "contradicting_evidence_ids": [],
  "alternative_hypotheses": [
    {"hypothesis": "string", "rejection_reason": "string", "evaluated_evidence_ids": ["ev-2"]}
  ],
  "unknowns": ["string"],
  "recommended_actions": [
    {"action": "string", "target_service": "string", "safety_assessment": "string", "is_automated_execution": false}
  ]
}
"""


def rule_based_diagnose(report: ObservationReport) -> DiagnosisReport:
    """Deterministic, evidence-driven rule-based diagnosis engine when LLM is unavailable."""
    valid_ev_ids = {ev.id: ev for ev in report.evidence}
    symptoms = list(report.initial_symptoms)
    if report.symptom_summary:
        symptoms.append(report.symptom_summary)

    # 1. Check for insufficient evidence
    if not report.evidence or (not report.affected_services and not report.suspected_services):
        return DiagnosisReport(
            incident_id=report.incident_id,
            description=report.description,
            diagnosis_status="insufficient_evidence",
            root_cause="Insufficient telemetry evidence collected to identify root cause.",
            confidence=0.1,
            affected_services=list(report.affected_services),
            symptoms=symptoms,
            causal_chain=[],
            supporting_evidence_ids=[],
            contradicting_evidence_ids=[],
            alternative_hypotheses=[
                AlternativeHypothesis(
                    hypothesis="Transient network or downstream failure",
                    rejection_reason="No metric anomalies, error logs, or traces recorded in window.",
                )
            ],
            unknowns=list(report.unknowns) + ["No error traces or metric threshold breaches observed."],
            recommended_actions=[
                RecommendedAction(
                    action="Check collector connectivity and extend time window",
                    target_service="otel-collector",
                    safety_assessment="Safe read-only check",
                )
            ],
        )

    # Analyze traces for deepest error service and errors
    deepest_error_services: dict[str, list[str]] = {}
    for tr in report.trace_refs:
        if tr.deepest_error_service:
            deepest_error_services.setdefault(tr.deepest_error_service, []).append(tr.evidence_id)

    # Analyze metric anomalies
    service_anomalies: dict[str, list[str]] = {}
    for anom in report.metric_anomalies:
        service_anomalies.setdefault(anom.service, []).append(anom.evidence_id)

    # Analyze error logs
    service_logs: dict[str, list[str]] = {}
    for lg in report.log_events:
        if lg.service:
            service_logs.setdefault(lg.service, []).append(lg.evidence_id)

    # Primary suspect identification:
    # 1. Service where errors originate deepest in trace call stack
    # 2. Suspected services from observation workflow
    # 3. Reported target service if confirmed by metric anomaly / logs / timeline
    # 4. First affected service by anomaly severity
    root_candidate = None
    supporting_eids: list[str] = []

    # Check if a reported service is explicitly in initial symptoms
    reported_target = None
    for s in report.affected_services:
        for init in report.initial_symptoms:
            if f"Reported service: {s}" in init or f"'{s}' supplied" in init:
                reported_target = s
                break

    if deepest_error_services:
        # Sort by frequency of being deepest erroring span
        root_candidate = max(deepest_error_services.keys(), key=lambda s: len(deepest_error_services[s]))
        supporting_eids.extend(deepest_error_services[root_candidate])
    elif report.suspected_services:
        root_candidate = report.suspected_services[0]
    elif reported_target and (reported_target in service_anomalies or reported_target in service_logs):
        root_candidate = reported_target
    elif report.affected_services:
        root_candidate = report.affected_services[0]

    if root_candidate and root_candidate in service_anomalies:
        supporting_eids.extend(service_anomalies[root_candidate])
    if root_candidate and root_candidate in service_logs:
        supporting_eids.extend(service_logs[root_candidate])

    # Deduplicate while preserving order and verifying validity
    supporting_eids = [eid for eid in dict.fromkeys(supporting_eids) if eid in valid_ev_ids]

    # Build causal chain
    causal_chain: list[CausalStep] = []
    step_num = 1
    if root_candidate:
        # Step 1: Root service failure
        root_ev = [eid for eid in supporting_eids if eid in valid_ev_ids]
        causal_chain.append(
            CausalStep(
                step_number=step_num,
                service=root_candidate,
                description=f"Service '{root_candidate}' experienced internal faults or failures, generating errors at the bottom of the call graph.",
                evidence_ids=root_ev[:2],
            )
        )
        step_num += 1

        # Step 2: Upstream propagation
        upstream_services = [s for s in report.affected_services if s != root_candidate]
        for up_svc in upstream_services:
            up_ev = [eid for eid in (service_anomalies.get(up_svc, []) + service_logs.get(up_svc, [])) if eid in valid_ev_ids]
            causal_chain.append(
                CausalStep(
                    step_number=step_num,
                    service=up_svc,
                    description=f"Upstream service '{up_svc}' encountered cascading errors or elevated latency when calling '{root_candidate}'.",
                    evidence_ids=up_ev[:2],
                )
            )
            step_num += 1

    # Alternative hypotheses
    alternatives = []
    if len(report.affected_services) > 1:
        other = [s for s in report.affected_services if s != root_candidate][0]
        other_ev = [eid for eid in service_anomalies.get(other, []) if eid in valid_ev_ids]
        alternatives.append(
            AlternativeHypothesis(
                hypothesis=f"Root cause originated in upstream '{other}' rather than '{root_candidate}'",
                rejection_reason=f"Distributed trace spans confirm that errors propagate upward from '{root_candidate}' as the leaf failure.",
                evaluated_evidence_ids=other_ev,
            )
        )
    else:
        alternatives.append(
            AlternativeHypothesis(
                hypothesis="External network infrastructure failure",
                rejection_reason="Errors and anomalies are localized to application microservice telemetry.",
                evaluated_evidence_ids=supporting_eids[:1],
            )
        )

    # Determine confidence & status
    if len(supporting_eids) >= 2 and deepest_error_services.get(root_candidate):
        confidence = 0.88
        status: DiagnosisStatus = "confirmed"
    elif len(supporting_eids) >= 1:
        confidence = 0.72
        status: DiagnosisStatus = "probable"
    else:
        confidence = 0.35
        status: DiagnosisStatus = "insufficient_evidence"

    # Specific root cause descriptions based on domain context & telemetry details
    rc_detail = f"Fault in service '{root_candidate}'"
    rec_actions = []

    # Check for known service patterns
    if root_candidate == "payment":
        rc_detail = "Payment service failed to process authorization/charge requests, causing checkout failure."
        rec_actions = [
            RecommendedAction(
                action="Verify payment gateway connectivity, credentials, and feature flag configuration.",
                target_service="payment",
                safety_assessment="Safe inspection / configuration audit.",
            )
        ]
    elif root_candidate == "cart":
        rc_detail = "Cart service internal error or Valkey datastore interaction failure."
        rec_actions = [
            RecommendedAction(
                action="Check valkey-cart status, cart pod resource limits, and error logs.",
                target_service="cart",
                safety_assessment="Safe read-only check.",
            )
        ]
    elif root_candidate == "product-catalog":
        # Check if latency vs memory
        is_latency = any("p95_latency" in valid_ev_ids[eid].summary for eid in supporting_eids if eid in valid_ev_ids)
        if is_latency:
            rc_detail = "Product catalog database lock contention / elevated query latency causing storefront degradation."
            rec_actions = [
                RecommendedAction(
                    action="Inspect database locks on astronomy-db and query queue lengths.",
                    target_service="product-catalog",
                    safety_assessment="Safe read-only database query analysis.",
                )
            ]
        else:
            rc_detail = "Product catalog service degradation or resource constraint."
            rec_actions = [
                RecommendedAction(
                    action="Check product-catalog container memory/CPU metrics and GOMEMLIMIT.",
                    target_service="product-catalog",
                    safety_assessment="Safe container metrics review.",
                )
            ]
    else:
        rc_detail = f"Service failure in '{root_candidate}' propagating errors to upstream caller microservices."
        rec_actions = [
            RecommendedAction(
                action=f"Inspect '{root_candidate}' container logs and health endpoints.",
                target_service=root_candidate or "unknown",
                safety_assessment="Safe read-only inspection.",
            )
        ]

    return DiagnosisReport(
        incident_id=report.incident_id,
        description=report.description,
        diagnosis_status=status,
        root_cause=rc_detail,
        confidence=confidence,
        affected_services=list(report.affected_services),
        symptoms=symptoms,
        causal_chain=causal_chain,
        supporting_evidence_ids=supporting_eids,
        contradicting_evidence_ids=[],
        alternative_hypotheses=alternatives,
        unknowns=list(report.unknowns),
        recommended_actions=rec_actions,
    )


async def diagnose(report: ObservationReport, use_llm: bool | None = None) -> DiagnosisReport:
    """Perform Checkpoint 2 Brain root cause diagnosis on an ObservationReport."""
    use_llm_flag = os.getenv("INCIDENT_LLM_ENABLED", "False").lower() == "true" if use_llm is None else use_llm
    valid_evidence_ids = {ev.id for ev in report.evidence}

    if not use_llm_flag:
        diag = rule_based_diagnose(report)
        # Ensure all referenced evidence IDs exist
        _clean_and_validate_evidence_refs(diag, valid_evidence_ids)
        return diag

    # LLM diagnosis path
    try:
        from src.agents.llm import ChatLLM
        llm = ChatLLM()

        # Build compact context for LLM prompt
        ev_summary = [{"id": ev.id, "signal": ev.signal, "service": ev.service, "summary": ev.summary} for ev in report.evidence]
        anom_summary = [{"service": a.service, "metric": a.metric, "value": a.value, "unit": a.unit, "evidence_id": a.evidence_id} for a in report.metric_anomalies]
        trace_summary = [{"trace_id": t.trace_id, "root_service": t.root_service, "deepest_error": t.deepest_error_service, "evidence_id": t.evidence_id} for t in report.trace_refs]

        prompt_payload = {
            "incident_id": report.incident_id,
            "description": report.description,
            "affected_services": report.affected_services,
            "suspected_services": report.suspected_services,
            "symptom_summary": report.symptom_summary,
            "evidence": ev_summary,
            "metric_anomalies": anom_summary,
            "traces": trace_summary,
            "unknowns": report.unknowns,
        }

        user_content = f"Analyze this incident observation report and provide the root-cause diagnosis JSON:\n```json\n{json.dumps(prompt_payload, indent=2)}\n```"
        messages = [
            {"role": "system", "content": SYSTEM_BRAIN_PROMPT},
            {"role": "user", "content": user_content},
        ]

        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, "content") else str(response)

        # Extract JSON from code fences if present
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        json_str = json_match.group(1) if json_match else content.strip()

        parsed = json.loads(json_str)
        parsed["incident_id"] = report.incident_id
        parsed["description"] = report.description
        parsed["raw_llm_response"] = content

        diag = DiagnosisReport.model_validate(parsed)
        _clean_and_validate_evidence_refs(diag, valid_evidence_ids)
        return diag

    except Exception as exc:
        # Graceful fallback to rule-based diagnosis on LLM error/invalid output
        fallback_diag = rule_based_diagnose(report)
        fallback_diag.unknowns.append(f"LLM diagnosis failed ({type(exc).__name__}: {str(exc)[:150]}); used deterministic Brain reasoning.")
        _clean_and_validate_evidence_refs(fallback_diag, valid_evidence_ids)
        return fallback_diag


def _clean_and_validate_evidence_refs(diag: DiagnosisReport, valid_ids: set[str]) -> None:
    """Strictly filter out any hallucinated evidence IDs and re-validate model integrity."""
    diag.supporting_evidence_ids = [eid for eid in diag.supporting_evidence_ids if eid in valid_ids]
    diag.contradicting_evidence_ids = [eid for eid in diag.contradicting_evidence_ids if eid in valid_ids]
    for step in diag.causal_chain:
        step.evidence_ids = [eid for eid in step.evidence_ids if eid in valid_ids]
    for alt in diag.alternative_hypotheses:
        alt.evaluated_evidence_ids = [eid for eid in alt.evaluated_evidence_ids if eid in valid_ids]

    if not diag.supporting_evidence_ids and diag.confidence > 0.7:
        diag.confidence = 0.65
        if diag.diagnosis_status == "confirmed":
            diag.diagnosis_status = "probable"
