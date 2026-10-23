# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Human-readable rendering of an ObservationReport (investigation trace + findings)."""

import json

from .models import ObservationReport


def render_markdown(r: ObservationReport, show_results: bool = True) -> str:
    out = [
        f"# Incident Zero observation report: {r.incident_id}",
        f"- Mode: **{r.mode}**" + (" (rule-based planner, no LLM reasoning)" if r.mode == "telemetry-only" else ""),
        f"- Window: {r.window_start.isoformat()} to {r.window_end.isoformat()}",
        f"- Affected services (observed): {', '.join(r.affected_services) or 'none'}",
        f"- Leads (unconfirmed): {', '.join(r.suspected_services) or 'none'}",
        "", "## Symptom summary", r.symptom_summary, "", "## Initial symptoms",
        *[f"- {s}" for s in r.initial_symptoms], "", "## Investigation trace",
    ]
    for c in r.tool_calls:
        out.append(f"### {c.id} `{c.tool}` [{c.signal}] -> {c.status.upper()} ({c.duration_ms} ms)")
        out.append(f"- Why: {c.reason}")
        out.append(f"- Query: `{c.query}`")
        if c.error:
            out.append(f"- Error: {c.error}")
        if show_results and c.result:
            text = json.dumps(c.result, default=str, indent=1)
            out += ["```json", text[:1500] + ("\n... (truncated for display)" if len(text) > 1500 else ""), "```"]
    out += ["", "## Findings"]
    for f in r.findings:
        out.append(f"- **{f.kind.upper()}**: {f.statement} _(evidence: {', '.join(f.evidence_ids)})_")
    if r.trace_refs:
        out += ["", "## Trace references"]
        out += [f"- {t.trace_id} {t.root_service}/{t.root_operation} {t.duration_ms} ms, "
                f"{len(t.error_spans)} error span(s), deepest error: {t.deepest_error_service} - {t.jaeger_url}"
                for t in r.trace_refs]
    out += ["", "## Evidence"]
    out += [f"- {e.id} (from {e.tool_call_id}, {e.signal}, {e.timestamp.isoformat() if e.timestamp else 'n/a'}): {e.summary}"
            for e in r.evidence]
    out += ["", "## Unknowns and limitations", *[f"- {u}" for u in r.unknowns]]
    if r.llm_narrative:
        out += ["", "## LLM narrative (cites tool call ids; not used to build the evidence above)", r.llm_narrative]
    return "\n".join(out)
