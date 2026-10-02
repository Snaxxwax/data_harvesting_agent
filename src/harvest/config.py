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
    tool_timeout: float = field(
        default_factory=lambda: float(os.getenv("HARVEST_TOOL_TIMEOUT", "300"))
    )
    # Maigret retries failed site checks inside one tool invocation. This is separate
    # from retrying the entire durable task, which would rerun the full scan.
    maigret_retries: int = field(
        default_factory=lambda: int(os.getenv("HARVEST_MAIGRET_RETRIES", "0"))
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
