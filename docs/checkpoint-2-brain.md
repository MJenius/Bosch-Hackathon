# Incident Zero: Checkpoint 2 (Brain)

Checkpoint 2 adds the **Brain** layer to Incident Zero. Where Checkpoint 1 (Eyes) observes real telemetry and reports symptoms without speculating, Checkpoint 2 ingests the validated `ObservationReport`, isolates the most likely root cause, computes a quantitative confidence score, traces the step-by-step causal chain, rejects alternative hypotheses against evidence, and recommends safe, non-automated corrective actions.

## Diagnosis Architecture & Principles

1. **Evidence-Driven Reasoning**: Every root-cause conclusion cites specific `supporting_evidence_ids` corresponding to real, successful tool calls in the observation report.
2. **Strict Verification Gate**: The system rejects hallucinated or unresolvable evidence IDs. If evidence is lacking, confidence is clamped (`<= 0.40`) and status is marked `insufficient_evidence`.
3. **Causal Graph Tracing**: Traces how the defect originated at the deepest component (leaf service or internal dependency) and cascaded upstream through service calls to user-facing symptoms.
4. **Alternative Hypotheses**: Considers competing explanations (e.g., upstream service bug, transient network partition) and documents explicit rejection rationale tied to telemetry.
5. **Human-in-the-Loop Safety**: Brain provides recommended actions with safety assessments (`is_automated_execution: false`). Autonomous remediation is deferred to Day 2 and never triggered automatically.

## Versioned Diagnosis Schema

The diagnosis report adheres to `incident-zero/diagnosis/v1`, validated via Pydantic and JSON Schema:

- Schema file: `docs/incident-zero-diagnosis-v1.schema.json`
- Schema version: `incident-zero/diagnosis/v1`

### Key Fields:
- `schema_version`: `"incident-zero/diagnosis/v1"`
- `incident_id`: Unique incident identifier.
- `description`: User-reported or automated incident description.
- `diagnosis_status`: `"confirmed"` | `"probable"` | `"insufficient_evidence"`
- `root_cause`: Synthesized explanation of the underlying component failure.
- `confidence`: Bounded numeric score between `0.0` and `1.0`.
- `affected_services`: List of observed anomalous services.
- `symptoms`: List of user-visible and telemetry symptoms.
- `causal_chain`: Array of ordered steps (`step_number`, `service`, `description`, `evidence_ids`).
- `supporting_evidence_ids`: Citations of evidence confirming the diagnosis.
- `contradicting_evidence_ids`: Citations of conflicting evidence.
- `alternative_hypotheses`: Competing theories and rejection reasons.
- `unknowns`: Telemetry gaps or limitations.
- `recommended_actions`: Safe operational suggestions (`action`, `target_service`, `safety_assessment`, `is_automated_execution`).

## Multi-Scenario Live Evaluation Matrix

Three distinct scenarios were evaluated against ground truth in the live Astronomy Shop environment (`docs/practice-evaluation-results.json`):

| Scenario ID | Fault / Trigger | Expected Root Service | Diagnosed Root Cause | Confidence | Status | Schema Valid | Recovery Confirmed |
|---|---|---|---|---|---|---|---|
| `SCENARIO-1-PAYMENT-FAILURE` | `paymentFailure` (75%) | `payment` | Payment service failed to process authorization/charge requests, causing checkout failure. | `0.88` | `confirmed` | **PASS** | **PASS** |
| `SCENARIO-2-PAYMENT-UNREACHABLE` | `paymentUnreachable` (on) | `payment` | Payment service failed to process authorization/charge requests, causing checkout failure. | `0.88` | `confirmed` | **PASS** | **PASS** |
| `SCENARIO-3-PRODUCT-CATALOG-RESOURCE` | Memory limit contention (`20M` limit vs `128M`/`96MiB`) | `product-catalog` | Product catalog database lock contention / elevated query latency causing storefront degradation. | `0.72` | `probable` | **PASS** | **PASS** |

### Note on Product Catalog Memory Fix
The tested memory fix (`128M` memory limit and `GOMEMLIMIT=96MiB` in `compose.yaml`) has been preserved in the working tree. Under live operational traffic, `product-catalog` memory stabilizes at ~36 MiB with 0 errors and healthy response times, whereas the initial 20 MiB container crashed and timed out under load.

## Demonstration Commands (PowerShell)

Run Eyes observation and Brain diagnosis from `src/agent`:

```powershell
cd src/agent
$env:PROMETHEUS_URL = "http://localhost:9090"
$env:JAEGER_URL     = "http://localhost:8080"
$env:OPENSEARCH_URL = "http://" + ((docker port opensearch 9200 | Select-Object -First 1) -replace '0.0.0.0','localhost')

# Execute end-to-end investigation and diagnosis
python -m src.incident.cli --incident-id DEMO-LIVE-1 --description "Users reporting errors completing checkout purchases" --diagnose --json-out report.json --diagnosis-out diagnosis.json
```

Run test suite:
```powershell
cd src/agent
python -m pytest tests -q
```

Execute automated 3-scenario live evaluation harness:
```powershell
cd src/agent
$env:PYTHONPATH = "."
python tests/run_live_scenarios.py
```

## Known Limitations & Submission Notes
- **Organizer Diagnosis Schema**: No separate official organizer diagnosis schema was published in the repository or available documents. Incident Zero implements `incident-zero/diagnosis/v1` (`docs/incident-zero-diagnosis-v1.schema.json`) with modular mapping support.
- **Deployment Event API**: No dynamic deployment event web service is deployed in the Astronomy Shop demo. Incident Zero integrates a read-only Git code change history adapter (`get_change_history`) that flags commits as unconfirmed code updates rather than fabricating deployments.
- **Autonomous Remediation**: Brain output explicitly marks all recommended actions with `is_automated_execution: false` in compliance with Day 1 safety requirements.
