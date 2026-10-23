# Incident Zero: Checkpoint 1 (Eyes) & Checkpoint 2 (Brain) Architecture

## Overview
Incident Zero is an AI-powered on-call incident investigation and root-cause analysis agent designed for the OpenTelemetry Astronomy Shop.
- **Checkpoint 1 (Eyes)** queries real telemetry (Prometheus metrics, Jaeger distributed traces, OpenSearch logs) and code change history to construct an auditable, evidence-backed `ObservationReport`.
- **Checkpoint 2 (Brain)** consumes the validated `ObservationReport` and infers the root cause, maps the causal chain, assigns a confidence score, evaluates alternative hypotheses, and recommends safe, non-automated corrective actions in a validated `DiagnosisReport`.

```mermaid
flowchart TD
    subgraph DemoLayer["Astronomy Shop Microservices & Telemetry"]
        SERVICES["Microservices (frontend, checkout, payment, product-catalog, etc.)"]
        LOADGEN["Locust Load Generator"]
        FLAGD["Feature Flags (flagd: paymentFailure, paymentUnreachable, etc.)"]
        COLLECTOR["OpenTelemetry Collector (spanmetrics connector)"]
        
        LOADGEN -->|HTTP traffic| SERVICES
        FLAGD -.->|fault injection| SERVICES
        SERVICES -->|OTLP metrics, logs, traces| COLLECTOR
    end

    subgraph ObservabilityLayer["Observability Backends"]
        PROM[("Prometheus<br/>:9090<br/>Instant & Range PromQL")]
        JAEGER[("Jaeger v2<br/>:8080/jaeger/ui/api/v3<br/>Distributed Traces")]
        OPENSEARCH[("OpenSearch<br/>:9200 (_plugins/_ppl)<br/>Structured Log Events")]
        GIT_REPO[("Git Repository History<br/>Code Commits (Safe Read-Only)")]

        COLLECTOR --> PROM
        COLLECTOR --> JAEGER
        COLLECTOR --> OPENSEARCH
    end

    subgraph AgentEyes["Incident Zero: Checkpoint 1 (Eyes Telemetry Observation)"]
        INPUT["Incident Input<br/>id / description / service / window"]
        TOOLKIT["Diagnostic Toolkit<br/>(telemetry.py: bounded, timeout, read-only)"]
        PLANNER["Investigation Workflow<br/>(workflow.py: telemetry-only or optional LLM)"]
        AUDIT_LOG["Tool Call Audit Trail<br/>(ToolCall: status, duration, query, result)"]
        OBS_REPORT["Observation Report<br/>models.py (schema: incident-zero/observation/v1)<br/>anomalies / logs / traces / evidence ids"]

        INPUT --> PLANNER
        PLANNER --> TOOLKIT
        TOOLKIT -->|query_metric| PROM
        TOOLKIT -->|search_traces / get_trace| JAEGER
        TOOLKIT -->|search_logs / count_logs| OPENSEARCH
        TOOLKIT -->|get_change_history| GIT_REPO
        TOOLKIT --> AUDIT_LOG
        AUDIT_LOG --> OBS_REPORT
    end

    subgraph AgentBrain["Incident Zero: Checkpoint 2 (Brain Root-Cause Diagnosis)"]
        DIAG_ENGINE["Diagnosis Engine<br/>(diagnosis.py: evidence-driven reasoning)"]
        EVIDENCE_RESOLVER["Evidence Verifier<br/>(Strictly checks all referenced evidence IDs)"]
        CAUSAL_GRAPH["Causal Chain & Alternative Hypotheses Evaluator"]
        DIAG_REPORT["Diagnosis Report<br/>(diagnosis_models.py: incident-zero/diagnosis/v1)<br/>root cause / confidence / causal chain / recommendations"]

        OBS_REPORT --> DIAG_ENGINE
        DIAG_ENGINE --> EVIDENCE_RESOLVER
        EVIDENCE_RESOLVER --> CAUSAL_GRAPH
        CAUSAL_GRAPH --> DIAG_REPORT
    end

    subgraph HumanOperator["Operator Interface & Review"]
        CLI["CLI Tool (python -m src.incident.cli)"]
        API["FastAPI REST Endpoints (/incident/investigate, /incident/diagnose)"]
        SRE["On-Call SRE Engineer<br/>(Reviews diagnosis & approves corrective actions)"]
        FUTURE_DAY2["Future Day 2: Autonomous Remediation & Recovery Loop<br/>(Explicitly gated by human approval - not automated)"]

        DIAG_REPORT --> CLI
        DIAG_REPORT --> API
        CLI --> SRE
        API --> SRE
        SRE -.->|approves safe action| FUTURE_DAY2
    end
```

## Architectural Components

### 1. Existing Demo & Telemetry Pipeline
- **Astronomy Shop Services**: Polyglot microservices (Go, C#, Java, Node.js, Ruby, PHP) instrumented with OpenTelemetry SDKs.
- **OpenTelemetry Collector**: Ingests traces and logs, generating Prometheus spanmetrics for server error ratios, request rates, and p95 latencies.
- **Flagd**: OpenFeature flag daemon managing controlled fault injections.

### 2. Observability Storage Backends
- **Prometheus**: Time-series metrics queried via PromQL instant vectors and bounded time ranges.
- **Jaeger v2**: Distributed trace queries via Jaeger UI API v3 (`/jaeger/ui/api/v3/traces`), extracting deepest failing spans.
- **OpenSearch**: Log aggregation queried via PPL (`/_plugins/_ppl`) with severity filtering (13=WARN, 17=ERROR).
- **Git Change History**: Read-only repository commit log analyzer reporting code-level modifications without claiming live deployment confirmation.

### 3. Eyes Observation Workflow (`src/agent/src/incident/`)
- **`telemetry.py`**: Fault-tolerant tool adapters with explicit limits, timeouts, error capturing, and NaN/infinity handling.
- **`workflow.py`**: Executes structured diagnostic plan across metrics, logs, traces, and code changes.
- **`models.py`**: Validates observation schema `incident-zero/observation/v1`. Programmatically validates that every finding references real, successful tool calls.

### 4. Brain Diagnosis Engine (`src/agent/src/incident/`)
- **`diagnosis.py`**: Consumes observation reports and derives:
  - Root cause component.
  - Causal propagation path from leaf fault to user-visible symptoms.
  - Mathematical confidence score (0.0 to 1.0) and status (`confirmed`, `probable`, `insufficient_evidence`).
  - Strict evidence citations (`supporting_evidence_ids` verified against observation evidence).
  - Alternative hypotheses with concrete rejection reasons.
  - Non-automated, safe operator recovery recommendations.
- **`diagnosis_models.py`**: Pydantic schema for `incident-zero/diagnosis/v1`.

### 5. Operator Interfaces
- **CLI**: `python -m src.incident.cli --incident-id <ID> --description "<desc>" --diagnose --json-out report.json --diagnosis-out diagnosis.json`
- **FastAPI**: Mounts `/incident/investigate` and `/incident/diagnose` supporting both structured JSON and formatted Markdown.
