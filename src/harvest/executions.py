"""Versioned acquisition evidence, independent of task completion and worker leases.

Only structured native data enters this boundary. Subprocess console streams are discarded;
never attempt to sanitize arbitrary console output with a best-effort regex.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

OUTCOMES = frozenset(
    {"found", "absent", "blocked", "errored", "unsupported", "skipped", "unfinished"}
)
_SECRET = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|token|api.?key|credential|headers)"
)


def safe(value):
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if _SECRET.search(str(k)) else safe(v) for k, v in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [safe(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    return value


def encode(value):
    return json.dumps(safe(value), ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def migrate(store):
    with store.transaction() as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != 3:
            return
        for sql in (
            """CREATE TABLE executions (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
                version INTEGER NOT NULL DEFAULT 1, tool TEXT NOT NULL, settings TEXT NOT NULL,
                build TEXT NOT NULL, started REAL NOT NULL, finished REAL, outcome TEXT NOT NULL,
                reason TEXT, checkpoint_path TEXT, external_id TEXT)""",
            """CREATE TABLE artifacts (
                id INTEGER PRIMARY KEY, execution_id INTEGER NOT NULL REFERENCES executions(id),
                kind TEXT NOT NULL, name TEXT NOT NULL, body_hash TEXT NOT NULL REFERENCES blobs(hash),
                created REAL NOT NULL, UNIQUE(execution_id,kind,name,body_hash))""",
            """CREATE TABLE tool_checks (
                execution_id INTEGER NOT NULL REFERENCES executions(id), name TEXT NOT NULL,
                outcome TEXT NOT NULL, reason TEXT NOT NULL, artifact_id INTEGER REFERENCES artifacts(id),
                updated REAL NOT NULL, PRIMARY KEY(execution_id,name))""",
            """CREATE TABLE operational_state (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)""",
            """CREATE TABLE incidents (id INTEGER PRIMARY KEY, name TEXT NOT NULL, opened REAL NOT NULL,
                resolved REAL, detail TEXT NOT NULL)""",
            "CREATE INDEX artifacts_execution ON artifacts(execution_id,id)",
            "PRAGMA user_version=4",
        ):
            db.execute(sql)


def begin(store, task, settings, build):
    with store.transaction() as db:
        store.owned(db, task)
        row = db.execute("SELECT id FROM executions WHERE task_id=?", (task["id"],)).fetchone()
        if row:
            # No restart of an externally executing task, including an ambiguous HTTP POST.
            raise RuntimeError("tool execution already exists; acquisition cannot be repeated")
        eid = db.execute(
            """INSERT INTO executions(task_id,tool,settings,build,started,outcome)
            VALUES(?,?,?,?,?,'running')""",
            (
                task["id"],
                task["payload"]["tool"],
                encode(settings).decode(),
                encode(build).decode(),
                time.time(),
            ),
        ).lastrowid
        directory = Path(store.path).parent / "checkpoints"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"{eid}-{task['token']}.jsonl"
        db.execute("UPDATE executions SET checkpoint_path=? WHERE id=?", (str(path), eid))
        return Recorder(store, eid, path)


class Recorder:
    def __init__(self, store, execution_id, path=None):
        self.store, self.id, self.path = store, execution_id, path
        self.offset = 0

    def artifact(self, kind, name, data):
        body = encode(data)
        hashed = hashlib.sha256(body).hexdigest()
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (hashed, body))
            db.execute(
                """INSERT OR IGNORE INTO artifacts(execution_id,kind,name,body_hash,created)
                VALUES(?,?,?,?,?)""",
                (self.id, kind, name, hashed, time.time()),
            )
            return db.execute(
                """SELECT id FROM artifacts WHERE execution_id=? AND kind=?
                AND name=? AND body_hash=?""",
                (self.id, kind, name, hashed),
            ).fetchone()[0]

    def check(self, name, outcome, reason="", native=None, records=None):
        if outcome not in OUTCOMES:
            raise ValueError("unknown check outcome")
        artifact = self.artifact("check", name, {"native": native, "results": records or []})
        with self.store.transaction() as db:
            db.execute(
                """INSERT INTO tool_checks VALUES(?,?,?,?,?,?) ON CONFLICT(execution_id,name)
                DO UPDATE SET outcome=excluded.outcome,reason=excluded.reason,
                artifact_id=excluded.artifact_id,updated=excluded.updated""",
                (self.id, name, outcome, reason, artifact, time.time()),
            )

    def external(self, scan_id):
        with self.store.transaction() as db:
            db.execute("UPDATE executions SET external_id=? WHERE id=?", (scan_id, self.id))

    def drain(self):
        if not self.path or not self.path.exists():
            return
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    break  # writer was interrupted; a truncated record is not evidence
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
                        raise ValueError("invalid checkpoint envelope")
                    if obj.get("kind") != "plan" and obj.get("outcome") not in OUTCOMES:
                        raise ValueError("invalid checkpoint verdict")
                except (ValueError, TypeError):
                    # A malformed line must not hide earlier/later completed checks. Never
                    # retain its unrestricted bytes: record the location and failure only.
                    self.check(
                        f"checkpoint:{self.offset}", "errored", "malformed structured checkpoint"
                    )
                    self.offset = stream.tell()
                    continue
                if obj.get("kind") == "plan":
                    self.artifact("plan", obj["name"], obj["native"])
                else:
                    records = obj.get("results")
                    if obj.get("tool") == "maigret" and obj["outcome"] == "found":
                        from .tools import _maigret_record

                        native = obj["native"]
                        records = [_maigret_record(obj["name"], native, native.get("username", ""))]
                    self.check(
                        obj["name"],
                        obj["outcome"],
                        obj.get("reason", ""),
                        obj.get("native"),
                        records,
                    )
                self.offset = stream.tell()

    def results(self, tool, target):
        with self.store.connection() as db:
            rows = db.execute(
                """SELECT a.kind,a.name,b.body,b.hash FROM artifacts a
                JOIN blobs b ON b.hash=a.body_hash WHERE a.execution_id=? ORDER BY a.id""",
                (self.id,),
            ).fetchall()
        results, ghunt = {}, {}
        for row in rows:
            if hashlib.sha256(row["body"]).hexdigest() != row["hash"]:
                raise ValueError("artifact hash mismatch")
            obj = json.loads(row["body"])
            if row["kind"] in {"normalized", "check"}:
                for record in obj.get("results", []):
                    key = (
                        record.get("url") or record.get("id") or json.dumps(record, sort_keys=True)
                    )
                    results[key] = record
            if tool == "ghunt" and row["kind"] == "check" and obj.get("native"):
                field = {
                    "profile": "profile",
                    "play_games_detail": "play_games",
                    "maps_stats": "maps",
                    "calendar": "calendar",
                }.get(row["name"])
                if field:
                    ghunt[field] = {"stats": obj["native"]} if field == "maps" else obj["native"]
        if tool == "ghunt" and ghunt.get("profile") and not results:
            from .tools import _ghunt_records

            return _ghunt_records({"PROFILE_CONTAINER": ghunt}, target)
        return list(results.values())

    def preserve_capture(self):
        """Publish completed checkpoint evidence after loss of ownership; never queue acquisition."""
        with self.store.connection() as db:
            row = db.execute(
                """SELECT e.*,t.job_id,t.payload,j.spec FROM executions e
                JOIN tasks t ON t.id=e.task_id JOIN jobs j ON j.id=t.job_id WHERE e.id=?""",
                (self.id,),
            ).fetchone()
        target = json.loads(row["payload"])["target"]
        records = self.results(row["tool"], target)
        if not records:
            return
        output = {
            "tool": row["tool"],
            "target": target,
            "results": records,
            "partial": "interrupted acquisition; completed checkpoints retained",
        }
        body = encode(output)
        hashed = hashlib.sha256(body).hexdigest()
        self.artifact("normalized", "interrupted-results", output)
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM captures WHERE task_id=?", (row["task_id"],)).fetchone():
                return
            db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (hashed, body))
            url = f"tool://{row['tool']}/{target}"
            cid = db.execute(
                """INSERT INTO captures(job_id,task_id,url,final_url,retrieved,status,
                headers,body_hash,changed,extractor,dataset) VALUES(?,?,?,?,?,200,?,?,1,?,?)""",
                (
                    row["job_id"],
                    row["task_id"],
                    url,
                    url,
                    row["started"],
                    json.dumps({"content-type": "application/json"}),
                    hashed,
                    "checkpoint/1",
                    json.loads(row["spec"])["dataset"],
                ),
            ).lastrowid
            self.store.event(
                db,
                row["job_id"],
                "checkpoint_preserved",
                {"capture": cid, "records": len(records), "network_allowed": False},
            )

    def finish(self, outcome, reason=""):
        self.drain()
        self.artifact(
            "execution-state",
            "completion",
            {"execution_completed": outcome == "completed", "reason": reason},
        )
        with self.store.transaction() as db:
            if outcome == "completed":
                bad = db.execute(
                    """SELECT 1 FROM tool_checks WHERE execution_id=?
                    AND outcome NOT IN ('found','absent','skipped') LIMIT 1""",
                    (self.id,),
                ).fetchone()
                if bad:
                    outcome = "partial"
                plans = db.execute(
                    """SELECT b.body FROM artifacts a JOIN blobs b ON b.hash=a.body_hash
                    WHERE a.execution_id=? AND a.kind='plan' ORDER BY a.id DESC LIMIT 1""",
                    (self.id,),
                ).fetchone()
                checked = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM tool_checks WHERE execution_id=?", (self.id,)
                    )
                }
                if plans and set(json.loads(plans[0]).get("sites", [])) - checked:
                    outcome = "partial"
            db.execute(
                "UPDATE executions SET outcome=?,reason=?,finished=? WHERE id=?",
                (outcome, reason, time.time(), self.id),
            )


def recover(store):
    with store.connection() as db:
        rows = db.execute(
            """SELECT e.* FROM executions e JOIN tasks t ON t.id=e.task_id
            WHERE (e.outcome='running' OR (t.status='running' AND t.lease_until<=?)) AND (t.status!='running' OR t.lease_until<=?)""",
            (time.time(), time.time()),
        ).fetchall()
    for row in rows:
        rec = Recorder(store, row["id"], Path(row["checkpoint_path"]))
        rec.finish(
            "interrupted", "worker stopped; completed checkpoints retained; scan not repeated"
        )
        rec.preserve_capture()


def coverage(store, job_id):
    job = store.job(job_id)
    root = job["root_id"] or job_id
    with store.connection() as db:
        rows = db.execute(
            """SELECT t.id AS task_id,t.job_id,t.status AS task_status,t.error,
            e.id,e.tool,t.payload,j.reason AS job_reason,e.settings,e.build,e.started,e.finished,e.outcome,e.reason,e.external_id
            FROM tasks t JOIN jobs j ON j.id=t.job_id LEFT JOIN executions e ON e.task_id=t.id
            WHERE (j.id=? OR j.root_id=?) AND t.kind='tool' ORDER BY t.id""",
            (root, root),
        ).fetchall()
        runs = []
        for row in rows:
            run = dict(row)
            payload = json.loads(run.pop("payload"))
            run["tool"] = run["tool"] or payload.get("tool")
            run["coverage"] = (
                "measured"
                if run["id"] is not None
                else "planned"
                if run["task_status"] in {"pending", "running"}
                else "unknown"
            )
            completion = db.execute(
                """SELECT b.body FROM artifacts a JOIN blobs b ON b.hash=a.body_hash
                WHERE a.execution_id=? AND a.kind='execution-state' ORDER BY a.id DESC LIMIT 1""",
                (run["id"],),
            ).fetchone()
            run["execution_completed"] = (
                json.loads(completion[0]).get("execution_completed") if completion else None
            )
            for field in ("settings", "build"):
                run[field] = json.loads(run[field]) if run[field] else None
            run["checks"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM tool_checks WHERE execution_id=? ORDER BY name", (run["id"],)
                )
            ]
            run["counts"] = check_summary(store, run["id"]) if run["id"] else {}
            run["artifacts"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM artifacts WHERE execution_id=? ORDER BY id", (run["id"],)
                )
            ]
            runs.append(run)
        jobs = [
            dict(r)
            for r in db.execute("SELECT id,status FROM jobs WHERE id=? OR root_id=?", (root, root))
        ]
    statuses = {j["status"] for j in jobs}
    status = (
        "running"
        if statuses & {"queued", "running"}
        else (
            "partial"
            if statuses - {"completed"}
            or any(r["outcome"] in {"partial", "interrupted", "errored", "blocked"} for r in runs)
            else "completed"
        )
    )
    return {
        "version": 1,
        "root_id": root,
        "status": status,
        "jobs": jobs,
        "executions": runs,
        "traffic": "Harvest fetch/search requests are metered; tool traffic and proxy spending are not capped by the dollar budget.",
    }


def build_info():
    path = Path("/app/release-manifest.json")
    if path.is_file():
        return json.loads(path.read_text())
    return {"revision": "development", "tools": "not verified in this environment"}


def check_summary(store, execution_id):
    with store.connection() as db:
        counts = dict(
            db.execute(
                "SELECT outcome,count(*) FROM tool_checks WHERE execution_id=? GROUP BY outcome",
                (execution_id,),
            )
        )
        plans = db.execute(
            """SELECT b.body FROM artifacts a JOIN blobs b ON b.hash=a.body_hash
            WHERE a.execution_id=? AND a.kind='plan' ORDER BY a.id DESC LIMIT 1""",
            (execution_id,),
        ).fetchone()
    if plans:
        planned = json.loads(plans[0]).get("sites", [])
        with store.connection() as db:
            checked = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM tool_checks WHERE execution_id=?", (execution_id,)
                )
            }
        missing = len(set(planned) - checked)
        if missing:
            counts["unfinished"] = counts.get("unfinished", 0) + missing
    return counts
