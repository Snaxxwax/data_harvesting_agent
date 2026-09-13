from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def canonical_url(value: str) -> str:
    """Preserve query order and path semantics; remove fragments and default ports."""
    if len(value) > 4096 or any(ord(c) < 32 or c.isspace() for c in value):
        raise ValueError("invalid URL characters or length")
    p = urlsplit(value)
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("an HTTP(S) URL without credentials is required")
    host = p.hostname.rstrip(".").encode("idna").decode().lower()
    if ":" in host:
        host = f"[{host}]"
    port = p.port
    authority = host if port in (None, 443 if p.scheme == "https" else 80) else f"{host}:{port}"
    return urlunsplit((p.scheme.lower(), authority, p.path or "/", p.query, ""))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class Limits(StrictModel):
    requests: int = Field(default=100, ge=1, le=1_000_000)
    bytes: int = Field(default=25_000_000, ge=1024, le=10_000_000_000)
    response_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    seconds: int = Field(default=900, ge=1, le=604800)
    depth: int = Field(default=3, ge=0, le=20)
    tasks: int = Field(default=300, ge=1, le=100_000)
    attempts: int = Field(default=3, ge=1, le=10)
    model_calls: int = Field(default=10, ge=0, le=10_000)
    model_tokens: int = Field(default=150_000, ge=0, le=100_000_000)
    cost_usd: float = Field(default=2, ge=0, le=1000)
    no_gain_pages: int = Field(default=8, ge=1, le=1000)
    domain_delay: float = Field(default=1, ge=0.1, le=120)
    records: int = Field(default=10_000, ge=1, le=1_000_000)
    claims: int = Field(default=100_000, ge=1, le=5_000_000)


class ReplaySpec(StrictModel):
    capture_ids: list[int] = Field(min_length=1, max_length=100)
    limits: Limits = Field(default_factory=Limits)

    @field_validator("capture_ids", mode="before")
    @classmethod
    def positive_ids(cls, values):
        if not isinstance(values, list) or any(type(v) is not int or v < 1 for v in values):
            raise ValueError("capture IDs must be positive integers")
        return values


class JobSpec(StrictModel):
    objective: str = Field(min_length=3, max_length=4000)
    dataset: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    mode: Literal["targeted", "enumerative", "continuous", "deep_research"] = "targeted"
    seeds: list[str] = Field(default_factory=list, max_length=100)
    fields: list[str] = Field(default_factory=list, max_length=50)
    allowed_domains: list[str] = Field(default_factory=list, max_length=100)
    use_model: bool = False
    limits: Limits = Field(default_factory=Limits)
    refresh_seconds: int | None = Field(default=None, ge=60, le=31_536_000)

    @field_validator("seeds")
    @classmethod
    def urls(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(canonical_url(v) for v in values))

    @field_validator("fields")
    @classmethod
    def field_names(cls, values: list[str]) -> list[str]:
        if any(not v.strip() or len(v) > 120 for v in values):
            raise ValueError("field names must be 1–120 characters")
        return list(dict.fromkeys(values))

    @field_validator("allowed_domains")
    @classmethod
    def domains(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-zA-Z0-9.-]+", v) for v in values):
            raise ValueError("allowed_domains must contain host names, without ports or wildcards")
        return [v.lower().rstrip(".") for v in values]

    @model_validator(mode="after")
    def continuous_interval(self) -> JobSpec:
        if self.mode == "continuous" and self.refresh_seconds is None:
            self.refresh_seconds = 86400
        return self


class Claim(StrictModel):
    entity_key: str = Field(min_length=1, max_length=4096)
    field: str = Field(min_length=1, max_length=200)
    value: object
    evidence: str = Field(max_length=20000)
    locator: str = Field(max_length=1000)
    method: str = "structured"
    confidence: float = Field(default=1, ge=0, le=1)


class Lead(StrictModel):
    url: str = Field(max_length=4096)
    reason: str = Field(default="link", max_length=1000)
    priority: int = Field(default=0, ge=-100, le=100)


class Extraction(StrictModel):
    claims: list[Claim] = Field(default_factory=list, max_length=5000)
    leads: list[Lead] = Field(default_factory=list, max_length=500)
    text: str = Field(default="", max_length=100_000)
    extractor: str = "builtin/1"
    warnings: list[str] = Field(default_factory=list, max_length=50)


class ExtractionBatch(StrictModel):
    extraction: Extraction
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    done: bool


class BudgetExceeded(Exception):
    pass


class LostLease(Exception):
    pass


class PolicyDenied(Exception):
    pass


class RetryLater(Exception):
    def __init__(self, reason: str, delay: float = 1):
        super().__init__(reason)
        self.delay = delay
