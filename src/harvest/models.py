from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def canonical_url(value: str) -> str:
    """Preserve query order and path semantics; remove fragments and default ports."""
    if len(value) > 4096 or any(ord(c) < 32 or c.isspace() for c in value):
        raise ValueError("invalid URL characters or length")
    # "{" and "}" are excluded from URI syntax, so a literal one is never a real address --
    # it is an unexpanded template such as https://t.me/{username}. Rejected here rather
    # than in one caller because every seed, lead and declared source routes through this
    # function, and a template that reaches a lead becomes a wasted fetch of a nonexistent
    # page whose 404 then reads as evidence.
    if "{" in value or "}" in value:
        raise ValueError("URL contains an unexpanded template placeholder")
    p = urlsplit(value)
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("an HTTP(S) URL without credentials is required")
    host = p.hostname.rstrip(".").encode("idna").decode().lower()
    if ":" in host:
        host = f"[{host}]"
    port = p.port
    authority = host if port in (None, 443 if p.scheme == "https" else 80) else f"{host}:{port}"
    return urlunsplit((p.scheme.lower(), authority, p.path or "/", p.query, ""))


_TOOL_SOURCE_RE = re.compile(r"^tool://([a-z0-9_-]{1,40})/(.+)$")


def canonical_source_url(value: str) -> str:
    """A declared source is an HTTP(S) URL, or the capture URL of a tool run.

    `tools.run` names its capture `tool://<name>/<target>`, and `dossier.reconcile` matches
    a source rule to an observation by exact `source_url`. Accepting only HTTP(S) here meant
    a tool's observations could never have a rule, so reconcile dropped every one of them and
    a job whose only evidence came from a tool produced an empty "completed" dossier.
    """
    match = _TOOL_SOURCE_RE.match(value)
    if match is None:
        return canonical_url(value)
    if re.fullmatch(TOOL_TARGET_PATTERN, match.group(2)) is None:
        raise ValueError("tool:// source URL target is not a valid tool target")
    return value


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
    # How many results one discovery search may turn into follow-up leads. This was a bare
    # `[:50]` in the engine, which made the breadth of a discovery job the only budget that
    # could not be stated in its spec -- and discovery is precisely where an unbounded lead
    # set costs the most, because every accepted result is a fetch against a host nobody
    # named. `select_leads` still applies scope, depth and dedup on top; this caps the input.
    search_results: int = Field(default=50, ge=1, le=500)
    claims: int = Field(default=100_000, ge=1, le=5_000_000)
    reading_passes: int = Field(default=3, ge=1, le=10)
    # Tool tasks are only ever enqueued at submit and never retried (a tool timeout is a
    # permanent task failure, see tools._exec), so this bounds actual invocations, not just
    # declarations. That matters because a tool's own requests bypass every budget above.
    tool_runs: int = Field(default=3, ge=0, le=50)
    # Identifiers a root tool run discovers (an address's handle, an email or full name a
    # profile states) that may each get one enrichment child job. 0 (the default) keeps
    # discovery from authorizing anything; setting it is the operator authorizing that many
    # one-hop pivots. Each pivot's tool runs still count against tool_runs above.
    pivots: int = Field(default=0, ge=0, le=20)


MAX_DISCOVERY_QUERIES = 5

TARGET_KEY_PATTERN = r"^[a-zA-Z0-9_-]{1,80}$"

# A tool target reaches an external argv. The first character is restricted to alphanumerics
# so the value can never be read as a flag by an argparse-based tool. Shared with tools.py so
# a spec is rejected at submit instead of failing mid-job with a generic adapter error.
TOOL_TARGET_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,253}$"


class Target(StrictModel):
    key: str = Field(pattern=TARGET_KEY_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    identifiers: dict[str, list[str]] = Field(default_factory=dict, max_length=20)

    @field_validator("identifiers")
    @classmethod
    def exact_identifiers(cls, values: dict[str, list[str]]) -> dict[str, list[str]]:
        for namespace, ids in values.items():
            if not namespace.strip() or len(namespace) > 100:
                raise ValueError("identifier namespace must be 1-100 characters")
            if not ids or len(ids) > 50:
                raise ValueError("identifier namespace needs 1-50 exact values")
            if any(not isinstance(v, str) or not v or len(v) > 500 for v in ids):
                raise ValueError("identifier values must be exact nonempty strings")
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate identifier value within one namespace")
        return values


class ToolRun(StrictModel):
    """One external OSINT CLI invocation. The target is explicit per tool because tools
    consume different identifier types (Maigret a username, GHunt an email)."""

    name: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    target: str = Field(pattern=TOOL_TARGET_PATTERN, min_length=1, max_length=254)
    # Whether the tool's own findings may become fetch tasks. A tool run is useful on its
    # own -- the capture and its claims are the evidence -- so crawling every profile URL it
    # reports is a separate, much larger decision than running the tool. Default True keeps
    # existing behaviour; False gives a scan whose request cost is exactly the tool's own.
    crawl: bool = True
    # Maigret only: check this many top-ranked sites instead of --all-sites. None (the
    # default) keeps the full database, which is the documented default breadth. Other
    # tools ignore it; it lives here rather than in Settings because breadth is a
    # per-run authorization and cost decision, not a deployment-wide one.
    top_sites: int | None = Field(default=None, ge=1, le=10_000)


class SourceRule(StrictModel):
    url: str = Field(max_length=4096)
    identifier_fields: dict[str, str] = Field(default_factory=dict, max_length=100)
    field_map: dict[str, str] = Field(default_factory=dict, max_length=100)
    document_target: str | None = Field(default=None, pattern=TARGET_KEY_PATTERN)
    document_fields: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("url")
    @classmethod
    def canonical(cls, value: str) -> str:
        return canonical_source_url(value)

    @field_validator("identifier_fields")
    @classmethod
    def exact_identifier_fields(cls, values: dict[str, str]) -> dict[str, str]:
        for field, namespace in values.items():
            if not field.strip() or len(field) > 200:
                raise ValueError("identifier_fields keys must be 1-200 characters")
            if not namespace.strip() or len(namespace) > 100:
                raise ValueError("identifier_fields values must be 1-100 characters")
        return values

    @field_validator("field_map")
    @classmethod
    def exact_field_map(cls, values: dict[str, str]) -> dict[str, str]:
        for field, dossier_field in values.items():
            if not field.strip() or len(field) > 200:
                raise ValueError("field_map keys must be 1-200 characters")
            if not dossier_field.strip() or len(dossier_field) > 120:
                raise ValueError("field_map values must be 1-120 characters")
        return values

    @field_validator("document_fields")
    @classmethod
    def exact_document_fields(cls, values: list[str]) -> list[str]:
        if any(not v.strip() or len(v) > 120 for v in values):
            raise ValueError("document_fields entries must be 1-120 characters")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def usable_rule(self) -> SourceRule:
        if not self.identifier_fields and not self.field_map and not self.document_target:
            raise ValueError("source rule needs identifier_fields, field_map, or document_target")
        if bool(self.document_target) != bool(self.document_fields):
            raise ValueError("document_target and document_fields must be set together")
        if self.document_target and self.url.startswith("tool://"):
            # Document binding matches the entity whose key is "url:" + rule.url, and a tool
            # capture never produces such an entity -- its records are per-account. Rejected
            # rather than accepted as a rule that silently binds nothing.
            raise ValueError(
                "a tool:// source cannot use document_target; its records are per-account, "
                "so bind them with identifier_fields instead"
            )
        return self


class Investigation(StrictModel):
    targets: list[Target] = Field(default_factory=list, max_length=50)
    sources: list[SourceRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def consistent(self) -> Investigation:
        keys = [t.key for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate target key")
        seen: dict[tuple[str, str], str] = {}
        for target in self.targets:
            for namespace, values in target.identifiers.items():
                for value in values:
                    pair = (namespace, value)
                    if pair in seen and seen[pair] != target.key:
                        raise ValueError(
                            f"identifier {namespace}:{value} is declared by multiple targets, "
                            "which would silently choose a target"
                        )
                    seen[pair] = target.key
        target_keys = set(keys)
        urls = [s.url for s in self.sources]
        if len(urls) != len(set(urls)):
            raise ValueError("duplicate source rule url")
        for source in self.sources:
            if source.document_target and source.document_target not in target_keys:
                raise ValueError(f"unknown document_target {source.document_target}")
        return self


class ReplaySpec(StrictModel):
    capture_ids: list[int] = Field(min_length=1, max_length=100)
    limits: Limits = Field(default_factory=Limits)
    investigation: Investigation | None = Field(default=None)

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
    discovery_queries: list[str] = Field(default_factory=list, max_length=50)
    allowed_domains: list[str] = Field(default_factory=list, max_length=100)
    use_model: bool = False
    limits: Limits = Field(default_factory=Limits)
    refresh_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    investigation: Investigation | None = Field(default=None)
    tools: list[ToolRun] = Field(default_factory=list, max_length=50)

    @field_validator("tools")
    @classmethod
    def distinct_tools(cls, values: list[ToolRun]) -> list[ToolRun]:
        seen = {(t.name, t.target) for t in values}
        if len(seen) != len(values):
            raise ValueError("duplicate tool and target")
        return values

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

    @field_validator("discovery_queries")
    @classmethod
    def query_text(cls, values: list[str]) -> list[str]:
        values = [v.strip() for v in values if v.strip()]
        if any(len(v) > 500 for v in values):
            raise ValueError("discovery_queries entries must be at most 500 characters")
        return list(dict.fromkeys(values))[:MAX_DISCOVERY_QUERIES]

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


class ActionableError(ValueError):
    """A failure whose message Harvest itself wrote: secret-free and safe to persist.

    Other ValueErrors can come from parsers or libraries and may quote fetched content, so
    the worker records only their type. These say what actually stopped the task.
    """


# Requested field -> source field names that hold the same thing. Maigret calls a profile's
# display name `fullname` and its address `url`; asking for display_name/profile_url must not
# report them missing. Report-only (records view and missing_fields), never extraction gating.
FIELD_ALIASES = {
    "display_name": ("fullname", "full_name", "name", "nickname"),
    "profile_url": ("url",),
}


class AuthorizationRequired(ValueError):
    """A proposed agent action materially exceeds the authorization the operator gave.

    A ValueError subclass so the existing API error handling returns it as a 422 with the
    message, while the agent interface can catch it specifically and return a structured
    "ask the operator" result instead of starting work. `suggestion` is the concrete thing
    the operator would have to authorize.
    """

    def __init__(self, reason: str, suggestion: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.suggestion = suggestion


class RetryLater(Exception):
    def __init__(self, reason: str, delay: float = 1):
        super().__init__(reason)
        self.delay = delay
