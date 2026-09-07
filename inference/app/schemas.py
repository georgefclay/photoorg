"""Request/response models for the JSON-body endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PathImageRequest(BaseModel):
    ref: str = Field(min_length=1)
    path: str = Field(min_length=1)


class FaceReference(BaseModel):
    person_id: int
    embedding: list[float] = Field(min_length=1)


class MatchFacesRequest(BaseModel):
    ref: str = Field(min_length=1)
    embedding: list[float] = Field(min_length=1)
    references: list[FaceReference] = Field(default_factory=list)
    top_k: int = Field(default=10, ge=1, le=500)


class BatchItem(BaseModel):
    ref: str = Field(min_length=1)
    path: str = Field(min_length=1)


class BatchRequest(BaseModel):
    items: list[BatchItem]
    skip_refs: list[str] = Field(default_factory=list)


class Envelope(BaseModel):
    ref: str
    model: str
    elapsed_ms: int
    prompt_version: str | None = None
    result: dict[str, Any]
