import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    ca_bundle: str | None = field(default_factory=lambda: os.getenv("HARVEST_CA_BUNDLE") or None)
    database: str = field(default_factory=lambda: os.getenv("HARVEST_DB", "data/harvest.sqlite"))
    api_token: str = field(default_factory=lambda: os.getenv("HARVEST_API_TOKEN", ""))
    user_agent: str = field(
        default_factory=lambda: os.getenv("HARVEST_USER_AGENT", "HarvestPlatform/0.5")
    )
    private_hosts: frozenset[str] = field(
        default_factory=lambda: frozenset(
            h.strip().lower()
            for h in os.getenv("HARVEST_PRIVATE_HOSTS", "").split(",")
            if h.strip()
        )
    )
    proxy: str | None = field(default_factory=lambda: os.getenv("HARVEST_EGRESS_PROXY") or None)
    # "direct" (the default) keeps the historical behaviour: no proxy unless one is
    # configured, and tools reach the internet straight from the host. "proxy" is
    # fail-closed -- every path that cannot be *shown* to route through
    # HARVEST_EGRESS_PROXY is refused rather than quietly sent out directly.
    egress_mode: str = field(
        default_factory=lambda: os.getenv("HARVEST_EGRESS_MODE", "direct").strip().lower()
    )
    # Canary for proxy mode: a public address that must NOT be reachable directly.
    # Maigret's activation helpers ignore --proxy upstream, so the application alone
    # cannot promise proxy-only egress; this probe tests the host's egress policy
    # instead of trusting an operator flag that says a firewall exists.
    egress_probe: str = field(
        default_factory=lambda: os.getenv("HARVEST_EGRESS_PROBE", "1.1.1.1:443").strip()
    )
    proxy_public_hosts: frozenset[str] = field(
        default_factory=lambda: frozenset(
            h.strip().lower()
            for h in os.getenv("HARVEST_PROXY_PUBLIC_HOSTS", "").split(",")
            if h.strip()
        )
    )
    search_url: str = field(default_factory=lambda: os.getenv("HARVEST_SEARCH_URL", ""))
    # External OSINT CLIs are opt-in per deployment: they make their own network requests
    # outside Fetcher, so no allowlist entry means no tool may run.
    tools: frozenset[str] = field(
        default_factory=lambda: frozenset(
            t.strip().lower() for t in os.getenv("HARVEST_TOOLS", "").split(",") if t.strip()
        )
    )
    # Worker threads in one `harvest worker` process. A job still runs one task at a time;
    # more threads stop one job's long tool scan from stalling every other job.
    worker_threads: int = field(
        default_factory=lambda: max(1, int(os.getenv("HARVEST_WORKER_THREADS", "1")))
    )
    # Global ceiling plus per-tool ceilings. The investigation's remaining wall clock is
    # still the final bound in Engine.process. The global default is high enough for Thorough;
    # deployments may lower it deliberately without changing the individual defaults.
    tool_timeout: float = field(
        default_factory=lambda: float(os.getenv("HARVEST_TOOL_TIMEOUT", "1800"))
    )
    ghunt_timeout: float = field(
        default_factory=lambda: float(os.getenv("HARVEST_GHUNT_TIMEOUT", "120"))
    )
    spiderfoot_timeout: float = field(
        default_factory=lambda: float(os.getenv("HARVEST_SPIDERFOOT_TIMEOUT", "900"))
    )
    maigret_timeout: float = field(
        default_factory=lambda: float(os.getenv("HARVEST_MAIGRET_TIMEOUT", "1800"))
    )
    # Outbound connections this deployment may hold open at once, across every tool and
    # every worker thread. The egress relay enforces it for everything that leaves
    # (tinyproxy MaxClients, from the same variable), queueing the excess instead of letting
    # it reach the upstream proxy, whose concurrency cap is per ACCOUNT. Harvest also splits it
    # between worker threads for Maigret's own -n, so Maigret queues in its scheduler, where a
    # waiting check's timeout has not started, rather than in the relay's accept backlog,
    # where it has.
    egress_max_connections: int = field(
        default_factory=lambda: int(os.getenv("HARVEST_EGRESS_MAX_CONNECTIONS", "128"))
    )
    # Maigret retries failed site checks inside one tool invocation. This is separate
    # from retrying the entire durable task, which would rerun the full scan. One pass by
    # default: a throttled check (proxy 429, site "Rate limited", timeout) is a temporary
    # error to Maigret, and without a retry pass it is silently reported as not-found. The
    # retry pass runs after the main pass drains, so it is also the backoff.
    maigret_retries: int = field(
        default_factory=lambda: int(os.getenv("HARVEST_MAIGRET_RETRIES", "1"))
    )
    maigret_cloudflare_bypass: bool = field(
        default_factory=lambda: (
            os.getenv("HARVEST_MAIGRET_CLOUDFLARE_BYPASS", "").lower() in {"1", "true", "yes"}
        )
    )
    # SpiderFoot NG REST API. Reached over a private network (Tailscale/Docker), never
    # the public internet: the key is a bearer credential sent on every call.
    spiderfoot_url: str = field(
        default_factory=lambda: os.getenv("HARVEST_SPIDERFOOT_URL", "").rstrip("/")
    )
    spiderfoot_api_key: str = field(
        default_factory=lambda: os.getenv("HARVEST_SPIDERFOOT_API_KEY", "")
    )
    # Default to one passive resolver. SpiderFoot's full module set is active
    # reconnaissance against the target, which is an authorization decision, so the
    # deployment must opt into it explicitly.
    spiderfoot_modules: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            m.strip()
            for m in os.getenv("HARVEST_SPIDERFOOT_MODULES", "sfp_dnsresolve").split(",")
            if m.strip()
        )
    )
    # Modules flagged `apikey` whose key the operator has configured INSIDE SpiderFoot. Harvest
    # cannot read SpiderFoot's module config reliably (see spiderfoot_egress below for why
    # its config API is not trustworthy on 6.1.0), so this is a declaration. A keyed module
    # not declared here is left out of the scan and reported as unavailable, unless it has
    # been observed producing events without a key (tools._SF_TESTED).
    spiderfoot_keyed_modules: frozenset[str] = field(
        default_factory=lambda: frozenset(
            m.strip()
            for m in os.getenv("HARVEST_SPIDERFOOT_KEYED_MODULES", "").split(",")
            if m.strip()
        )
    )
    # How SpiderFoot's OWN egress is routed, which this process cannot observe: its modules
    # run in a different container, so neither Harvest's proxy nor its egress probe covers
    # them. "direct" (the default) means unproxied, and proxy-only mode then refuses to run
    # the tool at all. "proxy-env" declares that the scanner container carries HTTP(S)_PROXY
    # pointing at HARVEST_EGRESS_PROXY.
    #
    # This is an operator declaration, which is weaker than a measurement, and it is a
    # declaration only because every measurable alternative was tried and does not work on
    # SpiderFoot NG 6.1.0:
    #   * its own `_socks*` global proxy IS persisted to Postgres by save_config(), but
    #     nothing reloads it at startup, so GET /api/v1/config always reports the hardcoded
    #     "" defaults -- and with two uvicorn workers a read right after a PATCH answers
    #     from whichever worker handles it, so the value read back is nondeterministic.
    #   * the scanner ignores it regardless: measured with an outbound-module scan, relay
    #     throughput was 0 bytes while the module reached its target, i.e. direct egress.
    # So asserting against that API was asserting against a value with no bearing on where
    # the packets go. verify-deployment.sh asserts the container-level truth instead: the env
    # var, the network attachment, and the scanner's actual exit IP.
    spiderfoot_egress: str = field(
        default_factory=lambda: os.getenv("HARVEST_SPIDERFOOT_EGRESS", "direct").strip().lower()
    )
    model_url: str = field(default_factory=lambda: os.getenv("HARVEST_MODEL_URL", ""))
    model_name: str = field(default_factory=lambda: os.getenv("HARVEST_MODEL_NAME", ""))
    model_key: str = field(default_factory=lambda: os.getenv("HARVEST_MODEL_KEY", ""))
    # Operator must supply a conservative upper bound covering input/output and reasoning tokens.
    model_usd_per_million: float | None = field(
        default_factory=lambda: (
            float(os.environ["HARVEST_MODEL_USD_PER_MILLION"])
            if "HARVEST_MODEL_USD_PER_MILLION" in os.environ
            else None
        )
    )
    model_output_tokens: int = 2000
    request_timeout: float = 20

    def __post_init__(self) -> None:
        if self.egress_mode not in {"direct", "proxy"}:
            raise ValueError("HARVEST_EGRESS_MODE must be 'direct' or 'proxy'")
        if self.egress_mode == "proxy" and not self.proxy:
            raise ValueError("HARVEST_EGRESS_MODE=proxy requires HARVEST_EGRESS_PROXY")
        # 500 is Webshare's standard per-account cap; above it the relay cannot protect it.
        if not 1 <= self.egress_max_connections <= 500:
            raise ValueError("HARVEST_EGRESS_MAX_CONNECTIONS must be between 1 and 500")
        if self.proxy_only:
            host, _, port = self.egress_probe.rpartition(":")
            if not host or not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError("HARVEST_EGRESS_PROBE must be host:port")

    def tool_timeout_for(self, name: str) -> float:
        specific = {
            "ghunt": self.ghunt_timeout,
            "spiderfoot": self.spiderfoot_timeout,
            "maigret": self.maigret_timeout,
        }.get(name, self.tool_timeout)
        return min(self.tool_timeout, specific)

    @property
    def proxy_only(self) -> bool:
        """True when no path may fall back to direct egress."""
        return self.egress_mode == "proxy"
