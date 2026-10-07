from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from .config import Settings
from .engine import Engine
from .models import Investigation, JobSpec, ReplaySpec


def export(engine, job_id, path):
    engine.store.job(job_id)
    after = ""
    # Atomic replacement prevents an interrupted export from replacing the previous one.
    output = Path(path)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w") as handle:
        while rows := engine.store.observations(job_id, after, 500):
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            after = rows[-1]["id"]
    temporary.replace(output)


def parse_tool(value: str) -> dict:
    """NAME:TARGET. A tool target can never contain ":" (see tools._TARGET_RE), so the
    first colon always separates the two; ToolRun validates both halves."""
    name, separator, target = value.partition(":")
    if not separator:
        raise SystemExit(f"--tool expects NAME:TARGET, got {value!r}")
    return {"name": name.strip().lower(), "target": target.strip()}


def main():
    parser = argparse.ArgumentParser(description="Self-hosted harvesting with durable provenance")
    parser.add_argument("--db", help="database path, overrides HARVEST_DB")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("submit", "run"):
        command = commands.add_parser(name)
        command.add_argument("spec", help="path to JSON JobSpec")
        command.add_argument("--key", help="idempotency key")
        if name == "run":
            command.add_argument("--export", dest="export_path")
    command = commands.add_parser("investigate")
    command.add_argument("objective")
    command.add_argument("--seed", action="append", default=[])
    command.add_argument(
        "--mode",
        choices=["targeted", "enumerative", "continuous", "deep_research"],
        default="targeted",
    )
    command.add_argument("--dataset", default="default")
    command.add_argument("--model", action="store_true")
    command.add_argument(
        "--tool",
        action="append",
        default=[],
        metavar="NAME:TARGET",
        help="external OSINT CLI to run, repeatable; requires HARVEST_TOOLS to permit NAME",
    )
    command.add_argument("--key")
    command = commands.add_parser("worker")
    command.add_argument("--once", action="store_true")
    for name in ("status", "resume", "cancel", "events", "captures", "extractions"):
        commands.add_parser(name).add_argument("job_id")
    command = commands.add_parser(
        "replay", help="offline deterministic re-extraction of existing captures"
    )
    command.add_argument("capture_ids", type=int, nargs="+")
    command.add_argument("--key")
    command.add_argument("--submit-only", action="store_true")
    command.add_argument("--investigation", metavar="PATH", help="path to JSON Investigation")
    command = commands.add_parser("export")
    command.add_argument("job_id")
    command.add_argument("path")
    command = commands.add_parser("dossier")
    command.add_argument("job_id")
    command.add_argument("--format", choices=["json", "markdown"], default="json")
    command.add_argument("--output")
    commands.add_parser("backup").add_argument("path")
    command = commands.add_parser("record-backup")
    command.add_argument("--kind", default="production")
    command.add_argument("--manifest-hash")
    command.add_argument("--verified", action="store_true")
    command = commands.add_parser("mark-backup-offhost")
    command.add_argument("backup_id", type=int)
    commands.add_parser("readiness")
    command = commands.add_parser("search-canary")
    command.add_argument(
        "--query",
        default='site:iana.org "IANA-managed Reserved Domains"',
        help="known-answer query used only for provider readiness",
    )
    commands.add_parser("disable-schedule").add_argument("schedule_id")
    command = commands.add_parser(
        "login-link", help="print a 60-second browser sign-in link for the web UI"
    )
    command.add_argument("--base-url", default="http://127.0.0.1:8000")
    command = commands.add_parser("serve")
    command.add_argument("--host", default="127.0.0.1")
    command.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    if args.db:
        settings.database = args.db
    if args.command == "login-link":
        from . import sessions

        # The fragment never reaches the server or its access log; the UI strips it on load.
        ticket = sessions.issue(
            settings.api_token, seconds=sessions.TICKET_SECONDS, purpose=sessions.TICKET_PURPOSE
        )
        print(f"{args.base_url.rstrip('/')}/#login={ticket}")
        return
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    engine = Engine(settings)
    if args.command == "replay":
        investigation = (
            Investigation.model_validate_json(Path(args.investigation).read_text())
            if args.investigation
            else None
        )
        job_id = engine.replay(
            ReplaySpec(capture_ids=args.capture_ids, investigation=investigation), args.key
        )
        if args.submit_only:
            print(job_id)
            return
        result = engine.run(job_id)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "completed" else 2)
    if args.command in {"submit", "run", "investigate"}:
        if args.command == "investigate":
            spec = JobSpec(
                objective=args.objective,
                seeds=args.seed,
                mode=args.mode,
                dataset=args.dataset,
                use_model=args.model,
                tools=[parse_tool(value) for value in args.tool],
            )
        else:
            spec = JobSpec.model_validate_json(Path(args.spec).read_text())
        job_id = engine.submit(spec, args.key)
        if args.command == "submit":
            print(job_id)
            return
        result = engine.run(job_id)
        if getattr(args, "export_path", None):
            export(engine, job_id, args.export_path)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "completed" else 2)
    if args.command == "worker":
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        if args.once or engine.settings.worker_threads == 1:
            engine.worker(stop, args.once)
        else:
            threads = [
                threading.Thread(target=engine.worker, args=(stop,), daemon=True)
                for _ in range(engine.settings.worker_threads)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                while thread.is_alive():  # timed joins keep SIGTERM handling responsive
                    thread.join(1)
    elif args.command == "status":
        print(json.dumps(engine.store.job(args.job_id), indent=2))
    elif args.command == "resume":
        result = engine.run(args.job_id)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "completed" else 2)
    elif args.command == "cancel":
        engine.store.stop(args.job_id)
    elif args.command == "events":
        print(json.dumps(engine.store.events(args.job_id, limit=1000), indent=2))
    elif args.command in {"captures", "extractions"}:
        print(json.dumps(getattr(engine.store, args.command)(args.job_id, limit=1000), indent=2))
    elif args.command == "export":
        export(engine, args.job_id, args.path)
    elif args.command == "dossier":
        try:
            result = engine.store.dossier(args.job_id)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        if args.format == "markdown":
            from .dossier import render_markdown

            text = render_markdown(result)
        else:
            text = json.dumps(result, indent=2)
        if args.output:
            output = Path(args.output)
            temporary = output.with_suffix(output.suffix + ".tmp")
            # Atomic replacement prevents an interrupted write from replacing the previous one.
            temporary.write_text(text)
            temporary.replace(output)
        else:
            print(text)
    elif args.command == "backup":
        engine.store.backup(args.path)
    elif args.command == "record-backup":
        backup_id = engine.store.record_backup(
            kind=args.kind,
            manifest_hash=args.manifest_hash,
            verified=args.verified,
        )
        print(backup_id)
    elif args.command == "mark-backup-offhost":
        engine.store.mark_backup_offhost(args.backup_id)
    elif args.command == "readiness":
        result = engine.readiness(record_incidents=True)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["ready"] else 2)
    elif args.command == "search-canary":
        if not settings.search_url:
            engine.store.set_incident(
                "search-canary",
                active=True,
                details={"reason": "HARVEST_SEARCH_URL is not configured"},
            )
            print(json.dumps({"ok": False, "reason": "search not configured"}))
            raise SystemExit(2)
        import httpx

        try:
            response = httpx.get(
                settings.search_url.rstrip("/") + "/search",
                params={"q": args.query, "format": "json"},
                timeout=min(settings.request_timeout, 20),
                trust_env=False,
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") if isinstance(payload, dict) else None
            failures = payload.get("unresponsive_engines") if isinstance(payload, dict) else None
            known = any(
                "iana.org" in str(item.get("url", "")).casefold()
                for item in (results or [])
                if isinstance(item, dict)
            )
            details = {
                "query": args.query,
                "results": len(results or []),
                "known_answer": known,
                "unresponsive_engines": failures or [],
            }
            engine.store.set_incident("search-canary", active=not known, details=details)
            print(json.dumps({"ok": known, **details}))
            raise SystemExit(0 if known else 2)
        except (httpx.HTTPError, ValueError) as exc:
            details = {"reason": type(exc).__name__}
            engine.store.set_incident("search-canary", active=True, details=details)
            print(json.dumps({"ok": False, **details}))
            raise SystemExit(2) from exc
    elif args.command == "disable-schedule":
        engine.store.disable_schedule(args.schedule_id)


if __name__ == "__main__":
    main()
