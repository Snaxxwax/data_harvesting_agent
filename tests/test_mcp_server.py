"""The MCP adapter: it registers the expected tools and normalises API responses to data.

The adapter is a thin HTTP shim, so these cover its own logic (tool surface, response
normalisation including the authorization_required path). Live end-to-end connection from
Hermes/Claude Code/Codex is verified against a running API outside the unit suite.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

mcp = pytest.importorskip("mcp")  # adapter needs the optional [agent] extra
from harvest import mcp_server  # noqa: E402


def test_registers_the_expected_tools():
    server = mcp_server.build_server()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "list_capabilities",
        "plan_investigation",
        "start_investigation",
        "investigation_status",
        "get_dossier",
        "get_evidence",
        "request_followup",
        "cancel_investigation",
    }


def _resp(status, json_body):
    return httpx.Response(status, json=json_body, request=httpx.Request("GET", "http://x"))


def test_result_passes_success_through():
    assert mcp_server._result(_resp(200, {"id": "j1", "status": "queued"}))["id"] == "j1"


def test_result_surfaces_authorization_required_as_data():
    # A 403 from /followup must come back as structured data the agent reasons about,
    # not an exception -- it is an expected "ask the operator" outcome.
    out = mcp_server._result(
        _resp(
            403,
            {
                "detail": {
                    "error": "authorization_required",
                    "reason": "out of scope",
                    "suggestion": "authorize a new investigation",
                }
            },
        )
    )
    assert out["ok"] is False
    assert out["error"] == "authorization_required"
    assert out["reason"] == "out of scope"


def test_result_reports_other_errors():
    out = mcp_server._result(_resp(422, {"detail": "bad spec"}))
    assert out["ok"] is False and out["error"] == "bad spec"
