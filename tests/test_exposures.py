import json
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from harvest.api import create_app
from harvest.cli import main
from harvest.config import Settings
from harvest.exposures import (
    ExposureImport,
    ExposureRecord,
    ExposureSearch,
    Identifier,
    import_jsonl,
    import_records,
    search,
)
from harvest.store import Store


def record(**changes):
    return {
        "source": "fixture-provider",
        "source_record_id": "original-row-1",
        "source_url": "https://example.org/breach/demo",
        "breach_name": "Fixture breach",
        "evidence_level": "record_observed",
        "identifiers": [{"kind": "email", "value": "alice@example.org"}],
        "observed_fields": ["email", "phone"],
        "reported_fields": ["password_hash"],
        "evidence_ref": "export-2026:row-1",
        "breach_date": "2020-01-01",
        "published_at": "2020-02-01T00:00:00Z",
        "acquired_at": "2026-10-14T00:00:00Z",
        **changes,
    }


def query(**changes):
    return ExposureSearch.model_validate(
        {
            "dataset": "customer-a",
            "identifier": {"kind": "email", "value": "alice@example.org"},
            **changes,
        }
    )


def put(store, *records, dataset="customer-a"):
    return import_records(store, ExposureImport(dataset=dataset, records=list(records)))


@pytest.fixture
def archive(tmp_path):
    return Store(tmp_path / "archive.sqlite")


def test_history_survives_reimport_restart_and_preserves_provenance(archive):
    original = record()
    assert put(archive, original)["inserted"] == 1
    assert put(archive, original) == {"dataset": "customer-a", "inserted": 0, "duplicates": 1}
    revised = record(observed_fields=["email", "address"], acquired_at="2026-10-15T00:00:00Z")
    put(archive, revised)
    result = search(Store(archive.path), query())
    assert len(result["items"]) == 2
    assert {tuple(row["observed_fields"]) for row in result["items"]} == {
        ("email", "phone"),
        ("address", "email"),
    }
    for row in result["items"]:
        assert row["breach_date"] == "2020-01-01"
        assert row["evidence_ref"] == "export-2026:row-1"
        assert row["source_url"] == original["source_url"]
        assert row["reported_fields"] == ["password_hash"]
        assert "identifiers" not in row
    assert result["identity_verified"] is False
    assert result["coverage"] == "local_imports_only"


def test_exact_match_dataset_source_and_evidence_filters(archive):
    put(
        archive,
        record(),
        record(source="another-provider", evidence_level="provider_reported", observed_fields=[]),
    )
    put(archive, record(breach_name="Other operator dataset"), dataset="customer-b")
    assert len(search(archive, query())["items"]) == 2
    assert len(search(archive, query(source="another-provider"))["items"]) == 1
    assert len(search(archive, query(evidence_level="record_observed"))["items"]) == 1
    for value in ("ali@example.org", "alice+tag@example.org", "Alice@example.org", "%", "' OR 1=1"):
        if "@" not in value:
            with pytest.raises(ValidationError):
                query(identifier={"kind": "email", "value": value})
        else:
            assert (
                search(archive, query(identifier={"kind": "email", "value": value}))["items"] == []
            )
    assert search(archive, query(dataset="unknown"))["indexed_records"] == 0
    assert search(archive, query(source="' OR 1=1 --"))["items"] == []
    assert (
        len(
            search(archive, query(identifier={"kind": "email", "value": "alice@EXAMPLE.ORG"}))[
                "items"
            ]
        )
        == 2
    )


def test_username_namespace_and_phone_exactness(archive):
    put(
        archive,
        record(
            identifiers=[
                {"kind": "username", "namespace": "forum-a", "value": "alice"},
                {"kind": "phone", "value": "+12025550123"},
            ]
        ),
    )
    for identifier, count in (
        ({"kind": "username", "namespace": "forum-a", "value": "alice"}, 1),
        ({"kind": "username", "namespace": "forum-b", "value": "alice"}, 0),
        ({"kind": "phone", "value": "+12025550123"}, 1),
        ({"kind": "phone", "value": "+12025550124"}, 0),
    ):
        assert len(search(archive, query(identifier=identifier))["items"]) == count


def test_cursor_pagination_does_not_drop_or_repeat_historical_rows(archive):
    put(archive, *(record(source_record_id=f"row-{i}") for i in range(5)))
    ids = []
    after = ""
    while True:
        page = search(archive, query(after=after, limit=2))
        ids.extend(row["id"] for row in page["items"])
        if not page["next_after"]:
            break
        after = page["next_after"]
    assert len(ids) == len(set(ids)) == 5
    assert ids == sorted(ids)


@pytest.mark.parametrize(
    "changes",
    [
        {"password": "not-accepted"},
        {"evidence_level": "provider_reported"},
        {"acquired_at": "2020-01-01T00:00:00"},
        {"source_url": "https://user:secret@example.org"},
        {"identifiers": []},
        {"observed_fields": ["arbitrary-value"]},
    ],
)
def test_rejects_raw_fields_and_ambiguous_evidence(changes):
    with pytest.raises(ValidationError):
        ExposureRecord.model_validate(record(**changes))


@pytest.mark.parametrize(
    "identifier",
    [
        {"kind": "username", "value": "alice"},
        {"kind": "phone", "value": "202-555-0123"},
        {"kind": "email", "value": "alice@example.org", "namespace": "forum"},
        {"kind": "email", "value": "alice@example.org\x00"},
    ],
)
def test_rejects_ambiguous_identifiers(identifier):
    with pytest.raises(ValidationError):
        Identifier.model_validate(identifier)


def test_normalization_is_idempotent(archive):
    put(archive, record())
    reordered = record(
        observed_fields=["phone", "email", "email"],
        acquired_at="2026-10-13T19:00:00-05:00",
        identifiers=[{"kind": "email", "value": "alice@EXAMPLE.ORG"}] * 2,
    )
    assert put(archive, reordered)["duplicates"] == 1


def test_api_protects_import_and_search_and_validates_whole_batch(archive):
    settings = Settings(database=archive.path, api_token="test-token-more-than-24-characters")
    client = TestClient(create_app(settings))
    batch = {"dataset": "customer-a", "records": [record()]}
    for path, body in (("/exposures/import", batch), ("/exposures/search", query().model_dump())):
        assert client.post(path, json=body).status_code == 401
    client.headers["Authorization"] = "Bearer " + settings.api_token
    invalid = {**batch, "records": [record(), record(password="secret")]}
    assert client.post("/exposures/import", json=invalid).status_code == 422
    assert search(archive, query())["indexed_records"] == 0
    assert client.post("/exposures/import", json=batch).json()["inserted"] == 1
    response = client.post("/exposures/search", json=query().model_dump())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert len(response.json()["items"]) == 1
    assert (
        client.post("/exposures/search", json={**query().model_dump(), "limit": 201}).status_code
        == 422
    )
    assert client.get("/meta").json()["historical_exposures"]["coverage"] == "local_imports_only"


def test_streaming_import_retry_after_bad_record(archive, tmp_path):
    path = tmp_path / "records.jsonl"
    rows = [record(source_record_id=f"row-{i}") for i in range(501)]
    path.write_text("\n".join(json.dumps(row) for row in rows[:500]) + '\n{"password":"secret"}\n')
    with pytest.raises(ValueError, match="line 501; 500 records committed") as error:
        import_jsonl(archive, path, "customer-a")
    assert "secret" not in str(error.value)
    path.write_text("\n".join(json.dumps(row) for row in rows))
    result = import_jsonl(archive, path, "customer-a")
    assert result == {"dataset": "customer-a", "inserted": 1, "duplicates": 500}
    path.write_bytes(b"x" * 65537)
    with pytest.raises(ValueError, match="line 1"):
        import_jsonl(archive, path, "customer-a")


def test_cli_import_and_search(archive, tmp_path, monkeypatch, capsys):
    source = tmp_path / "input.jsonl"
    source.write_text(json.dumps(record()) + "\n")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harvest",
            "--db",
            archive.path,
            "import-exposures",
            str(source),
            "--dataset",
            "customer-a",
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["inserted"] == 1
    request = tmp_path / "query.json"
    request.write_text(query().model_dump_json())
    monkeypatch.setattr(
        sys, "argv", ["harvest", "--db", archive.path, "search-exposures", str(request)]
    )
    main()
    assert len(json.loads(capsys.readouterr().out)["items"]) == 1
