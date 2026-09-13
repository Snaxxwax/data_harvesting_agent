"""Versioned, owned-source workload probes. Real HTTP; scripted model, not a quality benchmark."""

import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harvest.config import Settings
from harvest.engine import Engine
from harvest.extract import records_extraction
from harvest.models import JobSpec, Limits, ReplaySpec


@pytest.fixture
def workload(tmp_path):
    state = {"version": 1, "requests": Counter(), "model_inputs": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, media="application/json", status=200):
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            state["requests"][path] += 1
            if path == "/robots.txt":
                self.reply("User-agent: *\nAllow: /\n", "text/plain")
            elif path == "/directory.json":
                self.reply(
                    {
                        "@id": "urn:company:northstar",
                        "id": "northstar",
                        "name": "Northstar Labs",
                        "employees": 42,
                    }
                )
            elif path == "/profile.json":
                # A supplier export with a trailing comma. Do not silently repair it.
                self.reply('{"id":"northstar","employees":45,}')
            elif path == "/register.csv":
                rows = ["id,name,jurisdiction,status,employees,updated"]
                rows += [f"{i},Organization {i},GB,active,{i + 3},2026-09-01" for i in range(250)]
                self.reply("\n".join(rows), "text/csv")
            elif path == "/status.json":
                if state["version"] == 1:
                    self.reply({"id": "northstar", "employees": 42})
                else:
                    self.reply('{"id":"northstar","employees":45,}')
            elif path == "/search":
                self.reply(
                    {
                        "results": [
                            {
                                "url": state["base"] + "/research",
                                "title": "Northstar primary evidence",
                            }
                        ]
                    }
                )
            elif path == "/research":
                self.reply(
                    '<title>Northstar evidence index</title><a href="/primary.json">Primary register</a><a href="/legacy">Supplier export</a><a href="/report.txt">Audit report</a>',
                    "text/html",
                )
            elif path == "/primary.json":
                self.reply({"id": "northstar", "employees": 42, "status": "active"})
            elif path == "/legacy":
                self.reply(
                    "company=Northstar Labs\nemployees=45\n", "application/x-northstar-export"
                )
            elif path == "/report.txt":
                self.reply(
                    "Annual audit methodology. " * 700 + "Northstar accreditation is ISO 27001.",
                    "text/plain",
                )
            else:
                self.reply({}, status=404)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = json.loads(request["messages"][1]["content"])
            state["model_inputs"].append(prompt)
            quote = "Northstar accreditation is ISO 27001."
            claims = (
                [{"field": "accreditation", "value": "ISO 27001", "quote": quote, "confidence": 1}]
                if quote in prompt["SOURCE_TEXT"]
                else []
            )
            self.reply(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "claims": claims,
                                        "gaps": [] if claims else ["accreditation"],
                                        "rationale": "Report only the supplied evidence.",
                                    }
                                )
                            }
                        }
                    ]
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        database=str(tmp_path / "workload.sqlite"),
        private_hosts=frozenset({"127.0.0.1"}),
        proxy=None,
    )
    settings.search_url = state["base"]
    settings.model_url = state["base"] + "/v1"
    settings.model_name = "scripted-evidence-reader"
    settings.model_usd_per_million = 0
    yield Engine(settings), state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def run_case(engine, state, mode, paths, **kwargs):
    spec = JobSpec(
        objective="Investigate Northstar Labs employees and accreditation",
        mode=mode,
        seeds=[state["base"] + p for p in paths],
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=2, seconds=30, model_tokens=500000),
        **kwargs,
    )
    result = engine.run(engine.submit(spec))
    observations = engine.store.observations(result["id"], limit=10000)
    metrics = {
        "mode": mode,
        "status": result["status"],
        "captures": result["captures"],
        "observations": len(observations),
        "entities": len({o["entity_key"] for o in observations}),
        "missing_fields": result["missing_fields"],
        "requests": result["requests"],
    }
    print("WORKLOAD " + json.dumps(metrics, sort_keys=True))
    return result, observations


def test_targeted_imperfect_supplier_export(workload):
    engine, state = workload
    result, observations = run_case(
        engine, state, "targeted", ["/directory.json", "/profile.json"], fields=["employees"]
    )
    assert result["status"] == "partial"
    assert any(o["value"] == 42 for o in observations)
    assert result["captures"] == 2
    assert engine.store.captures(result["id"])[1]["body_hash"]


def test_enumeration_register_250_rows(workload):
    engine, state = workload
    result, observations = run_case(
        engine, state, "enumerative", ["/register.csv"], fields=["name", "status"]
    )
    assert result["status"] == "completed"
    assert len({o["entity_key"] for o in observations}) == 250
    assert result["records_processed"] == 250
    assert engine.store.extractions(result["id"])[0]["records_remaining"] == 0
    assert result["missing_fields"] == []  # Presence is NOT population coverage.


def test_continuous_source_schema_break(workload):
    engine, state = workload
    first, _ = run_case(
        engine, state, "continuous", ["/status.json"], fields=["employees"], refresh_seconds=60
    )
    state["version"] = 2
    with engine.store.transaction() as db:
        db.execute("UPDATE schedules SET next_run=0 WHERE id=?", (first["id"],))
    engine.store.schedule_tick()
    child = next(j for j in engine.store.jobs() if j["id"] != first["id"])
    refreshed = engine.run(child["id"])
    print(
        "WORKLOAD "
        + json.dumps(
            {
                "mode": "continuous_refresh",
                "status": refreshed["status"],
                "captures": refreshed["captures"],
                "requests": refreshed["requests"],
            },
            sort_keys=True,
        )
    )
    assert refreshed["status"] in {"failed", "partial"}
    assert refreshed["captures"] == 1
    assert engine.store.extractions(child["id"])[0]["status"] == "failed"
    entity = engine.store.canonical("default")[0]
    assert entity["fields"]["employees"]["value"] == 42
    assert entity["source_states"][0]["stale"]
    assert entity["source_states"][0]["status"] == "failed"


def test_deep_research_mixed_primary_evidence(workload):
    engine, state = workload
    result, observations = run_case(
        engine, state, "deep_research", [], fields=["employees", "accreditation"], use_model=True
    )
    assert result["status"] == "partial"
    assert "accreditation" in result["missing_fields"]
    assert len(state["model_inputs"]) == 3
    assert not any(o["field"] == "accreditation" for o in observations)
    assert result["captures"] == 5  # Search plus all four permitted source responses.


def test_owned_supplier_export_can_be_recovered_after_adapter_install(workload):
    engine, state = workload
    original, _ = run_case(engine, state, "targeted", ["/legacy"], fields=["employees"])
    assert original["extraction_progress"] == {"failed": 1}
    before = dict(state["requests"])
    capture = engine.store.captures(original["id"])[0]

    class SupplierAdapter:
        name = "owned-supplier-format/1"

        def accepts(self, media, url):
            return media == "application/x-northstar-export"

        def extract(self, body, url):
            record = dict(line.split("=", 1) for line in body.decode().splitlines())
            return records_extraction([record], url, extractor=self.name)

    engine.extractors.adapters.insert(0, SupplierAdapter())
    replay = engine.run(engine.replay(ReplaySpec(capture_ids=[capture["id"]])))
    rows = engine.store.observations(replay["id"])
    assert replay["status"] == "completed" and replay["requests"] == 0
    assert any(r["field"] == "employees" and r["value"] == "45" for r in rows)
    assert dict(state["requests"]) == before
    assert all(r["capture_ids"] == [capture["id"]] for r in rows)
    assert engine.store.observations(original["id"]) == []
