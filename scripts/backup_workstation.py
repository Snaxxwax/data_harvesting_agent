#!/usr/bin/env python3
"""Pull encrypted backups over Tailscale, verify plaintext in private scratch, retain tiers."""

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


def retained(paths):
    """Union of seven newest days, four ISO weeks and three calendar months."""
    keep = set()
    for category, limit in (("day", 7), ("week", 4), ("month", 3)):
        buckets = set()
        for path in sorted(paths, reverse=True):
            timestamp = dt.datetime.strptime(path.name, "harvest-%Y%m%dT%H%M%SZ.tar.gpg")
            bucket = {
                "day": timestamp.date(),
                "week": timestamp.isocalendar()[:2],
                "month": (timestamp.year, timestamp.month),
            }[category]
            if bucket not in buckets and len(buckets) < limit:
                buckets.add(bucket)
                keep.add(path)
    return keep


def verify(archive, keyring):
    with tempfile.TemporaryDirectory(prefix="harvest-verify-") as directory:
        root = Path(directory)
        plain = root / "backup.tar"
        subprocess.run(
            [
                "gpg",
                "--batch",
                "--homedir",
                str(keyring),
                "--output",
                str(plain),
                "--decrypt",
                str(archive),
            ],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        with tarfile.open(plain) as bundle:
            # No links, absolute paths, or path traversal from a remotely supplied archive.
            for member in bundle.getmembers():
                if (
                    member.issym()
                    or member.islnk()
                    or not (root / member.name).resolve().is_relative_to(root)
                ):
                    raise ValueError("unsafe backup archive member")
            bundle.extractall(root, filter="data")
        manifest = json.loads((root / "manifest.json").read_text())
        for name, expected in manifest["files"].items():
            path = (root / name).resolve()
            if (
                not path.is_relative_to(root)
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise ValueError("backup content hash mismatch")
        with sqlite3.connect(root / "harvest.sqlite") as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            for hashed, body in db.execute("SELECT hash,body FROM blobs"):
                if hashlib.sha256(body).hexdigest() != hashed:
                    raise ValueError("evidence hash mismatch")
        return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="ovh")
    parser.add_argument("--directory", type=Path, default=Path("/home/pop/backups/harvest"))
    parser.add_argument(
        "--keyring", type=Path, default=Path.home() / ".local/share/harvest-backup-key"
    )
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "rsync",
            "-a",
            "--include=harvest-*.tar.gpg",
            "--exclude=*",
            args.host + ":/opt/harvest/backups/encrypted/",
            str(args.directory) + "/",
        ],
        check=True,
    )
    paths = sorted(args.directory.glob("harvest-*.tar.gpg"))
    if not paths:
        raise RuntimeError("no off-host backups available")
    latest = paths[-1]
    manifest = verify(latest, args.keyring)
    acknowledgment = {
        "name": latest.name,
        "sha256": hashlib.sha256(latest.read_bytes()).hexdigest(),
        "created": manifest["created"],
        "verified_at": time.time(),
    }
    # Mark freshness only after decryption, manifest, integrity and evidence checks succeed.
    code = "import json,sys,os; from pathlib import Path; p=Path(os.environ['HARVEST_DB']).parent/'backup-verified.json'; tmp=p.with_suffix('.tmp'); tmp.write_text(json.dumps(json.load(sys.stdin))); tmp.replace(p)"
    subprocess.run(
        [
            "ssh",
            args.host,
            "docker",
            "exec",
            "-i",
            "harvest-platform-worker-1",
            "python",
            "-c",
            __import__("shlex").quote(code),
        ],
        input=json.dumps(acknowledgment),
        text=True,
        check=True,
    )
    keep = retained(paths)
    for path in paths:
        if path not in keep:
            path.unlink()
    (args.directory / "latest-verified.json").write_text(json.dumps(acknowledgment, indent=2))
    print(json.dumps(acknowledgment))


if __name__ == "__main__":
    main()
