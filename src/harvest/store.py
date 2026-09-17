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
"""


class Store:
    """Short transactions; each operation owns its connection. No network inside transactions."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise RuntimeError(f"unsupported database version {version}")
            # Rollback journal deliberately avoids old SQLite WAL-reset affected releases.
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript(SCHEMA)
        self._migrate()
        self._migrate_batches()

    def _migrate(self):
        """Schema 1 -> 2, including existing evidence and in-flight reason tasks."""
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] >= 2:
                return
            db.execute("ALTER TABLE jobs ADD COLUMN execution TEXT NOT NULL DEFAULT 'online'")
            db.execute("""CREATE TABLE extractions (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
                job_id TEXT NOT NULL REFERENCES jobs(id), capture_id INTEGER NOT NULL REFERENCES captures(id),
                extractor TEXT, outcome TEXT, created REAL NOT NULL, finished REAL
            )""")
            db.execute("CREATE INDEX extraction_capture ON extractions(capture_id,id)")
            db.execute("CREATE INDEX extraction_job ON extractions(job_id,id)")
            db.execute("""CREATE TABLE assertions (
                extraction_id INTEGER NOT NULL REFERENCES extractions(id),
                observation_id TEXT NOT NULL REFERENCES observations(id),
                PRIMARY KEY(extraction_id,observation_id)
            )""")
            # 0.1 committed retrieval, deterministic extraction and observations together.
            # Model sightings belong to the same analysis revision as their parent capture.
            db.execute("""INSERT INTO extractions(task_id,job_id,capture_id,extractor,outcome,created,finished)
                SELECT c.task_id,c.job_id,c.id,c.extractor,
                  CASE WHEN EXISTS(SELECT 1 FROM events e WHERE e.job_id=c.job_id
                    AND e.type='extraction_limit' AND json_extract(e.details,'$.task')=c.task_id)
                  THEN 'partial' ELSE 'complete' END,c.retrieved,c.retrieved
                FROM captures c JOIN tasks t ON t.id=c.task_id WHERE t.kind='fetch'""")
            db.execute("""INSERT INTO assertions
                SELECT x.id,s.observation_id FROM sightings s
                JOIN extractions x ON x.capture_id=s.capture_id""")
            db.execute("PRAGMA user_version=2")

    def _migrate_batches(self):
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 3:
                return
            db.execute("ALTER TABLE jobs ADD COLUMN records_processed INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE jobs ADD COLUMN claims_processed INTEGER NOT NULL DEFAULT 0")
            for definition in (
                "records_processed INTEGER",
                "records_total INTEGER",
                "batches INTEGER NOT NULL DEFAULT 0",
                "had_warnings INTEGER NOT NULL DEFAULT 0",
                "novel_claims INTEGER NOT NULL DEFAULT 0",
                "batch_format TEXT",
            ):
                db.execute("ALTER TABLE extractions ADD COLUMN " + definition)
            db.execute("""UPDATE jobs SET claims_processed=(SELECT count(*) FROM assertions a
                JOIN extractions x ON x.id=a.extraction_id WHERE x.job_id=jobs.id)""")
            db.execute("PRAGMA user_version=3")

    def replay(self, request, key=None):
        """Create an offline revision, never a new retrieval or a refresh schedule."""
        ids = sorted(set(request.capture_ids))
        with self.transaction() as db:
            captures = []
            for capture_id in ids:
                row = db.execute(
                    """SELECT c.*,t.kind,j.spec FROM captures c
                    JOIN tasks t ON t.id=c.task_id JOIN jobs j ON j.id=c.job_id WHERE c.id=?""",
                    (capture_id,),
                ).fetchone()
                if not row:
                    raise KeyError(capture_id)
                if row["kind"] != "fetch":
                    raise ValueError("replay accepts source captures, not search-service responses")
                captures.append(row)
            if len({c["dataset"] for c in captures}) != 1:
                raise ValueError("replay captures must belong to one dataset")
            if len(ids) > request.limits.tasks:
                raise ValueError("task budget must cover every selected capture")
            spec = JobSpec(
                objective="Offline re-extraction of captured evidence",
                dataset=captures[0]["dataset"],
                fields=JobSpec.model_validate_json(captures[0]["spec"]).fields,
                limits=request.limits,
                investigation=request.investigation,
            )
            fingerprint = digest(packed({"spec": spec.model_dump(), "capture_ids": ids}))
            if key:
                old = db.execute(
                    "SELECT id,spec_hash,spec,execution FROM jobs WHERE idempotency_key=?", (key,)
                ).fetchone()
                if old:
                    previous_ids = [
                        r[0]
                        for r in db.execute(
                            "SELECT DISTINCT capture_id FROM extractions WHERE job_id=? ORDER BY capture_id",
                            (old["id"],),
                        )
                    ]
                    if old["spec_hash"] != fingerprint and (
                        old["execution"] != "offline_replay"
                        or previous_ids != ids
                        or JobSpec.model_validate_json(old["spec"]) != spec
                    ):
                        raise ValueError(
                            "idempotency key already belongs to a different replay specification"
                        )
                    return old["id"]
            initial = [
                {"kind": "extract", "key": str(i), "payload": {"capture_id": i}} for i in ids
            ]
            job = self._create(db, spec, initial, key)
            db.execute(
                "UPDATE jobs SET execution='offline_replay',spec_hash=? WHERE id=?",
                (fingerprint, job),
            )
            self.event(db, job, "replay_created", {"capture_ids": ids, "network_allowed": False})
            return job

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
                    "SELECT id,spec_hash,spec,execution FROM jobs WHERE idempotency_key=?", (key,)
                ).fetchone()
                if old:
                    if old["spec_hash"] != digest(spec_json) and (
                        old["execution"] != "online"
                        or JobSpec.model_validate_json(old["spec"]) != spec
                    ):
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
        if cur.rowcount and task["kind"] == "extract":
            capture_id = task["payload"]["capture_id"]
            cap = db.execute("SELECT dataset FROM captures WHERE id=?", (capture_id,)).fetchone()
            if cap is None or cap["dataset"] != spec.dataset:
                raise ValueError("extraction capture is missing or outside the dataset")
            db.execute(
                """INSERT INTO extractions(task_id,job_id,capture_id,created)
                VALUES(?,?,?,?)""",
                (cur.lastrowid, job, capture_id, time.time()),
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
                """SELECT t.*,j.spec,j.execution FROM tasks t JOIN jobs j ON j.id=t.job_id
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
        self,
        task,
        *,
        response=None,
        extraction=None,
        leads=(),
        details=None,
        capture_id=None,
        batch=None,
    ):
        self.reserve(task)
        with self.transaction() as db:
            job = self.owned(db, task)
            spec = JobSpec.model_validate_json(job["spec"])
            novel = 0
            job_novel = 0
            extraction_id = None
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
                        extraction.extractor
                        if extraction
                        else ("acquisition/1" if task["kind"] == "fetch" else "discovery/1"),
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
                cap = db.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
                if task["kind"] == "reason":
                    revision = db.execute(
                        """SELECT * FROM extractions WHERE job_id=? AND capture_id=?
                        AND (? IS NULL OR id=?) ORDER BY id DESC LIMIT 1""",
                        (
                            job["id"],
                            capture_id,
                            task["payload"].get("extraction_id"),
                            task["payload"].get("extraction_id"),
                        ),
                    ).fetchone()
                else:
                    revision = db.execute(
                        "SELECT * FROM extractions WHERE task_id=?", (task["id"],)
                    ).fetchone()
                if not cap or not revision or revision["capture_id"] != capture_id:
                    raise ValueError("capture is not assigned to this extraction task")
                extraction_id = revision["id"]
                if job["claims_processed"] + len(extraction.claims) > spec.limits.claims:
                    raise BudgetExceeded("claims limit reached; last complete batch retained")
                if batch is not None:
                    if task["kind"] != "extract" or batch.start != (
                        revision["records_processed"] or 0
                    ):
                        raise LostLease("extraction cursor changed")
                    if batch.end < batch.start or (
                        batch.total is not None and batch.end > batch.total
                    ):
                        raise ValueError("invalid extraction progress")
                    if job["records_processed"] + batch.end - batch.start > spec.limits.records:
                        raise BudgetExceeded("records limit reached")
                    if revision["batch_format"] not in (None, extraction.extractor + ":batch/1"):
                        raise ValueError("batch adapter changed; create a new replay")
                    db.execute(
                        """UPDATE extractions SET records_processed=?,records_total=?,batches=batches+1,
                        batch_format=? WHERE id=?""",
                        (batch.end, batch.total, extraction.extractor + ":batch/1", extraction_id),
                    )
                    db.execute(
                        "UPDATE jobs SET records_processed=records_processed+? WHERE id=?",
                        (batch.end - batch.start, job["id"]),
                    )
                db.execute(
                    "UPDATE jobs SET claims_processed=claims_processed+? WHERE id=?",
                    (len(extraction.claims), job["id"]),
                )
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
                    # Scoped to this job's own extraction, unlike `novel` above: an observation
                    # already known globally (e.g. from an earlier job re-extracting the same
                    # evidence) is still a new assertion for this extraction the first time it
                    # links here, and must count as this job's own progress.
                    job_novel += db.execute(
                        "INSERT OR IGNORE INTO assertions VALUES(?,?)", (extraction_id, observation)
                    ).rowcount
                if task["kind"] == "extract":
                    warnings = bool(extraction.warnings) or revision["had_warnings"]
                    complete = batch is None or batch.done
                    db.execute(
                        "UPDATE extractions SET extractor=?,outcome=?,finished=?,had_warnings=?,novel_claims=novel_claims+? WHERE id=?",
                        (
                            extraction.extractor,
                            ("partial" if warnings else "complete") if complete else None,
                            time.time() if complete else None,
                            int(warnings),
                            novel,
                            extraction_id,
                        ),
                    )
                    self.event(
                        db,
                        job["id"],
                        "extracted" if complete else "extraction_batch",
                        {
                            "extraction": extraction_id,
                            "capture": capture_id,
                            "extractor": extraction.extractor,
                            "claims": len(extraction.claims),
                            "records_processed": batch.end if batch else None,
                            "records_total": batch.total if batch else None,
                        },
                    )
            for lead in leads:
                item = {**lead, "parent": task["id"]}
                if item.get("kind") in {"extract", "reason"} and capture_id is not None:
                    reading_pass = item.get("payload", {}).get("pass")
                    item["payload"] = {"capture_id": capture_id}
                    item["key"] = str(capture_id)
                    if item["kind"] == "reason":
                        item["payload"]["extraction_id"] = extraction_id
                    if reading_pass:
                        # A reread is justified only by a novel pass with fields still unresolved.
                        # `job_novel`, not `novel`: whether this job's own extraction learned
                        # something new, not whether the observation is new to the whole
                        # dataset -- an earlier, unrelated job must never stop a fresh job's
                        # reread just because it happened to see the same evidence first.
                        unresolved = [
                            f for f in spec.fields if f not in self._fields(db, job["id"])
                        ]
                        if not job_novel or not unresolved:
                            continue
                        item["key"] = f"{capture_id}:pass:{reading_pass}"
                        item["payload"]["pass"] = reading_pass
                        item["payload"]["unresolved_fields"] = unresolved
                self.enqueue(db, job["id"], item, spec)
            if batch is not None and not batch.done:
                # Keep the same lease and parser iterator. A crash reclaims this task at its cursor.
                return capture_id
            db.execute(
                "UPDATE tasks SET status='done',token=NULL,lease_until=NULL,error=NULL WHERE id=?",
                (task["id"],),
            )
            if task["kind"] == "extract":
                if batch is not None:
                    novel += revision["novel_claims"]
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
            result["evidence_captures"] = db.execute(
                """SELECT count(*) FROM (
                SELECT id FROM captures WHERE job_id=? UNION SELECT capture_id FROM extractions WHERE job_id=?)""",
                (job_id, job_id),
            ).fetchone()[0]
            result["extraction_progress"] = dict(
                db.execute(
                    """SELECT
                CASE WHEN t.status='done' THEN x.outcome ELSE t.status END state,count(*)
                FROM extractions x JOIN tasks t ON t.id=x.task_id WHERE x.job_id=? GROUP BY state""",
                    (job_id,),
                ).fetchall()
            )
            result["cost_reserved_usd"] = result["cost_microusd"] / 1_000_000
            fields = self._fields(db, job_id)
            result["missing_fields"] = [f for f in result["spec"]["fields"] if f not in fields]
            result["coverage"] = (
                "unmeasured; completion describes work execution, not population completeness"
            )
            return result

    @staticmethod
    def _fields(db, job_id):
        """Job-wide persisted field set; completeness is per job, not per entity or source."""
        return {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT o.field FROM observations o JOIN assertions a ON a.observation_id=o.id JOIN extractions x ON x.id=a.extraction_id WHERE x.job_id=?",
                (job_id,),
            )
        }

    def shown_spans(self, job_id, capture_id, *, exclude_task_id):
        """Adapter-text ranges already sent to the model for this capture, from persisted audits.

        Omits records from exclude_task_id: a retry of that same durable task must see the
        spans its own earlier (possibly failed) attempts already reserved, not treat them as
        already shown, so it reproduces the identical prompt/selection on every attempt.
        exclude_task_id is required (not defaulted) so callers cannot silently omit it and
        reintroduce the retry-selection-drift bug this guards against.
        """
        spans = []
        with self.connection() as db:
            rows = db.execute(
                "SELECT details FROM events WHERE job_id=? AND type='model_reserved' ORDER BY id",
                (job_id,),
            ).fetchall()
        for row in rows:
            details = json.loads(row[0])
            if details.get("capture_id") == capture_id and details.get("task") != exclude_task_id:
                spans.extend((s["start"], s["end"]) for s in details["selection"]["spans"])
        return spans

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

    def captures(self, job_id, after=0, limit=100):
        self.job(job_id)
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    """SELECT c.* FROM captures c WHERE c.id>?
                AND (c.job_id=? OR EXISTS(SELECT 1 FROM extractions x WHERE x.capture_id=c.id AND x.job_id=?))
                ORDER BY c.id LIMIT ?""",
                    (after, job_id, job_id, limit),
                )
            ]

    def extractions(self, job_id, after=0, limit=100):
        self.job(job_id)
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    """SELECT x.*,t.error,
                CASE WHEN x.records_total IS NOT NULL THEN x.records_total-coalesce(x.records_processed,0) END records_remaining,
                CASE WHEN t.status='done' THEN x.outcome ELSE t.status END status,
                (SELECT count(*) FROM assertions a WHERE a.extraction_id=x.id) observations
                FROM extractions x JOIN tasks t ON t.id=x.task_id
                WHERE x.job_id=? AND x.id>? ORDER BY x.id LIMIT ?""",
                    (job_id, after, limit),
                )
            ]

    def extraction_task(self, task):
        with self.connection() as db:
            row = db.execute("SELECT * FROM extractions WHERE task_id=?", (task["id"],)).fetchone()
            if row is None:
                raise ValueError("missing extraction revision")
            return dict(row)

    def observations(self, job_id, after="", limit=100):
        self.job(job_id)
        with self.connection() as db:
            rows = db.execute(
                """SELECT o.*,e.entity_key,max(c.retrieved) last_seen,
                  group_concat(DISTINCT c.id) capture_ids,group_concat(DISTINCT x.id) extraction_ids FROM observations o
                  JOIN entities e ON e.id=o.entity_id JOIN assertions a ON a.observation_id=o.id
                  JOIN extractions x ON x.id=a.extraction_id
                  JOIN captures c ON c.id=x.capture_id WHERE x.job_id=? AND o.id>?
                  GROUP BY o.id ORDER BY o.id LIMIT ?""",
                (job_id, after, limit),
            ).fetchall()
            return [
                {
                    **dict(r),
                    "value": json.loads(r["value"]),
                    "capture_ids": [int(x) for x in r["capture_ids"].split(",")],
                    "extraction_ids": [int(x) for x in r["extraction_ids"].split(",")],
                }
                for r in rows
            ]

    def canonical(self, dataset: str, after="", limit=100):
        """Latest usable analysis per source, with newer unsuccessful attempts disclosed."""
        with self.connection() as db:
            entities = db.execute(
                "SELECT * FROM entities WHERE dataset=? AND id>? ORDER BY id LIMIT ?",
                (dataset, after, limit),
            ).fetchall()
            results = []
            for entity in entities:
                rows = db.execute(
                    """SELECT DISTINCT o.*,c.retrieved,c.id capture_id,x.id extraction_id FROM observations o
                    JOIN assertions a ON a.observation_id=o.id JOIN extractions x ON x.id=a.extraction_id
                    JOIN captures c ON c.id=x.capture_id
                    WHERE o.entity_id=? AND x.id=(SELECT x2.id FROM extractions x2
                      JOIN captures c2 ON c2.id=x2.capture_id
                      WHERE c2.dataset=c.dataset AND c2.url=c.url AND x2.outcome IN ('complete','partial')
                      ORDER BY c2.retrieved DESC,c2.id DESC,x2.id DESC LIMIT 1) ORDER BY o.field,c.retrieved DESC""",
                    (entity["id"],),
                ).fetchall()
                fields = {}
                source_states = {}
                for r in rows:
                    if r["source_url"] not in source_states:
                        latest = db.execute(
                            """SELECT c.id capture_id,c.retrieved,x.id extraction_id,
                            CASE WHEN t.status='done' THEN x.outcome ELSE coalesce(t.status,'not_scheduled') END status
                            FROM captures c LEFT JOIN extractions x ON x.capture_id=c.id
                            LEFT JOIN tasks t ON t.id=x.task_id WHERE c.dataset=? AND c.url=?
                            ORDER BY c.retrieved DESC,c.id DESC,x.id DESC LIMIT 1""",
                            (dataset, r["source_url"]),
                        ).fetchone()
                        source_states[r["source_url"]] = {
                            "source_url": r["source_url"],
                            **dict(latest),
                            "stale": latest["capture_id"] != r["capture_id"]
                            or latest["extraction_id"] != r["extraction_id"],
                        }
                    field = fields.setdefault(
                        r["field"], {"value": None, "conflict": False, "candidates": []}
                    )
                    field["candidates"].append(
                        {
                            "value": json.loads(r["value"]),
                            "observation_id": r["id"],
                            "source_url": r["source_url"],
                            "capture_id": r["capture_id"],
                            "extraction_id": r["extraction_id"],
                            "stale": source_states[r["source_url"]]["stale"],
                            "retrieved": r["retrieved"],
                            "extraction_confidence": r["confidence"],
                        }
                    )
                for field in fields.values():
                    unique = {packed(x["value"]) for x in field["candidates"]}
                    field["conflict"] = len(unique) > 1
                    field["value"] = field["candidates"][0]["value"] if len(unique) == 1 else None
                results.append(
                    {
                        **dict(entity),
                        "fields": fields,
                        "source_states": list(source_states.values()),
                    }
                )
            return results

    def job_records(self, job_id):
        """Job-scoped per-entity fields, mirroring `canonical`'s value/conflict rules.

        Unlike `canonical`, which is dataset-wide and selects each source's latest usable
        extraction, this uses only this job's own assertion-scoped observations (like
        `dossier`), so a later job or replay can never change an existing job's record view.
        Every field the job's spec requested is backfilled onto every entity, even one with
        zero observations for it anywhere in the job: `missing` distinguishes "never
        observed" from a genuine `conflict` (observed, but disagreeing), so a caller never
        has to infer absence from a dict key that just isn't there.
        """
        job = self.job(job_id)
        requested_fields = job["spec"]["fields"]
        entities: dict[str, dict] = {}
        after = ""
        while rows := self.observations(job_id, after, 2000):
            for r in rows:
                entity = entities.setdefault(
                    r["entity_id"],
                    {"entity_id": r["entity_id"], "entity_key": r["entity_key"], "fields": {}},
                )
                field = entity["fields"].setdefault(
                    r["field"],
                    {"value": None, "conflict": False, "missing": False, "candidates": []},
                )
                field["candidates"].append(
                    {
                        "value": r["value"],
                        "observation_id": r["id"],
                        "source_url": r["source_url"],
                        "evidence": r["evidence"],
                        "locator": r["locator"],
                        "method": r["method"],
                        "extractor": r["extractor"],
                        "confidence": r["confidence"],
                        "capture_ids": r["capture_ids"],
                        "extraction_ids": r["extraction_ids"],
                        "last_seen": r["last_seen"],
                    }
                )
            after = rows[-1]["id"]
        results = []
        for entity in entities.values():
            for field in entity["fields"].values():
                unique = {packed(c["value"]) for c in field["candidates"]}
                field["conflict"] = len(unique) > 1
                field["value"] = field["candidates"][0]["value"] if len(unique) == 1 else None
            for name in requested_fields:
                entity["fields"].setdefault(
                    name, {"value": None, "conflict": False, "missing": True, "candidates": []}
                )
            results.append(entity)
        return sorted(results, key=lambda e: e["entity_id"])

    def dossier(self, job_id):
        """Job-scoped investigation view: exact-identifier reconciliation, never truth scoring.

        Built entirely from this job's own assertion-scoped observations/captures/extractions
        (`self.observations`/`self.captures`/`self.extractions`, already job-filtered), so a
        later job, refresh or replay can never change an existing job's dossier or lend it
        foreign identity evidence. Pages exhaustively: a job's own evidence is already bounded
        by its own limits, so looping to completion here is not an unbounded scan.
        """
        from .dossier import reconcile

        job = self.job(job_id)
        spec = JobSpec.model_validate(job["spec"])
        if spec.investigation is None:
            raise ValueError("job has no investigation; submit a job with an investigation spec")
        observations = []
        after = ""
        while rows := self.observations(job_id, after, 2000):
            observations.extend(rows)
            after = rows[-1]["id"]
        captures = []
        after_id = 0
        while rows := self.captures(job_id, after_id, 2000):
            captures.extend(rows)
            after_id = rows[-1]["id"]
        extractions = []
        after_id = 0
        while rows := self.extractions(job_id, after_id, 2000):
            extractions.extend(rows)
            after_id = rows[-1]["id"]
        result = reconcile(spec.investigation, observations)
        result["job_id"] = job_id
        result["dataset"] = spec.dataset
        result["sources"] = self._source_states(spec.investigation, captures, extractions)
        return result

    @staticmethod
    def _source_states(investigation, captures, extractions):
        """Expose every declared source, including ones never acquired in this job."""
        by_url = {}
        for cap in captures:
            current = by_url.get(cap["url"])
            if current is None or (cap["retrieved"], cap["id"]) > (
                current["retrieved"],
                current["id"],
            ):
                by_url[cap["url"]] = cap
        extraction_by_capture = {}
        for extraction in extractions:
            current = extraction_by_capture.get(extraction["capture_id"])
            if current is None or extraction["id"] > current["id"]:
                extraction_by_capture[extraction["capture_id"]] = extraction
        states = []
        for rule in investigation.sources:
            cap = by_url.get(rule.url)
            if cap is None:
                states.append(
                    {
                        "url": rule.url,
                        "acquired": False,
                        "final_url": None,
                        "retrieved": None,
                        "http_status": None,
                        "extraction_state": "not_acquired",
                        "warnings": False,
                        "capture_id": None,
                        "extraction_id": None,
                    }
                )
                continue
            extraction = extraction_by_capture.get(cap["id"])
            states.append(
                {
                    "url": rule.url,
                    "acquired": True,
                    "final_url": cap["final_url"],
                    "retrieved": cap["retrieved"],
                    "http_status": cap["status"],
                    "extraction_state": extraction["status"] if extraction else "not_scheduled",
                    "warnings": bool(extraction["had_warnings"]) if extraction else False,
                    "capture_id": cap["id"],
                    "extraction_id": extraction["id"] if extraction else None,
                }
            )
        return states

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
