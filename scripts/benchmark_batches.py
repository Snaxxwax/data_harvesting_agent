"""Owned-source HTTP + offline replay workload; reports measured results, not a scale claim."""

import argparse
import json
import resource
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harvest.config import Settings
from harvest.engine import Engine
from harvest.models import JobSpec, Limits, ReplaySpec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    args = parser.parse_args()
    if Path(args.db).exists() or not 1 <= args.rows <= 10000:
        parser.error("use a new database path and 1–10000 rows")
    records = [
        {
            "id": i,
            "name": f"Organization {i}",
            "jurisdiction": "GB",
            "status": "active",
            "employees": i + 2,
        }
        for i in range(args.rows)
    ]
    if args.format == "json":
        body = json.dumps({"items": records}).encode()
    else:
        body = (
            "id,name,jurisdiction,status,employees\n"
            + "\n".join(f"{r['id']},{r['name']},GB,active,{r['employees']}" for r in records)
        ).encode()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            robots = self.path == "/robots.txt"
            payload = b"User-agent: *\nAllow: /\n" if robots else body
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain"
                if robots
                else ("application/json" if args.format == "json" else "text/csv"),
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        engine = Engine(
            Settings(database=args.db, private_hosts=frozenset({"127.0.0.1"}), proxy=None)
        )
        spec = JobSpec(
            objective="Enumerate the owned organization register",
            mode="enumerative",
            seeds=[f"http://127.0.0.1:{server.server_port}/register.{args.format}"],
            allowed_domains=["127.0.0.1"],
            limits=Limits(depth=0, domain_delay=0.1),
        )
        started = time.perf_counter()
        job = engine.run(engine.submit(spec, "benchmark"))
        acquisition_seconds = time.perf_counter() - started
        observations = engine.store.observations(job["id"], limit=args.rows * 5 + 1)
        assert job["status"] == "completed" and len(observations) == args.rows * 5
        assert len({o["entity_key"] for o in observations}) == args.rows
        assert all(o["capture_ids"] == [1] and o["extraction_ids"] for o in observations)
        started = time.perf_counter()
        replay = engine.run(engine.replay(ReplaySpec(capture_ids=[1]), "benchmark-replay"))
        replay_seconds = time.perf_counter() - started
        assert replay["status"] == "completed" and replay["requests"] == 0
        assert {o["id"] for o in observations} == {
            o["id"] for o in engine.store.observations(replay["id"], limit=args.rows * 5 + 1)
        }
        assert requests == ["/robots.txt", f"/register.{args.format}"]
        with engine.store.connection() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        print(
            json.dumps(
                {
                    "format": args.format,
                    "source_records": args.rows,
                    "recovered_entities": len({o["entity_key"] for o in observations}),
                    "observations": len(observations),
                    "body_bytes": len(body),
                    "requests": len(requests),
                    "acquisition_seconds": round(acquisition_seconds, 3),
                    "replay_seconds": round(replay_seconds, 3),
                    "replay_requests": replay["requests"],
                    "batches": engine.store.extractions(job["id"])[0]["batches"],
                    "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    * (1 if sys.platform == "darwin" else 1024),
                    "database_bytes": Path(args.db).stat().st_size,
                    "integrity_check": "ok",
                },
                indent=2,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
