"""What Harvest can actually do, per deployment, as data a planner or an agent can read.

A capability is one way to turn an input into evidence: a built-in acquisition (fetch a URL,
crawl within scope, discover via search) or an external tool (maigret, ghunt, spiderfoot).
Each entry declares the input kinds it accepts, the evidence it produces, the settings and
binaries it needs, and a short cost/runtime note. `readiness()` adds the one thing that
cannot be static -- whether this deployment can run it right now, and if not, the single
reason why.

The point is honesty: every failure mode these features have (a tool sitting in HARVEST_TOOLS
but policy-denied on every run, a discovery job silently skipping an unconfigured search)
looks exactly like "found nothing" unless something says otherwise up front. The planner
proposes only ready capabilities and labels the rest; `/meta` and the agent interface report
the same structure, so all three answer "what can this instance do for this input" the same way.

No I/O: readiness is a pure function of Settings, so it is testable without a database,
network, or any tool installed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    name: str
    # Investigation kinds (planning.INVESTIGATION_TYPES, plus "dataset") this accepts as input.
    kinds: tuple[str, ...]
    evidence: str
    requires: tuple[str, ...]
    cost: str


# Static facts. Readiness is computed per-deployment in readiness() below. Tool kinds are
# taken from tools.TOOLS at call time so there is one source of truth for which identifier
# each tool consumes; the rest (evidence, cost) lives here because it is documentation, not
# behaviour.
_BUILTIN: tuple[Capability, ...] = (
    Capability(
        name="fetch",
        kinds=("url", "domain"),
        evidence="raw HTTP capture + deterministic extraction (metadata, JSON-LD, records)",
        requires=(),
        cost="one request per seed, within the job's request/byte/time budgets",
    ),
    Capability(
        name="crawl",
        kinds=("url", "domain"),
        evidence="scoped follow-up captures from discovered in-scope links",
        requires=("allowed_domains or depth > 0",),
        cost="bounded by limits.depth, limits.tasks and allowed_domains scope",
    ),
    Capability(
        name="discovery_search",
        kinds=("person", "organization", "username", "email", "phone", "address", "identifier"),
        evidence="candidate source URLs from a SearXNG query, then fetched as captures",
        requires=("HARVEST_SEARCH_URL",),
        cost="one search per query; results capped by limits.search_results before leads",
    ),
)

# Evidence/cost notes for the external tools, keyed by tool name. Kinds come from tools.TOOLS.
_TOOL_NOTES: dict[str, dict[str, str]] = {
    "maigret": {
        "evidence": "per-site account presence for a username, with parsed profile fields "
        "where a site exposes them",
        "cost": "a full --all-sites sweep uses every eligible site in the pinned Maigret "
        "database (currently thousands; disabled definitions remain excluded); "
        "ToolRun.top_sites trades breadth for a smaller scan",
    },
    "ghunt": {
        "evidence": "the Google account for an email address (gaia id, profile photo, "
        "account type)",
        "cost": "~100 KiB per lookup; requires an authenticated Google session",
    },
    "spiderfoot": {
        "evidence": "events from the modules that can fire for the input type (see `plans`): "
        "derived accounts, breach/paste hits, DNS and related identifiers",
        "cost": "a full email module sweep is ~250 s and makes its own out-of-budget requests",
    },
}


def _tool_requirements(name: str) -> tuple[str, ...]:
    reqs = {
        "maigret": ("HARVEST_TOOLS contains maigret", "maigret binary in the image"),
        "ghunt": (
            "HARVEST_TOOLS contains ghunt",
            "ghunt binary in the image",
            "an authenticated Google session (creds.m)",
        ),
        "spiderfoot": (
            "HARVEST_TOOLS contains spiderfoot",
            "HARVEST_SPIDERFOOT_URL",
            "HARVEST_SPIDERFOOT_API_KEY",
            "HARVEST_SPIDERFOOT_MODULES",
        ),
    }
    return reqs.get(name, ("HARVEST_TOOLS contains " + name,))


def registry() -> list[Capability]:
    """Every capability this build knows about, built-in first then external tools."""
    from .tools import TOOLS

    caps = list(_BUILTIN)
    for name, tool in TOOLS.items():
        notes = _TOOL_NOTES.get(name, {})
        caps.append(
            Capability(
                name=name,
                kinds=tuple(tool["kinds"]),
                evidence=notes.get("evidence", "tool output as evidence"),
                requires=_tool_requirements(name),
                cost=notes.get("cost", "makes its own out-of-budget network requests"),
            )
        )
    return caps


def _tool_blocker(name: str, settings) -> str:
    """The single reason a tool would refuse to run now, in the order the tool checks them.

    Returns "" when the tool is ready. SpiderFoot has the most ways to be blocked -- it is the
    one that was policy-denied on every run for weeks while looking enabled -- so its egress
    declaration is checked here too, matching tools._spiderfoot's own order.
    """
    if name not in settings.tools:
        return "not in HARVEST_TOOLS"
    if name == "spiderfoot":
        if not settings.spiderfoot_url:
            return "HARVEST_SPIDERFOOT_URL is not set"
        if not settings.spiderfoot_api_key:
            return "HARVEST_SPIDERFOOT_API_KEY is not set"
        if not settings.spiderfoot_modules:
            return "HARVEST_SPIDERFOOT_MODULES is empty, so no scan would run"
        if settings.egress_mode == "proxy" and settings.spiderfoot_egress != "proxy-env":
            return (
                "HARVEST_EGRESS_MODE=proxy but HARVEST_SPIDERFOOT_EGRESS="
                f"{settings.spiderfoot_egress}: its modules run in another container and would "
                "egress directly, so every run is refused"
            )
    # maigret/ghunt readiness beyond the allowlist (binary present, ghunt creds) can only be
    # verified in the worker image, not from Settings; verify-deployment.sh asserts those.
    return ""


def readiness(settings) -> dict[str, dict]:
    """Per-capability {ready, detail, kinds, evidence, cost, requires} for this deployment.

    `detail` is the single reason a capability is not ready, as a sentence, or "" when ready.
    """
    out: dict[str, dict] = {}
    for cap in registry():
        if cap.name == "discovery_search":
            ready = bool(settings.search_url)
            detail = (
                ""
                if ready
                else "HARVEST_SEARCH_URL is not set; discovery from an objective is "
                "unavailable and jobs with seeds skip search silently"
            )
        elif cap.name in ("fetch", "crawl"):
            ready, detail = True, ""
        else:  # an external tool
            detail = _tool_blocker(cap.name, settings)
            ready = not detail
        entry = {
            "ready": ready,
            "detail": detail,
            "kinds": list(cap.kinds),
            "evidence": cap.evidence,
            "cost": cap.cost,
            "requires": list(cap.requires),
        }
        if cap.name == "discovery_search":
            # Ready means configured. Provider health is only known per search, and each
            # job summary reports it (`search`); verify-deployment.sh probes it on deploy.
            entry["basis"] = "configured"
        if cap.name == "spiderfoot":
            from .tools import SPIDERFOOT_KIND_TYPES, spiderfoot_plan

            entry["modules"] = list(settings.spiderfoot_modules)
            entry["egress"] = settings.spiderfoot_egress
            # Per input type: the modules a scan will actually send, what each consumes, the
            # allowlisted ones it will not and why, and what the fork could do but is not enabled.
            entry["plans"] = {
                t: spiderfoot_plan(t, settings) for t in sorted(set(SPIDERFOOT_KIND_TYPES.values()))
            }
        out[cap.name] = entry
    return out


def for_kind(kind: str, settings) -> list[dict]:
    """Ready capabilities that accept this investigation kind, each as a small dict.

    Used by planning to propose only what this deployment can actually run for an input,
    and to disclose what it cannot.
    """
    status = readiness(settings)
    result = []
    for cap in registry():
        if kind not in cap.kinds:
            continue
        s = status[cap.name]
        ready, detail = s["ready"], s["detail"]
        if ready and cap.name == "spiderfoot":
            from .tools import SPIDERFOOT_KIND_TYPES

            plan = s["plans"][SPIDERFOOT_KIND_TYPES[kind]]
            if not plan["modules"]:
                ready = False
                detail = f"no allowlisted SpiderFoot module consumes a {plan['target_type']} target"
        result.append({"name": cap.name, "ready": ready, "detail": detail})
    return result
