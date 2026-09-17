import csv
import io

from fastapi.testclient import TestClient

from harvest.api import create_app


def client_for(engine):
    return TestClient(create_app(engine.settings))


def test_index_and_assets_are_public_and_have_security_headers(engine):
    client = client_for(engine)
    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert '<script src="/assets/app.js"' in index.text
    assert "content-security-policy" in index.headers
    assert "'unsafe-inline'" not in index.headers["content-security-policy"]
    assert index.headers["x-frame-options"] == "DENY"

    css = client.get("/assets/app.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    js = client.get("/assets/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert client.get("/assets/../models.py").status_code in (404, 307)
    assert client.get("/assets/nope.txt").status_code == 404


def test_protected_routes_require_auth_without_token_leak(engine):
    client = client_for(engine)
    assert client.get("/jobs").status_code == 401
    assert client.get("/meta").status_code == 401


def test_session_login_wrong_token_rejected_and_no_cookie_set(engine):
    client = client_for(engine)
    response = client.post("/session", json={"token": "wrong-token-that-is-long-enough"})
    assert response.status_code == 401
    assert "harvest_session" not in response.headers.get("set-cookie", "")


def test_session_login_then_cookie_authenticates_protected_routes(engine):
    client = client_for(engine)
    login = client.post("/session", json={"token": engine.settings.api_token})
    assert login.status_code == 200
    assert "harvest_session" in client.cookies

    meta = client.get("/meta")
    assert meta.status_code == 200
    assert meta.json()["version"]
    assert meta.json()["search_configured"] is False

    jobs = client.get("/jobs")
    assert jobs.status_code == 200


def test_logout_clears_session(engine):
    client = client_for(engine)
    client.post("/session", json={"token": engine.settings.api_token})
    assert client.get("/meta").status_code == 200
    client.post("/logout")
    assert client.get("/meta").status_code == 401


def test_bearer_auth_still_works_independently_of_cookie(engine):
    client = client_for(engine)
    headers = {"Authorization": "Bearer " + engine.settings.api_token}
    assert client.get("/jobs", headers=headers).status_code == 200


def test_plan_investigation_endpoint(engine):
    client = client_for(engine)
    client.post("/session", json={"token": engine.settings.api_token})
    response = client.post("/plan/investigation", json={"value": "example.org"})
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "domain"
    assert body["seeds"] == ["https://example.org/"]


def test_plan_investigation_endpoint_rejects_ambiguous_input_without_kind(engine):
    client = client_for(engine)
    client.post("/session", json={"token": engine.settings.api_token})
    response = client.post("/plan/investigation", json={"value": "Jane Doe"})
    assert response.status_code == 422


def test_plan_investigation_endpoint_rejects_malformed_explicit_domain(engine):
    """Regression for the reported bypass: explicit kind="domain" must reject a scheme
    or path, not silently build a malformed seed/site: query from it. Also covers the
    narrower follow-up: whitespace wrapping and more than one trailing slash/dot must be
    rejected outright, not silently normalized away before validation."""
    client = client_for(engine)
    client.post("/session", json={"token": engine.settings.api_token})
    for value in [
        "https://example.org",
        "example.org/path",
        " example.org ",
        "\texample.org\n",
        "example.org////",
        "example.org..",
    ]:
        response = client.post("/plan/investigation", json={"value": value, "kind": "domain"})
        assert response.status_code == 422, value


def test_plan_investigation_endpoint_accepts_case_insensitive_explicit_domain(engine):
    client = client_for(engine)
    client.post("/session", json={"token": engine.settings.api_token})
    response = client.post(
        "/plan/investigation", json={"value": "Sub.EXAMPLE.org", "kind": "domain"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["normalized"] == "sub.example.org"
    assert body["seeds"] == ["https://sub.example.org/"]
    assert body["discovery_queries"] == ["site:sub.example.org"]


def test_plan_dataset_endpoint(engine):
    client = client_for(engine)
    client.post("/session", json={"token": engine.settings.api_token})
    response = client.post("/plan/dataset", json={"description": "homes under $350,000"})
    assert response.status_code == 200
    assert "price" in response.json()["fields"]


def test_records_and_csv_export_reflect_conflicts(engine, source):
    client = client_for(engine)
    headers = {"Authorization": "Bearer " + engine.settings.api_token}
    data = {
        "objective": "Collect sample records",
        "seeds": [source["base"] + "/records", source["base"] + "/conflict"],
        "allowed_domains": ["127.0.0.1"],
        "limits": {"depth": 0, "domain_delay": 0.1},
    }
    job_id = client.post("/jobs", headers=headers, json=data).json()["id"]
    engine.run(job_id)

    records = client.get(f"/jobs/{job_id}/records", headers=headers)
    assert records.status_code == 200
    alpha = next(r for r in records.json() if "alpha" in r["entity_key"])
    assert alpha["fields"]["name"]["conflict"] is True

    csv_response = client.get(f"/jobs/{job_id}/export.csv", headers=headers)
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    alpha_row = next(r for r in rows if "alpha" in r["entity_key"])
    assert alpha_row["name__status"] == "conflict"
    assert alpha_row["name"] == ""


def test_record_candidate_capture_ids_resolve_via_authenticated_capture_endpoints(engine, source):
    client = client_for(engine)
    headers = {"Authorization": "Bearer " + engine.settings.api_token}
    data = {
        "objective": "Collect sample records",
        "seeds": [source["base"] + "/records"],
        "allowed_domains": ["127.0.0.1"],
        "limits": {"depth": 0, "domain_delay": 0.1},
    }
    job_id = client.post("/jobs", headers=headers, json=data).json()["id"]
    engine.run(job_id)

    records = client.get(f"/jobs/{job_id}/records", headers=headers).json()
    assert records
    candidate = next(iter(records[0]["fields"].values()))["candidates"][0]
    capture_id = candidate["capture_ids"][0]

    meta = client.get(f"/captures/{capture_id}", headers=headers)
    assert meta.status_code == 200
    assert meta.json()["id"] == capture_id
    assert "body" not in meta.json()

    body = client.get(f"/captures/{capture_id}/body", headers=headers)
    assert body.status_code == 200
    assert body.headers["content-type"] == "application/octet-stream"

    assert client.get(f"/captures/{capture_id}").status_code == 401


def test_rerun_endpoint_creates_new_job(engine, source):
    client = client_for(engine)
    headers = {"Authorization": "Bearer " + engine.settings.api_token}
    data = {
        "objective": "Collect sample records",
        "seeds": [source["base"] + "/page2"],
        "allowed_domains": ["127.0.0.1"],
        "limits": {"depth": 0, "domain_delay": 0.1},
    }
    job_id = client.post("/jobs", headers=headers, json=data).json()["id"]
    engine.run(job_id)
    response = client.post(f"/jobs/{job_id}/rerun", headers=headers)
    assert response.status_code == 202
    assert response.json()["id"] != job_id


def test_records_and_csv_expose_requested_field_missing_everywhere(engine, source):
    client = client_for(engine)
    headers = {"Authorization": "Bearer " + engine.settings.api_token}
    data = {
        "objective": "Collect sample records",
        "seeds": [source["base"] + "/page2"],
        "allowed_domains": ["127.0.0.1"],
        "fields": ["name", "phantom_field"],
        "limits": {"depth": 0, "domain_delay": 0.1},
    }
    job_id = client.post("/jobs", headers=headers, json=data).json()["id"]
    engine.run(job_id)

    records = client.get(f"/jobs/{job_id}/records", headers=headers).json()
    assert records
    for record in records:
        assert record["fields"]["phantom_field"]["missing"] is True
        assert record["fields"]["phantom_field"]["value"] is None
        assert record["fields"]["name"]["missing"] is False

    rows = list(
        csv.DictReader(io.StringIO(client.get(f"/jobs/{job_id}/export.csv", headers=headers).text))
    )
    assert all(r["phantom_field__status"] == "missing" for r in rows)
    assert all(r["phantom_field"] == "" for r in rows)


def test_export_csv_neutralizes_formula_leading_requested_field_header(engine, source):
    client = client_for(engine)
    headers = {"Authorization": "Bearer " + engine.settings.api_token}
    data = {
        "objective": "Collect sample records",
        "seeds": [source["base"] + "/page2"],
        "allowed_domains": ["127.0.0.1"],
        "fields": ["name", "=SUM(A1:A9)"],
        "limits": {"depth": 0, "domain_delay": 0.1},
    }
    job_id = client.post("/jobs", headers=headers, json=data).json()["id"]
    engine.run(job_id)

    csv_text = client.get(f"/jobs/{job_id}/export.csv", headers=headers).text
    header = next(csv.reader(io.StringIO(csv_text)))
    assert "=SUM(A1:A9)" not in header
    assert "'=SUM(A1:A9)" in header
    assert "'=SUM(A1:A9)__status" in header
    assert "'=SUM(A1:A9)__sources" in header


def test_rerun_endpoint_rejects_continuous_job(engine, source):
    client = client_for(engine)
    headers = {"Authorization": "Bearer " + engine.settings.api_token}
    data = {
        "objective": "Collect sample records",
        "seeds": [source["base"] + "/page2"],
        "allowed_domains": ["127.0.0.1"],
        "mode": "continuous",
        "refresh_seconds": 60,
        "limits": {"depth": 0, "domain_delay": 0.1},
    }
    job_id = client.post("/jobs", headers=headers, json=data).json()["id"]
    response = client.post(f"/jobs/{job_id}/rerun", headers=headers)
    assert response.status_code == 422
