#!/usr/bin/env python3
"""Offline fixture acquisition/replay endurance; two active investigations, API probes.

Uses no public network. Report stays `running` until elapsed duration and all gates pass.
"""

import argparse
import hashlib
import json
import os
import resource
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harvest import tools
from harvest.config import Settings
from harvest.engine import Engine
from harvest.models import JobSpec


def source_hash():
    source = Path(tools.__file__).parent
    hashes = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    hashes["runner"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=86400)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = args.directory / "soak.sqlite"
    settings = Settings(
        database=str(database),
        tools=frozenset({"maigret"}),
        proxy=None,
        api_token="fixture-soak-private-local-token",
        active_investigations=2,
        active_tools=2,
    )
    engine = Engine(settings)

    def fixture(target, workdir, timeout, cancelled, settings, **kwargs):
        native = {
            "username": target,
            "url_user": "https://example.test/" + target,
            "status": {"status": "Claimed", "ids": {"fullname": "Fixture Person"}},
            "site": {"checkType": "message"},
            "http_status": 200,
        }
        record = tools._maigret_record("Fixture", native, target)
        tools._RECORDER.get().check("Fixture", "found", native=native, records=[record])
        return [record]

    tools.TOOLS["maigret"]["run"] = fixture
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        **os.environ,
        "HARVEST_DB": str(database),
        "HARVEST_TOOLS": "",
        "HARVEST_API_TOKEN": settings.api_token,
    }
    api = subprocess.Popen(
        [sys.executable, "-m", "harvest.cli", "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started, rounds = time.time(), 0
    report = {
        "status": "running",
        "started": started,
        "required_seconds": args.duration,
        "concurrency": 2,
        "rounds": 0,
        "failures": [],
        "api_max_seconds": 0,
        "max_rss_kib": 0,
        "api_max_rss_kib": 0,
        "source_sha256": source_hash(),
    }
    try:
        for _ in range(60):
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/health",
                    headers={"Authorization": "Bearer " + settings.api_token},
                )
                if json.load(urllib.request.urlopen(request, timeout=2))["status"] == "ok":
                    break
            except OSError:
                if api.poll() is not None:
                    raise RuntimeError("soak API process exited before startup") from None
                time.sleep(1)
        else:
            raise RuntimeError("soak API startup timeout")
        started = time.time()
        report["started"] = started
        with ThreadPoolExecutor(max_workers=2) as pool:
            while time.time() - started < args.duration:
                jobs = [
                    engine.submit(
                        JobSpec(
                            objective="offline fixture soak",
                            dataset="soak",
                            tools=[
                                {
                                    "name": "maigret",
                                    "target": f"fixture_{rounds}_{i}",
                                    "crawl": False,
                                }
                            ],
                        )
                    )
                    for i in range(2)
                ]
                results = list(pool.map(engine.run, jobs))
                if any(j["status"] != "completed" for j in results):
                    raise RuntimeError("fixture investigation incomplete")
                for job in jobs:
                    captures = engine.store.captures(job)
                    from harvest.models import ReplaySpec

                    replay = engine.replay(ReplaySpec(capture_ids=[c["id"] for c in captures]))
                    engine.run(replay)
                before = time.monotonic()
                query = urllib.request.Request(
                    f"http://127.0.0.1:{port}/health",
                    headers={"Authorization": "Bearer " + settings.api_token},
                )
                assert json.load(urllib.request.urlopen(query, timeout=5))["status"] == "ok"
                latency = time.monotonic() - before
                report["api_max_seconds"] = max(report["api_max_seconds"], latency)
                report["max_rss_kib"] = max(
                    report["max_rss_kib"], resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                )
                # Linux is the deployment target; include API memory, not only workers.
                status = Path(f"/proc/{api.pid}/status").read_text()
                api_memory = next(
                    int(line.split()[1])
                    for line in status.splitlines()
                    if line.startswith("VmRSS:")
                )
                report["api_max_rss_kib"] = max(report["api_max_rss_kib"], api_memory)
                rounds += 1
                report.update(
                    rounds=rounds,
                    elapsed_seconds=time.time() - started,
                    database_bytes=sum(
                        p.stat().st_size for p in args.directory.glob("soak.sqlite*")
                    ),
                )
                if (
                    report["max_rss_kib"] > 512 * 1024
                    or report["api_max_rss_kib"] > 512 * 1024
                    or report["database_bytes"] > 1024**3
                    or latency > 5
                ):
                    raise RuntimeError("endurance resource or latency gate exceeded")
                (args.directory / "report.json").write_text(json.dumps(report, indent=2))
                time.sleep(min(args.interval, max(0, args.duration - (time.time() - started))))
        if report["source_sha256"] != source_hash():
            raise RuntimeError(
                "source changed during endurance run; repeat against the frozen release"
            )
        report["status"] = "passed"
        report["elapsed_seconds"] = time.time() - started
    except Exception as exc:
        report["status"] = "failed"
        report["failures"].append(type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        api.terminate()
        api.wait(timeout=10)
        (args.directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
