import socket

import httpcore
import pytest
from fastapi.testclient import TestClient

from harvest.api import create_app
from harvest.models import PolicyDenied, canonical_url
from harvest.network import PublicBackend, resolved_addresses


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@example.org/",
        "http://example.org/\nx",
        "ftp://example.org",
    ],
)
def test_rejects_unsafe_url_forms(url):
    with pytest.raises(ValueError):
        canonical_url(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:127.0.0.1"]
)
def test_blocks_private_and_metadata_destinations(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))],
    )
    with pytest.raises(PolicyDenied):
        resolved_addresses("source.example", 80)


def test_connection_pins_validated_ip_preventing_dns_rebinding(monkeypatch):
    calls = []

    def lookup(*args, **kwargs):
        calls.append("lookup")
        address = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

    connected = []
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(
        httpcore.SyncBackend,
        "connect_tcp",
        lambda self, host, *args: connected.append(host) or object(),
    )
    PublicBackend().connect_tcp("example.org", 80)
    assert connected == ["93.184.216.34"]
    assert calls == ["lookup"]


def test_api_auth_idempotency_events_and_export(engine, source):
    client = TestClient(create_app(engine.settings))
    assert client.get("/jobs").status_code == 401
    headers = {"Authorization": "Bearer " + engine.settings.api_token, "Idempotency-Key": "api-job"}
    data = {
        "objective": "Collect sample records",
        "seeds": [source["base"] + "/page2"],
        "allowed_domains": ["127.0.0.1"],
        "limits": {"depth": 0, "domain_delay": 0.1},
    }
    response = client.post("/jobs", headers=headers, json=data)
    assert response.status_code == 202
    job = response.json()["id"]
    assert client.post("/jobs", headers=headers, json=data).json()["id"] == job
    changed = {**data, "objective": "Something else"}
    assert client.post("/jobs", headers=headers, json=changed).status_code == 409
    engine.run(job)
    assert client.get(f"/jobs/{job}", headers=headers).json()["status"] == "completed"
    export = client.get(f"/jobs/{job}/export", headers=headers)
    assert export.status_code == 200 and '"evidence"' in export.text
    assert client.get(f"/jobs/{job}/events?limit=5000", headers=headers).status_code == 422
    assert client.get("/jobs/missing", headers=headers).status_code == 404
    raw = client.get("/captures/1/body", headers=headers)
    assert raw.headers["content-type"] == "application/octet-stream"
    assert raw.headers["x-content-type-options"] == "nosniff"
