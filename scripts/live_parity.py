#!/usr/bin/env python3
"""Bounded matched live comparison in an isolated database, using authorized audit targets.

Exactly two rounds × three tools × (standalone + Harvest) = twelve invocations maximum.
Targets are supplied in a private JSON file; use the existing authorized audit targets.
The durable ledger is written BEFORE invocation. Interrupted invocations are never retried.
"""

import argparse
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from acquisition_compare import compare

from harvest import executions, tools
from harvest.config import Settings
from harvest.engine import Engine
from harvest.models import JobSpec


def atomic(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(path)


def native_output(store, execution_id, name):
    with store.connection() as db:
        rows = db.execute(
            """SELECT a.kind,a.name,b.body FROM artifacts a JOIN blobs b ON b.hash=a.body_hash
            WHERE a.execution_id=? ORDER BY a.id""",
            (execution_id,),
        ).fetchall()
    if name == "spiderfoot":
        events = {
            row["body"]: json.loads(row["body"]) for row in rows if row["kind"] == "native-event"
        }
        return {"events": list(events.values())}
    native = [json.loads(row["body"]) for row in rows if row["kind"] == "native"]
    if not native:
        raise RuntimeError(
            "native report missing; retain checkpoints and investigate before sign-off"
        )
    return native[-1]


def standalone(engine, job_id, name, target, timeout, matched, build):
    task = engine.store.claim(job_id)
    recorder = executions.begin(engine.store, task, matched, build)
    stop, cancelled = threading.Event(), threading.Event()

    def heartbeat():
        while not stop.wait(1):
            try:
                engine.store.heartbeat(task, 30)
            except Exception:
                cancelled.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    marker = tools._RECORDER.set(recorder)
    try:
        with tempfile.TemporaryDirectory() as work:
            kwargs = (
                {"top_sites": None}
                if name == "maigret"
                else {"task_id": task["id"]}
                if name == "spiderfoot"
                else {}
            )
            raw = tools.TOOLS[name]["run"](
                target, work, timeout, cancelled, engine.settings, **kwargs
            )
        recorder.drain()
        output = raw if isinstance(raw, dict) else {"results": raw}
        recorder.finish("partial" if output.get("partial") else "completed")
        return recorder.id, output
    except Exception as exc:
        recorder.finish("interrupted", type(exc).__name__)
        recorder.preserve_capture()
        raise
    finally:
        stop.set()
        thread.join(5)
        tools._RECORDER.reset(marker)
        engine.store.stop(job_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--spiderfoot-revision",
        required=True,
        help="revision/image identity of the actual scanner deployment",
    )
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    ledger_path = args.directory / "ledger.json"
    if ledger_path.exists():
        raise SystemExit("ledger exists: refusing to repeat an interrupted or completed comparison")
    targets = json.loads(args.targets.read_text())
    if set(targets) != {"maigret", "ghunt", "spiderfoot"}:
        raise ValueError("one authorized target per tool is required")
    settings = Settings(
        database=str(args.directory / "comparison.sqlite"),
        scan_namespace="audit_" + uuid.uuid4().hex[:16],
    )
    engine = Engine(settings)
    ledger = {
        "started": time.time(),
        "maximum_invocations": 12,
        "invocations": [],
        "comparisons": [],
    }
    build = executions.build_info()
    if (
        not isinstance(build.get("tools"), dict)
        or not {"maigret", "ghunt"} <= build["tools"].keys()
    ):
        raise SystemExit("run inside the release image with both pinned tools installed")
    if not {"maigret", "ghunt", "spiderfoot"} <= settings.tools:
        raise SystemExit("all three tools must be configured before starting the comparison")
    credentials = Path.home() / ".malfrats/ghunt/creds.m"
    if not credentials.is_file() or not settings.spiderfoot_api_key:
        raise SystemExit("persistent credentials must be present before starting the comparison")
    ledger["namespace"] = settings.scan_namespace
    ledger["build"] = build
    for round_number in (1, 2):
        for name, target in targets.items():
            timeout = min(settings.tool_timeout, settings.tool_timeouts[name])
            matched = {
                "tool": name,
                "version": build["tools"].get(name, args.spiderfoot_revision),
                "target": target,
                "database_sha256": build.get("maigret", {}).get("database_sha256")
                if name == "maigret"
                else None,
                "modules": tools.spiderfoot_plan(tools._spiderfoot_target_type(target), settings)[
                    "modules"
                ]
                if name == "spiderfoot"
                else [],
                "credential_fingerprint": hashlib.sha256(
                    credentials.read_bytes()
                    if name == "ghunt"
                    else settings.spiderfoot_api_key.encode()
                    if name == "spiderfoot"
                    else b"no credentials"
                ).hexdigest(),
                "proxy_fingerprint": hashlib.sha256(
                    (settings.proxy or "direct").encode()
                ).hexdigest(),
                "retries": settings.maigret_retries,
                "concurrency": max(
                    1, settings.egress_max_connections // (2 * settings.worker_threads)
                ),
                "deadline_seconds": timeout,
                "top_sites": None,
            }
            outputs, references = {}, {}
            for mode in ("standalone", "harvest"):
                if len(ledger["invocations"]) >= 12:
                    raise RuntimeError("invocation ceiling reached")
                entry = {
                    "round": round_number,
                    "tool": name,
                    "mode": mode,
                    "started": time.time(),
                    "outcome": "unfinished",
                }
                ledger["invocations"].append(entry)
                atomic(ledger_path, ledger)
                spec = JobSpec(
                    objective="authorized matched audit comparison",
                    dataset="parity",
                    tools=[{"name": name, "target": target, "crawl": False}],
                    limits={"seconds": int(timeout) + 60, "pivots": 0, "tool_runs": 1},
                )
                job_id = engine.submit(spec)
                entry["job_id"] = job_id
                atomic(ledger_path, ledger)
                if mode == "standalone":
                    try:
                        execution_id, outputs[mode] = standalone(
                            engine, job_id, name, target, timeout, matched, build
                        )
                    except Exception as exc:
                        entry["outcome"] = type(exc).__name__
                        atomic(ledger_path, ledger)
                        raise
                else:
                    engine.run(job_id)
                    captures = engine.store.captures(job_id)
                    if not captures:
                        entry["outcome"] = "no capture"
                        atomic(ledger_path, ledger)
                        raise RuntimeError(
                            "live candidate failed before retaining evidence; inspect coverage"
                        )
                    outputs[mode] = json.loads(engine.store.capture(captures[0]["id"])["body"])
                    execution_id = executions.coverage(engine.store, job_id)["executions"][0]["id"]
                references[mode] = {
                    "settings": matched,
                    "native": native_output(engine.store, execution_id, name),
                }
                outputs[mode]["settings"] = matched
                prefix = f"round-{round_number}-{name}-{mode}"
                atomic(args.directory / (prefix + "-native.json"), references[mode])
                atomic(args.directory / (prefix + "-normalized.json"), outputs[mode])
                entry["retention"] = compare(references[mode], outputs[mode])
                entry.update(outcome="returned", finished=time.time(), job_id=job_id)
                atomic(ledger_path, ledger)
            # Compare sets and fields; changing remote content is a discrepancy to review,
            # not an automatic assertion that the adapter dropped evidence.
            comparison = compare(references["standalone"], outputs["harvest"])
            reverse = compare(references["harvest"], outputs["standalone"])
            result = {
                "round": round_number,
                "tool": name,
                "settings": matched,
                "standalone_to_harvest": comparison,
                "harvest_to_standalone": reverse,
                "requires_review": not comparison["passed"] or not reverse["passed"],
            }
            ledger["comparisons"].append(result)
            atomic(ledger_path, ledger)
    ledger["finished"] = time.time()
    ledger["passed"] = all(not row["requires_review"] for row in ledger["comparisons"]) and all(
        row.get("retention", {}).get("passed") for row in ledger["invocations"]
    )
    atomic(ledger_path, ledger)


if __name__ == "__main__":
    main()
