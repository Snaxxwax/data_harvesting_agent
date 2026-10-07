"""Saved native outputs; no live tools or third-party requests in parity tests."""

import importlib.util
import json
from pathlib import Path

from harvest import tools

FIXTURES = Path(__file__).parent / "fixtures" / "parity"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_audited_spiderfoot_baseline_has_four_usable_findings():
    normalized = [
        tools._spiderfoot_records(
            fixture(f"spiderfoot-{source}.json")["events"], "audit.person@example.test"
        )
        for source in ("standalone", "harvest")
    ]
    keys = [{(r["event_type"], r["data"], r.get("url")) for r in group} for group in normalized]
    assert keys[0] == keys[1]
    assert len(keys[0]) == 4
    for group in normalized:
        assert {r["ownership"] for r in group if r.get("url")} == {"candidate"}


def test_maigret_retains_all_native_profile_fields():
    for name, site in fixture("maigret-found.json").items():
        record = tools._maigret_record(name, site, "audit_handle")
        for key, value in (site["status"].get("ids") or {}).items():
            if not key.startswith("_") and key not in {"username", "url", "status"}:
                assert record[key] == value
        assert record["url"] == site["url_user"]


def test_comparison_detects_nested_field_loss_and_refuses_unmatched_settings():
    import copy

    import pytest

    path = Path(__file__).resolve().parents[1] / "scripts/acquisition_compare.py"
    spec = importlib.util.spec_from_file_location("acquisition_compare", path)
    comparison = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(comparison)
    settings = {key: None for key in comparison.MATCH}
    settings.update(tool="ghunt", target="audit.person@example.test", version="2.3.4")
    native = fixture("ghunt-native.json")
    reference = {"settings": settings, "native": native}
    candidate = {
        "settings": dict(settings),
        "results": tools._ghunt_records(native, settings["target"]),
    }
    assert comparison.compare(reference, candidate)["passed"]
    candidate = copy.deepcopy(candidate)
    candidate["results"][0]["profile"]["personId"] = "changed"
    lost = comparison.compare(reference, candidate)
    assert not lost["passed"]
    assert any(row["field"] == "/profile/personId" for row in lost["missing_or_changed_fields"])
    candidate["settings"]["deadline_seconds"] = 1
    with pytest.raises(ValueError, match="deadline_seconds"):
        comparison.compare(reference, candidate)
