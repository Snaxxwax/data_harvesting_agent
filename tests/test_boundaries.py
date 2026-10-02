import json
import os
import subprocess
import sys
import time

from harvest.extract import CsvAdapter, JsonAdapter
from harvest.models import JobSpec, Limits


def test_gzip_bound_and_integrity():
    import gzip

    import pytest

    from harvest.models import PolicyDenied
    from harvest.network import decode_body

    assert decode_body(gzip.compress(b"hello"), "gzip", 100) == b"hello"
    with pytest.raises(PolicyDenied, match="size limit"):
        decode_body(gzip.compress(b"x" * 1000000), "gzip", 1024)
    with pytest.raises(PolicyDenied, match="truncated"):
        decode_body(gzip.compress(b"hello")[:-4], "gzip", 100)


def test_wildcard_robots_denial_is_enforced(engine, source):
    source["robots"] = "User-agent: *\nDisallow: /*page*\n"
    job = engine.submit(JobSpec(objective="Collect sample", seeds=[source["base"] + "/page2"]))
    result = engine.run(job)
    assert result["progress"]["blocked"] == 1
    assert "/page2" not in source["requests"]


def test_jsonld_fragment_identifiers_remain_distinct():
    body = b'[{"@id":"#alpha","name":"A"},{"@id":"#beta","name":"B"}]'
    result = JsonAdapter().extract(body, "https://example.org/page")
    assert len({c.entity_key for c in result.claims}) == 2


def test_proxy_hosts_require_exact_operator_allowlist(engine, source):
    import pytest

    from harvest.models import PolicyDenied
    from harvest.network import Fetcher

    engine.settings.proxy = "http://127.0.0.1:9"
    engine.settings.proxy_public_hosts = frozenset({"example.org"})
    job = engine.submit(JobSpec(objective="Public metadata", seeds=["https://example.org"]))
    task = engine.store.claim(job)
    fetcher = Fetcher(engine.store, engine.settings, task)
    try:
        assert fetcher.guard("https://example.org") == "https://example.org/"
        with pytest.raises(PolicyDenied):
            fetcher.guard("https://evil.example.org")
        with pytest.raises(PolicyDenied):
            fetcher.guard("http://169.254.169.254/")
    finally:
        fetcher.close()


def test_single_object_json_pointer_identifies_actual_evidence():
    body = b'{"id":"a", "name":"Alpha"}'
    result = JsonAdapter().extract(body, "https://example.org/a.json")
    claim = next(c for c in result.claims if c.field == "name")
    assert claim.locator == "/name"
    assert json.loads(body)[claim.locator[1:]] == claim.value


def test_csv_and_json_limits_are_explicit():
    body = json.dumps([{"id": i} for i in range(101)]).encode()
    assert JsonAdapter().extract(body, "https://example.org/data").warnings
    csv = "id,name\n" + "\n".join(f"{i},name{i}" for i in range(101))
    result = CsvAdapter().extract(csv.encode(), "https://example.org/data.csv")
    assert result.warnings
    assert len(result.claims) == 200


def test_pagination_not_limited_by_research_depth(engine, source):
    spec = JobSpec(
        objective="Enumerate records",
        seeds=[source["base"] + "/records"],
        allowed_domains=["127.0.0.1"],
        mode="enumerative",
        limits=Limits(depth=0, domain_delay=0.1),
    )
    result = engine.run(engine.submit(spec))
    assert result["captures"] == 2


def test_delayed_retry_cannot_hide_elapsed_deadline(engine, source):
    spec = JobSpec(
        objective="Collect records", seeds=[source["base"] + "/records"], limits=Limits(seconds=1)
    )
    job = engine.submit(spec)
    task = engine.store.claim(job)
    engine.store.defer(task, "Retry-After tomorrow", 86400)
    with engine.store.transaction() as db:
        db.execute("UPDATE jobs SET started=? WHERE id=?", (time.time() - 2, job))
    result = engine.run(job)
    assert result["status"] == "budget_exhausted"
    assert result["requests"] == 0


def test_killed_process_releases_uncommitted_transaction_and_recovers(engine, source, tmp_path):
    """SIGKILL with an open write transaction tests OS/process failure, not an exception mock."""
    spec = JobSpec(
        objective="Collect records",
        seeds=[source["base"] + "/page2"],
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=0),
    )
    job = engine.submit(spec)
    ready = tmp_path / "ready"
    code = """
import sys,time
from pathlib import Path
from harvest.store import Store
s=Store(sys.argv[1]); task=s.claim(sys.argv[2], lease_seconds=0.2)
with s.transaction() as db:
 db.execute("INSERT INTO blobs VALUES('uncommitted',X'00')")
 Path(sys.argv[3]).write_text('ready')
 time.sleep(60)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, engine.store.path, job, str(ready)], env=os.environ.copy()
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "worker did not enter transaction"
        child.kill()
        child.wait(timeout=5)
        time.sleep(0.25)
        result = engine.run(job)
        assert result["status"] == "completed"
        with engine.store.connection() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert (
                db.execute("SELECT count(*) FROM blobs WHERE hash='uncommitted'").fetchone()[0] == 0
            )
            assert (
                db.execute("SELECT attempts FROM tasks WHERE job_id=?", (job,)).fetchone()[0] == 2
            )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_proxy_without_allowlist_permits_job_scope_and_still_blocks_internal(engine):
    """An empty HARVEST_PROXY_PUBLIC_HOSTS means 'no operator allowlist', not 'deny all'.

    Policy then falls back to the job's own scope plus name-shape rules, so leads found
    mid-job are crawlable without enumerating hosts, while internal destinations stay denied.
    """
    import pytest

    from harvest.models import PolicyDenied
    from harvest.network import Fetcher

    engine.settings.proxy = "http://127.0.0.1:9"
    engine.settings.proxy_public_hosts = frozenset()
    job = engine.submit(JobSpec(objective="Public metadata", seeds=["https://example.org"]))
    task = engine.store.claim(job)
    fetcher = Fetcher(engine.store, engine.settings, task)
    try:
        # A discovered lead on an unrelated public host is allowed without enumeration.
        assert fetcher.guard("https://news.example.net/story") == "https://news.example.net/story"
        for blocked in (
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://10.0.0.5/admin",
            "http://127.0.0.1:8000/",
            "http://[::1]/",
            "http://db.internal/",
            "http://wiki.corp/",
            "http://printer.local/",
            "http://localhost/",
            "http://user/",  # single-label name
        ):
            with pytest.raises(PolicyDenied):
                fetcher.guard(blocked)
        # Non-HTTP schemes and embedded credentials never reach the proxy branch:
        # canonical_url() rejects them first.
        for rejected in ("file:///etc/passwd", "https://u:p@example.org/"):
            with pytest.raises(ValueError):
                fetcher.guard(rejected)
    finally:
        fetcher.close()


def test_proxy_scope_still_honours_allowed_domains(engine):
    """allowed_domains remains the job's own boundary even with no operator allowlist."""
    import pytest

    from harvest.models import PolicyDenied
    from harvest.network import Fetcher

    engine.settings.proxy = "http://127.0.0.1:9"
    engine.settings.proxy_public_hosts = frozenset()
    job = engine.submit(
        JobSpec(
            objective="Scoped",
            seeds=["https://example.org"],
            allowed_domains=["example.org"],
        )
    )
    task = engine.store.claim(job)
    fetcher = Fetcher(engine.store, engine.settings, task)
    try:
        assert fetcher.guard("https://sub.example.org/x")
        with pytest.raises(PolicyDenied, match="outside allowed domains"):
            fetcher.guard("https://elsewhere.net/x")
    finally:
        fetcher.close()


def test_proxy_guard_runs_on_every_redirect_hop(engine):
    """A redirect to a private address is denied, not followed.

    raw_get() re-guards `current` at the top of each iteration, so the policy applies to
    every hop rather than only to the URL the job named.
    """
    import httpx
    import pytest

    from harvest.models import PolicyDenied
    from harvest.network import Fetcher

    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        # content= must be an iterator: a Response built with no body is already
        # stream-consumed, and raw_get() reads every response via iter_raw().
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/"},
            content=iter([b""]),
        )

    engine.settings.proxy = "http://127.0.0.1:9"
    engine.settings.proxy_public_hosts = frozenset()
    job = engine.submit(JobSpec(objective="Redirect", seeds=["https://example.org"]))
    task = engine.store.claim(job)
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = Fetcher(engine.store, engine.settings, task, client=client)
    try:
        with pytest.raises(PolicyDenied, match="public"):
            fetcher.raw_get("https://example.org/start")
        # The redirect target was never fetched: only the first hop left the process.
        assert hops == ["https://example.org/start"]
    finally:
        fetcher.close()


def test_proxy_guard_cannot_see_through_dns_known_limitation(engine):
    """Documents a real gap: name shape is checked, resolution is not.

    In proxy mode the proxy resolves the name, so a public hostname pointing at a private
    address passes the application guard. This test pins that behaviour deliberately -- if
    it ever starts failing, resolution-time checking was added and the DEPLOYMENT.md
    section "Proxy-mode SSRF boundary" should be updated to match.
    """
    from harvest.network import require_public_host

    # nip.io and friends resolve <ip>.nip.io -> <ip>. Public name, private address.
    for rebinding in ("10.0.0.5.nip.io", "127.0.0.1.nip.io", "169.254.169.254.nip.io"):
        require_public_host(rebinding)  # no exception: the gap, asserted explicitly

    # The literal forms of those same addresses are still rejected.
    import pytest

    from harvest.models import PolicyDenied

    for literal in ("10.0.0.5", "127.0.0.1", "169.254.169.254"):
        with pytest.raises(PolicyDenied):
            require_public_host(literal)
