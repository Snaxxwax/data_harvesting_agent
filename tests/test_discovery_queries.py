import pytest

from harvest.engine import Engine
from harvest.models import JobSpec, Limits


def test_discovery_queries_used_when_no_seeds(engine, source):
    engine.settings.search_url = source["base"]
    engine = Engine(engine.settings)
    spec = JobSpec(
        objective="Find Alpha protocol documentation",
        discovery_queries=["Alpha protocol", "Alpha documentation"],
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=0),
    )
    job = engine.submit(spec)
    with engine.store.connection() as db:
        keys = [
            r[0]
            for r in db.execute(
                "SELECT key FROM tasks WHERE job_id=? AND kind='search' ORDER BY key", (job,)
            )
        ]
    assert keys == ["Alpha documentation", "Alpha protocol"]


def test_discovery_queries_without_search_configured_fails_clearly(engine):
    spec = JobSpec(
        objective="Find Alpha protocol documentation",
        discovery_queries=["Alpha protocol"],
        allowed_domains=["127.0.0.1"],
    )
    with pytest.raises(ValueError, match="HARVEST_SEARCH_URL"):
        engine.submit(spec)


def test_seeds_and_discovery_queries_together_add_search_only_if_configured(engine, source):
    spec = JobSpec(
        objective="Find Alpha",
        seeds=[source["base"] + "/page"],
        discovery_queries=["Alpha protocol"],
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=0),
    )
    job = engine.submit(spec)
    with engine.store.connection() as db:
        kinds = {
            r[0] for r in db.execute("SELECT kind FROM tasks WHERE job_id=?", (job,)).fetchall()
        }
    assert kinds == {"fetch"}


def test_seeds_and_discovery_queries_together_add_search_when_configured(engine, source):
    engine.settings.search_url = source["base"]
    engine = Engine(engine.settings)
    spec = JobSpec(
        objective="Find Alpha",
        seeds=[source["base"] + "/page"],
        discovery_queries=["Alpha protocol"],
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=0),
    )
    job = engine.submit(spec)
    with engine.store.connection() as db:
        kinds = {
            r[0] for r in db.execute("SELECT kind FROM tasks WHERE job_id=?", (job,)).fetchall()
        }
    assert kinds == {"fetch", "search"}


def test_discovery_queries_are_bounded_and_deduplicated_by_model():
    spec = JobSpec(
        objective="x" * 10,
        discovery_queries=["a", "a", "b", "c", "d", "e", "f"],
    )
    assert spec.discovery_queries == ["a", "b", "c", "d", "e"]
