"""Dossier reconciliation unit + integration tests."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


class TestToolEvidenceReachesTheDossier:
    """A tool capture's source URL is `tool://<name>/<target>`, which source rules used to
    reject, so reconcile found no rule for any tool observation and dropped every one of
    them in silence: a job whose only evidence was a tool scan produced an empty dossier
    that nonetheless reported "completed"."""

    def test_a_tool_capture_can_be_declared_as_a_source(self):
        rule = SourceRule(
            url="tool://maigret/Snaxxwax",
            identifier_fields={"username": "username"},
            field_map={"fullname": "display_name"},
        )
        assert rule.url == "tool://maigret/Snaxxwax"

    def test_a_tool_source_cannot_claim_a_document_binding(self):
        """It would bind nothing: a tool's records are per-account, never one document."""
        with pytest.raises(Exception, match="document_target"):
            SourceRule(
                url="tool://maigret/Snaxxwax",
                document_target="alpha",
                document_fields=["status"],
            )

    def test_observations_with_no_rule_are_counted_not_dropped(self):
        from harvest.dossier import reconcile

        obs = [
            _obs(id=1, source_url="tool://maigret/Snaxxwax", entity_key="url:https://x/a"),
            _obs(id=2, source_url="tool://maigret/Snaxxwax", entity_key="url:https://x/a"),
            _obs(id=3, source_url="tool://maigret/Snaxxwax", entity_key="url:https://x/b"),
        ]
        r = reconcile(_inv(), obs)
        assert not r["targets"][0]["fields"]
        assert r["excluded"] == [
            {
                "source_url": "tool://maigret/Snaxxwax",
                "reason": "no_source_rule",
                "observations": 3,
                "entities": 2,
                "entity_keys": ["url:https://x/a", "url:https://x/b"],
            }
        ]

    def test_an_identifier_match_is_not_a_verified_identity(self):
        """A handle overlapping says an account by that name exists. The dossier has to
        carry the strength of the observation that matched, and say what it does not mean."""
        from harvest.dossier import reconcile, render_markdown

        targets = [Target(key="s", label="Snaxxwax", identifiers={"username": ["Snaxxwax"]})]
        sources = [
            SourceRule(
                url="tool://maigret/Snaxxwax",
                identifier_fields={"username": "username"},
                field_map={"url": "profile_url"},
            )
        ]
        obs = [
            _obs(
                id=1,
                source_url="tool://maigret/Snaxxwax",
                entity_key="url:https://heroesworld.ru/user/Snaxxwax/",
                field="username",
                value="Snaxxwax",
                confidence=0.4,
            ),
            _obs(
                id=2,
                source_url="tool://maigret/Snaxxwax",
                entity_key="url:https://heroesworld.ru/user/Snaxxwax/",
                field="url",
                value="https://heroesworld.ru/user/Snaxxwax/",
                confidence=0.4,
            ),
        ]
        r = reconcile(_inv(targets=targets, sources=sources), obs)
        target = r["targets"][0]
        assert target["fields"]["profile_url"]["value"] == "https://heroesworld.ru/user/Snaxxwax/"
        # The weak status-code check that admitted it is visible, not flattened to 1.0.
        assert target["matched_entities"][0]["match"]["confidence"] == 0.4
        text = render_markdown({**r, "job_id": "j", "dataset": "d", "sources": []})
        assert "identity not verified" in text
        assert "at confidence 0.4" in text

    def test_saved_snaxxwax_scan_reaches_a_dossier(self, tmp_path, monkeypatch):
        """End to end on the real saved scan: tool -> capture -> claims -> dossier.

        Replays tests/data/maigret_snaxxwax_simple.json rather than running a scan, so this
        costs no proxy allowance. `crawl=False` keeps it to the tool's own evidence, which is
        also what makes it hermetic -- no follow-up fetch is attempted.
        """
        import time

        from harvest import tools
        from harvest.models import Limits, ToolRun

        report = json.loads(
            (Path(__file__).parent / "data" / "maigret_snaxxwax_simple.json").read_text()
        )

        def fake_exec(argv, timeout, cwd, cancelled=None, env=None):
            path = Path(argv[argv.index("--folderoutput") + 1]) / f"report_{argv[1]}_simple.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(tools, "_exec", fake_exec)
        settings = Settings(
            database=str(tmp_path / "h.sqlite"),
            api_token="test-operator-token-at-least-24-characters",
            tools=frozenset({"maigret"}),
        )

        def no_network(*args, **kwargs):
            raise AssertionError("crawl=False must not acquire anything over the network")

        # Not just asserted after the fact: with a real fetcher a crawl regression would send
        # live requests to every site the saved scan names before the assertion could fail.
        eng = Engine(settings, fetcher_factory=no_network)
        inv = Investigation(
            targets=[Target(key="s", label="Snaxxwax", identifiers={"username": ["Snaxxwax"]})],
            sources=[
                SourceRule(
                    url="tool://maigret/Snaxxwax",
                    identifier_fields={"username": "username"},
                    field_map={
                        "url": "profile_url",
                        "fullname": "display_name",
                        "uid": "account_id",
                        "created_at": "account_created",
                    },
                )
            ],
        )
        jid = eng.submit(
            JobSpec(
                objective="Investigate Snaxxwax",
                investigation=inv,
                tools=[ToolRun(name="maigret", target="Snaxxwax", crawl=False)],
                limits=Limits(),
            )
        )
        deadline = time.time() + 30
        while eng.store.job(jid)["status"] in {"queued", "running"} and time.time() < deadline:
            eng.step(jid)
        dossier = eng.store.dossier(jid)
        target = dossier["targets"][0]

        # The whole point: tool evidence is in the dossier, not silently excluded.
        assert not dossier["excluded"], dossier["excluded"]
        assert target["fields"]["profile_url"]["candidates"], "tool evidence never arrived"
        urls = {c["value"] for c in target["fields"]["profile_url"]["candidates"]}
        assert "https://github.com/Snaxxwax" in urls

        # Fields that used to be buried in status.ids are now their own sourced claims.
        assert target["fields"]["account_id"]["value"] == "105263527"
        assert target["fields"]["account_created"]["value"] == "2022-05-10T04:32:39Z"

        # Account existence is not identity attribution: every site that answered to the
        # handle was admitted, including ones with nothing but a status code behind them,
        # and the confidence of each is what distinguishes them.
        strengths = {
            c["extraction_confidence"] for c in target["fields"]["profile_url"]["candidates"]
        }
        assert strengths != {1.0}, "every account still looks equally certain"
        assert min(strengths) <= 0.4 and max(strengths) == 0.9

        # No scan artefact became evidence, and nothing was queued to crawl.
        body = json.dumps(dossier)
        assert "noonewouldeverusethis7" not in body and "{username}" not in body
        with eng.store.connection() as db:
            assert not db.execute(
                "SELECT count(*) FROM tasks WHERE job_id=? AND kind='fetch'", (jid,)
            ).fetchone()[0]


def test_suppressed_leads_are_counted_on_live_jobs_too(tmp_path, monkeypatch):
    """`suppressed_leads` was hardcoded to 0 for every non-replay task, so a job that threw
    away most of its leads was indistinguishable from one that found none. It is now the
    difference between what the extraction produced and what became a task -- here, a tool
    run with crawl off, where everything the scan found is suppressed by definition."""
    import time

    from harvest import tools
    from harvest.models import Limits, ToolRun

    report = json.loads(
        (Path(__file__).parent / "data" / "maigret_snaxxwax_simple.json").read_text()
    )

    def fake_exec(argv, timeout, cwd, cancelled=None, env=None):
        path = Path(argv[argv.index("--folderoutput") + 1]) / f"report_{argv[1]}_simple.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(tools, "_exec", fake_exec)
    settings = Settings(
        database=str(tmp_path / "h.sqlite"),
        api_token="test-operator-token-at-least-24-characters",
        tools=frozenset({"maigret"}),
    )
    eng = Engine(settings, fetcher_factory=lambda *a, **k: pytest.fail("no network expected"))
    jid = eng.submit(
        JobSpec(
            objective="Investigate Snaxxwax",
            tools=[ToolRun(name="maigret", target="Snaxxwax", crawl=False)],
            limits=Limits(),
        )
    )
    deadline = time.time() + 30
    while eng.store.job(jid)["status"] in {"queued", "running"} and time.time() < deadline:
        eng.step(jid)
    extract = [
        e["details"]
        for e in eng.store.events(jid, 0, 500)
        if e["type"] == "task_done" and "suppressed_leads" in e["details"]
    ]
    assert extract and extract[0]["crawl"] is False
    assert extract[0]["accepted_leads"] == 0
    assert extract[0]["suppressed_leads"] > 0, "the scan's own findings were not counted"
