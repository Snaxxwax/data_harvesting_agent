from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpcore
import httpx

from .config import Settings
from .extract import Extractors, http_url
from .models import (
    BudgetExceeded,
    JobSpec,
    LostLease,
    PolicyDenied,
    ReplaySpec,
    RetryLater,
    canonical_url,
)
from .network import Fetcher, in_scope
from .reasoning import Reasoner
from .store import Store, digest

log = logging.getLogger("harvest")
ACTIVE = {"queued", "running"}


class Engine:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store=None,
        fetcher_factory=Fetcher,
        reasoner=None,
    ):
        self.settings = settings or Settings()
        self.store = store or Store(self.settings.database)
        self.extractors = Extractors()
        self.fetcher_factory = fetcher_factory
        self.reasoner = reasoner or Reasoner(self.settings, self.store)

    def submit(self, spec: JobSpec, key=None):
        if spec.use_model and not self.reasoner.configured():
            raise ValueError(
                "use_model requires HARVEST_MODEL_URL, HARVEST_MODEL_NAME and HARVEST_MODEL_USD_PER_MILLION"
            )
        seeds = spec.seeds or [
            x.rstrip(".,;)") for x in re.findall(r"https?://[^\s<>]+", spec.objective)
        ]
        initial = []
        for url in seeds:
            url = canonical_url(url)
            if not in_scope(url, spec):
                raise ValueError("seed is outside allowed domains")
            initial.append({"kind": "fetch", "key": url, "payload": {"url": url}})
        if not initial:
            if not self.settings.search_url:
                raise ValueError(
                    "provide a seed URL or configure HARVEST_SEARCH_URL for discovery from an objective"
                )
            initial.append(
                {"kind": "search", "key": spec.objective, "payload": {"query": spec.objective}}
            )
        return self.store.create(spec, initial, key)

    def context(self, job_id):
        observations = self.store.observations(job_id, limit=40)
        job = self.store.job(job_id)
        with self.store.connection() as db:
            visited = [
                r[0]
                for r in db.execute(
                    "SELECT key FROM tasks WHERE job_id=? AND kind IN ('fetch','search') ORDER BY id DESC LIMIT 50",
                    (job_id,),
                )
            ]
            previous = db.execute(
                "SELECT details FROM events WHERE job_id=? AND type='task_done' ORDER BY id DESC LIMIT 20",
                (job_id,),
            ).fetchall()
        decisions = [
            json.loads(r[0])["decision"] for r in previous if "decision" in json.loads(r[0])
        ]
        values = [
            {
                "entity": r["entity_key"],
                "field": r["field"],
                "value": r["value"],
                "source": r["source_url"],
            }
            for r in observations
        ]
        return {
            "observations": values,
            "visited_or_queued": visited,
            "missing_fields": job["missing_fields"],
            "previous_decisions": decisions[:2],
        }

    def replay(self, spec: ReplaySpec, key=None):
        return self.store.replay(spec, key)

    def extract_capture(self, task):
        spec = JobSpec.model_validate_json(task["spec"])
        cap = self.store.capture(task["payload"]["capture_id"])
        if digest(cap["body"]) != cap["body_hash"]:
            raise ValueError("capture body hash mismatch")
        headers = json.loads(cap["headers"])
        revision = self.store.extraction_task(task)
        remaining = max(
            0, spec.limits.records - self.store.job(task["job_id"])["records_processed"]
        )
        batches = self.extractors.batches(
            cap["body"],
            cap["final_url"],
            headers.get("content-type", ""),
            revision["records_processed"] or 0,
            remaining,
        )
        if batches is not None:
            done = False
            for batch in batches:
                self.store.reserve(task)
                self.commit_extraction(task, cap, headers, batch.extraction, batch=batch)
                done = batch.done
            if not done:
                raise BudgetExceeded("records limit reached; partial extraction retained")
            return
        if revision["batches"]:
            raise ValueError("adapter changed during extraction; create a new replay")
        extraction = self.extractors.extract(
            cap["body"], cap["final_url"], headers.get("content-type", "")
        )
        self.commit_extraction(task, cap, headers, extraction)

    def commit_extraction(self, task, cap, headers, extraction, batch=None):
        spec = JobSpec.model_validate_json(task["spec"])
        for item in headers.get("link", "").split(","):
            match = re.search(r'<([^>]+)>;\s*rel="?next"?', item)
            if match:
                from .models import Lead

                if candidate := http_url(match.group(1), cap["final_url"]):
                    extraction.leads.insert(
                        0, Lead(url=candidate, reason="pagination", priority=50)
                    )
        offline = task["execution"] == "offline_replay"
        leads = [] if offline else self.select_leads(task, extraction.leads)
        found_fields = {c.field for c in extraction.claims}
        if batch is not None and batch.done:
            with self.store.connection() as db:
                found_fields.update(
                    r[0]
                    for r in db.execute(
                        """SELECT DISTINCT o.field FROM observations o
                    JOIN assertions a ON a.observation_id=o.id JOIN extractions x ON x.id=a.extraction_id WHERE x.task_id=?""",
                        (task["id"],),
                    )
                )
        needs_reason = (
            not extraction.claims
            or bool(set(spec.fields) - found_fields)
            or spec.mode == "deep_research"
        )
        if (
            not offline
            and spec.use_model
            and needs_reason
            and extraction.text
            and (batch is None or batch.done)
        ):
            leads.append(
                {
                    "kind": "reason",
                    "key": "capture",
                    "payload": {},
                    "depth": task["depth"],
                    "priority": 100,
                    "reason": "evidence extraction and gap analysis",
                }
            )
        self.store.finish(
            task,
            capture_id=cap["id"],
            extraction=extraction,
            batch=batch,
            leads=leads,
            details={
                "extracted_claims": len(extraction.claims),
                "accepted_leads": len(leads),
                "offline": offline,
                "suppressed_leads": len(extraction.leads) if offline else 0,
            },
        )

    def select_leads(self, task, leads, *, search=False):
        spec = JobSpec.model_validate_json(task["spec"])
        with self.store.connection() as db:
            seen = [
                r[0]
                for r in db.execute(
                    "SELECT key FROM tasks WHERE job_id=? AND kind='fetch'", (task["job_id"],)
                )
            ]
        hosts = {}
        for url in seen:
            host = urlsplit(url).hostname
            hosts[host] = hosts.get(host, 0) + 1
        result, added = [], set()
        words = set(re.findall(r"[a-z]{4,}", spec.objective.lower()))
        for lead in leads:
            # Pagination expands one population; it does not consume investigation depth.
            depth = task["depth"] if lead.reason == "pagination" else task["depth"] + 1
            if depth > spec.limits.depth:
                continue
            url = http_url(lead.url)
            if not url or url in seen or url in added or not in_scope(url, spec):
                continue
            if (
                spec.mode in {"enumerative", "continuous"}
                and not search
                and lead.reason.startswith("link:")
            ):
                continue
            added.add(url)
            host = urlsplit(url).hostname
            relevance = sum(w in (url + " " + lead.reason).lower() for w in words)
            diversity = 15 if host not in hosts else -min(hosts[host], 15)
            priority = (
                lead.priority + relevance * 3 + (diversity if spec.mode == "deep_research" else 0)
            )
            result.append(
                {
                    "kind": "fetch",
                    "key": url,
                    "payload": {"url": url},
                    "depth": depth,
                    "priority": priority,
                    "reason": lead.reason,
                }
            )
        return sorted(result, key=lambda x: x["priority"], reverse=True)[: 30 if search else 50]

    def process(self, task):
        spec = JobSpec.model_validate_json(task["spec"])
        self.store.reserve(task)
        if task["execution"] == "offline_replay" and task["kind"] != "extract":
            raise PolicyDenied("offline replay cannot execute network or model tasks")
        if task["kind"] == "extract":
            self.extract_capture(task)
            return
        if task["kind"] == "reason":
            cap = self.store.capture(task["payload"]["capture_id"])
            if digest(cap["body"]) != cap["body_hash"]:
                raise ValueError("capture body hash mismatch")
            extraction = self.extractors.extract(
                cap["body"], cap["final_url"], json.loads(cap["headers"]).get("content-type", "")
            )
            reading_pass = task["payload"].get("pass", 1)
            result, decision, details = self.reasoner.decide(
                task,
                cap["final_url"],
                extraction.text,
                self.context(task["job_id"]),
                normalizer=extraction.extractor,
                body_hash=cap["body_hash"],
                fields=task["payload"].get("unresolved_fields"),
                exclude=self.store.shown_spans(
                    task["job_id"], cap["id"], exclude_task_id=task["id"]
                )
                if reading_pass > 1
                else (),
                reading_pass=reading_pass,
            )
            if decision is None:
                self.store.finish(task, extraction=result, capture_id=cap["id"], details=details)
                return
            leads = self.select_leads(task, decision.leads)
            if reading_pass < spec.limits.reading_passes:
                # Store.finish enqueues this only if the pass was novel and fields remain missing.
                leads.append(
                    {
                        "kind": "reason",
                        "key": "capture",
                        "payload": {"pass": reading_pass + 1},
                        "depth": task["depth"],
                        "priority": 100,
                        "reason": "reread unresolved fields",
                    }
                )
            if self.settings.search_url and spec.mode == "deep_research":
                for query in decision.queries[:5]:
                    query = query.strip()[:1000]
                    if query:
                        leads.append(
                            {
                                "kind": "search",
                                "key": query,
                                "payload": {"query": query},
                                "depth": task["depth"] + 1,
                                "priority": 35,
                                "reason": "research gap or contradiction",
                            }
                        )
            self.store.finish(
                task, extraction=result, capture_id=cap["id"], leads=leads, details=details
            )
            return
        fetcher = self.fetcher_factory(self.store, self.settings, task)
        try:
            if task["kind"] == "search":
                from .models import Lead

                url = (
                    self.settings.search_url.rstrip("/")
                    + "/search?"
                    + urlencode({"q": task["payload"]["query"], "format": "json"})
                )
                response = fetcher.raw_get(url, service=True)
                if response.status != 200:
                    raise ValueError(f"search returned HTTP {response.status}")
                data = json.loads(response.body)
                found = [
                    Lead(
                        url=r["url"], reason="search: " + str(r.get("title", ""))[:200], priority=25
                    )
                    for r in data.get("results", [])[:50]
                    if isinstance(r, dict) and isinstance(r.get("url"), str)
                ]
                leads = self.select_leads(task, found, search=True)
                self.store.finish(
                    task,
                    response=response,
                    leads=leads,
                    details={
                        "query": task["payload"]["query"],
                        "search_results": len(found),
                        "accepted_leads": len(leads),
                    },
                )
                return
            response = fetcher.fetch(task["payload"]["url"])
            # The extraction task and evidence become durable together, before any parser runs.
            self.store.finish(
                task,
                response=response,
                leads=[
                    {
                        "kind": "extract",
                        "key": "capture",
                        "payload": {},
                        "depth": task["depth"],
                        "priority": 100,
                        "reason": "interpret captured evidence",
                    }
                ],
                details={"acquired": True},
            )
        finally:
            fetcher.close()

    def step(self, job_id=None):
        self.store.expire_deadlines(job_id)
        if job_id is None or self.store.job(job_id)["execution"] != "offline_replay":
            self.store.schedule_tick()
        task = self.store.claim(job_id)
        if task is None:
            self.store.settle(job_id)
            return False
        stop_heartbeat = threading.Event()

        def heartbeat():
            while not stop_heartbeat.wait(15):
                try:
                    self.store.heartbeat(task)
                except LostLease:
                    return
                except Exception:
                    log.exception("heartbeat_failed", extra={"task_id": task["id"]})
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self.process(task)
        except LostLease:
            log.info("lease_lost task=%s", task["id"])
        except BudgetExceeded as exc:
            self.store.stop(task["job_id"], "budget_exhausted", str(exc))
        except PolicyDenied as exc:
            self._if_owned(lambda exc=exc: self.store.fail(task, exc, "blocked"))
        except RetryLater as exc:
            self._if_owned(
                lambda exc=exc: self.store.defer(
                    task, exc, max(exc.delay, 2 ** (task["attempts"] - 1)) + random.uniform(0, 0.5)
                )
            )
        except (httpx.HTTPError, httpcore.NetworkError, httpcore.TimeoutException, OSError) as exc:
            # Never persist provider URLs/headers or secrets from exception reprs.
            self._if_owned(
                lambda exc=exc: self.store.defer(
                    task, type(exc).__name__, min(60, 2 ** task["attempts"]) + random.uniform(0, 1)
                )
            )
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            self._if_owned(
                lambda exc=exc: self.store.fail(
                    task, type(exc).__name__ + ": invalid source or adapter result"
                )
            )
        except Exception:
            log.exception("unexpected_task_error task=%s", task["id"])
            self._if_owned(
                lambda: self.store.fail(task, "unexpected adapter failure; inspect worker log")
            )
        finally:
            stop_heartbeat.set()
            thread.join(timeout=1)
        job = self.store.job(task["job_id"])
        spec = JobSpec.model_validate(job["spec"])
        if (
            spec.mode == "deep_research"
            and job["status"] in ACTIVE
            and job["no_gain"] >= spec.limits.no_gain_pages
        ):
            with self.store.connection() as db:
                pending_reason = db.execute(
                    "SELECT count(*) FROM tasks WHERE job_id=? AND kind='reason' AND status IN ('pending','running')",
                    (task["job_id"],),
                ).fetchone()[0]
            if not pending_reason:
                self.store.stop(
                    task["job_id"],
                    "plateau",
                    "consecutive acquisitions yielded no novel observations",
                )
        self.store.settle(task["job_id"])
        log.info("task_processed job=%s task=%s", task["job_id"], task["id"])
        return True

    @staticmethod
    def _if_owned(action):
        try:
            action()
        except LostLease:
            pass

    def run(self, job_id, poll=0.2):
        while self.store.job(job_id)["status"] in ACTIVE:
            if not self.step(job_id):
                time.sleep(poll)
        return self.store.job(job_id)

    def worker(self, stop=None, once=False):
        stop = stop or threading.Event()
        while not stop.is_set():
            worked = self.step()
            if once:
                return
            if not worked:
                stop.wait(0.5)
