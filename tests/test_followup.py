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
