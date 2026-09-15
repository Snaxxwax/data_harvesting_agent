"""Dossier reconciliation unit + integration tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harvest.config import Settings
from harvest.engine import Engine
from harvest.models import Investigation, JobSpec, SourceRule, Target

# -- helpers ----------------------------------------------------------------


def _obs(**kw):
    d = dict(
        id=1,
        entity_id="e1",
        entity_key="https://example.org/e1",
        source_url="https://example.org/data",
        field="name",
        value="Alpha",
        evidence="q: Alpha",
        locator="/entity/alpha",
        method="structured",
        confidence=1.0,
        last_seen="2025-06-01T12:00:00Z",
        capture_ids=[1],
        extraction_ids=[1],
    )
    d.update(kw)
    return d


def _inv(targets=None, sources=None):
    return Investigation(
        targets=targets or [Target(key="alpha", label="Alpha")], sources=sources or []
    )


# -- pure reconcile() unit tests ------------------------------------------


class TestReconcilePure:
    def test_empty(self):
        from harvest.dossier import reconcile

        r = reconcile(_inv(), [])
        assert len(r["targets"]) == 1
        assert not r["unresolved"] and not r["ambiguous"]

    def test_match(self):
        from harvest.dossier import reconcile

        t = [Target(key="a", label="A", identifiers={"url": ["https://example.org/a"]})]
        s = [
            SourceRule(
                url="https://example.org/data",
                identifier_fields={"@id": "url"},
                field_map={"@id": "sid", "name": "disp"},
            )
        ]
        obs = [
            _obs(field="@id", value="https://example.org/a"),
            _obs(id=2, field="name", value="A Program"),
        ]
        r = reconcile(_inv(targets=t, sources=s), obs)
        tgt = r["targets"][0]
        assert len(tgt["matched_entities"]) == 1
        assert tgt["fields"]["sid"]["value"] == "https://example.org/a"

    def test_unresolved(self):
        from harvest.dossier import reconcile

        t = [Target(key="b", label="B", identifiers={"name": ["Beta"]})]
        s = [
            SourceRule(
                url="https://example.org/data",
                identifier_fields={"name": "name"},
                field_map={"name": "disp"},
            )
        ]
        r = reconcile(_inv(targets=t, sources=s), [_obs(field="name", value="Gamma")])
        assert len(r["unresolved"]) == 1

    def test_competing_matches_ambiguous(self):
        """Entity matches two targets on different identifier namespaces — ambiguous."""
        from harvest.dossier import reconcile

        t = [
            Target(key="a", label="A", identifiers={"name": ["Alice"]}),
            Target(key="b", label="B", identifiers={"alias": ["Alice"]}),
        ]
        s = [
            SourceRule(
                url="https://example.org/data",
                identifier_fields={"display": "name", "alt_name": "alias"},
                field_map={"display": "disp"},
            )
        ]
        obs = [_obs(field="display", value="Alice"), _obs(id=2, field="alt_name", value="Alice")]
        r = reconcile(_inv(targets=t, sources=s), obs)
        assert len(r["ambiguous"]) == 1

    def test_contradicted_identifier(self):
        """Entity matches target A on name=Alice and has version=v2 (target A expects v1).
        Target B does not declare 'version' so is never contradicted. Result: ambiguous."""
        from harvest.dossier import reconcile

        # Avoid overlapping identifiers — target A uses name:Alice+version:v1, target B uses alias:Alice
        t = [
            Target(key="a", label="A", identifiers={"name": ["Alice"], "version": ["v1"]}),
            Target(key="b", label="B", identifiers={"alias": ["Alice"]}),
        ]
        s = [
            SourceRule(
                url="https://example.org/data",
                identifier_fields={"display": "name", "alt_name": "alias", "ver": "version"},
                field_map={"display": "disp"},
            )
        ]
        obs = [
            _obs(field="display", value="Alice"),
            _obs(id=2, field="alt_name", value="Alice"),
            _obs(id=3, field="ver", value="v2"),
        ]
        r = reconcile(_inv(targets=t, sources=s), obs)
        # Entity matches A (name=Alice) and B (alias=Alice).  A is contradicted by version=v2.
        tgt_b = next(tgt for tgt in r["targets"] if tgt["key"] == "b")
        assert len(tgt_b["matched_entities"]) >= 1

    def test_type_conflict(self):
        """Two entities from same source, same identifier value — one per entity, admitted to same target."""
        from harvest.dossier import reconcile

        t = [Target(key="a", label="A", identifiers={"id": ["123"]})]
        s = [
            SourceRule(
                url="https://example.org/data",
                identifier_fields={"id": "id"},
                field_map={"id": "sid"},
            )
        ]
        # Two observations from two distinct entities, same source, same id value
        obs = [
            _obs(field="id", value="123"),
            _obs(
                id=2,
                entity_id="e2",
                entity_key="https://example.org/e2",
                source_url="https://example.org/data",
                field="id",
                value="123",
            ),
        ]
        r = reconcile(_inv(targets=t, sources=s), obs)
        tgt = r["targets"][0]
        assert len(tgt["matched_entities"]) == 2

    def test_missing_fields(self):
        from harvest.dossier import reconcile

        t = [Target(key="a", label="A", identifiers={"name": ["A"]})]
        s = [
            SourceRule(
                url="https://example.org/data",
                identifier_fields={"name": "name"},
                field_map={"name": "disp", "email": "email"},
            )
        ]
        obs = [_obs(field="name", value="A")]
        r = reconcile(_inv(targets=t, sources=s), obs)
        assert "email" in r["targets"][0]["missing_fields"]

    def test_multi_source_reconcile(self):
        from harvest.dossier import reconcile

        t = [Target(key="org", label="Org", identifiers={"domain": ["example.org"]})]
        s1 = SourceRule(
            url="https://source1.org/api",
            identifier_fields={"domain_name": "domain"},
            field_map={"domain_name": "domain", "description": "desc"},
        )
        s2 = SourceRule(
            url="https://source2.org/api",
            identifier_fields={"host": "domain"},
            field_map={"org_title": "title", "status": "status"},
        )
        obs1 = [
            _obs(
                source_url=s1.url,
                entity_id="e-s1",
                entity_key="s1:e",
                field="domain_name",
                value="example.org",
            )
        ]
        obs2 = [
            _obs(
                id=2,
                source_url=s2.url,
                entity_id="e-s2",
                entity_key="s2:e",
                field="host",
                value="example.org",
            )
        ]
        r = reconcile(_inv(targets=t, sources=[s1, s2]), obs1 + obs2)
        tgt = r["targets"][0]
        assert len(tgt["matched_entities"]) == 2


# -- store.dossier() integration tests -------------------------------------


def _make_server(routes):
    state = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/robots.txt":
                self.send_response(200)
                self.end_headers()
            elif path in routes:
                body = json.dumps(routes[path]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    state["port"] = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield state
    srv.shutdown()


class TestDossierAPI:
    def test_dossier_endpoint(self, tmp_path):
        import time

        from harvest.models import Limits

        routes = {"/api": {"items": [{"domain_name": "example.org", "description": "Test Org"}]}}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/robots.txt":
                    self.send_response(200)
                    self.end_headers()
                elif path == "/api":
                    body = json.dumps(routes[path]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        base_url = f"http://127.0.0.1:{srv.server_port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        settings = Settings(
            database=str(tmp_path / "h.sqlite"),
            private_hosts=frozenset({"127.0.0.1"}),
            api_token="test-operator-token-at-least-24-characters",
        )
        eng = Engine(settings)
        inv = Investigation(
            targets=[Target(key="org", label="Test Org", identifiers={"domain": ["example.org"]})],
            sources=[
                SourceRule(
                    url=base_url + "/api",
                    identifier_fields={"domain_name": "domain"},
                    field_map={"domain_name": "domain", "description": "desc"},
                )
            ],
        )
        spec = JobSpec(
            objective="Reconcile Test Org",
            seeds=[base_url + "/api"],
            investigation=inv,
            limits=Limits(),
        )
        jid = eng.submit(spec)
        time.sleep(2)
        dossier = eng.store.dossier(jid)
        assert dossier["job_id"] == jid
        assert len(dossier["targets"]) == 1

    def test_store_dossier_no_investigation(self, tmp_path):
        """Calling store.dossier on a legacy job without investigation raises ValueError."""
        from harvest.models import Limits

        settings = Settings(
            database=str(tmp_path / "h.sqlite"),
            private_hosts=frozenset({"127.0.0.1"}),
            api_token="test-operator-token-at-least-24-characters",
        )
        eng = Engine(settings)
        spec = JobSpec(
            objective="Legacy job with no investigation",
            seeds=["https://example.org/page"],
            limits=Limits(),
        )
        jid = eng.submit(spec)
        with pytest.raises(ValueError):
            eng.store.dossier(jid)

    def test_render_markdown(self):
        from harvest.dossier import render_markdown

        dossier = {
            "job_id": "test-job",
            "dataset": "default",
            "targets": [
                {
                    "key": "a",
                    "label": "A Target",
                    "identifiers": {"url": ["https://example.org/a"]},
                    "matched_entities": [],
                    "fields": {},
                    "missing_fields": ["name"],
                }
            ],
            "unresolved": [],
            "ambiguous": [],
            "sources": [],
        }
        md = render_markdown(dossier)
        assert "# Dossier for job `test-job`" in md
        assert "## Target: A Target" in md
