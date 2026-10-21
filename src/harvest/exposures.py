"""Local historical exposure index. Import metadata, never raw credentials or dump files.

This archive is deliberately separate from generic observations: finding an email on a
profile is not evidence that it appeared in a breach. Importers explicitly state the
source and evidence level. No network access or identity inference happens here.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .models import StrictModel, canonical_url
from .store import digest, packed

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Dataset = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]
FieldName = Literal[
    "email",
    "username",
    "phone",
    "name",
    "address",
    "date_of_birth",
    "password",
    "password_hash",
    "session_token",
    "financial",
    "government_id",
    "other",
]
EvidenceLevel = Literal["record_observed", "provider_reported", "publisher_claim"]


class Identifier(StrictModel):
    kind: Literal["email", "phone", "username"]
    value: str = Field(min_length=1, max_length=254)
    namespace: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def canonical(self):
        value = self.value.strip()
        if not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("identifier must be a nonempty value without whitespace or controls")
        if self.kind == "email":
            if re.fullmatch(r"[^@]+@[^@]+", value) is None:
                raise ValueError("email must contain a local part and domain")
            local, domain = value.rsplit("@", 1)
            domain = domain.encode("idna").decode().lower()
            if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain) is None:
                raise ValueError("invalid email domain")
            # Preserve local-part case, dots and plus tags; provider-specific aliases
            # must be enrolled explicitly rather than merged into somebody else's record.
            value = f"{local}@{domain}"
        elif self.kind == "phone" and re.fullmatch(r"\+[1-9][0-9]{6,14}", value) is None:
            raise ValueError("phone must use international +countrycode format")
        if self.kind == "username":
            if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", self.namespace) is None:
                raise ValueError("username requires a lowercase source namespace")
        elif self.namespace:
            raise ValueError("namespace is only supported for usernames")
        self.value = value
        return self


class ExposureRecord(StrictModel):
    breach_id: Dataset | None = None
    source: Text
    source_record_id: Text
    source_url: str = Field(max_length=4096)
    breach_name: Text
    evidence_level: EvidenceLevel
    identifiers: list[Identifier] = Field(min_length=1, max_length=20)
    observed_fields: list[FieldName] = Field(default_factory=list, max_length=20)
    reported_fields: list[FieldName] = Field(default_factory=list, max_length=20)
    # A locator in the source (row ID, provider result ID, capture reference), not a
    # free-text copy of a sensitive record. The source URL is never fetched on import.
    evidence_ref: Text
    breach_date: date | None = None
    published_at: AwareDatetime | None = None
    acquired_at: AwareDatetime

    @field_validator("source_url")
    @classmethod
    def source_address(cls, value):
        return canonical_url(value)

    @field_validator("acquired_at", "published_at")
    @classmethod
    def utc_timestamp(cls, value):
        return value.astimezone(UTC) if value else None

    @model_validator(mode="after")
    def evidence_contract(self):
        if self.evidence_level != "record_observed" and self.observed_fields:
            raise ValueError("observed_fields requires record_observed evidence")
        self.observed_fields = sorted(set(self.observed_fields))
        self.reported_fields = sorted(set(self.reported_fields))
        unique = {packed(i.model_dump()): i for i in self.identifiers}
        self.identifiers = [unique[key] for key in sorted(unique)]
        return self


class ExposureImport(StrictModel):
    dataset: Dataset
    records: list[ExposureRecord] = Field(min_length=1, max_length=500)


class ExposureSearch(StrictModel):
    dataset: Dataset
    breach_id: Dataset | None = None
    identifier: Identifier
    source: Text | None = None
    evidence_level: EvidenceLevel | None = None
    after: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    limit: int = Field(default=50, ge=1, le=200)


def migrate(store):
    with store.transaction() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS exposure_records (
            id TEXT PRIMARY KEY, dataset TEXT NOT NULL, source TEXT NOT NULL,
            evidence_level TEXT NOT NULL, indexed_at REAL NOT NULL, payload TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS exposure_identifiers (
            dataset TEXT NOT NULL, kind TEXT NOT NULL, namespace TEXT NOT NULL,
            value TEXT NOT NULL, record_id TEXT NOT NULL REFERENCES exposure_records(id),
            PRIMARY KEY(dataset,kind,namespace,value,record_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS exposure_dataset ON exposure_records(dataset,id)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS exposure_identifier_record ON exposure_identifiers(record_id,kind,namespace,value)"
        )


def import_records(store, batch: ExposureImport):
    """Commit a validated batch atomically. Identical imports are safe to retry.

    The content digest includes acquisition time: a changed record or later observation
    is retained as another historical revision, never an overwrite of old evidence.
    """
    inserted = 0
    with store.transaction() as db:
        for record in batch.records:
            payload = record.model_dump(mode="json")
            if record.breach_id is None:
                # Preserve fingerprints of records imported before catalog linking existed.
                payload.pop("breach_id")
            elif not db.execute(
                "SELECT 1 FROM breach_catalog WHERE id=?", (record.breach_id,)
            ).fetchone():
                raise ValueError("exposure references an unknown breach_id")
            record_id = digest(packed([batch.dataset, payload]))
            result = db.execute(
                "INSERT OR IGNORE INTO exposure_records VALUES(?,?,?,?,?,?)",
                (
                    record_id,
                    batch.dataset,
                    record.source,
                    record.evidence_level,
                    time.time(),
                    packed(payload),
                ),
            )
            inserted += result.rowcount
            if record.breach_id is not None:
                db.execute(
                    "INSERT OR IGNORE INTO exposure_breaches VALUES(?,?)",
                    (record_id, record.breach_id),
                )
            for identifier in record.identifiers:
                db.execute(
                    "INSERT OR IGNORE INTO exposure_identifiers VALUES(?,?,?,?,?)",
                    (
                        batch.dataset,
                        identifier.kind,
                        identifier.namespace,
                        identifier.value,
                        record_id,
                    ),
                )
    return {
        "dataset": batch.dataset,
        "inserted": inserted,
        "duplicates": len(batch.records) - inserted,
    }


def import_jsonl(store, path, dataset):
    """Stream normalized records in retryable batches; bound each input line to 64 KiB."""
    totals = {"dataset": dataset, "inserted": 0, "duplicates": 0}
    # Validate the dataset even for an empty file.
    ExposureSearch(dataset=dataset, identifier={"kind": "email", "value": "test@example.org"})
    records = []

    def commit():
        result = import_records(store, ExposureImport(dataset=dataset, records=records))
        for key in ("inserted", "duplicates"):
            totals[key] += result[key]
        records.clear()

    with open(path, "rb") as handle:
        line_number = 0
        while line := handle.readline(65537):
            line_number += 1
            try:
                if len(line) > 65536:
                    raise ValueError("oversized record")
                if not line.strip():
                    continue
                records.append(ExposureRecord.model_validate_json(line))
            except ValueError:
                # Never echo a malformed input record into logs or the terminal.
                raise ValueError(
                    f"invalid exposure record at line {line_number}; "
                    f"{totals['inserted']} records committed before this batch; "
                    "fix the input and retry (identical records are deduplicated)"
                ) from None
            if len(records) == 500:
                commit()
    if records:
        commit()
    return totals


def search(store, query: ExposureSearch):
    """Indexed exact lookup over every imported revision, with bounded cursor pages."""
    identifier = query.identifier
    conditions = [
        "i.dataset=?",
        "i.kind=?",
        "i.namespace=?",
        "i.value=?",
        "i.record_id>?",
    ]
    params = [query.dataset, identifier.kind, identifier.namespace, identifier.value, query.after]
    if query.breach_id is not None:
        conditions.append(
            "EXISTS(SELECT 1 FROM exposure_breaches b WHERE b.record_id=r.id AND b.breach_id=?)"
        )
        params.append(query.breach_id)
    for column, value in (("source", query.source), ("evidence_level", query.evidence_level)):
        if value is not None:
            conditions.append(f"r.{column}=?")
            params.append(value)
    with store.connection() as db:
        count = db.execute(
            "SELECT count(*) FROM exposure_records WHERE dataset=?", (query.dataset,)
        ).fetchone()[0]
        rows = db.execute(
            "SELECT r.* FROM exposure_identifiers i JOIN exposure_records r ON r.id=i.record_id "
            "WHERE " + " AND ".join(conditions) + " ORDER BY i.record_id LIMIT ?",
            [*params, query.limit + 1],
        ).fetchall()
    items = []
    for row in rows[: query.limit]:
        payload = json.loads(row["payload"])
        # Return exposure categories and provenance, not other identifiers in the row.
        payload.pop("identifiers")
        items.append({"id": row["id"], "indexed_at": row["indexed_at"], **payload})
    from .breach_catalog import CoverageQuery, coverage_matrix

    catalog_coverage = coverage_matrix(
        store,
        CoverageQuery(
            dataset=query.dataset,
            breach_id=query.breach_id,
            kind=identifier.kind,
            namespace=identifier.namespace,
            source=query.source,
            evidence_level=query.evidence_level,
        ),
        identifier=identifier,
    )
    return {
        "dataset": query.dataset,
        "coverage": "local_imports_only",
        "indexed_records": count,
        "catalog_coverage": catalog_coverage,
        "match": "exact_identifier",
        "identity_verified": False,
        "items": items,
        "next_after": items[-1]["id"] if len(rows) > query.limit else None,
        "limitations": [
            "No match means no match in imported records; it does not establish no exposure.",
            "Evidence levels and dates are supplied by the importer, not independently verified.",
            "Historical exposure does not establish that a credential is currently usable.",
        ],
    }
