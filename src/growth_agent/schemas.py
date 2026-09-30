"""Shared input and output contracts for the growth content workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Locale = Literal["en-US", "es-ES"]


class Brief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    locale: Locale
    feature: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    seed_keyword: str = Field(min_length=1)
    cta: str = Field(min_length=1)


class ClaimUse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1)
    sentence: str = Field(min_length=1)
    location: str = Field(min_length=1)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    meta_description: str = Field(min_length=1)
    h1: str = Field(min_length=1)
    intro: str = Field(min_length=1)
    body: list[str] = Field(min_length=1, max_length=4)
    social_posts: list[str] = Field(min_length=2, max_length=2)
    claim_uses: list[ClaimUse] = Field(default_factory=list)

    @field_validator("title", "meta_description", "h1", "intro")
    @classmethod
    def clean_string(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Content field cannot be blank")
        return cleaned


class GenerationResult(BaseModel):
    draft: Draft
    mode: str
    model: str | None = None
    prompt_version: str
    duration_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None


class RunResult(BaseModel):
    run_id: str
    brief: Brief
    status: Literal["pending_review", "insufficient_evidence", "needs_revision", "model_error"]
    facts: list[dict]
    draft: Draft | None = None
    checks: dict = Field(default_factory=dict)
    generation: GenerationResult | None = None
    trace: list[dict] = Field(default_factory=list)
    reviewed: bool = False
    disclaimer: str = (
        "Independent prototype based on public CapCut pages. "
        "Not affiliated with CapCut; every draft requires human review."
    )
