"""Deterministic, LLM-free intake planning: turn a free-text investigation input or dataset
description into seeds, a small bounded/deduplicated discovery query set, and default fields.

No I/O. `detect_investigation_type` only classifies structurally unambiguous inputs (URL,
domain, email, phone, an explicit @handle); free text that could plausibly be a person name,
organization, address or opaque identifier is deliberately left "unknown" so a caller (the web
UI) must ask the operator to pick one explicitly, rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import MAX_DISCOVERY_QUERIES, canonical_url

INVESTIGATION_TYPES = (
    "email",
    "phone",
    "person",
    "username",
    "organization",
    "domain",
    "url",
    "address",
    "identifier",
)

DEFAULT_FIELDS = {
    "email": ["full_name", "email", "phone", "organization", "profile_url"],
    "phone": ["full_name", "phone", "organization", "profile_url"],
    "person": ["full_name", "email", "phone", "address", "organization", "profile_url"],
    "username": ["display_name", "username", "profile_url", "bio"],
    "organization": ["name", "website", "address", "phone", "email"],
    "domain": ["title", "description", "organization", "contact_email"],
    "url": ["title", "description"],
    "address": ["formatted_address", "organization", "phone"],
    "identifier": ["full_name", "organization", "profile_url"],
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_DOMAIN_RE = re.compile(r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$")
_PHONE_RE = re.compile(r"^\+?[0-9()\-.\s]{7,20}$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.]{2,32}$")

_DATASET_FIELD_RULES = [
    (
        re.compile(r"\b(homes?|houses?|real estate|realty|propert(?:y|ies))\b", re.I),
        ["address", "price", "bedrooms", "bathrooms", "square_feet", "url"],
    ),
    (
        re.compile(r"\b(marketplace|listing|listings|for sale|classifieds|used)\b", re.I),
        ["title", "price", "condition", "seller", "availability", "location", "url", "posted_date"],
    ),
    (
        re.compile(r"\b(product|products|category|catalog|store)\b", re.I),
        ["title", "price", "brand", "seller", "availability", "url", "description"],
    ),
    (
        re.compile(
            r"\b(cars?|vehicles?|trucks?|suvs?|sedans?|motorcycles?|autos?|automobiles?)\b", re.I
        ),
        ["year", "make", "model", "mileage", "vin", "price", "url"],
    ),
    (
        re.compile(r"(\$|\b(price|cost|under|below|over|budget)\b)", re.I),
        ["price"],
    ),
]
DEFAULT_DATASET_FIELDS = ["title", "price", "url", "description"]


@dataclass
class Plan:
    kind: str
    normalized: str
    seeds: list[str] = field(default_factory=list)
    discovery_queries: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)


def _bounded(queries: list[str], limit: int = MAX_DISCOVERY_QUERIES) -> list[str]:
    return list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))[:limit]


_DOMAIN_ERROR = (
    "domain must be a bare hostname, e.g. example.org or sub.example.org (no scheme, "
    "path, port, credentials, or surrounding/embedded whitespace; at most one trailing "
    "slash and one trailing dot)"
)


def _normalize_domain(value: str) -> str:
    """The strict bare-hostname contract shared by auto-detection and an explicit
    kind="domain". A domain investigation input must be exactly one hostname: no scheme,
    path, port, userinfo/credentials, or malformed label.

    Nothing here calls .strip() -- leading/trailing/embedded whitespace (including tabs
    and newlines) is rejected outright, never silently discarded, so a caller must not
    pre-strip the value before this runs (that would hide the exact evidence this
    validation exists to catch). At most one trailing "/" and, separately, at most one
    trailing "." are tolerated (e.g. a URL bar paste or an absolute-FQDN dot) and stripped;
    a second one of either means real content followed and the input is rejected, not
    silently collapsed into an unintended seed.
    """
    if not value or any(c.isspace() for c in value):
        raise ValueError(_DOMAIN_ERROR)
    host = value
    if host.endswith("/"):
        host = host.removesuffix("/")
        if host.endswith("/"):
            raise ValueError(_DOMAIN_ERROR)
    if host.endswith("."):
        host = host.removesuffix(".")
        if host.endswith("."):
            raise ValueError(_DOMAIN_ERROR)
    host = host.lower()
    if _DOMAIN_RE.match(host) is None:
        raise ValueError(_DOMAIN_ERROR)
    return host


def detect_investigation_type(value: str) -> str:
    v = value.strip()
    if not v:
        raise ValueError("value is required")
    if v.lower().startswith(("http://", "https://")):
        return "url"
    if _EMAIL_RE.match(v):
        return "email"
    try:
        # The unstripped `value`, not `v`: whitespace-wrapped input must not auto-detect
        # as a clean domain just because trimming it would look like one.
        _normalize_domain(value)
    except ValueError:
        pass
    else:
        return "domain"
    if _PHONE_RE.match(v) and sum(c.isdigit() for c in v) >= 7:
        return "phone"
    if v.startswith("@") and _HANDLE_RE.match(v[1:]):
        return "username"
    return "unknown"


def plan_investigation(value: str, kind: str | None = None) -> Plan:
    raw_value = value
    value = value.strip()
    if not value:
        raise ValueError("value is required")
    # Auto-detection must see the unstripped input too: detect_investigation_type applies
    # the same unstripped `_normalize_domain` rule, so whitespace-wrapped input can't
    # auto-classify as "domain" either.
    detected = kind or detect_investigation_type(raw_value)
    if detected not in INVESTIGATION_TYPES:
        if kind is None:
            raise ValueError(
                "could not determine an input type automatically; choose one explicitly"
            )
        raise ValueError(f"unknown investigation type {detected!r}")

    if detected == "url":
        url = canonical_url(value)
        return Plan(kind="url", normalized=url, seeds=[url], fields=DEFAULT_FIELDS["url"])
    if detected == "domain":
        # Validate the ORIGINAL, unstripped input, whether kind was explicit or
        # auto-detected: an unvalidated explicit kind must not be able to turn
        # "https://example.org", "example.org/path", or " example.org " (silently
        # stripped) into a malformed seed/site: query.
        host = _normalize_domain(raw_value)
        url = canonical_url(f"https://{host}/")
        # The direct seed always works without search; this bounded site: query is
        # additive and only ever dispatched if search happens to be configured too
        # (see Engine.submit: seeds alone are enough, so an unconfigured search is
        # skipped silently rather than failing the job).
        return Plan(
            kind="domain",
            normalized=host,
            seeds=[url],
            discovery_queries=_bounded([f"site:{host}"]),
            fields=DEFAULT_FIELDS["domain"],
        )
    if detected == "email":
        email = value.strip().lower()
        local, _, domain = email.partition("@")
        queries = _bounded([f'"{email}"', f"{local} {domain}", f"{email} contact"])
        return Plan(
            kind="email",
            normalized=email,
            discovery_queries=queries,
            fields=DEFAULT_FIELDS["email"],
        )
    if detected == "phone":
        digits = re.sub(r"[^0-9+]", "", value)
        queries = _bounded([f'"{digits}"', f'"{value.strip()}"'])
        return Plan(
            kind="phone",
            normalized=digits,
            discovery_queries=queries,
            fields=DEFAULT_FIELDS["phone"],
        )
    if detected == "username":
        handle = value.strip().lstrip("@")
        queries = _bounded([f'"{handle}"', f"{handle} profile", f"@{handle}"])
        return Plan(
            kind="username",
            normalized=handle,
            discovery_queries=queries,
            fields=DEFAULT_FIELDS["username"],
        )
    if detected == "person":
        name = " ".join(value.split())
        queries = _bounded([f'"{name}"', f"{name} profile", f"{name} contact"])
        return Plan(
            kind="person",
            normalized=name,
            discovery_queries=queries,
            fields=DEFAULT_FIELDS["person"],
        )
    if detected == "organization":
        name = " ".join(value.split())
        queries = _bounded([f'"{name}"', f"{name} official site", f"{name} contact"])
        return Plan(
            kind="organization",
            normalized=name,
            discovery_queries=queries,
            fields=DEFAULT_FIELDS["organization"],
        )
    if detected == "address":
        address = " ".join(value.split())
        queries = _bounded([f'"{address}"'])
        return Plan(
            kind="address",
            normalized=address,
            discovery_queries=queries,
            fields=DEFAULT_FIELDS["address"],
        )
    identifier = value.strip()
    queries = _bounded([f'"{identifier}"'])
    return Plan(
        kind="identifier",
        normalized=identifier,
        discovery_queries=queries,
        fields=DEFAULT_FIELDS["identifier"],
    )


def plan_dataset(
    description: str, fields: list[str] | None = None, seeds: list[str] | None = None
) -> Plan:
    description = description.strip()
    if not description:
        raise ValueError("description is required")
    derived: list[str] = []
    for pattern, extra in _DATASET_FIELD_RULES:
        if pattern.search(description):
            derived.extend(extra)
    if fields:
        resolved_fields = list(dict.fromkeys(f.strip() for f in fields if f.strip()))
    else:
        resolved_fields = list(dict.fromkeys(derived)) or list(DEFAULT_DATASET_FIELDS)
    normalized_seeds = [canonical_url(s) for s in (seeds or [])]
    queries = (
        []
        if normalized_seeds
        else _bounded([description, f"{description} listings", f"{description} for sale"])
    )
    return Plan(
        kind="dataset",
        normalized=description,
        seeds=normalized_seeds,
        discovery_queries=queries,
        fields=resolved_fields,
    )
