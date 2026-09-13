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
from .models import BudgetExceeded, JobSpec, LostLease, PolicyDenied, RetryLater, canonical_url
from .network import Fetcher, in_scope
from .reasoning import Reasoner
from .store import Store

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
        present = {r["field"] for r in observations}
        return {
            "observations": values,
            "visited_or_queued": visited,
            "missing_fields": [f for f in job["spec"]["fields"] if f not in present],
            "previous_decisions": decisions[:2],
        }

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
        if task["kind"] == "reason":
            cap = self.store.capture(task["payload"]["capture_id"])
            extraction = self.extractors.extract(
                cap["body"], cap["final_url"], json.loads(cap["headers"]).get("content-type", "")
            )
            result, decision, details = self.reasoner.decide(
                task, cap["final_url"], extraction.text, self.context(task["job_id"])
            )
            leads = self.select_leads(task, decision.leads)
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
            extraction = self.extractors.extract(
                response.body, response.final_url, response.headers.get("content-type", "")
            )
            # RFC Link pagination is complementary to body pagination.
            link_header = response.headers.get("link", "")
            for item in link_header.split(","):
                match = re.search(r'<([^>]+)>;\s*rel="?next"?', item)
                if match:
                    from .models import Lead

                    if candidate := http_url(match.group(1), response.final_url):
                        extraction.leads.insert(
                            0, Lead(url=candidate, reason="pagination", priority=50)
                        )
            leads = self.select_leads(task, extraction.leads)
            found_fields = {c.field for c in extraction.claims}
            needs_reason = (
                not extraction.claims
                or bool(set(spec.fields) - found_fields)
                or spec.mode == "deep_research"
            )
            if spec.use_model and needs_reason and extraction.text:
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
                response=response,
                extraction=extraction,
                leads=leads,
                details={"extracted_claims": len(extraction.claims), "accepted_leads": len(leads)},
            )
        finally:
            fetcher.close()

    def step(self, job_id=None):
        self.store.expire_deadlines(job_id)
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
