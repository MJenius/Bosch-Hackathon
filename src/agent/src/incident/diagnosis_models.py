# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint 2 Brain: Root-cause diagnosis models and schema validation."""

from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

DiagnosisStatus = Literal["confirmed", "probable", "insufficient_evidence"]


class CausalStep(BaseModel):
    step_number: int = Field(ge=1)
    service: str
    description: str
    evidence_ids: list[str] = Field(default_factory=list)


class AlternativeHypothesis(BaseModel):
    hypothesis: str
    rejection_reason: str
    evaluated_evidence_ids: list[str] = Field(default_factory=list)


class RecommendedAction(BaseModel):
    action: str
    target_service: str
    safety_assessment: str
    is_automated_execution: Literal[False] = False  # Brain MUST NOT execute automated remediation


class DiagnosisReport(BaseModel):
    schema_version: Literal["incident-zero/diagnosis/v1"] = "incident-zero/diagnosis/v1"
    incident_id: str
    description: str
    diagnosis_status: DiagnosisStatus
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    affected_services: list[str] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    causal_chain: list[CausalStep] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    alternative_hypotheses: list[AlternativeHypothesis] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_llm_response: str | None = None

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        return round(v, 3)

    @model_validator(mode="after")
    def validate_evidence_and_status(self):
        # High confidence requires supporting evidence
        if self.confidence > 0.7 and not self.supporting_evidence_ids:
            raise ValueError("Diagnoses with confidence > 0.7 must cite supporting evidence IDs")
        if self.diagnosis_status == "confirmed" and self.confidence < 0.6:
            raise ValueError("Confirmed diagnosis status requires confidence >= 0.6")
        if self.diagnosis_status == "insufficient_evidence" and self.confidence > 0.4:
            raise ValueError("Insufficient evidence status must have confidence <= 0.4")
        return self
