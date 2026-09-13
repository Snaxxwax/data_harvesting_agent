import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    ca_bundle: str | None = field(default_factory=lambda: os.getenv("HARVEST_CA_BUNDLE") or None)
    database: str = field(default_factory=lambda: os.getenv("HARVEST_DB", "data/harvest.sqlite"))
    api_token: str = field(default_factory=lambda: os.getenv("HARVEST_API_TOKEN", ""))
    user_agent: str = field(
        default_factory=lambda: os.getenv("HARVEST_USER_AGENT", "HarvestPlatform/0.3")
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
