#!/usr/bin/env python3
"""Create one encrypted consistency-checked backup on OVH. No plaintext off-host copies."""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/opt/harvest"))
    parser.add_argument("--recipient", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("/opt/harvest/backups/encrypted"))
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="harvest-backup-") as temporary:
        work = Path(temporary)
        # SQLite's online backup API handles active readers/writers and rollback journals.
        code = "import sqlite3; s=sqlite3.connect('/data/harvest.sqlite'); d=sqlite3.connect('/data/backup.sqlite'); s.backup(d); assert d.execute('pragma integrity_check').fetchone()[0]=='ok'; d.close(); s.close()"
        run("docker", "exec", "harvest-platform-worker-1", "python", "-c", code)
        run(
            "docker",
            "cp",
            "harvest-platform-worker-1:/data/backup.sqlite",
            str(work / "harvest.sqlite"),
        )
        run("docker", "exec", "harvest-platform-worker-1", "rm", "/data/backup.sqlite")
        with (work / "spiderfoot.dump").open("wb") as output:
            run(
                "docker",
                "exec",
                "sf-postgres",
                "pg_dump",
                "-U",
                "spiderfoot",
                "-d",
                "spiderfoot",
                "-Fc",
                stdout=output,
            )
        run(
            "docker",
            "cp",
            "harvest-platform-worker-1:/home/harvest/.malfrats",
            str(work / "ghunt-credentials"),
        )
        # Include tool journals so the most recent completed checks survive an interrupted run.
        probe = subprocess.run(
            ["docker", "exec", "harvest-platform-worker-1", "test", "-d", "/data/checkpoints"]
        )
        if probe.returncode == 0:
            run(
                "docker",
                "cp",
                "harvest-platform-worker-1:/data/checkpoints",
                str(work / "checkpoints"),
            )
        with tarfile.open(work / "configuration.tar", "w") as bundle:
            for relative in (
                "credentials.env",
                "app/.env",
                "app/.env.build",
                "app/compose.yaml",
                "app/compose.override.yaml",
                "app/compose.egress-proxy.yaml",
                "app/compose.ghunt.yaml",
                "app/compose.searxng.yaml",
                "app/compose.readiness.yaml",
                "app/spiderfoot-profiles.json",
                "app/.env.release",
                "app/release-manifest.json",
                "spiderfoot-ng/.env",
                "spiderfoot-ng/compose.core.yml",
                "searxng/settings.yml",
                "sf-dns/Corefile",
                "egress-relay/tinyproxy.conf",
                "egress-relay/upstream.secret",
                "egress-relay/tinyproxy.conf.template",
                "egress-relay/entrypoint.sh",
            ):
                path = args.root / relative
                if path.is_file():
                    bundle.add(path, arcname=relative)
        manifest = {"version": 1, "created": time.time(), "files": {}, "images": {}}
        for path in sorted(work.rglob("*")):
            if path.is_file():
                manifest["files"][str(path.relative_to(work))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        for container in (
            "harvest-platform-api-1",
            "harvest-platform-worker-1",
            "sf-api",
            "sf-celery-worker",
            "sf-postgres",
        ):
            result = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.Image}}", container], text=True
            ).strip()
            manifest["images"][container] = result
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        archive = work / "backup.tar"
        with tarfile.open(archive, "w") as bundle:
            for path in work.iterdir():
                if path != archive:
                    bundle.add(path, arcname=path.name)
        name = time.strftime("harvest-%Y%m%dT%H%M%SZ.tar.gpg", time.gmtime())
        temporary_out = args.output / (name + ".tmp")
        run(
            "gpg",
            "--batch",
            "--yes",
            "--trust-model",
            "always",
            "--recipient-file",
            str(args.recipient),
            "--output",
            str(temporary_out),
            "--encrypt",
            str(archive),
        )
        temporary_out.replace(args.output / name)
        if os.environ.get("SUDO_UID"):
            os.chown(args.output / name, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
        print(name)


if __name__ == "__main__":
    main()
