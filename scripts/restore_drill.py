#!/usr/bin/env python3
"""Restore a verified backup into an isolated, disposable PostgreSQL/API stack."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from backup_workstation import verify


def cmd(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--keyring", type=Path, required=True)
    parser.add_argument("--image", default="harvest-platform:readiness-dev")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    started = time.time()
    manifest = verify(args.archive, args.keyring)
    name = "harvest-restore-" + uuid.uuid4().hex[:10]
    report = {
        "archive_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        "started": started,
        "source_images": manifest["images"],
        "passed": False,
    }
    with tempfile.TemporaryDirectory(prefix="harvest-restore-") as directory:
        root = Path(directory)
        cmd(
            "gpg",
            "--batch",
            "--homedir",
            str(args.keyring),
            "--output",
            str(root / "backup.tar"),
            "--decrypt",
            str(args.archive),
        )
        with tarfile.open(root / "backup.tar") as bundle:
            bundle.extractall(root, filter="data")
        # This is the restore rule, also used in a real recovery before starting any worker.
        with sqlite3.connect(root / "harvest.sqlite") as db:
            active = db.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            db.execute(
                "UPDATE tasks SET status='cancelled',token=NULL,lease_until=NULL,error='restored backup: interrupted; acquisition not repeated' WHERE status IN ('pending','running')"
            )
            db.execute(
                "UPDATE jobs SET status='partial',reason='restored backup: interrupted; acquisition not repeated',finished=? WHERE status IN ('queued','running')",
                (time.time(),),
            )
            report["interrupted_jobs"] = active
            report["evidence_blobs"] = db.execute("SELECT count(*) FROM blobs").fetchone()[0]
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
        with tarfile.open(root / "configuration.tar") as bundle:
            names = set(bundle.getnames())
            required = {"app/.env", "spiderfoot-ng/.env", "egress-relay/upstream.secret"}
            if not required <= names:
                raise ValueError("backup missing required protected configuration")
            report["configuration_files"] = len(names)
        cmd("docker", "network", "create", "--internal", name)
        try:
            cmd(
                "docker",
                "run",
                "-d",
                "--name",
                name + "-pg",
                "--network",
                name,
                "-e",
                "POSTGRES_PASSWORD=isolated-restore-only",
                "postgres@sha256:f7d23353e1b15400d22ebe31189f4d314b87a4c129cc400c8c2d8d4ca127bf81",
            )
            for _ in range(60):
                ready = subprocess.run(
                    ["docker", "exec", name + "-pg", "pg_isready", "-U", "postgres"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if ready.returncode == 0:
                    break
                time.sleep(1)
            with (root / "spiderfoot.dump").open("rb") as dump:
                cmd(
                    "docker",
                    "exec",
                    "-i",
                    name + "-pg",
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    stdin=dump,
                )
            count = (
                cmd(
                    "docker",
                    "exec",
                    name + "-pg",
                    "psql",
                    "-U",
                    "postgres",
                    "-Atc",
                    "SELECT count(*) FROM tbl_scan_results",
                )
                .stdout.decode()
                .strip()
            )
            report["spiderfoot_events"] = int(count)
            # Match image uid without loosening directory permissions for other host users.
            cmd(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0",
                "-v",
                str(root) + ":/restore",
                args.image,
                "chown",
                "-R",
                "10001:10001",
                "/restore",
            )
            cmd(
                "docker",
                "run",
                "-d",
                "--name",
                name + "-api",
                "--network",
                name,
                "-v",
                str(root) + ":/data",
                "-e",
                "HARVEST_API_TOKEN=isolated-restore-drill-token-only",
                args.image,
            )
            probe = "import urllib.request,json; q=urllib.request.Request('http://127.0.0.1:8000/health',headers={'Authorization':'Bearer isolated-restore-drill-token-only'}); assert json.load(urllib.request.urlopen(q))['status']=='ok'"
            for _ in range(30):
                result = subprocess.run(
                    ["docker", "exec", name + "-api", "python", "-c", probe],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("restored API did not become live")
            check = "import sqlite3; d=sqlite3.connect('/data/harvest.sqlite'); assert d.execute(\"select count(*) from jobs where status in ('queued','running')\").fetchone()[0]==0; print(d.execute('pragma user_version').fetchone()[0])"
            report["restored_schema"] = int(
                cmd("docker", "exec", name + "-api", "python", "-c", check).stdout
            )
            report["duration_seconds"] = round(time.time() - started, 2)
            report["passed"] = report["duration_seconds"] < 7200
        finally:
            for suffix in ("-api", "-pg"):
                subprocess.run(
                    ["docker", "rm", "-f", name + suffix],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            subprocess.run(
                ["docker", "network", "rm", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            cmd(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0",
                "-v",
                str(root) + ":/restore",
                args.image,
                "chown",
                "-R",
                f"{os.getuid()}:{os.getgid()}",
                "/restore",
            )
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
