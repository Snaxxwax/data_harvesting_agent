from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .models import FIELD_ALIASES, BudgetExceeded, JobSpec, LostLease, canonical_url


def packed(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


# Which limit to raise for each stop reason. The old UI said "raise the request budget" for
# every budget stop, including a wall-clock stop at 26/100 requests.
_STOP_ADVICE = (
    ("wall-clock", "the time budget ran out: raise limits.seconds (requests were not the limit)"),
    ("tool_runs", "every allowed tool run was used: raise limits.tool_runs"),
    ("requests", "the request budget ran out: raise limits.requests"),
    ("bytes", "the download budget ran out: raise limits.bytes"),
    ("task", "the task budget ran out: raise limits.tasks or lower limits.depth"),
    ("records", "the record budget ran out: raise limits.records"),
    ("claims", "the claim budget ran out: raise limits.claims"),
    ("model", "the model budget ran out: raise limits.model_calls/model_tokens"),
    ("cost", "the spend budget ran out: raise limits.cost_usd"),
)


def stop_advice(status, reason):
    if status == "plateau":
        return "stopped because recent pages added nothing new; results are what was found"
    if status == "partial":
        if "time allowance" in (reason or ""):
            return "a tool scan ran out of time: raise limits.seconds (or HARVEST_TOOL_TIMEOUT)"
        return "some sources failed, were blocked or hit an extraction limit; see Warnings"
    if status == "failed":
        if "lease expired" in (reason or ""):
            return "the worker stopped mid-task (restart or crash): rerun the job"
        return "no task succeeded; the reason names the first failure (see Warnings)"
    if status == "cancelled":
        return "cancelled; evidence collected before the cancellation is kept"
    if status != "budget_exhausted":
        return None
    # Only the stop cause; Store.stop appends "; N unfinished task(s) cancelled".
    reason = (reason or "").split(";")[0]
    for needle, advice in _STOP_ADVICE:
        if needle in reason:
            return advice
    return f"a budget ran out ({reason}); see limits"


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
CREATE TABLE IF NOT EXISTS tool_executions (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
 job_id TEXT NOT NULL REFERENCES jobs(id), tool TEXT NOT NULL, target TEXT NOT NULL,
 started REAL NOT NULL, finished REAL, outcome TEXT NOT NULL DEFAULT 'running',
 settings TEXT NOT NULL, version TEXT, run_id TEXT,
 diagnostics TEXT NOT NULL DEFAULT '{}', checks TEXT NOT NULL DEFAULT '{}',
 capture_id INTEGER REFERENCES captures(id)
);
CREATE INDEX IF NOT EXISTS tool_execution_job ON tool_executions(job_id,id);
CREATE TABLE IF NOT EXISTS tool_artifacts (
 id INTEGER PRIMARY KEY, execution_id INTEGER NOT NULL REFERENCES tool_executions(id),
 kind TEXT NOT NULL, body_hash TEXT NOT NULL REFERENCES blobs(hash),
 content_type TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(execution_id,kind)
);
CREATE INDEX IF NOT EXISTS tool_artifact_execution ON tool_artifacts(execution_id,id);
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
        self._migrate_investigations()

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

    def _migrate_investigations(self):
        """Add jobs.root_id: the investigation a follow-up job spends against.

        Not a user_version bump -- additive and idempotent, so it composes with the numbered
        migrations above. Existing follow-up children (parent set, no schedule) are backfilled
        so their historical spend is counted against their root from now on. Scheduled refresh
        runs also carry parent_id but are deliberately NOT folded in: each refresh is a new,
        operator-scheduled run with its own budget.
        """
        with self.transaction() as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            if "root_id" in columns:
                return
            db.execute("ALTER TABLE jobs ADD COLUMN root_id TEXT REFERENCES jobs(id)")
            db.execute("CREATE INDEX IF NOT EXISTS job_root ON jobs(root_id)")
            rows = db.execute(
                "SELECT id,parent_id FROM jobs WHERE parent_id IS NOT NULL "
                "AND schedule_key IS NULL ORDER BY created"
            ).fetchall()
            for row in rows:
                parent = db.execute(
                    "SELECT id,root_id FROM jobs WHERE id=?", (row["parent_id"],)
                ).fetchone()
                if parent:
                    db.execute(
                        "UPDATE jobs SET root_id=? WHERE id=?",
                        (parent["root_id"] or parent["id"], row["id"]),
                    )

    # Counters that are summed across an investigation and checked against the ROOT job's
    # limits. jobs column -> Limits attribute (cost is stored in micro-USD).
    _SHARED = (
        ("requests", "requests"),
        ("bytes", "bytes"),
        ("model_calls", "model_calls"),
        ("model_tokens", "model_tokens"),
        ("cost_microusd", "cost_usd"),
    )

    @staticmethod
    def _ceiling(limits, attr):
        value = getattr(limits, attr)
        return int(value * 1_000_000) if attr == "cost_usd" else value

    def _investigation_usage(self, db, root):
        """(root limits, summed counters, task count, tool-run count, first start) for one
        investigation: the root job plus every follow-up job, however deeply chained."""
        root_row = db.execute("SELECT spec FROM jobs WHERE id=?", (root,)).fetchone()
        limits = JobSpec.model_validate_json(root_row["spec"]).limits
        cols = ",".join(f"coalesce(sum({c}),0)" for c, _ in self._SHARED)
        totals = db.execute(
            f"SELECT {cols},min(started) FROM jobs WHERE id=? OR root_id=?", (root, root)
        ).fetchone()
        used = {c: totals[i] for i, (c, _) in enumerate(self._SHARED)}
        tasks, tools = db.execute(
            """SELECT count(*),coalesce(sum(t.kind='tool'),0) FROM tasks t JOIN jobs j
            ON j.id=t.job_id WHERE j.id=? OR j.root_id=?""",
            (root, root),
        ).fetchone()
        return limits, used, tasks, tools, totals[len(self._SHARED)]

    def seconds_left(self, task):
        """Wall clock remaining for this task's job AND its investigation, whichever is less.

        A tool run is one task that can take minutes; it must be bounded by this, not by
        HARVEST_TOOL_TIMEOUT alone, or it runs on past the deadline the operator set."""
        with self.connection() as db:
            job = db.execute(
                "SELECT spec,started,root_id,id FROM jobs WHERE id=?", (task["job_id"],)
            ).fetchone()
            limits = JobSpec.model_validate_json(job["spec"]).limits
            ilimits, _, _, _, started = self._investigation_usage(db, job["root_id"] or job["id"])
        now = time.time()
        left = [limits.seconds - (now - (job["started"] or now))]
        if started is not None:
            left.append(ilimits.seconds - (now - started))
        return max(0.0, min(left))

    def _admit_child(self, db, root, initial):
        """Refuse a follow-up the investigation can no longer afford, inside the creating
        transaction so two concurrent follow-ups cannot both pass a check-then-insert."""
        limits, used, tasks, tools, started = self._investigation_usage(db, root)
        for column, attr in self._SHARED:
            if used[column] >= self._ceiling(limits, attr):
                raise BudgetExceeded(f"investigation {attr} budget exhausted")
        if started is not None and time.time() - started >= limits.seconds:
            raise BudgetExceeded("investigation wall-clock deadline reached")
        new_tools = sum(1 for t in initial if t["kind"] == "tool")
        if tools + new_tools > limits.tool_runs:
            raise BudgetExceeded(
                f"investigation tool_runs budget exhausted ({tools}/{limits.tool_runs} used)"
            )
        if tasks + len(initial) > limits.tasks:
            raise BudgetExceeded("investigation task budget exhausted")

    def investigation(self, job_id):
        """Investigation-wide budget: what is ENFORCED, and what is only estimated.

        Enforced counters are Harvest's own requests/bytes/model spend, summed over the root
        and all follow-ups and checked against the root's limits on every reservation. External
        tools (maigret, ghunt, spiderfoot) make their own network requests that Harvest does
        not proxy-meter per run; only their NUMBER of runs is enforced (tool_runs). Their
        network cost is reported as a documented estimate, with the measured size of the
        output each run returned -- never presented as metered spend.
        """
        from .capabilities import _TOOL_NOTES

        with self.connection() as db:
            row = db.execute("SELECT id,root_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            root = row["root_id"] or row["id"]
            limits, used, tasks, tools, started = self._investigation_usage(db, root)
            job_ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM jobs WHERE id=? OR root_id=? ORDER BY created", (root, root)
                )
            ]
            runs = db.execute(
                """SELECT c.url,length(b.body) FROM captures c JOIN blobs b ON b.hash=c.body_hash
                JOIN jobs j ON j.id=c.job_id WHERE c.url LIKE 'tool://%' AND (j.id=? OR j.root_id=?)
                ORDER BY c.id""",
                (root, root),
            ).fetchall()
            # Elapsed wall clock stops when the investigation does. It was always now-started,
            # so a job finished yesterday reported 42,000 s "used" of a 900 s limit.
            ended, live = db.execute(
                """SELECT max(finished),coalesce(sum(status IN ('queued','running')),0)
                FROM jobs WHERE id=? OR root_id=?""",
                (root, root),
            ).fetchone()
            pivots = [
                {k: d.get(k) for k in ("child", "kind", "value", "source", "tools", "queries")}
                for d in (
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT details FROM events WHERE job_id=? AND type='pivot' ORDER BY id",
                        (root,),
                    )
                )
            ]
        enforced = {
            attr: {
                "used": used[c] / 1_000_000 if attr == "cost_usd" else used[c],
                "limit": getattr(limits, attr),
            }
            for c, attr in self._SHARED
        }
        enforced["tool_runs"] = {"used": tools, "limit": limits.tool_runs}
        enforced["tasks"] = {"used": tasks, "limit": limits.tasks}
        enforced["seconds"] = {
            "used": round(((ended if not live and ended else time.time()) - started), 1)
            if started
            else 0,
            "limit": limits.seconds,
        }
        return {
            "root_id": root,
            "jobs": len(job_ids),
            "job_ids": job_ids,
            "enforced": enforced,
            "pivots": pivots,
            "external_tool_runs": [
                {
                    "tool": url.split("/")[2],
                    "network_cost": "estimated, not metered: "
                    + _TOOL_NOTES.get(url.split("/")[2], {}).get("cost", "unknown"),
                    "measured_output_bytes": size,
                }
                for url, size in runs
            ],
        }

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
                if row["kind"] not in {"fetch", "tool"}:
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

    def start_tool_execution(self, task, *, tool: str, target: str, settings: dict, version=None):
        """Persist the execution envelope before any external tool is started."""
        with self.transaction() as db:
            self.owned(db, task)
            db.execute(
                """INSERT INTO tool_executions(
                    task_id,job_id,tool,target,started,outcome,settings,version
                ) VALUES(?,?,?,?,?,'running',?,?)
                ON CONFLICT(task_id) DO NOTHING""",
                (task["id"], task["job_id"], tool, target, time.time(), packed(settings), version),
            )

    @staticmethod
    def _finish_tool_execution(
        db,
        task_id: int,
        *,
        outcome: str,
        capture_id=None,
        run_id=None,
        version=None,
        diagnostics=None,
        checks=None,
        native_body: bytes | None = None,
    ):
        row = db.execute("SELECT id FROM tool_executions WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return
        execution_id = row["id"]
        if native_body is not None:
            body_hash = digest(native_body)
            db.execute(
                "INSERT OR IGNORE INTO blobs(hash,body) VALUES(?,?)", (body_hash, native_body)
            )
            db.execute(
                """INSERT INTO tool_artifacts(execution_id,kind,body_hash,content_type,created)
                VALUES(?,?,?,?,?)
                ON CONFLICT(execution_id,kind) DO UPDATE SET
                  body_hash=excluded.body_hash,content_type=excluded.content_type,created=excluded.created""",
                (
                    execution_id,
                    "native-structured",
                    body_hash,
                    "application/json",
                    time.time(),
                ),
            )
        db.execute(
            """UPDATE tool_executions SET finished=?,outcome=?,capture_id=coalesce(?,capture_id),
            run_id=coalesce(?,run_id),version=coalesce(?,version),diagnostics=?,checks=?
            WHERE id=?""",
            (
                time.time(),
                outcome,
                capture_id,
                run_id,
                version,
                packed(diagnostics or {}),
                packed(checks or {}),
                execution_id,
            ),
        )

    def tool_executions(self, job_id: str):
        self.job(job_id)
        with self.connection() as db:
            rows = db.execute(
                """SELECT e.*,
                (SELECT count(*) FROM tool_artifacts a WHERE a.execution_id=e.id) artifacts
                FROM tool_executions e WHERE e.job_id=? ORDER BY e.id""",
                (job_id,),
            ).fetchall()
            return [
                {
                    **dict(r),
                    "settings": json.loads(r["settings"]),
                    "diagnostics": json.loads(r["diagnostics"]),
                    "checks": json.loads(r["checks"]),
                }
                for r in rows
            ]

    def tool_artifact(self, execution_id: int, kind: str):
        with self.connection() as db:
            row = db.execute(
                """SELECT a.*,b.body FROM tool_artifacts a JOIN blobs b ON b.hash=a.body_hash
                WHERE a.execution_id=? AND a.kind=?""",
                (execution_id, kind),
            ).fetchone()
            if row is None:
                raise KeyError((execution_id, kind))
            return dict(row)

    def create(
        self,
        spec: JobSpec,
        initial: list[dict],
        key: str | None = None,
        parent: str | None = None,
        root: str | None = None,
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
            if root:
                self._admit_child(db, root, initial)
            job = self._create(db, spec, initial, key, parent, root=root)
            if spec.mode == "continuous":
                db.execute(
                    "INSERT INTO schedules VALUES(?,?,?,?,?,1)",
                    (job, spec_json, spec.refresh_seconds, time.time() + spec.refresh_seconds, job),
                )
            return job

    def _create(self, db, spec, initial, key=None, parent=None, schedule_key=None, root=None):
        job = uuid.uuid4().hex
        data = packed(spec.model_dump())
        db.execute(
            "INSERT INTO jobs(id,spec,spec_hash,idempotency_key,created,parent_id,schedule_key,root_id) VALUES(?,?,?,?,?,?,?,?)",
            (job, data, digest(data), key, time.time(), parent, schedule_key, root),
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
        row = db.execute("SELECT root_id FROM jobs WHERE id=?", (job,)).fetchone()
        if row:
            # Every frontier also counts against the whole investigation's task budget, so a
            # root cannot keep expanding on the allowance its follow-ups already used.
            limits, _, tasks, _, _ = self._investigation_usage(db, row[0] or job)
            if tasks >= limits.tasks:
                self.event(
                    db, job, "frontier_limit", {"limit": limits.tasks, "scope": "investigation"}
                )
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

    def claim(
        self,
        job_id: str | None = None,
        lease_seconds: float = 120,
        *,
        max_active_investigations: int | None = None,
        max_running_tools: int | None = None,
    ):
        with self.transaction() as db:
            # BEGIN IMMEDIATE may wait for another writer. Start all claim timing only after
            # the transaction owns the write lock so lock contention cannot shorten the lease.
            now = time.time()
            expired = db.execute(
                """SELECT t.*,j.spec FROM tasks t JOIN jobs j ON j.id=t.job_id
                WHERE t.status='running' AND t.lease_until<=? AND j.status='running'""",
                (now,),
            ).fetchall()
            for task in expired:
                limit = JobSpec.model_validate_json(task["spec"]).limits.attempts
                # A tool attempt may have sent thousands of requests before its worker
                # disappeared. Reclaiming it must never launch the scan again.
                status = (
                    "failed" if task["kind"] == "tool" or task["attempts"] >= limit else "pending"
                )
                db.execute(
                    "UPDATE tasks SET status=?,token=NULL,error=? WHERE id=?",
                    (
                        status,
                        "worker lease expired: the worker stopped mid-task (restart or crash)"
                        + (
                            "; a tool run is never repeated automatically, rerun the job"
                            if task["kind"] == "tool"
                            else ""
                        ),
                        task["id"],
                    ),
                )
                if task["kind"] == "tool":
                    self._finish_tool_execution(
                        db,
                        task["id"],
                        outcome="interrupted",
                        diagnostics={"reason": "worker lease expired"},
                    )
                self.event(
                    db, task["job_id"], "lease_expired", {"task": task["id"], "status": status}
                )
            candidates = db.execute(
                """SELECT t.*,j.spec,j.execution,j.root_id,j.status job_status
                FROM tasks t JOIN jobs j ON j.id=t.job_id
                WHERE t.status='pending' AND t.ready<=? AND j.status IN ('queued','running')
                AND (? IS NULL OR j.id=?)
                AND NOT EXISTS(SELECT 1 FROM tasks r WHERE r.job_id=j.id AND r.status='running')
                ORDER BY t.priority DESC,t.id LIMIT 100""",
                (now, job_id, job_id),
            ).fetchall()
            active_roots = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT coalesce(root_id,id) FROM jobs WHERE status='running'"
                )
            }
            running_tools = db.execute(
                "SELECT count(*) FROM tasks WHERE kind='tool' AND status='running'"
            ).fetchone()[0]
            row = None
            for candidate in candidates:
                root = candidate["root_id"] or candidate["job_id"]
                if (
                    max_active_investigations is not None
                    and root not in active_roots
                    and len(active_roots) >= max_active_investigations
                ):
                    continue
                if (
                    candidate["kind"] == "tool"
                    and max_running_tools is not None
                    and running_tools >= max_running_tools
                ):
                    continue
                row = candidate
                break
            if row is None:
                return None
            token = uuid.uuid4().hex
            claimed_at = time.time()
            db.execute(
                "UPDATE tasks SET status='running',attempts=attempts+1,token=?,lease_until=? WHERE id=?",
                (token, claimed_at + lease_seconds, row["id"]),
            )
            db.execute(
                "UPDATE jobs SET status='running',started=coalesce(started,?) WHERE id=?",
                (claimed_at, row["job_id"]),
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
            # Investigation-wide: the root's ceilings bound the SUM over the root and every
            # follow-up, so N concurrent or chained children cannot spend N budgets. Checked
            # in this same BEGIN IMMEDIATE transaction, which serialises sibling reservations.
            root = job["root_id"] or job["id"]
            ilimits, used, _, _, started = self._investigation_usage(db, root)
            if started is not None and time.time() - started >= ilimits.seconds:
                raise BudgetExceeded("investigation wall-clock deadline reached")
            for column, attr in self._SHARED:
                if used[column] + values[column] > self._ceiling(ilimits, attr):
                    raise BudgetExceeded(f"investigation {column} limit reached")
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
            # A tool may already have made unbudgeted requests before any failure,
            # including a post-scan storage error. Never relaunch it through defer.
            failed = task["kind"] == "tool" or (failure and task["attempts"] >= limit)
            db.execute(
                "UPDATE tasks SET status=?,ready=?,token=NULL,lease_until=NULL,error=?,attempts=attempts-? WHERE id=?",
                (
                    "failed" if failed else "pending",
                    time.time() + delay,
                    str(error)[:1000],
                    0 if failure or task["kind"] == "tool" else 1,
                    task["id"],
                ),
            )
            self.event(
                db,
                task["job_id"],
                "failed" if failed else "deferred",
                {"task": task["id"], "reason": str(error)[:1000], "delay": delay},
            )
            if task["kind"] == "tool":
                self._finish_tool_execution(
                    db,
                    task["id"],
                    outcome="failed" if failed else "interrupted",
                    diagnostics={"reason": str(error)[:1000]},
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
            if task["kind"] == "tool":
                self._finish_tool_execution(
                    db,
                    task["id"],
                    outcome="cancelled" if status == "cancelled" else "failed",
                    diagnostics={"reason": str(error)[:1000]},
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
                    # A value dropped for lack of field-level evidence is a deliberate evidence
                    # decision, not an incomplete extraction: visible, but not "partial".
                    kind = (
                        "evidence_omitted"
                        if warning.startswith("socid value")
                        else "extraction_limit"
                    )
                    self.event(db, job["id"], kind, {"task": task["id"], "reason": warning})
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
                    # Carried across the rewrite below, like `pass`: the acquisition task is
                    # what knows whether its findings may be crawled (ToolRun.crawl), and the
                    # extract task is where leads are selected. Dropping it here silently
                    # restored crawling for every tool run that asked not to be crawled.
                    crawl = item.get("payload", {}).get("crawl")
                    item["payload"] = {"capture_id": capture_id}
                    item["key"] = str(capture_id)
                    if crawl is not None:
                        item["payload"]["crawl"] = crawl
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
            if task["kind"] == "tool" and response is not None:
                meta = getattr(response, "tool_meta", None) or {}
                self._finish_tool_execution(
                    db,
                    task["id"],
                    outcome="partial" if meta.get("partial") else "complete",
                    capture_id=capture_id,
                    run_id=meta.get("run_id"),
                    version=meta.get("version"),
                    diagnostics=meta.get("diagnostics"),
                    checks=meta.get("checks"),
                    native_body=getattr(response, "native_body", None),
                )
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
        """Stop a job; cancelling an investigation's root also cancels its live follow-ups and
        pivots, which otherwise kept scanning after the operator pressed Cancel."""
        if status not in {"cancelled", "budget_exhausted", "plateau"}:
            raise ValueError("invalid stop status")
        with self.transaction() as db:
            old = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not old:
                raise KeyError(job_id)
            targets = [job_id] if old[0] in {"queued", "running"} else []
            if status == "cancelled":
                targets += [
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM jobs WHERE root_id=? AND status IN ('queued','running')",
                        (job_id,),
                    )
                ]
            for job in targets:
                # Name what was left undone: "budget_exhausted" alone read as if the tool runs
                # had been cut short when only queued crawl pages were dropped.
                undone = db.execute(
                    "UPDATE tasks SET status='cancelled',token=NULL WHERE job_id=? AND status IN ('pending','running')",
                    (job,),
                ).rowcount
                why = reason if job == job_id else f"investigation {job_id} cancelled"
                why += f"; {undone} unfinished task(s) cancelled" if undone else ""
                db.execute(
                    "UPDATE jobs SET status=?,finished=?,reason=? WHERE id=?",
                    (status, time.time(), why, job),
                )
                db.execute(
                    """UPDATE tool_executions SET finished=coalesce(finished,?),outcome=
                    CASE WHEN outcome='running' THEN ? ELSE outcome END
                    WHERE job_id=?""",
                    (time.time(), "cancelled" if status == "cancelled" else "interrupted", job),
                )
                self.event(db, job, status, {"reason": why})

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
                # Every queued task ran (there is no crawl "frontier" in a tool-only job).
                reason = "all queued work finished"
                # A tool run cut short by its time allowance kept partial results; the job
                # must not read as complete because its remaining tasks then ran out.
                if db.execute(
                    """SELECT 1 FROM events WHERE job_id=? AND type='task_done'
                    AND details LIKE '%"partial":%' LIMIT 1""",
                    (row["id"],),
                ).fetchone():
                    status = "partial"
                    reason += "; a tool run hit its time allowance and kept partial results"
                if not counts.get("done", 0):
                    status = "failed"
                if status != "completed":
                    # Say what actually went wrong, not just that the queue emptied.
                    first = db.execute(
                        """SELECT error FROM tasks WHERE job_id=? AND status IN ('failed','blocked')
                        AND error IS NOT NULL ORDER BY kind='tool' DESC,id LIMIT 1""",
                        (row["id"],),
                    ).fetchone()
                    if first:
                        failed = counts.get("failed", 0) + counts.get("blocked", 0)
                        reason += f"; {failed} task(s) failed or blocked, e.g. {first[0]}"
                db.execute(
                    "UPDATE jobs SET status=?,finished=?,reason=? WHERE id=?",
                    (status, time.time(), reason, row["id"]),
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
        result["investigation"] = self.investigation(job_id)
        with self.connection() as db:
            fields = self._satisfied_fields(result["spec"], self._fields(db, job_id))
            result["missing_fields"] = [f for f in result["spec"]["fields"] if f not in fields]
            result["stop_advice"] = stop_advice(result["status"], result["reason"])
            result["coverage"] = (
                "unmeasured; completion describes work execution, not population completeness"
            )
            return result

    @staticmethod
    def _satisfied_fields(spec, observed):
        """`observed`, plus the dossier names a source rule's field_map renames those into.

        A field_map entry means "the record calls this `id`, the dossier calls it `gaia_id`".
        So a job that declares the dossier-side name in `fields` has that field genuinely
        populated by an observation stored under the SOURCE-side name. Comparing `fields`
        against observation names alone therefore reported every mapped field as missing
        while the dossier held a value for it -- which is precisely what this value is read
        for, in `GET /jobs/{id}` and the job detail page.

        Report-only, and deliberately not applied to the other `fields` consumers. Reread
        gating in `enqueue()`, reasoning gating in the engine, and the model's own field list
        must keep asking the narrower question "did extraction name this field", because a
        reread is justified by a field the extractor still has not produced; crediting a
        rename there would cancel a pass that is in fact still needed.

        Job-wide, like `_fields`: it reports that the job produced the field somewhere, not
        that any particular target bound it. Per-target truth is the dossier's own
        `missing_fields`, which reconciles against `field_map` values directly.
        """
        # Tested against `observed`, never against the set being built: a field_map is one
        # hop from record name to dossier name, so chaining a->b with b->c must NOT credit
        # c, and membership must not depend on rule or key iteration order.
        satisfied = set(observed)
        satisfied.update(f for f, sources in FIELD_ALIASES.items() if observed & set(sources))
        for rule in (spec.get("investigation") or {}).get("sources") or []:
            for source_field, dossier_field in (rule.get("field_map") or {}).items():
                if source_field in observed:
                    satisfied.add(dossier_field)
        return satisfied

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

    def discovered(self, job_id: str, needle: str) -> bool:
        """Did this job's OWN evidence already surface `needle`?

        The gate on agent follow-ups: an agent may pursue a URL or identifier the job already
        found, never one it injects. `needle` counts as discovered if it is a capture URL, an
        observation's source URL or value, or an entity key in this job -- so a lead the job
        produced can be followed, while an arbitrary new target (a different person) cannot be
        laundered through a "follow-up". Scoped to one job; the caller passes the parent.
        """
        self.job(job_id)
        packed_value = packed(needle)
        with self.connection() as db:
            hit = db.execute(
                """SELECT 1 WHERE
                  EXISTS(SELECT 1 FROM captures c WHERE c.job_id=? AND (c.url=? OR c.final_url=?))
                  OR EXISTS(SELECT 1 FROM observations o JOIN assertions a ON a.observation_id=o.id
                       JOIN extractions x ON x.id=a.extraction_id
                       WHERE x.job_id=? AND (o.source_url=? OR o.value=?))
                  OR EXISTS(SELECT 1 FROM entities e JOIN observations o ON o.entity_id=e.id
                       JOIN assertions a ON a.observation_id=o.id JOIN extractions x ON x.id=a.extraction_id
                       WHERE x.job_id=? AND (e.entity_key=? OR e.entity_key=?))
                LIMIT 1""",
                (
                    job_id,
                    needle,
                    needle,
                    job_id,
                    needle,
                    packed_value,
                    job_id,
                    needle,
                    "url:" + needle,
                ),
            ).fetchone()
        return hit is not None

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
                alias = next(
                    (a for a in FIELD_ALIASES.get(name, ()) if a in entity["fields"]), None
                )
                entity["fields"].setdefault(
                    name,
                    {**entity["fields"][alias], "via": alias}
                    if alias
                    else {"value": None, "conflict": False, "missing": True, "candidates": []},
                )
            results.append(entity)
        return sorted(results, key=lambda e: e["entity_id"])

    def job_summary(self, job_id):
        """One row per account a tool reported, with the three questions kept apart:
        does a profile exist (tool's `existence`), did our own fetch of it show the searched
        identifier (`page_check`, from this job or its follow-ups), and is it the subject's
        (`ownership` -- never confirmed by tool evidence). Unknowns are listed, not implied."""
        job = self.job(job_id)
        with self.connection() as db:
            jobs = [job_id] + [
                r[0] for r in db.execute("SELECT id FROM jobs WHERE root_id=?", (job_id,))
            ]
            marks = ",".join("?" * len(jobs))
            fetches = {}
            for key, status, error in db.execute(
                f"SELECT key,status,error FROM tasks WHERE kind='fetch' AND job_id IN ({marks})",
                jobs,
            ):
                if fetches.get(key, ("",))[0] != "done":
                    fetches[key] = (status, error)
            pivots = {
                d["child"]: d
                for d in (
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT details FROM events WHERE job_id=? AND type='pivot' ORDER BY id",
                        (job_id,),
                    )
                )
            }
            searches = [
                d
                for d in (
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT details FROM events WHERE job_id=? AND type='task_done'", (job_id,)
                    )
                )
                if isinstance(d, dict) and "query" in d
            ]
        by_job = {j: self.job_records(j) for j in jobs}
        records = [r for rows in by_job.values() for r in rows]
        checks = {
            r["entity_key"]: r["fields"]["page_check"]["value"]
            for r in records
            if "page_check" in r["fields"] and not r["fields"]["page_check"].get("missing")
        }

        def value(fields, name):
            field = fields.get(name) or {}
            return None if field.get("missing") or field.get("conflict") else field.get("value")

        off_target: dict[str, int] = {}

        def account(record, tally):
            fields = record["fields"]
            if value(fields, "existence") is None or not isinstance(value(fields, "url"), str):
                return None
            if via := value(fields, "pivoted_via"):
                # Reached through a name or another subject's identifier, not the target's.
                if tally:
                    off_target[via] = off_target.get(via, 0) + 1
                return None
            url = value(fields, "url")
            try:
                canon = canonical_url(url)
            except ValueError:
                canon = url
            check = checks.get("url:" + canon)
            if check is None:
                status, error = fetches.get(canon, ("not_fetched", None))
                check = f"unchecked: {error or status}"
            return {
                "site": value(fields, "sitename") or urlsplit(url).hostname,
                "url": url,
                "existence": value(fields, "existence"),
                "page_check": check,
                "ownership": value(fields, "ownership"),
                "display_name": value(fields, "display_name"),
                "entity_id": record["entity_id"],
            }

        def ranked(rows, tally=False):
            found = [a for a in (account(r, tally) for r in rows) if a]
            return sorted(
                found,
                key=lambda a: (
                    a["page_check"] != "profile_evidence",
                    a["existence"] != "observed",
                    a["site"] or "",
                ),
            )

        # A pivot child's accounts belong to the identifier it pivoted to, not to this job's
        # subject: listed under that pivot, never merged into `accounts`.
        accounts = ranked(
            [r for j, rows in by_job.items() if j not in pivots for r in rows], tally=True
        )
        pivot_rows = [
            {
                "job_id": child,
                "kind": d["kind"],
                "value": d["value"],
                "source": d.get("source"),
                "status": self.job(child)["status"],
                "accounts": ranked(by_job.get(child, [])),
            }
            for child, d in pivots.items()
        ]
        unknowns = [f"requested field never observed: {f}" for f in job["missing_fields"]]
        if accounts:
            unknowns.append(
                "ownership: no account is confirmed as the subject's; a matching handle or a "
                "page naming it shows an account exists, not who runs it"
            )
        unchecked = sum(a["page_check"].startswith("unchecked") for a in accounts)
        if unchecked:
            unknowns.append(f"{unchecked} reported account(s) were never fetched or verified")
        for via, count in sorted(off_target.items()):
            unknowns.append(
                f"{count} account(s) SpiderFoot reached through a {via}, not through the "
                "target's own identifiers, are kept as evidence but not listed"
            )
        if pivot_rows:
            unknowns.append(
                "pivots: each pivot's accounts belong to the identifier it pivoted to, which is "
                "linked to the subject only by where it was found"
            )
        # A finished search task means the query ran, not that discovery worked: engines can
        # be down while the rest answer with noise. Outages were kept per task and never
        # reached the summary, so three degraded searches read as "nothing on the web".
        search = {
            "runs": len(searches),
            "degraded": sum(bool(d.get("unresponsive_engines")) for d in searches),
            "unresponsive_engines": sorted(
                {e for d in searches for e in d.get("unresponsive_engines") or []}
            ),
            "results": sum(d.get("search_results") or 0 for d in searches),
            "accepted_leads": sum(d.get("accepted_leads") or 0 for d in searches),
        }
        if search["degraded"]:
            unknowns.append(
                f"discovery search degraded: {search['degraded']}/{search['runs']} search(es) "
                f"ran with engines down ({', '.join(search['unresponsive_engines'])}); "
                f"{search['accepted_leads']} lead(s) accepted from {search['results']} "
                "result(s), so missing web leads are not evidence of absence"
            )
        tool_execution_rows = [execution for jid in jobs for execution in self.tool_executions(jid)]
        with self.connection() as db:
            tool_task_count = db.execute(
                f"SELECT count(*) FROM tasks WHERE kind='tool' AND job_id IN ({marks})",
                jobs,
            ).fetchone()[0]
        legacy_tool_runs = max(0, tool_task_count - len(tool_execution_rows))
        tool_coverage = {
            "executions": len(tool_execution_rows),
            "legacy_unknown": legacy_tool_runs,
            "outcomes": {},
            "checks": {
                "found": 0,
                "absent": 0,
                "blocked": 0,
                "errored": 0,
                "unsupported": 0,
                "skipped": 0,
                "unfinished": 0,
            },
        }
        for execution in tool_execution_rows:
            outcome = execution["outcome"]
            tool_coverage["outcomes"][outcome] = tool_coverage["outcomes"].get(outcome, 0) + 1
            for name in tool_coverage["checks"]:
                tool_coverage["checks"][name] += int(execution["checks"].get(name, 0) or 0)
            coverage = execution["diagnostics"].get("coverage")
            if coverage in {"unknown", "partial"}:
                unknowns.append(
                    f"{execution['tool']} execution {execution['id']} has {coverage} native "
                    "check coverage; a finished process is not evidence every source was checked"
                )
        if legacy_tool_runs:
            unknowns.append(
                f"{legacy_tool_runs} legacy tool run(s) predate execution diagnostics; "
                "their acquisition coverage is unknown"
            )
        limits = job["spec"]["limits"]
        tool_runs = job["investigation"]["enforced"]["tool_runs"]
        return {
            "job_id": job_id,
            "status": job["status"],
            "reason": job["reason"],
            "stop_advice": job["stop_advice"],
            # Harvest's own fetches only. Tools make their own requests outside this budget.
            "requests": f"{job['requests']}/{limits['requests']}",
            "tool_runs": f"{tool_runs['used']}/{tool_runs['limit']}",
            "tool_requests": "not metered: each tool run makes its own requests outside the request budget",
            "accounts": accounts,
            "verified_pages": sum(a["page_check"] == "profile_evidence" for a in accounts),
            "pivots": pivot_rows,
            "search": search,
            "tool_coverage": tool_coverage,
            "unknowns": unknowns,
        }

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
