from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import BudgetExceeded, JobSpec, LostLease


def packed(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, spec TEXT NOT NULL, spec_hash TEXT NOT NULL, idempotency_key TEXT UNIQUE,
 status TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL, started REAL, finished REAL,
 reason TEXT, requests INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
 model_calls INTEGER NOT NULL DEFAULT 0, model_tokens INTEGER NOT NULL DEFAULT 0,
 cost_microusd INTEGER NOT NULL DEFAULT 0, no_gain INTEGER NOT NULL DEFAULT 0,
 parent_id TEXT REFERENCES jobs(id), schedule_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), kind TEXT NOT NULL,
 key TEXT NOT NULL, payload TEXT NOT NULL, depth INTEGER NOT NULL, priority INTEGER NOT NULL,
 parent INTEGER REFERENCES tasks(id), reason TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 ready REAL NOT NULL DEFAULT 0, lease_until REAL, token TEXT, error TEXT,
 UNIQUE(job_id,kind,key)
);
CREATE INDEX IF NOT EXISTS task_ready ON tasks(status,ready,priority);
CREATE INDEX IF NOT EXISTS task_job ON tasks(job_id,status);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), at REAL NOT NULL,
 type TEXT NOT NULL, details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_job ON events(job_id,id);
CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS captures (
 id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
 task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id), url TEXT NOT NULL, final_url TEXT NOT NULL,
 retrieved REAL NOT NULL, status INTEGER NOT NULL, headers TEXT NOT NULL,
 body_hash TEXT NOT NULL REFERENCES blobs(hash), previous_id INTEGER REFERENCES captures(id),
 changed INTEGER NOT NULL, extractor TEXT NOT NULL, dataset TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS capture_source ON captures(dataset,url,id);
CREATE TABLE IF NOT EXISTS entities (
 id TEXT PRIMARY KEY, dataset TEXT NOT NULL, entity_key TEXT NOT NULL,
 UNIQUE(dataset,entity_key)
);
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id), field TEXT NOT NULL,
 value TEXT NOT NULL, source_url TEXT NOT NULL, evidence TEXT NOT NULL, locator TEXT NOT NULL,
 method TEXT NOT NULL, extractor TEXT NOT NULL, confidence REAL NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sightings (
 observation_id TEXT NOT NULL REFERENCES observations(id), capture_id INTEGER NOT NULL REFERENCES captures(id),
 PRIMARY KEY(observation_id,capture_id)
);
CREATE INDEX IF NOT EXISTS sighting_capture ON sightings(capture_id);
CREATE TABLE IF NOT EXISTS origin_state (origin TEXT PRIMARY KEY, next_request REAL NOT NULL);
CREATE TABLE IF NOT EXISTS robots (
 origin TEXT PRIMARY KEY, text TEXT NOT NULL, expires REAL NOT NULL, status INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
 id TEXT PRIMARY KEY, spec TEXT NOT NULL, interval INTEGER NOT NULL, next_run REAL NOT NULL,
 last_job TEXT REFERENCES jobs(id), enabled INTEGER NOT NULL DEFAULT 1
);
PRAGMA user_version=1;
"""


class Store:
    """Short transactions; each operation owns its connection. No network inside transactions."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"unsupported database version {version}")
            # Rollback journal deliberately avoids old SQLite WAL-reset affected releases.
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def event(db, job: str, kind: str, details):
        db.execute(
            "INSERT INTO events(job_id,at,type,details) VALUES(?,?,?,?)",
            (job, time.time(), kind, packed(details)),
        )

    def create(
        self, spec: JobSpec, initial: list[dict], key: str | None = None, parent: str | None = None
    ) -> str:
        spec_json = packed(spec.model_dump())
        with self.transaction() as db:
            if key:
                old = db.execute(
                    "SELECT id,spec_hash FROM jobs WHERE idempotency_key=?", (key,)
                ).fetchone()
                if old:
                    if old["spec_hash"] != digest(spec_json):
                        raise ValueError(
                            "idempotency key already belongs to a different job specification"
                        )
                    return old["id"]
            job = self._create(db, spec, initial, key, parent)
            if spec.mode == "continuous":
                db.execute(
                    "INSERT INTO schedules VALUES(?,?,?,?,?,1)",
                    (job, spec_json, spec.refresh_seconds, time.time() + spec.refresh_seconds, job),
                )
            return job

    def _create(self, db, spec, initial, key=None, parent=None, schedule_key=None):
        job = uuid.uuid4().hex
        data = packed(spec.model_dump())
        db.execute(
            "INSERT INTO jobs(id,spec,spec_hash,idempotency_key,created,parent_id,schedule_key) VALUES(?,?,?,?,?,?,?)",
            (job, data, digest(data), key, time.time(), parent, schedule_key),
        )
        for task in initial:
            self.enqueue(db, job, task, spec)
        self.event(db, job, "created", {"objective": spec.objective, "mode": spec.mode})
        return job

    def enqueue(self, db, job, task, spec: JobSpec):
        if task.get("depth", 0) > spec.limits.depth:
            return False
        if (
            db.execute("SELECT count(*) FROM tasks WHERE job_id=?", (job,)).fetchone()[0]
            >= spec.limits.tasks
        ):
            self.event(db, job, "frontier_limit", {"limit": spec.limits.tasks})
            return False
        cur = db.execute(
            """INSERT OR IGNORE INTO tasks(job_id,kind,key,payload,depth,priority,parent,reason)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                job,
                task["kind"],
                task["key"],
                packed(task["payload"]),
                task.get("depth", 0),
                task.get("priority", 0),
                task.get("parent"),
                task.get("reason", "seed"),
            ),
        )
        return bool(cur.rowcount)

    @staticmethod
    def owned(db, task):
        row = db.execute(
            """SELECT j.* FROM jobs j JOIN tasks t ON t.job_id=j.id
             WHERE t.id=? AND t.token=? AND t.status='running' AND t.lease_until>?
             AND j.status='running'""",
            (task["id"], task["token"], time.time()),
        ).fetchone()
        if not row:
            raise LostLease("job cancelled or task lease lost")
        return row

    def claim(self, job_id: str | None = None, lease_seconds: float = 120):
        now = time.time()
        with self.transaction() as db:
            expired = db.execute(
                """SELECT t.*,j.spec FROM tasks t JOIN jobs j ON j.id=t.job_id
                WHERE t.status='running' AND t.lease_until<=? AND j.status='running'""",
                (now,),
            ).fetchall()
            for task in expired:
                limit = JobSpec.model_validate_json(task["spec"]).limits.attempts
                status = "failed" if task["attempts"] >= limit else "pending"
                db.execute(
                    "UPDATE tasks SET status=?,token=NULL,error='worker lease expired' WHERE id=?",
                    (status, task["id"]),
                )
                self.event(
                    db, task["job_id"], "lease_expired", {"task": task["id"], "status": status}
                )
            row = db.execute(
                """SELECT t.*,j.spec FROM tasks t JOIN jobs j ON j.id=t.job_id
                WHERE t.status='pending' AND t.ready<=? AND j.status IN ('queued','running')
                AND (? IS NULL OR j.id=?)
                AND NOT EXISTS(SELECT 1 FROM tasks r WHERE r.job_id=j.id AND r.status='running')
                ORDER BY t.priority DESC,t.id LIMIT 1""",
                (now, job_id, job_id),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE tasks SET status='running',attempts=attempts+1,token=?,lease_until=? WHERE id=?",
                (token, now + lease_seconds, row["id"]),
            )
            db.execute(
                "UPDATE jobs SET status='running',started=coalesce(started,?) WHERE id=?",
                (now, row["job_id"]),
            )
            result = dict(row)
            result.update(
                token=token, attempts=row["attempts"] + 1, payload=json.loads(row["payload"])
            )
            return result

    def heartbeat(self, task, lease_seconds=120):
        with self.transaction() as db:
            self.owned(db, task)
            db.execute(
                "UPDATE tasks SET lease_until=? WHERE id=? AND token=?",
                (time.time() + lease_seconds, task["id"], task["token"]),
            )

    def reserve(self, task, *, requests=0, byte_count=0, model_calls=0, model_tokens=0, cost=0):
        with self.transaction() as db:
            job = self.owned(db, task)
            limits = JobSpec.model_validate_json(job["spec"]).limits
            values = {
                "requests": requests,
                "bytes": byte_count,
                "model_calls": model_calls,
                "model_tokens": model_tokens,
                "cost_microusd": cost,
            }
            maxima = {
                "requests": limits.requests,
                "bytes": limits.bytes,
                "model_calls": limits.model_calls,
                "model_tokens": limits.model_tokens,
                "cost_microusd": int(limits.cost_usd * 1_000_000),
            }
            if time.time() - job["started"] >= limits.seconds:
                raise BudgetExceeded("wall-clock deadline reached")
            for field, delta in values.items():
                if delta < 0:
                    raise ValueError("negative reservation")
                if job[field] + delta > maxima[field]:
                    raise BudgetExceeded(f"{field} limit reached")
            db.execute(
                """UPDATE jobs SET requests=requests+?,bytes=bytes+?,model_calls=model_calls+?,
                model_tokens=model_tokens+?,cost_microusd=cost_microusd+? WHERE id=?""",
                (requests, byte_count, model_calls, model_tokens, cost, job["id"]),
            )

    def delay_origin(self, origin: str, delay: float) -> float:
        with self.transaction() as db:
            row = db.execute(
                "SELECT next_request FROM origin_state WHERE origin=?", (origin,)
            ).fetchone()
            now = time.time()
            wait = max(0, row[0] - now) if row else 0
            if wait == 0:
                db.execute(
                    "INSERT INTO origin_state VALUES(?,?) ON CONFLICT(origin) DO UPDATE SET next_request=excluded.next_request",
                    (origin, now + delay),
                )
            return wait

    def defer(self, task, error, delay: float, *, failure=True):
        with self.transaction() as db:
            self.owned(db, task)
            limit = JobSpec.model_validate_json(task["spec"]).limits.attempts
            failed = failure and task["attempts"] >= limit
            db.execute(
                "UPDATE tasks SET status=?,ready=?,token=NULL,lease_until=NULL,error=?,attempts=attempts-? WHERE id=?",
                (
                    "failed" if failed else "pending",
                    time.time() + delay,
                    str(error)[:1000],
                    0 if failure else 1,
                    task["id"],
                ),
            )
            self.event(
                db,
                task["job_id"],
                "failed" if failed else "deferred",
                {"task": task["id"], "reason": str(error)[:1000], "delay": delay},
            )

    def fail(self, task, error, status="failed"):
        with self.transaction() as db:
            self.owned(db, task)
            db.execute(
                "UPDATE tasks SET status=?,error=?,token=NULL WHERE id=?",
                (status, str(error)[:1000], task["id"]),
            )
            self.event(
                db, task["job_id"], status, {"task": task["id"], "reason": str(error)[:1000]}
            )

    def finish(
        self, task, *, response=None, extraction=None, leads=(), details=None, capture_id=None
    ):
        self.reserve(task)
        with self.transaction() as db:
            job = self.owned(db, task)
            spec = JobSpec.model_validate_json(job["spec"])
            novel = 0
            changed = False
            if response is not None:
                body_hash = digest(response.body)
                old = db.execute(
                    "SELECT id,body_hash FROM captures WHERE dataset=? AND url=? ORDER BY id DESC LIMIT 1",
                    (spec.dataset, response.url),
                ).fetchone()
                changed = old is None or old["body_hash"] != body_hash
                db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (body_hash, response.body))
                capture_id = db.execute(
                    """INSERT INTO captures(job_id,task_id,url,final_url,retrieved,status,headers,
                   body_hash,previous_id,changed,extractor,dataset) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job["id"],
                        task["id"],
                        response.url,
                        response.final_url,
                        response.retrieved,
                        response.status,
                        packed(response.headers),
                        body_hash,
                        old["id"] if old else None,
                        int(changed),
                        extraction.extractor if extraction else "discovery/1",
                        spec.dataset,
                    ),
                ).lastrowid
                self.event(
                    db,
                    job["id"],
                    "capture",
                    {"capture": capture_id, "changed": changed, "sha256": body_hash},
                )
            if extraction and capture_id is not None:
                for warning in extraction.warnings:
                    self.event(
                        db, job["id"], "extraction_limit", {"task": task["id"], "reason": warning}
                    )
                cap = db.execute(
                    "SELECT * FROM captures WHERE id=? AND job_id=?", (capture_id, job["id"])
                ).fetchone()
                if not cap:
                    raise ValueError("capture is not part of job")
                for claim in extraction.claims:
                    entity = digest(packed([spec.dataset, claim.entity_key]))
                    db.execute(
                        "INSERT OR IGNORE INTO entities VALUES(?,?,?)",
                        (entity, spec.dataset, claim.entity_key),
                    )
                    observation = digest(
                        packed(
                            [
                                entity,
                                claim.field,
                                claim.value,
                                cap["url"],
                                claim.evidence,
                                claim.locator,
                                claim.method,
                                extraction.extractor,
                                claim.confidence,
                            ]
                        )
                    )
                    novel += db.execute(
                        "INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            observation,
                            entity,
                            claim.field,
                            packed(claim.value),
                            cap["url"],
                            claim.evidence,
                            claim.locator,
                            claim.method,
                            extraction.extractor,
                            claim.confidence,
                            time.time(),
                        ),
                    ).rowcount
                    db.execute(
                        "INSERT OR IGNORE INTO sightings VALUES(?,?)", (observation, capture_id)
                    )
            for lead in leads:
                item = {**lead, "parent": task["id"]}
                if item.get("kind") == "reason" and capture_id is not None:
                    item["payload"] = {"capture_id": capture_id}
                    item["key"] = str(capture_id)
                self.enqueue(db, job["id"], item, spec)
            db.execute(
                "UPDATE tasks SET status='done',token=NULL,lease_until=NULL,error=NULL WHERE id=?",
                (task["id"],),
            )
            if task["kind"] == "fetch":
                db.execute(
                    "UPDATE jobs SET no_gain=CASE WHEN ? > 0 THEN 0 ELSE no_gain+1 END WHERE id=?",
                    (novel, job["id"]),
                )
            elif novel:
                db.execute("UPDATE jobs SET no_gain=0 WHERE id=?", (job["id"],))
            self.event(
                db,
                job["id"],
                "task_done",
                {"task": task["id"], "novel_observations": novel, **(details or {})},
            )
            return capture_id

    def stop(self, job_id, status="cancelled", reason="operator requested cancellation"):
        if status not in {"cancelled", "budget_exhausted", "plateau"}:
            raise ValueError("invalid stop status")
        with self.transaction() as db:
            old = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not old:
                raise KeyError(job_id)
            if old[0] not in {"queued", "running"}:
                return
            db.execute(
                "UPDATE jobs SET status=?,finished=?,reason=? WHERE id=?",
                (status, time.time(), reason, job_id),
            )
            db.execute(
                "UPDATE tasks SET status='cancelled',token=NULL WHERE job_id=? AND status IN ('pending','running')",
                (job_id,),
            )
            self.event(db, job_id, status, {"reason": reason})

    def settle(self, job_id: str | None = None):
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running') AND (? IS NULL OR id=?)",
                (job_id, job_id),
            ).fetchall()
            for row in rows:
                counts = dict(
                    db.execute(
                        "SELECT status,count(*) FROM tasks WHERE job_id=? GROUP BY status",
                        (row["id"],),
                    ).fetchall()
                )
                if counts.get("pending", 0) + counts.get("running", 0):
                    continue
                status = (
                    "partial"
                    if counts.get("failed", 0) or counts.get("blocked", 0)
                    else "completed"
                )
                limited = db.execute(
                    "SELECT 1 FROM events WHERE job_id=? AND type IN ('extraction_limit','frontier_limit') LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if limited:
                    status = "partial"
                if not counts.get("done", 0):
                    status = "failed"
                db.execute(
                    "UPDATE jobs SET status=?,finished=?,reason='frontier exhausted' WHERE id=?",
                    (status, time.time(), row["id"]),
                )
                self.event(db, row["id"], "finished", {"status": status, "tasks": counts})

    def expire_deadlines(self, job_id=None):
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,spec,started FROM jobs WHERE status='running' AND (? IS NULL OR id=?)",
                (job_id, job_id),
            ).fetchall()
        for row in rows:
            limit = JobSpec.model_validate_json(row["spec"]).limits.seconds
            if row["started"] is not None and time.time() - row["started"] >= limit:
                self.stop(row["id"], "budget_exhausted", "wall-clock deadline reached")

    def job(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            result = dict(row)
            result["spec"] = json.loads(row["spec"])
            result["progress"] = dict(
                db.execute(
                    "SELECT status,count(*) FROM tasks WHERE job_id=? GROUP BY status", (job_id,)
                ).fetchall()
            )
            result["captures"] = db.execute(
                "SELECT count(*) FROM captures WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            result["cost_reserved_usd"] = result["cost_microusd"] / 1_000_000
            fields = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT o.field FROM observations o JOIN sightings s ON s.observation_id=o.id JOIN captures c ON c.id=s.capture_id WHERE c.job_id=?",
                    (job_id,),
                )
            }
            result["missing_fields"] = [f for f in result["spec"]["fields"] if f not in fields]
            result["coverage"] = (
                "unmeasured; completion describes work execution, not population completeness"
            )
            return result

    def jobs(self, limit=100):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,status,created,reason FROM jobs ORDER BY created DESC LIMIT ?",
                    (limit,),
                )
            ]

    def events(self, job_id, after=0, limit=100):
        self.job(job_id)
        with self.connection() as db:
            return [
                {**dict(r), "details": json.loads(r["details"])}
                for r in db.execute(
                    "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                    (job_id, after, limit),
                )
            ]

    def capture(self, capture_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT c.*,b.body FROM captures c JOIN blobs b ON b.hash=c.body_hash WHERE c.id=?",
                (capture_id,),
            ).fetchone()
            if not row:
                raise KeyError(capture_id)
            return dict(row)

    def observations(self, job_id, after="", limit=100):
        self.job(job_id)
        with self.connection() as db:
            rows = db.execute(
                """SELECT o.*,e.entity_key,max(c.retrieved) last_seen,
                  group_concat(DISTINCT c.id) capture_ids FROM observations o
                  JOIN entities e ON e.id=o.entity_id JOIN sightings s ON s.observation_id=o.id
                  JOIN captures c ON c.id=s.capture_id WHERE c.job_id=? AND o.id>?
                  GROUP BY o.id ORDER BY o.id LIMIT ?""",
                (job_id, after, limit),
            ).fetchall()
            return [
                {
                    **dict(r),
                    "value": json.loads(r["value"]),
                    "capture_ids": [int(x) for x in r["capture_ids"].split(",")],
                }
                for r in rows
            ]

    def canonical(self, dataset: str, after="", limit=100):
        """Latest source captures; disagreement is returned rather than overwritten."""
        with self.connection() as db:
            entities = db.execute(
                "SELECT * FROM entities WHERE dataset=? AND id>? ORDER BY id LIMIT ?",
                (dataset, after, limit),
            ).fetchall()
            results = []
            for entity in entities:
                rows = db.execute(
                    """SELECT DISTINCT o.*,c.retrieved,c.id capture_id FROM observations o
                    JOIN sightings s ON s.observation_id=o.id JOIN captures c ON c.id=s.capture_id
                    WHERE o.entity_id=? AND c.id=(SELECT c2.id FROM captures c2
                      WHERE c2.dataset=c.dataset AND c2.url=c.url ORDER BY c2.retrieved DESC,c2.id DESC LIMIT 1) ORDER BY o.field,c.retrieved DESC""",
                    (entity["id"],),
                ).fetchall()
                fields = {}
                for r in rows:
                    field = fields.setdefault(
                        r["field"], {"value": None, "conflict": False, "candidates": []}
                    )
                    field["candidates"].append(
                        {
                            "value": json.loads(r["value"]),
                            "observation_id": r["id"],
                            "source_url": r["source_url"],
                            "capture_id": r["capture_id"],
                            "retrieved": r["retrieved"],
                            "extraction_confidence": r["confidence"],
                        }
                    )
                for field in fields.values():
                    unique = {packed(x["value"]) for x in field["candidates"]}
                    field["conflict"] = len(unique) > 1
                    field["value"] = field["candidates"][0]["value"] if len(unique) == 1 else None
                results.append({**dict(entity), "fields": fields})
            return results

    def schedule_tick(self):
        with self.transaction() as db:
            now = time.time()
            due = db.execute(
                "SELECT s.* FROM schedules s JOIN jobs j ON j.id=s.last_job WHERE s.enabled=1 AND s.next_run<=? AND j.status NOT IN ('queued','running')",
                (now,),
            ).fetchall()
            for schedule in due:
                spec = JobSpec.model_validate_json(schedule["spec"])
                previous = db.execute(
                    "SELECT kind,key,payload FROM tasks WHERE job_id=? AND parent IS NULL",
                    (schedule["last_job"],),
                ).fetchall()
                initial = [
                    {"kind": r["kind"], "key": r["key"], "payload": json.loads(r["payload"])}
                    for r in previous
                ]
                key = f"{schedule['id']}:{schedule['next_run']}"
                job = self._create(db, spec, initial, parent=schedule["last_job"], schedule_key=key)
                db.execute(
                    "UPDATE schedules SET last_job=?,next_run=? WHERE id=?",
                    (job, now + schedule["interval"], schedule["id"]),
                )

    def disable_schedule(self, schedule_id):
        with self.transaction() as db:
            if not db.execute("UPDATE schedules SET enabled=0 WHERE id=?", (schedule_id,)).rowcount:
                raise KeyError(schedule_id)

    def backup(self, destination):
        if Path(destination).resolve() == Path(self.path).resolve():
            raise ValueError("backup destination must differ from live database")
        with self.connection() as db, sqlite3.connect(destination) as target:
            db.backup(target)
