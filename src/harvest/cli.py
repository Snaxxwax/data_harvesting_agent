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
from .models import JobSpec, ReplaySpec


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
    command = commands.add_parser("export")
    command.add_argument("job_id")
    command.add_argument("path")
    command = commands.add_parser("dossier")
    command.add_argument("job_id")
    command.add_argument("--format", choices=["json", "markdown"], default="json")
    command.add_argument("--output")
    commands.add_parser("backup").add_argument("path")
    commands.add_parser("disable-schedule").add_argument("schedule_id")
    command = commands.add_parser("serve")
    command.add_argument("--host", default="127.0.0.1")
    command.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    if args.db:
        settings.database = args.db
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    engine = Engine(settings)
    if args.command == "replay":
        job_id = engine.replay(ReplaySpec(capture_ids=args.capture_ids), args.key)
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
        engine.worker(stop, args.once)
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
    elif args.command == "disable-schedule":
        engine.store.disable_schedule(args.schedule_id)


if __name__ == "__main__":
    main()
