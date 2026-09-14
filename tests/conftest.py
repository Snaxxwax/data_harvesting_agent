import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harvest.config import Settings
from harvest.engine import Engine


@pytest.fixture
def source():
    state = {
        "requests": [],
        "counts": Counter(),
        "version": 1,
        "robots": "User-agent: *\nDisallow: /private\n",
        "model_calls": 0,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body=b"", content_type="application/json", **headers):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), str(value))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = self.path.split("?")[0]
            state["requests"].append(self.path)
            state["counts"][path] += 1
            if path == "/robots.txt":
                self.reply(200, state["robots"], "text/plain")
            elif path in state.get("routes", {}):
                body, media = state["routes"][path]
                self.reply(200, body, media)
            elif path == "/records":
                tag = f'"v{state["version"]}"'
                if self.headers.get("If-None-Match") == tag:
                    self.reply(304, ETag=tag)
                else:
                    self.reply(
                        200,
                        {
                            "items": [
                                {
                                    "@id": "https://example.org/entity/alpha",
                                    "name": "Alpha",
                                    "size": state["version"],
                                }
                            ],
                            "next": state["base"] + "/page2",
                        },
                        ETag=tag,
                    )
            elif path == "/page2":
                self.reply(200, [{"@id": "https://example.org/entity/beta", "name": "Beta"}])
            elif path == "/conflict":
                self.reply(
                    200, {"@id": "https://example.org/entity/alpha", "name": "Different Alpha"}
                )
            elif path == "/unstable":
                if state["counts"][path] == 1:
                    self.reply(503, Retry_After="1")
                else:
                    self.reply(200, {"id": "recovered", "name": "Recovered"})
            elif path == "/redirect":
                self.reply(302, Location=state["base"] + "/private")
            elif path == "/private":
                self.reply(200, {"secret": "must not be acquired"})
            elif path == "/large":
                self.reply(200, "x" * 200000, "text/plain")
            elif path == "/bad":
                self.reply(200, "{bad json")
            elif path == "/page":
                self.reply(
                    200,
                    '<html><title>Alpha project</title><p>Alpha supports HTTP/2.</p><a href="/evidence">Primary documentation</a></html>',
                    "text/html",
                )
            elif path == "/evidence":
                self.reply(
                    200,
                    "<html><title>Alpha documentation</title><p>Alpha supports HTTP/2.</p></html>",
                    "text/html",
                )
            elif path == "/search":
                self.reply(
                    200,
                    {
                        "results": [
                            {"url": state["base"] + "/page", "title": "Alpha primary source"},
                            {"url": state["base"] + "/evidence", "title": "Documentation"},
                        ]
                    },
                )
            else:
                self.reply(404)

        def do_POST(self):
            state["model_post_attempts"] = state.get("model_post_attempts", 0) + 1
            if state["model_post_attempts"] in state.get("fail_model_attempts", ()):
                # Simulate a transient provider failure: connection drops with no response sent.
                self.close_connection = True
                return
            state["model_calls"] += 1
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["last_model_request"] = data
            reader = state.get("model_reader")
            answer = (
                reader(data)
                if reader
                else state.get(
                    "model_answer",
                    {
                        "claims": [
                            {
                                "field": "protocol",
                                "value": "HTTP/2",
                                "quote": "Alpha supports HTTP/2.",
                                "confidence": 0.9,
                            },
                            {
                                "field": "invented",
                                "value": "HTTP/9",
                                "quote": "Alpha supports HTTP/9.",
                                "confidence": 1,
                            },
                        ],
                        "leads": [],
                        "queries": ["Alpha protocol primary evidence"],
                        "gaps": ["independent verification"],
                        "contradictions": [],
                        "rationale": "Follow primary documentation and seek independent evidence.",
                    },
                )
            )
            self.reply(
                200,
                {
                    "choices": [{"message": {"content": json.dumps(answer)}}],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 100},
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def engine(tmp_path, source):
    settings = Settings(
        database=str(tmp_path / "harvest.sqlite"),
        private_hosts=frozenset({"127.0.0.1"}),
        api_token="test-operator-token-at-least-24-characters",
        proxy=None,
    )
    return Engine(settings)
