import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from harvest.api import create_app
from harvest.breach_catalog import (
    BreachCatalogImport,
    BreachEntry,
    CoverageImport,
    CoverageQuery,
    coverage_matrix,
    import_catalog,
    import_coverage,
)
from harvest.cli import main
from harvest.config import Settings
from harvest.exposures import ExposureImport, ExposureSearch, import_records, search
from harvest.store import Store, digest, packed


def entry(key="incident-a", **changes):
    return {
        "id": key,
        "name": "Fixture incident",
        "kind": "incident",
        "references": [
            {
                "url": "https://example.org/disclosure",
                "publisher": "Fixture",
                "role": "primary",
                "checked_at": "2026-01-01T00:00:00Z",
            }
        ],
        **changes,
    }


def provider(key="incident-a", **changes):
    return {
        "breach_id": key,
        "source": "fixture",
        "availability": "available",
        "identifiers": [{"kind": "email"}],
        "evidence_detail": "membership",
        "reference": entry()["references"][0],
        **changes,
    }


def exposure(key="incident-a", **changes):
    return {
        "breach_id": key,
        "source": "fixture",
        "source_record_id": "row-1",
        "source_url": "https://example.org/evidence",
        "breach_name": "Fixture incident",
        "evidence_level": "provider_reported",
        "reported_fields": ["email"],
        "identifiers": [{"kind": "email", "value": "alice@example.org"}],
        "evidence_ref": "row-1",
        "acquired_at": "2026-01-01T00:00:00Z",
        **changes,
    }


@pytest.fixture
def archive(tmp_path):
    store = Store(tmp_path / "catalog.sqlite")
    import_catalog(store, BreachCatalogImport(entries=[entry()]))
    return store


def matrix(store, **changes):
    return coverage_matrix(store, CoverageQuery(dataset="demo", kind="email", **changes))


def put(store, *records, dataset="demo"):
    return import_records(store, ExposureImport(dataset=dataset, records=list(records)))


def test_metadata_and_provider_availability_never_claim_a_search(archive):
    assert matrix(archive)["items"][0]["status"] == "metadata_only"
    import_coverage(archive, CoverageImport(dataset="demo", entries=[provider()]))
    row = matrix(archive)["items"][0]
    assert row["status"] == "provider_not_connected"
    assert row["searchable_now"] is False
    assert row["providers"][0]["searched"] is False
    assert row["providers"][0]["connector_configured"] is False
    assert matrix(archive, source="different")["items"][0]["status"] == "metadata_only"
    assert (
        coverage_matrix(archive, CoverageQuery(dataset="other", kind="email"))["items"][0]["status"]
        == "metadata_only"
    )


def test_search_distinguishes_no_match_from_unsearched_and_ignores_page_cursor(archive):
    import_catalog(archive, BreachCatalogImport(entries=[entry("incident-b")]))
    put(archive, exposure())
    for value, outcome in (
        ("alice@example.org", "matched"),
        ("bob@example.org", "no_match_in_imported_records"),
    ):
        result = search(
            archive, ExposureSearch(dataset="demo", identifier={"kind": "email", "value": value})
        )
        by_id = {row["breach"]["id"]: row for row in result["catalog_coverage"]["items"]}
        assert by_id["incident-a"]["lookup_outcome"] == outcome
        assert by_id["incident-b"]["lookup_outcome"] == "not_searched_no_local_records"
        assert by_id["incident-a"]["completeness"] == "unknown"
    result = search(
        archive,
        ExposureSearch(
            dataset="demo",
            identifier={"kind": "email", "value": "alice@example.org"},
            after="f" * 64,
        ),
    )
    assert not result["items"]
    assert result["catalog_coverage"]["items"][0]["lookup_outcome"] == "matched"


def test_kind_namespace_dataset_and_evidence_filters_apply_to_coverage(archive):
    put(
        archive,
        exposure(identifiers=[{"kind": "username", "namespace": "forum-a", "value": "alice"}]),
    )
    put(archive, exposure(), dataset="other")
    assert matrix(archive)["items"][0]["searchable_now"] is False
    for namespace, expected in (("forum-a", True), ("forum-b", False)):
        row = coverage_matrix(
            archive, CoverageQuery(dataset="demo", kind="username", namespace=namespace)
        )["items"][0]
        assert row["searchable_now"] is expected
    put(archive, exposure())
    assert matrix(archive)["items"][0]["imported_record_revisions"] == 1
    assert matrix(archive, source="elsewhere")["items"][0]["searchable_now"] is False
    assert matrix(archive, evidence_level="record_observed")["items"][0]["searchable_now"] is False


def test_breach_filter_and_unknown_ids(archive):
    import_catalog(archive, BreachCatalogImport(entries=[entry("incident-b")]))
    put(archive, exposure(), exposure("incident-b"))
    result = search(
        archive,
        ExposureSearch(
            dataset="demo",
            breach_id="incident-b",
            identifier={"kind": "email", "value": "alice@example.org"},
        ),
    )
    assert len(result["items"]) == 1
    assert result["items"][0]["breach_id"] == "incident-b"
    assert len(result["catalog_coverage"]["items"]) == 1
    with pytest.raises(KeyError):
        matrix(archive, breach_id="missing")
    with pytest.raises(ValueError, match="unknown breach_id"):
        put(archive, exposure(source_record_id="not-committed"), exposure("missing"))
    assert matrix(archive)["items"][0]["imported_record_revisions"] == 1


def test_catalog_revisions_and_provider_retries_preserve_current_metadata(archive):
    revised = entry(name="Corrected name")
    assert import_catalog(archive, BreachCatalogImport(entries=[revised]))["revisions_added"] == 1
    assert import_catalog(archive, BreachCatalogImport(entries=[entry()]))["duplicates"] == 1
    assert matrix(Store(archive.path))["items"][0]["breach"]["name"] == "Corrected name"
    original = CoverageImport(dataset="demo", entries=[provider()])
    import_coverage(archive, original)
    import_coverage(
        archive, CoverageImport(dataset="demo", entries=[provider(availability="unavailable")])
    )
    assert import_coverage(archive, original)["duplicates"] == 1
    assert matrix(archive)["items"][0]["status"] == "records_unavailable"
    with archive.connection() as db:
        assert db.execute("SELECT count(*) FROM breach_catalog_revisions").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM breach_coverage_revisions").fetchone()[0] == 2


def test_catalog_pagination_and_unsupported_provider_kinds(archive):
    import_catalog(archive, BreachCatalogImport(entries=[entry("incident-b"), entry("incident-c")]))
    page = matrix(archive, limit=2)
    assert page["next_after"] == "incident-b"
    assert matrix(archive, after=page["next_after"])["items"][0]["breach"]["id"] == "incident-c"
    import_coverage(
        archive, CoverageImport(dataset="demo", entries=[provider(identifiers=[{"kind": "phone"}])])
    )
    assert matrix(archive)["items"][0]["status"] == "coverage_unknown_or_unsupported"


def test_legacy_exposure_fingerprints_are_unchanged_and_unlinked_is_visible(archive):
    raw = exposure()
    raw.pop("breach_id")
    batch = ExposureImport(dataset="demo", records=[raw])
    payload = batch.records[0].model_dump(mode="json")
    payload.pop("breach_id")
    # Simulate the previous version's row, which did not know about breach IDs.
    record_id = digest(packed(["demo", payload]))
    with archive.transaction() as db:
        db.execute(
            "INSERT INTO exposure_records VALUES(?,?,?,?,?,?)",
            (record_id, "demo", "fixture", "provider_reported", 1, packed(payload)),
        )
    assert import_records(archive, batch)["duplicates"] == 1
    assert matrix(archive)["unlinked_record_revisions"] == 1
    assert matrix(archive)["items"][0]["searchable_now"] is False
    result = search(
        archive,
        ExposureSearch(dataset="demo", identifier={"kind": "email", "value": "alice@example.org"}),
    )
    assert len(result["items"]) == 1


def test_rejects_compilation_date_and_unsourced_count():
    with pytest.raises(ValidationError, match="single breach date"):
        BreachEntry.model_validate(entry(kind="compilation", breach_date="2020-01-01"))
    with pytest.raises(ValidationError, match="cite an entry reference"):
        BreachEntry.model_validate(
            entry(counts=[{"value": 3, "unit": "records", "source_url": "https://elsewhere.org/"}])
        )


def test_coverage_batch_rolls_back_unknown_incident(archive):
    with pytest.raises(ValueError):
        import_coverage(
            archive, CoverageImport(dataset="demo", entries=[provider(), provider("unknown")])
        )
    assert matrix(archive)["items"][0]["providers"] == []


def test_api_auth_validation_and_linked_search(archive):
    settings = Settings(database=archive.path, api_token="test-operator-token-at-least-24-chars")
    client = TestClient(create_app(settings))
    requests = {
        "/breach-catalog/import": {"entries": [entry()]},
        "/breach-coverage/import": {"dataset": "demo", "entries": [provider()]},
        "/breach-coverage/search": {"dataset": "demo", "kind": "email"},
    }
    for path, body in requests.items():
        assert client.post(path, json=body).status_code == 401
    client.headers["Authorization"] = "Bearer " + settings.api_token
    for path, body in requests.items():
        assert client.post(path, json=body).status_code == 200
    bad = {"dataset": "demo", "records": [exposure("missing")]}
    assert client.post("/exposures/import", json=bad).status_code == 422
    assert (
        client.post(
            "/breach-coverage/search",
            json={"dataset": "demo", "kind": "email", "breach_id": "missing"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/exposures/import", json={"dataset": "demo", "records": [exposure()]}
        ).status_code
        == 200
    )
    result = client.post(
        "/exposures/search",
        json={
            "dataset": "demo",
            "breach_id": "incident-a",
            "identifier": {"kind": "email", "value": "alice@example.org"},
        },
    )
    assert result.json()["catalog_coverage"]["items"][0]["lookup_outcome"] == "matched"


def test_cli_starter_catalog_is_metadata_only(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    database = str(tmp_path / "starter.sqlite")
    for command, filename in (
        ("import-breach-catalog", "breach-catalog.json"),
        ("import-breach-coverage", "breach-coverage.json"),
        ("breach-coverage", "breach-coverage-query.json"),
    ):
        monkeypatch.setattr(
            sys, "argv", ["harvest", "--db", database, command, str(root / "examples" / filename)]
        )
        main()
        result = json.loads(capsys.readouterr().out)
    assert len(result["items"]) == 4
    assert all(row["status"] == "provider_not_connected" for row in result["items"])
    assert all(not row["searchable_now"] for row in result["items"])
    store = Store(database)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM exposure_records").fetchone()[0] == 0
