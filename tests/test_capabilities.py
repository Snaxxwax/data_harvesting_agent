"""The capability registry: static facts plus per-deployment readiness, all pure."""

from __future__ import annotations

import os
from unittest import mock

from harvest import capabilities
from harvest.config import Settings


def _settings(**env):
    with mock.patch.dict(os.environ, env, clear=False):
        return Settings()


def test_registry_covers_builtins_and_tools():
    names = {c.name for c in capabilities.registry()}
    assert {"fetch", "crawl", "discovery_search", "maigret", "ghunt", "spiderfoot"} <= names


def test_fetch_is_always_ready():
    r = capabilities.readiness(Settings())
    assert r["fetch"]["ready"] is True
    assert r["crawl"]["ready"] is True


def test_discovery_search_not_ready_without_url():
    r = capabilities.readiness(_settings(HARVEST_SEARCH_URL=""))
    assert r["discovery_search"]["ready"] is False
    assert "HARVEST_SEARCH_URL" in r["discovery_search"]["detail"]


def test_discovery_search_ready_with_url():
    r = capabilities.readiness(_settings(HARVEST_SEARCH_URL="http://searxng:8080"))
    assert r["discovery_search"]["ready"] is True
    assert r["discovery_search"]["detail"] == ""


def test_tool_not_in_allowlist_reports_reason():
    r = capabilities.readiness(_settings(HARVEST_TOOLS=""))
    assert r["maigret"]["ready"] is False
    assert r["maigret"]["detail"] == "not in HARVEST_TOOLS"


def test_spiderfoot_blocked_reasons_in_order():
    # Allowlisted but nothing else configured -> first missing piece is the URL.
    r = capabilities.readiness(_settings(HARVEST_TOOLS="spiderfoot", HARVEST_SPIDERFOOT_URL=""))
    assert r["spiderfoot"]["ready"] is False
    assert "URL" in r["spiderfoot"]["detail"]


def test_spiderfoot_proxy_egress_gap_is_reported():
    r = capabilities.readiness(
        _settings(
            HARVEST_TOOLS="spiderfoot",
            HARVEST_SPIDERFOOT_URL="http://sf:8001",
            HARVEST_SPIDERFOOT_API_KEY="k",
            HARVEST_SPIDERFOOT_MODULES="sfp_dnsresolve",
            HARVEST_EGRESS_MODE="proxy",
            HARVEST_EGRESS_PROXY="http://relay:8888",
            HARVEST_SPIDERFOOT_EGRESS="direct",
        )
    )
    assert r["spiderfoot"]["ready"] is False
    assert "egress directly" in r["spiderfoot"]["detail"]


def test_spiderfoot_ready_when_fully_configured():
    r = capabilities.readiness(
        _settings(
            HARVEST_TOOLS="spiderfoot",
            HARVEST_SPIDERFOOT_URL="http://sf:8001",
            HARVEST_SPIDERFOOT_API_KEY="k",
            HARVEST_SPIDERFOOT_MODULES="sfp_dnsresolve,sfp_accounts",
        )
    )
    assert r["spiderfoot"]["ready"] is True
    assert r["spiderfoot"]["detail"] == ""
    assert "sfp_accounts" in r["spiderfoot"]["modules"]


def test_for_kind_filters_by_input_kind():
    s = _settings(HARVEST_TOOLS="maigret,ghunt", HARVEST_SEARCH_URL="http://searxng:8080")
    email_caps = {c["name"]: c for c in capabilities.for_kind("email", s)}
    assert "ghunt" in email_caps and email_caps["ghunt"]["ready"] is True
    assert "maigret" not in email_caps  # maigret consumes usernames, not emails
    username_caps = {c["name"] for c in capabilities.for_kind("username", s)}
    assert "maigret" in username_caps
