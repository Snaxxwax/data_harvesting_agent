"""The egress relay as deployed: the real entrypoint.sh and template in the real tinyproxy
image, chained to a fake upstream that behaves like Webshare -- a hard cap on simultaneous
connections per account, answered with `429 client_connect_high_concurrency` beyond it.

Regression for the burst that full Maigret scans produce: ~3,000 checks at 100 at once per
worker thread. Skipped where docker is unavailable.
"""

import shutil
import socket
import socketserver
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

RELAY = Path(__file__).resolve().parents[1] / "deploy" / "ovh-vps" / "egress-relay"
IMAGE = "vimagick/tinyproxy"
CRED = "user:pass"


def _docker_ok():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="needs docker")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeWebshare(socketserver.ThreadingTCPServer):
    """CONNECT-only upstream proxy with an account-wide concurrency cap."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, limit):
        self.limit, self.active, self.peak = limit, 0, 0
        self.accepted = self.rejected = self.unauthenticated = 0
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Upstream)


class _Upstream(socketserver.BaseRequestHandler):
    def handle(self):
        srv, sock = self.server, self.request
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                return
            head += chunk
        if b"Proxy-Authorization: Basic dXNlcjpwYXNz" not in head:
            with srv.lock:
                srv.unauthenticated += 1
            sock.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            return
        with srv.lock:
            over = srv.active >= srv.limit
            if over:
                srv.rejected += 1
            else:
                srv.active += 1
                srv.accepted += 1
                srv.peak = max(srv.peak, srv.active)
        if over:
            body = b"client_connect_high_concurrency"
            sock.sendall(
                b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: %d\r\n\r\n%s"
                % (len(body), body)
            )
            return
        try:
            sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            while sock.recv(4096):  # hold the tunnel until the client side closes it
                pass
        except OSError:
            pass
        finally:
            with srv.lock:
                srv.active -= 1


@pytest.fixture
def upstream():
    servers = []

    def start(limit):
        srv = FakeWebshare(limit)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _relay_files(tmp_path, upstream_port, relay_port):
    template = tmp_path / "tinyproxy.conf.template"
    template.write_text(
        (RELAY / "tinyproxy.conf.template").read_text().replace("Port 8888", f"Port {relay_port}")
    )
    secret = tmp_path / "upstream.secret"
    secret.write_text(f"HARVEST_EGRESS_UPSTREAM={CRED}@127.0.0.1:{upstream_port}\n")
    return template, secret


def _run_relay(tmp_path, template, secret, max_connections, detach):
    name = f"harvest-relay-test-{tmp_path.name[-20:]}".replace("_", "-")
    argv = [
        "docker", "run", "--rm", "--name", name, "--network", "host",
        "--entrypoint", "/usr/local/bin/egress-entrypoint.sh",
        "-v", f"{template}:/etc/tinyproxy/tinyproxy.conf.template:ro",
        "-v", f"{RELAY / 'entrypoint.sh'}:/usr/local/bin/egress-entrypoint.sh:ro",
        "-v", f"{secret}:/run/secrets/egress-upstream:ro",
        "--tmpfs", "/run/tinyproxy:mode=0700",
    ]  # fmt: skip
    if max_connections is not None:
        argv += ["-e", f"HARVEST_EGRESS_MAX_CONNECTIONS={max_connections}"]
    argv += (["-d"] if detach else []) + [IMAGE]
    try:
        return name, subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        # Only a foreground relay that should have refused to start gets here.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail("relay started and kept running when it should have refused to")


@pytest.fixture
def relay(tmp_path, upstream):
    names = []

    def start(max_connections, upstream_limit):
        up = upstream(upstream_limit)
        port = _free_port()
        template, secret = _relay_files(tmp_path, up.server_address[1], port)
        name, proc = _run_relay(tmp_path, template, secret, max_connections, detach=True)
        assert proc.returncode == 0, proc.stderr
        names.append(name)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            pytest.fail(f"relay never listened: {logs.stderr[-500:]}")
        return port, up

    yield start
    for name in names:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _tunnel(port, hold):
    """One CONNECT through the relay, held open `hold` seconds; returns the status code."""
    with socket.create_connection(("127.0.0.1", port), timeout=60) as s:
        s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                return "closed"
            head += chunk
        status = head.split(b" ", 2)[1].decode()
        if status == "200":
            time.sleep(hold)
        return status


def _burst(port, n, hold):
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(lambda _: _tunnel(port, hold), range(n)))


def test_relay_cap_keeps_a_burst_under_the_upstream_limit(relay):
    port, up = relay(max_connections=8, upstream_limit=10)

    statuses = _burst(port, 40, hold=0.5)

    # Every client was served -- the excess queued in the relay, nothing was refused.
    assert statuses == ["200"] * 40
    assert up.rejected == 0 and up.unauthenticated == 0
    assert up.accepted == 40
    assert up.peak <= 8


def test_uncapped_relay_reproduces_the_429_burst(relay):
    # The pre-fix shape: the relay admitting tinyproxy's compiled-in 100 (what it ran with
    # before MaxClients was configured) in front of an upstream whose account cap is lower. This is what the cap above prevents; if this test
    # stops seeing 429s the fake upstream no longer models the failure.
    port, up = relay(max_connections=100, upstream_limit=10)
    statuses = _burst(port, 40, hold=0.5)

    assert up.rejected > 0
    assert "429" in statuses


def test_cancelled_clients_do_not_hold_relay_or_upstream_slots(relay):
    port, up = relay(max_connections=4, upstream_limit=50)
    # Fill every slot, then queue 30 more and abandon them -- what killing a Maigret process
    # group mid-scan does to its sockets.
    holders = ThreadPoolExecutor(4)
    held = [holders.submit(_tunnel, port, 2.0) for _ in range(4)]
    time.sleep(0.5)
    abandoned = []
    for _ in range(30):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        abandoned.append(s)
    for s in abandoned:
        s.close()
    assert [f.result() for f in held] == ["200"] * 4
    holders.shutdown()

    # Once the queue drains, the relay serves new work promptly and nothing is left open
    # upstream on behalf of a client that went away.
    started = time.monotonic()
    assert _tunnel(port, 0) == "200"
    assert time.monotonic() - started < 10
    deadline = time.monotonic() + 10
    while up.active and time.monotonic() < deadline:
        time.sleep(0.1)
    assert up.active == 0
    assert up.peak <= 4


@pytest.mark.parametrize("value", ["abc", "0", "501", "12x", "-3"])
def test_relay_refuses_to_start_on_a_bad_limit(tmp_path, value):
    template, secret = _relay_files(tmp_path, 9, _free_port())
    _, proc = _run_relay(tmp_path, template, secret, value, detach=False)
    assert proc.returncode != 0
    assert "HARVEST_EGRESS_MAX_CONNECTIONS" in proc.stderr


def test_relay_still_refuses_to_start_without_an_upstream(tmp_path):
    template, secret = _relay_files(tmp_path, 9, _free_port())
    secret.write_text("# no credential\n")
    _, proc = _run_relay(tmp_path, template, secret, 8, detach=False)
    assert proc.returncode != 0
    assert "refusing to start" in proc.stderr
