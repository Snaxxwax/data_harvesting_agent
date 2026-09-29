"""Profile identifiers stay tied to captured HTML and explicit dossier rules."""

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from harvest.api import create_app
from harvest.extract import Extractors
from harvest.models import JobSpec, Limits

MAL_HTML = b"""<!DOCTYPE html><html><head><title>MAL profile</title>
<meta property="og:url" content="https://myanimelist.net/profile/Xinil">
<script type="application/ld+json">{"@type":"Person","name":"Xinil"}</script>
</head><body><div class="user-profile">
<a href="#" data-ga-click-param="uid:1" title="msg"><i></i></a>
</div></body></html>"""

WEEBLY_HTML = b"""<!DOCTYPE html><html><head><title>Weebly profile</title>
<link rel="stylesheet" href="https://cdn2.editmysite.com/css/main.css">
</head><body><script>com_currentSite = "183235046254098859";
com_userID = "125320777";</script></body></html>"""


def investigation(url):
    return {
        "targets": [
            {
                "key": "mal_account",
                "label": "Declared MAL account",
                "identifiers": {"myanimelist.uid": ["1"]},
            }
        ],
        "sources": [
            {
                "url": url,
                "identifier_fields": {"mal_uid": "myanimelist.uid"},
                "field_map": {"mal_uid": "platform_id", "mal_username": "username"},
            }
        ],
    }


@pytest.mark.parametrize(
    ("body", "url", "expected"),
    [
        (
            MAL_HTML,
            "https://myanimelist.net/profile/Xinil",
            {"mal_uid": "1", "mal_username": "Xinil"},
        ),
        (
            WEEBLY_HTML,
            "https://example.weebly.com/",
            {"uid": "125320777", "weebly_site_id": "183235046254098859"},
        ),
    ],
)
def test_installed_adapter_extracts_literal_ids_and_keeps_html(body, url, expected):
    result = Extractors().extract(body, url, "text/html")
    assert result.extractor == "socid-html/1"
    assert next(c for c in result.claims if c.field == "page_title")
    page = body.decode()
    for field, value in expected.items():
        claim = next(c for c in result.claims if c.field == field)
        assert claim.value == value
        assert claim.entity_key == "url:" + url
        assert claim.method == "html"
        assert claim.evidence == value
        assert claim.locator.startswith("socid:")
        offset = claim.locator.rsplit(":chars:", 1)[1]
        start, end = map(int, offset.split("-"))
        assert page[start:end] == claim.evidence
    assert not any(c.field == "_extractor" for c in result.claims)
    if body == MAL_HTML:
        assert any(c.field == "name" and c.method == "structured" for c in result.claims)


def test_unsupported_or_failed_socid_keeps_base_html(monkeypatch):
    import harvest.socid_adapter as adapter

    def fail(_page):
        raise ValueError("broken scheme")

    monkeypatch.setattr(adapter.socid_extractor, "extract", fail)
    result = Extractors().extract(b"<title>Plain</title>", "https://example.org/", "text/html")
    assert result.extractor == "html-jsonld/1"
    assert [(c.field, c.value) for c in result.claims] == [("page_title", "Plain")]
    assert result.warnings == ["socid extraction failed: ValueError"]

    monkeypatch.setattr(
        adapter.socid_extractor,
        "extract",
        lambda _page: {"uid": "not-in-source", "_extractor": "fixture"},
    )
    result = Extractors().extract(b"<title>Plain</title>", "https://example.org/", "text/html")
    assert result.extractor == "html-jsonld/1"
    assert [c.field for c in result.claims] == ["page_title"]
    assert "socid value without literal source evidence omitted" in result.warnings


def test_api_replay_matches_only_declared_profile(engine, source):
    source["routes"] = {
        "/mal": (MAL_HTML, "text/html"),
        "/weebly": (WEEBLY_HTML, "text/html"),
    }
    mal_url = source["base"] + "/mal"
    original = engine.run(
        engine.submit(
            JobSpec(
                objective="Collect two profile pages",
                seeds=[mal_url, source["base"] + "/weebly"],
                allowed_domains=["127.0.0.1"],
                limits=Limits(depth=0, domain_delay=0.1),
            )
        )
    )
    captures = engine.store.captures(original["id"])
    prior = engine.store.observations(original["id"], limit=1000)
    count = len(source["requests"])
    client = TestClient(create_app(engine.settings))
    headers = {"Authorization": "Bearer " + engine.settings.api_token}
    response = client.post(
        "/replays",
        headers=headers,
        json={"capture_ids": [c["id"] for c in captures], "investigation": investigation(mal_url)},
    )
    assert response.status_code == 202, response.text
    replay = engine.run(response.json()["id"])
    assert replay["status"] == "completed" and replay["requests"] == 0
    assert len(source["requests"]) == count
    dossier = client.get(f"/jobs/{replay['id']}/dossier", headers=headers).json()
    target = dossier["targets"][0]
    assert len(target["matched_entities"]) == 1
    assert target["fields"]["platform_id"]["value"] == "1"
    assert target["fields"]["username"]["value"] == "Xinil"
    assert all(x["source_url"] == mal_url for x in target["matched_entities"])
    assert engine.store.observations(original["id"], limit=1000) == prior
    assert engine.store.captures(original["id"]) == captures


def test_cli_replay_accepts_investigation_file(engine, source, tmp_path):
    source["routes"] = {"/mal": (MAL_HTML, "text/html")}
    url = source["base"] + "/mal"
    original = engine.run(
        engine.submit(
            JobSpec(
                objective="Collect MAL profile",
                seeds=[url],
                limits=Limits(depth=0, domain_delay=0.1),
            )
        )
    )
    capture = engine.store.captures(original["id"])[0]
    path = tmp_path / "investigation.json"
    path.write_text(json.dumps(investigation(url)))
    count = len(source["requests"])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harvest.cli",
            "--db",
            engine.store.path,
            "replay",
            str(capture["id"]),
            "--investigation",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    replay = json.loads(result.stdout)
    assert replay["requests"] == 0 and len(source["requests"]) == count
    assert engine.store.dossier(replay["id"])["targets"][0]["fields"]["platform_id"]["value"] == "1"
