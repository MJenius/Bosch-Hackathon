# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""HTTP endpoints for the incident observation workflow, mounted on the existing agent app."""

import httpx
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .diagnosis import diagnose
from .diagnosis_models import DiagnosisReport
from .models import IncidentInput, ObservationReport
from .render import render_diagnosis_markdown, render_markdown
from .telemetry import DiagnosticToolkit, TelemetryConfig
from .workflow import investigate

router = APIRouter(prefix="/incident", tags=["incident"])


async def _run(inp: IncidentInput) -> ObservationReport:
    async with httpx.AsyncClient() as client:
        return await investigate(inp, DiagnosticToolkit(TelemetryConfig.from_env(), client))


@router.post("/investigate", response_model=ObservationReport)
async def investigate_json(inp: IncidentInput) -> ObservationReport:
    return await _run(inp)


@router.post("/investigate/markdown", response_class=PlainTextResponse)
async def investigate_markdown(inp: IncidentInput) -> str:
    return render_markdown(await _run(inp))


@router.post("/diagnose", response_model=DiagnosisReport)
async def diagnose_json(inp: IncidentInput) -> DiagnosisReport:
    obs = await _run(inp)
    return await diagnose(obs)


@router.post("/diagnose/markdown", response_class=PlainTextResponse)
async def diagnose_markdown_endpoint(inp: IncidentInput) -> str:
    obs = await _run(inp)
    diag = await diagnose(obs)
    return render_diagnosis_markdown(diag)
