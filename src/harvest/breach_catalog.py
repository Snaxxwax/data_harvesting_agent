"""Incident metadata and provider coverage, independent of person-level exposure evidence."""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .exposures import Dataset, EvidenceLevel, Identifier, Text
from .models import StrictModel, canonical_url
from .store import digest, packed


class Reference(StrictModel):
    url: str = Field(max_length=4096)
    publisher: Text
    role: Literal["primary", "provider", "secondary"]
    checked_at: AwareDatetime

    @field_validator("url")
    @classmethod
    def address(cls, value):
        return canonical_url(value)

    @field_validator("checked_at")
    @classmethod
    def utc_timestamp(cls, value):
        return value.astimezone(UTC)


class ReportedCount(StrictModel):
    value: int = Field(ge=0)
    unit: Literal["records", "accounts", "people", "email_addresses", "passwords", "documents"]
    status: Literal["reported", "estimated", "disputed", "confirmed"] = "reported"
    source_url: str = Field(max_length=4096)

    @field_validator("source_url")
    @classmethod
    def address(cls, value):
        return canonical_url(value)


class BreachEntry(StrictModel):
    id: Dataset
    name: Text
    kind: Literal["incident", "exposure", "scrape", "compilation"]
    breach_date: date | None = None
    disclosed_date: date | None = None
    # Preserve source category names instead of guessing how to collapse unfamiliar fields.
    reported_fields: list[Text] = Field(default_factory=list, max_length=50)
    counts: list[ReportedCount] = Field(default_factory=list, max_length=20)
    references: list[Reference] = Field(min_length=1, max_length=20)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def source_contract(self):
        if self.kind == "compilation" and self.breach_date is not None:
            raise ValueError("a compilation has no single breach date; use disclosed_date")
        if any(c.source_url not in {r.url for r in self.references} for c in self.counts):
            raise ValueError("each count must cite an entry reference")
        self.reported_fields = sorted(set(self.reported_fields))
        return self


class BreachCatalogImport(StrictModel):
    entries: list[BreachEntry] = Field(min_length=1, max_length=500)


class SearchableIdentifier(StrictModel):
    kind: Literal["email", "phone", "username"]
    namespace: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def namespace_contract(self):
        # Reuse the actual lookup namespace rules without enrolling a real identifier.
        value = {"email": "test@example.org", "phone": "+12025550123", "username": "test"}
        Identifier(kind=self.kind, namespace=self.namespace, value=value[self.kind])
        return self


class CoverageEntry(StrictModel):
    breach_id: Dataset
    source: Text
    availability: Literal["available", "unavailable", "unknown"]
    identifiers: list[SearchableIdentifier] = Field(default_factory=list, max_length=20)
    evidence_detail: Literal["membership", "field_categories", "record_fields", "unknown"]
    reference: Reference
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def available_contract(self):
        if self.availability == "available" and not self.identifiers:
            raise ValueError("available coverage requires supported identifier kinds")
        return self


class CoverageImport(StrictModel):
    dataset: Dataset
    entries: list[CoverageEntry] = Field(min_length=1, max_length=500)


class CoverageQuery(SearchableIdentifier):
    dataset: Dataset
    breach_id: Dataset | None = None
    source: Text | None = None
    evidence_level: EvidenceLevel | None = None
    after: str = Field(default="", pattern=r"^(?:[a-zA-Z0-9_-]{1,80})?$")
    limit: int = Field(default=50, ge=1, le=200)


def migrate(store):
    with store.transaction() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS breach_catalog (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS breach_catalog_revisions (
            revision TEXT PRIMARY KEY, breach_id TEXT NOT NULL REFERENCES breach_catalog(id),
            payload TEXT NOT NULL, imported_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS breach_coverage (
            dataset TEXT NOT NULL, breach_id TEXT NOT NULL REFERENCES breach_catalog(id),
            source TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(dataset,breach_id,source)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS breach_coverage_revisions (
            revision TEXT PRIMARY KEY, dataset TEXT NOT NULL,
            breach_id TEXT NOT NULL REFERENCES breach_catalog(id), source TEXT NOT NULL,
            payload TEXT NOT NULL, imported_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS exposure_breaches (
            record_id TEXT PRIMARY KEY REFERENCES exposure_records(id),
            breach_id TEXT NOT NULL REFERENCES breach_catalog(id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS exposure_breach ON exposure_breaches(breach_id)")


def import_catalog(store, batch: BreachCatalogImport):
    revisions = 0
    with store.transaction() as db:
        for entry in batch.entries:
            payload = packed(entry.model_dump(mode="json"))
            revision = digest(payload)
            if db.execute(
                "SELECT 1 FROM breach_catalog_revisions WHERE revision=?", (revision,)
            ).fetchone():
                continue  # retrying an old import must not revert the current catalog
            db.execute(
                "INSERT INTO breach_catalog VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "payload=excluded.payload,updated_at=excluded.updated_at",
                (entry.id, payload, time.time()),
            )
            db.execute(
                "INSERT INTO breach_catalog_revisions VALUES(?,?,?,?)",
                (revision, entry.id, payload, time.time()),
            )
            revisions += 1
    return {"revisions_added": revisions, "duplicates": len(batch.entries) - revisions}


def import_coverage(store, batch: CoverageImport):
    revisions = 0
    with store.transaction() as db:
        for entry in batch.entries:
            if not db.execute(
                "SELECT 1 FROM breach_catalog WHERE id=?", (entry.breach_id,)
            ).fetchone():
                raise ValueError("coverage references an unknown breach_id")
            payload = packed(entry.model_dump(mode="json"))
            revision = digest(packed([batch.dataset, payload]))
            if db.execute(
                "SELECT 1 FROM breach_coverage_revisions WHERE revision=?", (revision,)
            ).fetchone():
                continue
            db.execute(
                "INSERT INTO breach_coverage VALUES(?,?,?,?) "
                "ON CONFLICT(dataset,breach_id,source) DO UPDATE SET payload=excluded.payload",
                (batch.dataset, entry.breach_id, entry.source, payload),
            )
            db.execute(
                "INSERT INTO breach_coverage_revisions VALUES(?,?,?,?,?,?)",
                (revision, batch.dataset, entry.breach_id, entry.source, payload, time.time()),
            )
            revisions += 1
    return {
        "dataset": batch.dataset,
        "revisions_added": revisions,
        "duplicates": len(batch.entries) - revisions,
    }


def coverage_matrix(store, query: CoverageQuery, identifier: Identifier | None = None):
    """Availability comes from local indexed records, never a provider declaration.

    An optional identifier adds per-incident match outcomes, independent of result-page
    cursors. Source and evidence filters apply to local coverage as well as matching.
    """
    with store.connection() as db:
        if (
            query.breach_id
            and not db.execute(
                "SELECT 1 FROM breach_catalog WHERE id=?", (query.breach_id,)
            ).fetchone()
        ):
            raise KeyError(query.breach_id)
        conditions, params = ["id>?"], [query.after]
        if query.breach_id:
            conditions.append("id=?")
            params.append(query.breach_id)
        rows = db.execute(
            "SELECT * FROM breach_catalog WHERE "
            + " AND ".join(conditions)
            + " ORDER BY id LIMIT ?",
            [*params, query.limit + 1],
        ).fetchall()
        items = []
        for row in rows[: query.limit]:
            local_where = ["b.breach_id=?", "r.dataset=?", "i.kind=?", "i.namespace=?"]
            local_params = [row["id"], query.dataset, query.kind, query.namespace]
            for column, value in (
                ("source", query.source),
                ("evidence_level", query.evidence_level),
            ):
                if value is not None:
                    local_where.append(f"r.{column}=?")
                    local_params.append(value)
            sql = (
                " FROM exposure_breaches b JOIN exposure_records r ON r.id=b.record_id "
                "JOIN exposure_identifiers i ON i.record_id=r.id WHERE " + " AND ".join(local_where)
            )
            local_count = db.execute("SELECT count(DISTINCT r.id)" + sql, local_params).fetchone()[
                0
            ]
            providers = []
            for coverage in db.execute(
                "SELECT payload FROM breach_coverage WHERE dataset=? AND breach_id=? ORDER BY source",
                (query.dataset, row["id"]),
            ):
                provider = json.loads(coverage["payload"])
                if query.source is not None and provider["source"] != query.source:
                    continue
                supported = {"kind": query.kind, "namespace": query.namespace} in provider[
                    "identifiers"
                ]
                age = (
                    datetime.now(UTC) - datetime.fromisoformat(provider["reference"]["checked_at"])
                ).total_seconds()
                providers.append(
                    {
                        **provider,
                        "identifier_supported": supported,
                        "check_stale": age > 30 * 86400,
                        "connector_configured": False,
                        "searched": False,
                    }
                )
            if local_count:
                status = "local_records_available"
            elif any(
                p["availability"] == "available" and p["identifier_supported"] for p in providers
            ):
                status = "provider_not_connected"
            elif providers and all(p["availability"] == "unavailable" for p in providers):
                status = "records_unavailable"
            elif providers:
                status = "coverage_unknown_or_unsupported"
            else:
                status = "metadata_only"
            item = {
                "breach": json.loads(row["payload"]),
                "status": status,
                "searchable_now": bool(local_count),
                "imported_record_revisions": local_count,
                "providers": providers,
                "completeness": "unknown",
            }
            if identifier is not None:
                matches = db.execute(
                    "SELECT count(DISTINCT r.id)" + sql + " AND i.value=?",
                    [*local_params, identifier.value],
                ).fetchone()[0]
                item["matching_record_revisions"] = matches
                item["lookup_outcome"] = (
                    "matched"
                    if matches
                    else "no_match_in_imported_records"
                    if local_count
                    else "not_searched_no_local_records"
                )
            items.append(item)
        unlinked = db.execute(
            "SELECT count(*) FROM exposure_records r WHERE dataset=? AND NOT EXISTS "
            "(SELECT 1 FROM exposure_breaches b WHERE b.record_id=r.id)",
            (query.dataset,),
        ).fetchone()[0]
    return {
        "dataset": query.dataset,
        "kind": query.kind,
        "namespace": query.namespace,
        "items": items,
        "unlinked_record_revisions": unlinked,
        "next_after": items[-1]["breach"]["id"] if len(rows) > query.limit else None,
        "external_providers_searched": False,
    }
