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

from . import tools
from .config import Settings
from .extract import Extractors, http_url
from .models import (
    ActionableError,
    AuthorizationRequired,
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
from .tools import TOOLS
from .tools import run as run_tool

log = logging.getLogger("harvest")
ACTIVE = {"queued", "running"}
TOOL_FINISH_MARGIN = 15  # seconds a tool run leaves before the deadline to save its results
TOOL_LEASE = 30  # seconds; with SWEEP_SECONDS, bounds a dead worker's orphaned scan to ~45 s
SWEEP_SECONDS = 15

# ponytail: name heuristics, not a site-specific map. Misses an unconventional sign-in path;
# add a pattern when a capture shows one.
_AUTH_SEGMENT = re.compile(
    r"(?i)^(?:(?:log|sign)[-_]?(?:in|on|up|out)\w*|register|registration|join|auth|oauth2?"
    r"|sso|password|forgot\w*|reset\w*|request(?:password|username))$"
)
_AUTH_TEXT = re.compile(
    r"(?i)\b(?:log ?in|log ?out|sign ?(?:in|up|on|out)|forgot|password|create (?:an )?account)\b"
)


def auth_like(url: str, text: str = "") -> bool:
    """A sign-in/sign-up/password page, by path segment or link text. Never evidence about a
    subject, and following one loops through returnUrl/OAuth redirects."""
    segments = [seg for seg in urlsplit(url).path.split("/") if seg]
    return any(_AUTH_SEGMENT.match(seg) for seg in segments) or bool(_AUTH_TEXT.search(text))


# ponytail: markup heuristics, not a per-site parser. Where a page states the handle in a
# profile position is where it can be shown; extend the patterns when a capture needs it.
_ID_KEYS = "username|uniqueid|unique_id|login|screen_name|screenname|handle|user_name|nickname|slug"
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_HEADING = re.compile(r"(?is)<h1[^>]*>(.*?)</h1>")
_META_TITLE = re.compile(
    r"""(?is)<meta[^>]+(?:property|name)=["'](?:og:title|twitter:title|profile:username)["'][^>]*>"""
)
_NOT_PROFILE = re.compile(
    r"(?i)\b(?:not found|page not found|404|doesn'?t exist|does not exist|no such user|"
    r"user not found|search|results for|suspended|unavailable|sign ?in|log ?in)\b"
)
_BLOCKED = re.compile(
    r"(?i)just a moment|attention required|access denied|captcha|are you a robot|"
    r"verify you are human|enable javascript|javascript is (?:disabled|required)"
)


def _visible_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    return " ".join(re.sub(r"(?s)<[^>]+>", " ", html).split())


def profile_evidence(html: str, identifier: str):
    """(start, end) of the identifier where the page states it AS the profile's identity:
    the value of an identity key in embedded data (`"uniqueId":"<h>"`), or a whole word in
    the <title>, first <h1> or og/twitter title -- and only when those do not say the page
    is a not-found, search or sign-in page. A mention elsewhere (body text, an echoed
    `?next=`/`returnUrl=`) is not evidence of a profile."""
    needle = re.escape(identifier)
    data = re.search(rf'(?i)"(?:{_ID_KEYS})"\s*:\s*"(@?{needle})"', html)
    if data:
        return data.span(1)
    title = _TITLE.search(html)
    if title and _NOT_PROFILE.search(_visible_text(title.group(1))):
        return None  # the page names itself as a not-found, search or sign-in page
    for pattern in (_TITLE, _HEADING, _META_TITLE):
        if not (match := pattern.search(html)):
            continue  # only the FIRST title / h1 / meta title states what the page is
        if pattern is _HEADING and _NOT_PROFILE.search(_visible_text(match.group(1))):
            return None
        if word := re.search(rf"(?i)(?<![\w/=%.-])@?{needle}(?![\w/=%-])", match.group(0)):
            return match.start() + word.start(), match.start() + word.end()
    return None


def page_check(identifiers, requested_url, final_url, text, duplicate_of=None):
    """What a fetched page says about a searched identifier: verdict, evidence, locator.

    Only `profile_evidence` (the page states the identifier as its identity, see
    profile_evidence()) counts as a verified profile page and is crawled further. Every
    other verdict is unverified, and says why: a redirect away, a sign-in wall, a
    byte-identical page under another URL (archived or catch-all site), a blocked or
    JavaScript-only page whose content cannot show a profile, a handle merely mentioned in
    text, or no mention at all."""
    if duplicate_of is not None:
        return "duplicate_content", f"capture {duplicate_of}", f"capture:{duplicate_of}"
    requested, final = urlsplit(requested_url).path, urlsplit(final_url).path
    if requested.rstrip("/") != final.rstrip("/") and not any(
        i.lower() in final.lower() for i in identifiers
    ):
        return "redirected_away", final_url, "final_url"
    if auth_like(final_url):
        return "login_wall", final_url, "final_url"
    for identifier in identifiers:
        if span := profile_evidence(text, identifier):
            return "profile_evidence", text[span[0] : span[1]], f"chars:{span[0]}-{span[1]}"
    visible = _visible_text(text)
    if blocked := _BLOCKED.search(visible[:5000]):
        return "unverified_blocked_or_script", blocked.group(0), "visible_text"
    if len(visible) < 200 and re.search(r"(?i)<script\b", text):
        # Little rendered text but scripts: a JavaScript app shell. Its content (and so the
        # profile, or its absence) only exists after rendering, which Harvest does not do.
        return "unverified_blocked_or_script", final_url, "final_url"
    if any(identifier.lower() in visible.lower() for identifier in identifiers):
        return "unverified_mention_only", final_url, "final_url"
    return "unverified_no_identifier", final_url, "final_url"


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

    def submit(self, spec: JobSpec, key=None, parent=None, root=None):
        if spec.use_model and not self.reasoner.configured():
            raise ValueError(
                "use_model requires HARVEST_MODEL_URL, HARVEST_MODEL_NAME and HARVEST_MODEL_USD_PER_MILLION"
            )
        seeds = spec.seeds or [
            x.rstrip(".,;)") for x in re.findall(r"https?://[^\s<>]+", spec.objective)
        ]
        if len(spec.tools) > spec.limits.tool_runs:
            raise ValueError(
                f"{len(spec.tools)} tool runs exceeds limits.tool_runs={spec.limits.tool_runs}"
            )
        initial = []
        for tool in spec.tools:
            if tool.name not in TOOLS:
                raise ValueError(f"unknown tool {tool.name!r}")
            if tool.name not in self.settings.tools:
                raise ValueError(f"tool {tool.name!r} is not enabled; add it to HARVEST_TOOLS")
            initial.append(
                {
                    "kind": "tool",
                    "key": f"{tool.name}:{tool.target}",
                    "payload": {
                        "tool": tool.name,
                        "target": tool.target,
                        "crawl": tool.crawl,
                        "top_sites": tool.top_sites,
                    },
                }
            )
        for url in seeds:
            url = canonical_url(url)
            if not in_scope(url, spec):
                raise ValueError("seed is outside allowed domains")
            initial.append({"kind": "fetch", "key": url, "payload": {"url": url}})
        # An explicit bounded query set feeds discovery when present; otherwise the whole
        # objective is the one implicit query, exactly as before this field existed.
        queries = list(spec.discovery_queries)
        if not initial and not queries:
            queries = [spec.objective]
        if queries:
            if not self.settings.search_url:
                if not initial:
                    raise ValueError(
                        "provide a seed URL or configure HARVEST_SEARCH_URL for discovery from an objective"
                    )
                # Seeds alone are enough to proceed; discovery is silently skipped, not an error.
            else:
                for query in queries:
                    initial.append({"kind": "search", "key": query, "payload": {"query": query}})
        if not initial:
            raise ValueError(
                "provide a seed URL or configure HARVEST_SEARCH_URL for discovery from an objective"
            )
        return self.store.create(spec, initial, key, parent=parent, root=root)

    def follow_up(self, parent_id, *, url=None, tool=None, target=None, key=None):
        """Start a child job that INHERITS the parent investigation's authorization.

        This is the agent-facing expansion control, and the whole point is that it cannot
        widen what the operator authorized. The child inherits the parent's scope
        (allowed_domains), budget ceilings (limits), dataset, declared targets and model
        permission; the agent chooses only WHICH discovered thing to pursue, not new scope.

        Two material-exceedance guards, both content-independent (they never trust anything a
        fetched page said):
          * scope -- a URL must be in the parent's allowed_domains; a tool must be enabled.
          * provenance -- the URL or identifier must already appear in the PARENT's own
            evidence (Store.discovered). An agent can follow a lead the investigation found;
            it cannot inject an unrelated person and call it a follow-up. "Discovery alone
            must not authorize unrestricted expansion into unrelated people."

        Anything outside those raises AuthorizationRequired, which the agent interface turns
        into an "ask the operator" result rather than silently doing it.

        Budget is investigation-wide, not per child: every follow-up records the ROOT job of
        its investigation, and Store.reserve checks the SUM of requests/bytes/model spend over
        the root and all descendants against the root's limits (as are tool_runs, tasks and
        the wall clock, in Store._admit_child). Siblings running concurrently and chains of
        follow-ups-of-follow-ups therefore share one balance; a retried follow-up with the same
        key returns the same child, and one without a key still spends from the same balance.
        A child whose investigation is exhausted is refused with BudgetExceeded.
        """
        parent = self.store.job(parent_id)
        root = parent.get("root_id") or parent_id
        # Idempotency keys are global in the jobs table; a follow-up's key means "this request
        # within THIS investigation". Unscoped, two investigations retrying the same URL as
        # their key got each other's child job back.
        key = f"followup:{root}:{key}" if key else None
        pspec = JobSpec.model_validate(parent["spec"])
        if bool(url) == bool(tool):
            raise ValueError("follow_up takes exactly one of url= or tool=")

        if url:
            canon = canonical_url(url)
            if not in_scope(canon, pspec):
                raise AuthorizationRequired(
                    f"{urlsplit(canon).hostname} is outside this investigation's authorized "
                    f"scope (allowed_domains={pspec.allowed_domains or 'any'})",
                    suggestion=f"authorize a new investigation for {urlsplit(canon).hostname}",
                )
            if not self.store.discovered(parent_id, canon):
                raise AuthorizationRequired(
                    "that URL was not discovered by this investigation, so following it would "
                    "expand beyond the original request",
                    suggestion=f"start a new investigation with seed {canon}",
                )
            child = JobSpec.model_validate(
                {
                    **pspec.model_dump(),
                    "objective": f"follow-up on discovered source {canon}",
                    "mode": "targeted",
                    "seeds": [canon],
                    "discovery_queries": [],
                    "tools": [],
                    "refresh_seconds": None,
                }
            )
            return self.submit(child, key=key, parent=parent_id, root=root)

        if tool not in self.settings.tools:
            raise AuthorizationRequired(
                f"tool {tool!r} is not enabled on this deployment",
                suggestion=f"add {tool} to HARVEST_TOOLS, then re-request",
            )
        if not target:
            raise ValueError("a tool follow-up needs a target")
        if not self.store.discovered(parent_id, target):
            raise AuthorizationRequired(
                f"the identifier {target!r} was not discovered by this investigation, so "
                "scanning it would expand into a target the operator did not authorize",
                suggestion=f"authorize an investigation whose target is {target!r}",
            )
        child = JobSpec.model_validate(
            {
                **pspec.model_dump(),
                "objective": f"follow-up {tool} scan of discovered identifier",
                "mode": "targeted",
                "seeds": [],
                "discovery_queries": [],
                "tools": [{"name": tool, "target": target}],
                "refresh_seconds": None,
            }
        )
        return self.submit(child, key=key, parent=parent_id, root=root)

    def rerun(self, job_id):
        """Resubmit a prior job's exact spec as a new durable job; never mutates old history.

        Rerunning a follow-up stays inside its investigation (same root, same shared balance),
        so rerun is not a way to mint a fresh budget for an exhausted investigation. Rerunning
        a root job is the operator re-authorizing the whole investigation and starts anew.
        """
        job = self.store.job(job_id)
        spec = JobSpec.model_validate(job["spec"])
        if spec.mode == "continuous":
            raise ValueError(
                "continuous jobs refresh automatically through their schedule; "
                "disable the schedule instead of rerunning, to avoid a duplicate schedule"
            )
        return self.submit(
            spec, parent=job["parent_id"] if job.get("root_id") else None, root=job.get("root_id")
        )

    def context(self, job_id):
        observations = self.store.observations(job_id, limit=40)
        job = self.store.job(job_id)
        with self.store.connection() as db:
            visited = [
                r[0]
                for r in db.execute(
                    "SELECT key FROM tasks WHERE job_id=? AND kind IN ('fetch','search','tool') ORDER BY id DESC LIMIT 50",
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
        result = {
            "observations": values,
            "visited_or_queued": visited,
            "missing_fields": job["missing_fields"],
            "previous_decisions": decisions[:2],
        }
        spec = JobSpec.model_validate(job["spec"])
        if spec.investigation and spec.investigation.targets:
            # Bounded key/label only: identity reconciliation stays deterministic and
            # out-of-band, never delegated to the model's own judgment.
            result["declared_targets"] = [
                {"key": t.key, "label": t.label} for t in spec.investigation.targets[:20]
            ]
        return result

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
        crawl = task["payload"].get("crawl", True)
        verdict = self.check_page(task, cap, extraction) if batch is None else None
        if verdict not in (None, "profile_evidence"):
            crawl = False  # a generic page's links are the site's, not the subject's
        accepted = [] if offline or not crawl else self.select_leads(task, extraction.leads)
        # Every lead the extraction produced that did not become a task: already seen, out of
        # scope, over the depth limit, deduplicated, past the per-task cap, or not crawled at
        # all. Previously this was reported as 0 for every online task, so a job that threw
        # away most of its leads looked like one that had none.
        suppressed = len(extraction.leads) - len(accepted)
        leads = list(accepted)
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
                "accepted_leads": len(accepted),
                "offline": offline,
                "crawl": crawl,
                "suppressed_leads": suppressed,
                **({"page_check": verdict} if verdict else {}),
            },
        )

    def identifiers(self, task):
        """The identifiers this investigation searched for: tool targets and declared target
        identifiers, of this job and of its root (a follow-up's own spec carries no tools)."""
        specs = [JobSpec.model_validate_json(task["spec"])]
        root = self.store.job(task["job_id"]).get("root_id")
        if root:
            specs.append(JobSpec.model_validate(self.store.job(root)["spec"]))
        found = set()
        for spec in specs:
            found.update(t.target for t in spec.tools)
            for target in spec.investigation.targets if spec.investigation else ():
                found.update(v for values in target.identifiers.values() for v in values)
        # The local part is how a handle shows up on a profile page for an email target.
        return sorted(
            {i.split("@")[0] if "@" in i[1:] else i.lstrip("@") for i in found if len(i) >= 3}
        )

    def check_page(self, task, cap, extraction):
        """Record a page_check claim for a fetched page in an identifier investigation, and
        return its verdict. Byte-identical pages under another URL in the same job are
        duplicates in any job: their links were already offered once."""
        if not cap["url"].startswith(("http://", "https://")):
            return None
        with self.store.connection() as db:
            row = db.execute(
                "SELECT min(id) FROM captures WHERE job_id=? AND body_hash=? AND id<? AND url<>?",
                (task["job_id"], cap["body_hash"], cap["id"], cap["url"]),
            ).fetchone()
        duplicate_of = row[0] if row else None
        identifiers = self.identifiers(task)
        if not identifiers:
            return "duplicate_content" if duplicate_of else None
        from .models import Claim

        verdict, evidence, locator = page_check(
            identifiers,
            cap["url"],
            cap["final_url"],
            cap["body"].decode("utf-8", errors="replace"),
            duplicate_of,
        )
        extraction.claims.append(
            Claim(
                entity_key="url:" + canonical_url(cap["url"]),
                field="page_check",
                value=verdict,
                evidence=evidence[:20000],
                locator=locator,
                method="derived:page-check/1",
                confidence=1.0 if verdict == "profile_evidence" else 0.9,
            )
        )
        return verdict

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
            if lead.reason.startswith("link:") and auth_like(url, lead.reason[5:]):
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

    def process(self, task, cancelled: threading.Event | None = None):
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
        if task["kind"] == "tool":
            # Bounded by the job/investigation wall clock as well as HARVEST_TOOL_TIMEOUT: a
            # scan started with 4 minutes left used to run its full 10, past the deadline.
            # The run must END before the deadline, with time left to read its results and
            # record the capture: at the deadline itself any worker's expire_deadlines stops
            # the job, revoking this task's lease, and a partial result was then discarded.
            left = self.store.seconds_left(task) - TOOL_FINISH_MARGIN
            if left < 5:
                raise BudgetExceeded("wall-clock deadline reached before the tool could run")
            allowance = min(self.settings.tool_timeout, left)
            with self.store.transaction() as db:
                # One task per job runs at a time, so a long scan is otherwise a silent gap.
                self.store.event(
                    db,
                    task["job_id"],
                    "tool_started",
                    {
                        "task": task["id"],
                        "tool": task["payload"]["tool"],
                        "max_seconds": round(allowance),
                    },
                )
            cap = run_tool(
                task["payload"]["tool"],
                task["payload"]["target"],
                self.settings,
                cancelled,
                top_sites=task["payload"].get("top_sites"),
                timeout=allowance,
            )
            partial = json.loads(cap.body).get("partial")
            # `crawl` rides on the extract task because that is where leads are selected.
            # A tool run is useful without it: the capture and its claims are the evidence,
            # while crawling every profile it reports is a separate and much larger request
            # budget than running the tool once.
            crawl = task["payload"].get("crawl", True)
            self.store.finish(
                task,
                response=cap,
                leads=[
                    {
                        "kind": "extract",
                        "key": "capture",
                        "payload": {} if crawl else {"crawl": False},
                        "depth": task["depth"],
                        "priority": 100,
                        "reason": "interpret tool output",
                    }
                ],
                details={
                    "tool": task["payload"]["tool"],
                    "acquired": True,
                    "crawl": crawl,
                    **({"partial": partial} if partial else {}),
                },
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
                    for r in data.get("results", [])[: spec.limits.search_results]
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
        cancelled = threading.Event()

        def heartbeat():
            while not stop_heartbeat.wait(1 if task["kind"] == "tool" else 15):
                try:
                    # A tool run heartbeats every second, so a short lease costs nothing and
                    # bounds how long a dead worker's scan runs on before the sweep stops it.
                    self.store.heartbeat(task, TOOL_LEASE if task["kind"] == "tool" else 120)
                except LostLease:
                    cancelled.set()
                    return
                except Exception:
                    log.exception("heartbeat_failed", extra={"task_id": task["id"]})
                    cancelled.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self.process(task, cancelled)
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
        except ActionableError as exc:
            self._if_owned(lambda exc=exc: self.store.fail(task, str(exc)[:500]))
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

    def recover_external_scans(self):
        """Stop SpiderFoot scans orphaned by a dead worker, when no live task owns one."""
        if "spiderfoot" not in self.settings.tools or not self.settings.spiderfoot_url:
            return 0
        with self.store.connection() as db:
            live = db.execute(
                """SELECT count(*) FROM tasks WHERE kind='tool' AND status='running'
                AND lease_until>? AND payload LIKE '%"tool":"spiderfoot"%'""",
                (time.time(),),
            ).fetchone()[0]
        if live:
            return 0
        try:
            stopped = tools.stop_orphaned_spiderfoot_scans(self.settings)
        except httpx.HTTPError as exc:
            log.warning("spiderfoot orphan sweep failed: %s", type(exc).__name__)
            return 0
        if stopped:
            log.warning("stopped %d orphaned spiderfoot scan(s)", stopped)
        return stopped

    def worker(self, stop=None, once=False):
        stop = stop or threading.Event()
        next_sweep = 0.0
        while not stop.is_set():
            if time.monotonic() >= next_sweep:
                next_sweep = time.monotonic() + SWEEP_SECONDS
                self.recover_external_scans()
            worked = self.step()
            if once:
                return
            if not worked:
                stop.wait(0.5)
