"""Agent follow-ups inherit the parent investigation's authorization and cannot widen it."""

from __future__ import annotations

import pytest

from harvest.models import AuthorizationRequired, JobSpec


def _parent(engine, source):
    # A targeted job scoped to the test host, seeded at /page, which links to /evidence.
    spec = JobSpec(
        objective="investigate alpha",
        seeds=[source["base"] + "/page"],
        allowed_domains=["127.0.0.1"],
        limits={"depth": 2},
    )
    job = engine.submit(spec)
    engine.run(job)
    return job


def test_follow_up_discovered_in_scope_url_starts_child(engine, source):
    parent = _parent(engine, source)
    child = engine.follow_up(parent, url=source["base"] + "/evidence")
    row = engine.store.job(child)
    assert row["parent_id"] == parent
    spec = JobSpec.model_validate(row["spec"])
    assert spec.seeds == [source["base"] + "/evidence"]
    # Authorization inherited, not widened.
    assert spec.allowed_domains == ["127.0.0.1"]


def test_follow_up_undiscovered_url_needs_authorization(engine, source):
    parent = _parent(engine, source)
    # In scope (same host) but this path was never seen by the investigation.
    with pytest.raises(AuthorizationRequired) as exc:
        engine.follow_up(parent, url=source["base"] + "/never-seen-path")
    assert "not discovered" in exc.value.reason
    assert exc.value.suggestion


def test_follow_up_out_of_scope_url_needs_authorization(engine, source):
    parent = _parent(engine, source)
    with pytest.raises(AuthorizationRequired) as exc:
        engine.follow_up(parent, url="http://8.8.8.8/")
    assert "scope" in exc.value.reason


def test_follow_up_tool_not_enabled_needs_authorization(engine, source):
    parent = _parent(engine, source)
    with pytest.raises(AuthorizationRequired) as exc:
        engine.follow_up(parent, tool="maigret", target="somehandle")
    assert "not enabled" in exc.value.reason


def test_follow_up_rejects_both_or_neither(engine, source):
    parent = _parent(engine, source)
    with pytest.raises(ValueError):
        engine.follow_up(parent, url="http://x/", tool="maigret")
    with pytest.raises(ValueError):
        engine.follow_up(parent)


# --- Investigation-wide budget: follow-ups share ONE balance, they never mint new ones. ---

from dataclasses import replace  # noqa: E402

from harvest.models import BudgetExceeded  # noqa: E402


def _spent(engine, job):
    return engine.store.job(job)["investigation"]["enforced"]["requests"]["used"]


def _warm_spend(engine, source):
    # The first run also fetches robots.txt, which is then cached; measure a warm run so the
    # root below spends exactly what we budget for.
    _budgeted_parent(engine, source, requests=50)
    return _spent(engine, _budgeted_parent(engine, source, requests=50))


def _budgeted_parent(engine, source, requests):
    spec = JobSpec(
        objective="investigate alpha",
        seeds=[source["base"] + "/page"],
        allowed_domains=["127.0.0.1"],
        limits={"depth": 2, "requests": requests},
    )
    job = engine.submit(spec)
    engine.run(job)
    return job


def test_children_record_the_root_and_chains_keep_it(engine, source):
    parent = _parent(engine, source)
    child = engine.follow_up(parent, url=source["base"] + "/evidence")
    engine.run(child)
    grandchild = engine.follow_up(child, url=source["base"] + "/evidence")
    assert engine.store.job(child)["root_id"] == parent
    assert engine.store.job(grandchild)["root_id"] == parent
    assert engine.store.job(grandchild)["investigation"]["jobs"] == 3


def test_sequential_siblings_cannot_exceed_the_root_ceiling(engine, source):
    used = _warm_spend(engine, source)
    # Re-budget at exactly one more request than the parent already spent: room for one
    # child fetch, not one per child.
    root = _budgeted_parent(engine, source, requests=used + 1)
    first = engine.follow_up(root, url=source["base"] + "/evidence", key="a")
    engine.run(first)
    assert _spent(engine, root) == used + 1
    with pytest.raises(BudgetExceeded):
        engine.follow_up(root, url=source["base"] + "/evidence", key="b")


def test_concurrent_siblings_share_one_balance(engine, source):
    base = _warm_spend(engine, source)
    root = _budgeted_parent(engine, source, requests=base + 5)
    a = engine.follow_up(root, url=source["base"] + "/evidence", key="a")
    b = engine.follow_up(root, url=source["base"] + "/evidence", key="b")
    ta, tb = engine.store.claim(a), engine.store.claim(b)
    # Each child alone is allowed base+5 by its own (inherited) limits; together they may
    # spend only the 5 the investigation has left, however the reservations interleave.
    granted = 0
    for _ in range(10):
        for task in (ta, tb):
            try:
                engine.store.reserve(task, requests=1)
                granted += 1
            except BudgetExceeded:
                pass
    assert granted == 5
    assert _spent(engine, a) == _spent(engine, root) == base + 5


def test_root_cannot_spend_what_its_followups_used(engine, source):
    base = _warm_spend(engine, source)
    root = _budgeted_parent(engine, source, requests=base + 2)
    child = engine.follow_up(root, url=source["base"] + "/evidence")
    task = engine.store.claim(child)
    engine.store.reserve(task, requests=2)
    # Re-open the root with fresh work: its own counter says it has room; the investigation does not.
    with engine.store.transaction() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (root,))
        db.execute(
            "INSERT INTO tasks(job_id,kind,key,payload,depth,priority,reason) VALUES(?,?,?,?,0,0,'t')",
            (root, "fetch", "x", "{}"),
        )
    rtask = engine.store.claim(root)
    with pytest.raises(BudgetExceeded, match="investigation"):
        engine.store.reserve(rtask, requests=1)


def test_retried_followup_with_same_key_is_the_same_child(engine, source):
    parent = _parent(engine, source)
    first = engine.follow_up(parent, url=source["base"] + "/evidence", key="retry-1")
    again = engine.follow_up(parent, url=source["base"] + "/evidence", key="retry-1")
    assert first == again
    assert engine.store.job(parent)["investigation"]["jobs"] == 2


def test_rerun_of_a_followup_stays_in_the_investigation(engine, source):
    used = _warm_spend(engine, source)
    root = _budgeted_parent(engine, source, requests=used + 1)
    child = engine.follow_up(root, url=source["base"] + "/evidence")
    engine.run(child)
    with pytest.raises(BudgetExceeded):
        engine.rerun(child)


def test_retried_requests_are_counted(engine, source):
    job = engine.submit(JobSpec(objective="retry", seeds=[source["base"] + "/unstable"]))
    engine.run(job)
    row = engine.store.job(job)
    # robots.txt + the 503 attempt + the successful retry: every attempt spends.
    assert row["requests"] == 3
    assert row["investigation"]["enforced"]["requests"]["used"] == 3


def test_tool_runs_are_investigation_wide(engine, source, monkeypatch):
    engine.settings = replace(engine.settings, tools=frozenset({"maigret"}))
    monkeypatch.setattr(engine.store, "discovered", lambda *a: True)
    spec = JobSpec(
        objective="investigate alpha",
        seeds=[source["base"] + "/page"],
        allowed_domains=["127.0.0.1"],
        limits={"tool_runs": 1},
    )
    root = engine.submit(spec)
    engine.run(root)
    engine.follow_up(root, tool="maigret", target="alpha1")
    with pytest.raises(BudgetExceeded, match="tool_runs"):
        engine.follow_up(root, tool="maigret", target="alpha2")


def test_external_tool_cost_is_labelled_estimated_not_enforced(engine, source):
    parent = _parent(engine, source)
    inv = engine.store.job(parent)["investigation"]
    assert set(inv["enforced"]) >= {"requests", "bytes", "cost_usd", "tool_runs", "seconds"}
    assert inv["external_tool_runs"] == []
