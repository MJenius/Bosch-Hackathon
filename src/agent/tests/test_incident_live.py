# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Live smoke test against real Prometheus / Jaeger / OpenSearch. Skipped unless INCIDENT_LIVE=1.

Run from src/agent (backends reachable from the host):
  INCIDENT_LIVE=1 PROMETHEUS_URL=http://localhost:9090 JAEGER_URL=http://localhost:8080 \
  OPENSEARCH_URL=http://localhost:<docker port opensearch 9200> python -m pytest tests/test_incident_live.py -q
"""

import asyncio
import os

import httpx
import pytest

from src.incident.models import IncidentInput, ObservationReport
from src.incident.telemetry import DiagnosticToolkit, TelemetryConfig
from src.incident.workflow import investigate

pytestmark = pytest.mark.skipif(os.getenv("INCIDENT_LIVE") != "1", reason="set INCIDENT_LIVE=1 to hit real backends")


async def _run(inp):
    async with httpx.AsyncClient() as client:
        return await investigate(inp, DiagnosticToolkit(TelemetryConfig.from_env(), client), use_llm=False)


def test_live_backends_return_real_telemetry():
    r = asyncio.run(_run(IncidentInput(incident_id="SMOKE", description="smoke test", service="frontend")))
    ObservationReport.model_validate(r.model_dump())
    by_signal = {}
    for c in r.tool_calls:
        by_signal.setdefault(c.signal, []).append(c)
    for signal in ("metrics", "logs", "traces"):
        assert signal in by_signal, f"no {signal} tool call made"
        errors = [c.error for c in by_signal[signal] if c.status == "error"]
        assert len(errors) < len(by_signal[signal]), f"all {signal} calls failed: {errors}"
    assert any(c.status == "ok" for c in by_signal["metrics"])
    assert any(c.status == "ok" for c in by_signal["traces"]), "Jaeger returned no traces for frontend"
    assert r.evidence
