# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Command-line demo: run an investigation against live backends and print the report.

Example (from src/agent, with backends reachable from the host):
  python -m src.incident.cli --description "checkout errors reported" --lookback 15
"""

import argparse
import asyncio
import json
import sys

import httpx

from .models import IncidentInput
from .render import render_markdown
from .telemetry import DiagnosticToolkit, TelemetryConfig
from .workflow import investigate


async def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Incident Zero - Checkpoint 1 observation run")
    p.add_argument("--incident-id")
    p.add_argument("--description", default="")
    p.add_argument("--service")
    p.add_argument("--lookback", type=int, default=15, help="minutes")
    p.add_argument("--llm", action="store_true", help="use the LLM planner (requires LLM_* env)")
    p.add_argument("--json-out", help="write the full validated JSON report here")
    p.add_argument("--no-results", action="store_true", help="hide raw tool results in the trace")
    a = p.parse_args(argv)
    inp = IncidentInput(incident_id=a.incident_id, description=a.description, service=a.service,
                        lookback_minutes=a.lookback)
    async with httpx.AsyncClient() as client:
        report = await investigate(inp, DiagnosticToolkit(TelemetryConfig.from_env(), client), use_llm=a.llm)
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(report.model_dump(mode="json"), f, indent=2)
    sys.stdout.reconfigure(encoding="utf-8")
    print(render_markdown(report, show_results=not a.no_results))
    return 0 if any(c.status != "error" for c in report.tool_calls) else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
