#!/usr/bin/env python3
"""Isolated paired rollback drill: previous images + the backup taken under them.

Never touches production. Secrets come from the restored protected configuration and stay
in container environments; nothing secret is printed or written to the report.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path("/home/pop/data_harvesting_agent/scripts")))
from backup_workstation import verify  # noqa: E402

PG = "postgres@sha256:f7d23353e1b15400d22ebe31189f4d314b87a4c129cc400c8c2d8d4ca127bf81"
REDIS = "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"


def run(*args, check=True, **kw):
    return subprocess.run(args, check=check, capture_output=True, text=True, **kw)


def env_file(text):
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def wait(cmd, tries=90):
    for _ in range(tries):
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            return True
        time.sleep(1)
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--keyring", type=Path, required=True)
    p.add_argument("--harvest-image", required=True)
    p.add_argument("--sf-api-image", required=True)
    p.add_argument("--sf-scanner-image", required=True)
    p.add_argument("--schema4-database", type=Path, help="candidate-migrated copy for refusal check")
    p.add_argument("--report", type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    t0 = time.time()
    manifest = verify(a.archive, a.keyring)
    report = {
        "archive_sha256": hashlib.sha256(a.archive.read_bytes()).hexdigest(),
        "backup_created": manifest["created"],
        "backup_images": manifest["images"],
        "rollback_images": {
            "harvest": run("docker", "image", "inspect", a.harvest_image, "--format", "{{.Id}}").stdout.strip(),
            "sf_api": run("docker", "image", "inspect", a.sf_api_image, "--format", "{{.Id}}").stdout.strip(),
            "sf_scanner": run("docker", "image", "inspect", a.sf_scanner_image, "--format", "{{.Id}}").stdout.strip(),
        },
        "checks": {},
        "passed": False,
    }
    images = manifest["images"]
    report["checks"]["images_paired_with_backup"] = (
        images["harvest-platform-api-1"] == images["harvest-platform-worker-1"] == report["rollback_images"]["harvest"]
        and images["sf-api"] == report["rollback_images"]["sf_api"]
        and images["sf-celery-worker"] == report["rollback_images"]["sf_scanner"]
    )
    name = "harvest-rollback-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="harvest-rollback-") as d:
        root = Path(d)
        run("gpg", "--batch", "--homedir", str(a.keyring), "--output", str(root / "b.tar"), "--decrypt", str(a.archive))
        with tarfile.open(root / "b.tar") as t:
            t.extractall(root, filter="data")
        (root / "b.tar").unlink()
        with tarfile.open(root / "configuration.tar") as t:
            t.extractall(root / "configuration", filter="data")
        app_env = env_file((root / "configuration/app/.env").read_text())
        sf_env = env_file((root / "configuration/spiderfoot-ng/.env").read_text())
        creds = root / "ghunt-credentials/ghunt/creds.m"
        report["checks"]["ghunt_credential_restored"] = creds.is_file() and (
            manifest["files"].get("ghunt-credentials/ghunt/creds.m")
            == hashlib.sha256(creds.read_bytes()).hexdigest()
        )
        # Interrupted-job rule BEFORE any worker could start.
        with sqlite3.connect(root / "harvest.sqlite") as db:
            report["restored_schema_before_start"] = db.execute("PRAGMA user_version").fetchone()[0]
            report["active_jobs_in_backup"] = db.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            db.execute("UPDATE tasks SET status='cancelled',token=NULL,lease_until=NULL,error='restored backup: interrupted; acquisition not repeated' WHERE status IN ('pending','running')")
            db.execute("UPDATE jobs SET status='partial',reason='restored backup: interrupted; acquisition not repeated',finished=? WHERE status IN ('queued','running')", (time.time(),))
            report["checks"]["sqlite_integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            report["checks"]["sqlite_foreign_keys"] = not db.execute("PRAGMA foreign_key_check").fetchall()
            bad = [h for h, body in db.execute("SELECT hash, body FROM blobs") if hashlib.sha256(body).hexdigest() != h]
            report["evidence_blobs"] = db.execute("SELECT count(*) FROM blobs").fetchone()[0]
            report["checks"]["blob_hashes"] = not bad
            counts = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("jobs", "captures", "observations")}
        report["restored_counts"] = counts
        # Compose configuration from the backup must render (pre-release topology, no readiness overlay).
        app = root / "configuration/app"
        files = [f for f in ("compose.yaml", "compose.override.yaml", "compose.egress-proxy.yaml",
                             "compose.ghunt.yaml", "compose.searxng.yaml") if (app / f).is_file()]
        rendered = subprocess.run(
            ["docker", "compose", "--project-directory", str(app), "--env-file", str(app / ".env"),
             *sum((["-f", str(app / f)] for f in files), []), "config", "--format", "json"],
            capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "COMPOSE_FILE"},
        )
        if rendered.returncode == 0:
            cfg = json.loads(rendered.stdout)
            report["rollback_compose"] = {
                "files": files,
                "worker_networks": sorted(cfg["services"]["worker"].get("networks", {})),
                "harvest_egress_internal": cfg["networks"].get("harvest-egress", {}).get("internal"),
                "readiness_overlay_absent": "monitor" not in cfg["services"],
            }
        report["checks"]["rollback_compose_renders"] = rendered.returncode == 0
        run("docker", "network", "create", "--internal", name)
        try:
            run("docker", "run", "-d", "--name", name + "-pg", "--network", name, "--network-alias", "postgres",
                "-e", "POSTGRES_PASSWORD=isolated-rollback-only", PG)
            run("docker", "run", "-d", "--name", name + "-redis", "--network", name, "--network-alias", "redis", REDIS)
            assert wait(["docker", "exec", name + "-pg", "pg_isready", "-U", "postgres"])
            time.sleep(2)
            with (root / "spiderfoot.dump").open("rb") as dump:
                subprocess.run(["docker", "exec", "-i", name + "-pg", "pg_restore", "--exit-on-error", "--no-owner",
                                "-U", "postgres", "-d", "postgres"], stdin=dump, check=True, capture_output=True)
            q = lambda sql: run("docker", "exec", name + "-pg", "psql", "-U", "postgres", "-Atc", sql).stdout.strip()
            report["spiderfoot_events"] = int(q("SELECT count(*) FROM tbl_scan_results"))
            report["spiderfoot_scans"] = int(q("SELECT count(*) FROM tbl_scan_instance"))
            sf_container_env = {
                "SF_SERVICE_ROLE": "api", "SF_DEPLOYMENT_MODE": "microservice",
                "SF_POSTGRES_DSN": "postgresql://postgres:isolated-rollback-only@postgres:5432/postgres",
                "SF_REDIS_URL": "redis://redis:6379/0", "SF_API_HOST": "0.0.0.0", "SF_API_PORT": "8001",
                "SF_API_WORKERS": "2", "SF_API_KEY_SECRET": sf_env["SF_API_KEY_SECRET"],
                "SF_JWT_SECRET": sf_env["SF_JWT_SECRET"], "SF_ADMIN_PASSWORD": sf_env["SF_ADMIN_PASSWORD"],
                "SF_AUTH_REQUIRED": "true", "SF_MINIO_ENABLED": "false", "SF_QDRANT_ENABLED": "false",
            }
            envfile = root / "sf-container.env"
            envfile.write_text("".join(f"{k}={v}\n" for k, v in sf_container_env.items()))
            run("docker", "run", "-d", "--name", name + "-sfapi", "--network", name, "--env-file", str(envfile),
                "--entrypoint", "/usr/local/bin/docker-entrypoint.sh", a.sf_api_image,
                "python", "-m", "spiderfoot.service_runner", "--service", "api", "--port", "8001")
            envfile.unlink()
            health = "import json,urllib.request; d=json.load(urllib.request.urlopen('http://localhost:8001/health',timeout=8)); raise SystemExit(0 if d['components']['postgresql']['status']=='up' else 1)"
            report["checks"]["previous_spiderfoot_api_healthy"] = wait(["docker", "exec", name + "-sfapi", "python", "-c", health], 120)
            auth = ("import os,urllib.request,json; r=urllib.request.Request('http://localhost:8001/api/v1/scans?page_size=1',"
                    "headers={'X-API-Key':os.environ['K']}); d=json.load(urllib.request.urlopen(r,timeout=10)); print(d.get('total', len(d.get('items',[]))))")
            res = subprocess.run(["docker", "exec", "-e", "K=" + app_env["HARVEST_SPIDERFOOT_API_KEY"], name + "-sfapi",
                                  "python", "-c", auth], capture_output=True, text=True)
            report["checks"]["restored_spiderfoot_key_authenticates"] = res.returncode == 0
            # Previous Harvest image on the restored (schema 3) database.
            run("docker", "run", "--rm", "--network", "none", "--user", "0", "-v", f"{root}:/restore",
                a.harvest_image, "chown", "-R", "10001:10001", "/restore")
            run("docker", "run", "-d", "--name", name + "-api", "--network", name, "-v", f"{root}:/data",
                "-e", "HARVEST_API_TOKEN=isolated-rollback-drill-token-only", a.harvest_image)
            probe = "import urllib.request,json; q=urllib.request.Request('http://127.0.0.1:8000/health',headers={'Authorization':'Bearer isolated-rollback-drill-token-only'}); assert json.load(urllib.request.urlopen(q))['status']=='ok'"
            report["checks"]["previous_harvest_api_live"] = wait(["docker", "exec", name + "-api", "python", "-c", probe], 60)
            check = ("import sqlite3; d=sqlite3.connect('/data/harvest.sqlite'); print(d.execute('pragma user_version').fetchone()[0],"
                     "d.execute(\"select count(*) from jobs where status in ('queued','running')\").fetchone()[0])")
            version, active = run("docker", "exec", name + "-api", "python", "-c", check).stdout.split()
            report["schema_after_previous_api_start"] = int(version)
            report["checks"]["schema_unchanged_by_previous_code"] = int(version) == report["restored_schema_before_start"]
            report["checks"]["no_active_jobs_after_rule"] = int(active) == 0
        finally:
            for s in ("-api", "-sfapi", "-redis", "-pg"):
                subprocess.run(["docker", "rm", "-f", name + s], capture_output=True)
            subprocess.run(["docker", "network", "rm", name], capture_output=True)
            subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "0", "-v", f"{root}:/restore",
                            a.harvest_image, "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/restore"], capture_output=True)
    if a.schema4_database:
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / "harvest.sqlite"
            copy.write_bytes(a.schema4_database.read_bytes())
            os.chmod(d, 0o777)
            os.chmod(copy, 0o666)
            res = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{d}:/data", a.harvest_image,
                                  "python", "-c", "from harvest.store import Store; Store('/data/harvest.sqlite')"],
                                 capture_output=True, text=True)
            report["previous_code_on_schema4"] = {
                "refused": res.returncode != 0 and "unsupported database version 4" in res.stderr,
                "error_tail": res.stderr.strip().splitlines()[-1][:200] if res.stderr else "",
            }
            report["checks"]["previous_code_refuses_schema4"] = report["previous_code_on_schema4"]["refused"]
    report["duration_seconds"] = round(time.time() - t0, 2)
    report["passed"] = all(report["checks"].values())
    report["drill_script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    a.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
