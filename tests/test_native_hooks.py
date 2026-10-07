"""Check the pinned native reporting boundary without contacting remote services."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def hook():
    path = Path(__file__).resolve().parents[1] / "deploy/tool-hooks/harvest_native.py"
    spec = importlib.util.spec_from_file_location("native_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("status", "code", "error", "expected"),
    [
        ("Claimed", 200, None, "found"),
        ("Available", 200, None, "absent"),
        ("Available", 429, None, "blocked"),
        ("Unknown", None, {"type": "Request timeout"}, "errored"),
        ("Illegal", None, None, "skipped"),
        ("Unknown", None, None, "unfinished"),
    ],
)
def test_native_check_outcomes_are_distinct(tmp_path, monkeypatch, status, code, error, expected):
    path = tmp_path / "checks.jsonl"
    monkeypatch.setenv("HARVEST_CHECKPOINT", str(path))
    hook().maigret_result(
        (
            "Fixture",
            {
                "username": "audit",
                "http_status": code,
                "status": SimpleNamespace(
                    json=lambda: {"status": status, "error": error, "ids": {"fullname": "Audit"}}
                ),
                "site": SimpleNamespace(
                    json={"checkType": "message", "headers": {"Authorization": "secret"}}
                ),
                "cookies": "session-secret",
                "response_text": "unrestricted page",
            },
        )
    )
    content = path.read_text()
    obj = json.loads(content)
    assert obj["outcome"] == expected
    assert "secret" not in content and "unrestricted page" not in content
    assert obj["native"]["status"]["ids"]["fullname"] == "Audit"
