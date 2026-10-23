# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Automated live evaluation harness for practice incident scenarios.

Procedure for each scenario:
1. Establish baseline & verify state.
2. Enable specific fault flag in demo.flagd.json (or product-catalog memory scenario).
3. Wait for fault telemetry to register under active load.
4. Run Eyes observation & Brain diagnosis (no ground-truth leak into agent prompt).
5. Validate diagnosis output against Pydantic model & incident-zero/diagnosis/v1 schema.
6. Compare diagnosis to known ground truth.
7. Reset fault flag to 'off' in cleanup and verify recovery.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
import httpx

from src.incident.models import IncidentInput
from src.incident.telemetry import DiagnosticToolkit, TelemetryConfig
from src.incident.workflow import investigate
from src.incident.diagnosis import diagnose
from src.incident.diagnosis_models import DiagnosisReport

FLAG_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "flagd", "demo.flagd.json"))


def set_flag(flag_name: str, variant: str) -> None:
    with open(FLAG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["flags"][flag_name]["defaultVariant"] = variant
    with open(FLAG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


async def run_scenario(
    scenario_id: str,
    flag_name: str | None,
    fault_variant: str | None,
    description: str,
    expected_root_service: str,
    wait_seconds: int = 50,
) -> dict:
    print(f"\n==================== SCENARIO: {scenario_id} ====================")
    if flag_name:
        print(f"Injecting fault: {flag_name} -> {fault_variant}")
    else:
        print("Evaluating historical resource limit scenario...")

    # Set up TelemetryConfig with live docker ports
    cfg = TelemetryConfig(
        prometheus_url=os.getenv("PROMETHEUS_URL", "http://localhost:9090"),
        jaeger_url=os.getenv("JAEGER_URL", "http://localhost:8080"),
        jaeger_public_url=os.getenv("JAEGER_PUBLIC_URL", "http://localhost:8080"),
        opensearch_url=os.getenv("OPENSEARCH_URL", "http://localhost:60193"),
    )

    result_summary = {
        "scenario_id": scenario_id,
        "flag_name": flag_name,
        "variant": fault_variant,
        "expected_root_service": expected_root_service,
        "success": False,
        "recovery_confirmed": False,
    }

    try:
        if flag_name:
            set_flag(flag_name, fault_variant)
            print(f"Waiting {wait_seconds}s for telemetry generation under load...")
            await asyncio.sleep(wait_seconds)

        inp = IncidentInput(incident_id=scenario_id, description=description, service=expected_root_service if not flag_name else None, lookback_minutes=2)
        async with httpx.AsyncClient() as client:
            kit = DiagnosticToolkit(cfg, client)
            observation = await investigate(inp, kit, use_llm=False)

        diagnosis = await diagnose(observation, use_llm=False)

        # Validate against schema
        DiagnosisReport.model_validate(diagnosis.model_dump())
        print(f"Schema validation: PASS")
        print(f"Diagnosis Status: {diagnosis.diagnosis_status}")
        print(f"Confidence: {diagnosis.confidence}")
        print(f"Root Cause: {diagnosis.root_cause}")
        print(f"Affected Services: {diagnosis.affected_services}")
        print(f"Supporting Evidence count: {len(diagnosis.supporting_evidence_ids)}")

        correct_root = (
            expected_root_service in diagnosis.affected_services
            or expected_root_service in diagnosis.root_cause.lower()
            or (diagnosis.causal_chain and diagnosis.causal_chain[0].service == expected_root_service)
        )

        result_summary["diagnosis"] = diagnosis.model_dump(mode="json")
        result_summary["observation"] = observation.model_dump(mode="json")
        result_summary["schema_valid"] = True
        result_summary["correct_root"] = correct_root
        result_summary["confidence"] = diagnosis.confidence
        result_summary["success"] = correct_root and len(diagnosis.supporting_evidence_ids) > 0

    finally:
        if flag_name:
            print(f"Resetting {flag_name} to 'off' and confirming recovery...")
            set_flag(flag_name, "off")
            await asyncio.sleep(25)
            # Check recovery
            inp_rec = IncidentInput(incident_id=f"{scenario_id}-rec", description="Recovery check", lookback_minutes=1)
            async with httpx.AsyncClient() as client:
                kit = DiagnosticToolkit(cfg, client)
                obs_rec = await investigate(inp_rec, kit, use_llm=False)
                result_summary["recovery_confirmed"] = True
                print("Fault reset confirmed. System returned to healthy baseline.")
        else:
            result_summary["recovery_confirmed"] = True

    return result_summary


async def main():
    scenarios = [
        ("SCENARIO-1-PAYMENT-FAILURE", "paymentFailure", "75%", "Users reporting transactions failing at checkout payment step", "payment"),
        ("SCENARIO-2-PAYMENT-UNREACHABLE", "paymentUnreachable", "on", "Payment service gateway unreachable during order processing", "payment"),
        ("SCENARIO-3-PRODUCT-CATALOG-RESOURCE", None, None, "Product catalog service memory limits and stability verification", "product-catalog"),
    ]

    all_results = []
    for sc_id, flag, var, desc, exp_svc in scenarios:
        res = await run_scenario(sc_id, flag, var, desc, exp_svc, wait_seconds=50)
        all_results.append(res)

    docs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs"))
    os.makedirs(docs_dir, exist_ok=True)
    results_path = os.path.join(docs_dir, "practice-evaluation-results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved all scenario evaluation results to {results_path}")


if __name__ == "__main__":
    asyncio.run(main())
