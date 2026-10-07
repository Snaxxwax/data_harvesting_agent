from __future__ import annotations

import ipaddress
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from protego import Protego

from .config import Settings
from .models import JobSpec, PolicyDenied, RetryLater, canonical_url


def resolved_addresses(host: str, port: int, private_hosts=frozenset()):
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(r[4][0] for r in records))
    if not addresses:
        raise PolicyDenied("host has no addresses")
    if host.lower().rstrip(".") not in private_hosts:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or (
                getattr(ip, "ipv4_mapped", None) and not ip.ipv4_mapped.is_global
            ):
                raise PolicyDenied("non-public destination is not explicitly configured")
    return addresses


class PublicBackend(httpcore.SyncBackend):
    def __init__(self, private_hosts=frozenset()):
        self.private_hosts = private_hosts

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # Resolve once, validate the entire result, and connect to that exact IP.
        # TLS still receives the original host through httpcore's server_hostname.
        addresses = resolved_addresses(host, port, self.private_hosts)
        last_error = None
        for address in addresses:
            try:
                return super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error


class CoreStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        yield from self.stream

    def close(self):
        self.stream.close()


class PublicTransport(httpx.BaseTransport):
    """HTTPX/HTTPCore public transport APIs; no global DNS patching."""

    def __init__(self, private_hosts=frozenset(), ca_bundle=None):
        self.pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(cafile=ca_bundle),
            network_backend=PublicBackend(private_hosts),
            max_connections=4,
            retries=0,
        )

    def handle_request(self, request):
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = self.pool.handle_request(req)
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=CoreStream(response.stream),
            extensions=response.extensions,
        )

    def close(self):
        self.pool.close()


@dataclass
class Capture:
    url: str
    final_url: str
    status: int
    headers: dict
    body: bytes
    retrieved: float
    # Tool captures may carry a second, non-normalized structured artifact plus a
    # credential-redacted execution envelope. HTTP fetches leave these unset.
    native_body: bytes | None = None
    tool_meta: dict | None = None


# Names that must never leave through the proxy. In proxy mode the proxy resolves the
# destination, so the resolved address is unknowable here and the shape of the name is
# all that can be checked.
INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".corp",
    ".home.arpa",
)


def require_public_host(hostname: str | None):
    """Reject anything that is not a routable public destination, by name or by literal.

    LIMITATION, by construction: this checks the *shape* of the name, never what it
    resolves to. In proxy mode the proxy performs DNS, so the worker cannot see the
    destination address -- it has no resolver at all (its network is `internal: true`),
    so resolved_addresses(), which is what guards direct mode, cannot run here. A public
    name that resolves to a private address (DNS rebinding, split-horizon DNS, or a
    wildcard service such as `10.0.0.5.nip.io`) therefore PASSES this check.

    What actually contains that case is the egress path, not this function:
      * the worker has no route off-host except the relay (no gateway on its network);
      * the relay, with a blanket `Upstream`, never connects to the destination itself --
        it only opens a socket to the upstream proxy and forwards the request;
      * the upstream proxy refuses private destinations (Webshare answers 403).
    The last of those is a third party's policy, so it is a mitigation and not a
    guarantee. See "Proxy-mode SSRF boundary" in deploy/ovh-vps/DEPLOYMENT.md.
    """
    if not hostname:
        raise PolicyDenied("destination has no host")
    host = hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.endswith(INTERNAL_SUFFIXES) or "." not in host:
            raise PolicyDenied("proxy destination must be a public hostname") from None
    else:
        if not address.is_global or (
            getattr(address, "ipv4_mapped", None) and not address.ipv4_mapped.is_global
        ):
            raise PolicyDenied("proxy destination must be public")


def in_scope(url: str, spec: JobSpec):
    host = urlsplit(canonical_url(url)).hostname
    return not spec.allowed_domains or any(
        host == h or host.endswith("." + h) for h in spec.allowed_domains
    )


def retry_delay(value: str | None) -> float:
    if not value:
        return 1
    try:
        return max(1, min(86400, float(value)))
    except ValueError:
        try:
            return max(1, min(86400, parsedate_to_datetime(value).timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            return 1


def decode_body(body: bytes, encoding: str, limit: int) -> bytes:
    if encoding in ("", "identity"):
        return body
    if encoding not in {"gzip", "deflate"}:
        raise PolicyDenied("unsupported content encoding")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(body, limit + 1)
    except zlib.error as exc:
        raise PolicyDenied("invalid compressed response") from exc
    if len(decoded) > limit or decoder.unconsumed_tail:
        raise PolicyDenied("decoded response size limit reached")
    if not decoder.eof or decoder.unused_data:
        raise PolicyDenied("truncated or concatenated compressed response")
    return decoded


class Fetcher:
    def __init__(self, store, settings: Settings, task, client=None):
        self.store, self.settings, self.task = store, settings, task
        self.spec = JobSpec.model_validate_json(task["spec"])
        self.client = client or httpx.Client(
            transport=None
            if settings.proxy
            else PublicTransport(settings.private_hosts, settings.ca_bundle),
            verify=ssl.create_default_context(cafile=settings.ca_bundle),
            proxy=settings.proxy,
            trust_env=False,
            follow_redirects=False,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent, "Accept-Encoding": "identity"},
        )
        # Built on first use, and only for our own private services. See client_for().
        self._service_client = None

    def close(self):
        self.client.close()
        if self._service_client is not None:
            self._service_client.close()

    def client_for(self, url, *, service=False):
        """The proxied client for targets; a direct one for our own private services.

        The search service lives on the worker's internal Docker network, so sending its
        request through the egress proxy asks the UPSTREAM to resolve a container name:
        measured, that answers `HTTP 502` on every discovery search even once the policy
        guard allows the host. Routing a target direct would defeat proxy-only egress, so
        the exemption is per-host (see `_is_search_service`) and never a mode switch --
        SpiderFoot reaches its own API the same way, with a client that is not the fetcher's.

        No PublicTransport either: that exists to pin a target's resolved address away from
        private ranges, and this destination is deliberately a private address.
        """
        if not (service and self._is_search_service(urlsplit(url).hostname)):
            return self.client
        if self._service_client is None:
            self._service_client = httpx.Client(
                verify=ssl.create_default_context(cafile=self.settings.ca_bundle),
                trust_env=False,
                follow_redirects=False,
                timeout=self.settings.request_timeout,
                headers={
                    "User-Agent": self.settings.user_agent,
                    "Accept-Encoding": "identity",
                },
            )
        return self._service_client

    def _is_search_service(self, hostname) -> bool:
        """Is this host the deployment's own configured discovery search service?

        Matched by hostname against HARVEST_SEARCH_URL rather than trusting the `service`
        flag alone, because `raw_get` re-guards every redirect hop with the same flag: a
        search service that answered with a redirect would otherwise carry its exemption to
        wherever it pointed.
        """
        configured = getattr(self.settings, "search_url", "")
        if not configured or not hostname:
            return False
        want = urlsplit(configured).hostname
        return bool(want) and hostname.lower() == want.lower()

    def guard(self, url, *, service=False):
        url = canonical_url(url)
        if not service and not in_scope(url, self.spec):
            raise PolicyDenied("destination is outside allowed domains")
        p = urlsplit(url)
        # The discovery search service is this deployment's own infrastructure on a private
        # network -- the same category as SpiderFoot, which bypasses the fetcher entirely for
        # exactly this reason. It is named by HARVEST_SEARCH_URL, never by a job spec or a
        # crawled page, so the destination rules below do not apply to it: they exist to stop
        # a *target* resolving somewhere internal, and a single-label container name like
        # `searxng` fails require_public_host() by design. Without this, proxy-only mode
        # blocks every discovery search with "proxy destination must be a public hostname"
        # while the service sits one Docker network away, working.
        if service and self._is_search_service(p.hostname):
            return url
        # Remote DNS: the proxy resolves the name, so the destination IP is never visible
        # here and the resolved_addresses() check that guards direct mode cannot run.
        # HARVEST_PROXY_PUBLIC_HOSTS is the operator's stand-in for it and is enforced
        # exactly WHEN SET. When unset, policy is the job's own allowed_domains (checked
        # above) plus the name-shape rules below, so leads discovered mid-job are crawlable
        # without enumerating every host in advance. Reaching anything internal is prevented
        # by routing, not by this list: the worker's network is `internal: true` with no
        # gateway, so its only path off-host is the egress relay. No ambient proxy is ever
        # used (trust_env=False), so an unreachable proxy fails closed instead of going direct.
        if self.settings.proxy:
            if self.settings.proxy_public_hosts:
                if p.hostname not in self.settings.proxy_public_hosts:
                    raise PolicyDenied("proxy destination is not in HARVEST_PROXY_PUBLIC_HOSTS")
            require_public_host(p.hostname)
            return url
        resolved_addresses(
            p.hostname, p.port or (443 if p.scheme == "https" else 80), self.settings.private_hosts
        )
        return url

    def throttle(self, url, delay=None):
        origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        while True:
            self.store.reserve(self.task)
            wait = self.store.delay_origin(origin, delay or self.spec.limits.domain_delay)
            if wait == 0:
                return
            time.sleep(min(wait, 0.2))

    def raw_get(self, url, *, headers=None, service=False, check_robots=False):
        original = canonical_url(url)
        current = original
        for _ in range(6):
            current = self.guard(current, service=service)
            if check_robots:
                self.robots_allowed(current)
            self.throttle(current)
            self.store.reserve(self.task, requests=1)
            body = bytearray()
            started = time.monotonic()
            client = self.client_for(current, service=service)
            with client.stream("GET", current, headers=headers) as response:
                encoding = response.headers.get("content-encoding", "identity").lower()
                if encoding not in ("", "identity", "gzip", "deflate"):
                    raise PolicyDenied("unsupported content encoding")
                for chunk in response.iter_raw(chunk_size=65536):
                    self.store.reserve(self.task, byte_count=len(chunk))
                    if len(body) + len(chunk) > self.spec.limits.response_bytes:
                        raise PolicyDenied("response size limit reached")
                    if time.monotonic() - started > self.settings.request_timeout:
                        raise RetryLater("response duration limit reached", 1)
                    body.extend(chunk)
                kept_headers = {
                    k: v
                    for k, v in response.headers.items()
                    if k
                    in {"content-type", "etag", "last-modified", "location", "retry-after", "link"}
                }
                status = response.status_code
                if status not in {204, 304}:
                    body = decode_body(bytes(body), encoding, self.spec.limits.response_bytes)
                    if encoding not in ("", "identity"):
                        kept_headers["x-harvest-decoded-from"] = encoding
            if status in {301, 302, 303, 307, 308}:
                location = kept_headers.get("location")
                if not location:
                    raise PolicyDenied("redirect without location")
                try:
                    current = canonical_url(urljoin(current, location))
                except ValueError:
                    # e.g. an app-scheme or oversized sign-in redirect: a policy stop, not a
                    # parser failure, and the location itself is not echoed.
                    raise PolicyDenied("redirect to a non-HTTP(S) or invalid URL") from None
                headers = None  # Do not forward conditionals across destinations.
                continue
            if status in {408, 425, 429} or status >= 500:
                delay = retry_delay(kept_headers.get("retry-after"))
                origin = f"{urlsplit(current).scheme}://{urlsplit(current).netloc}"
                with self.store.transaction() as db:
                    db.execute(
                        "INSERT INTO origin_state VALUES(?,?) ON CONFLICT(origin) DO UPDATE SET next_request=max(next_request,excluded.next_request)",
                        (origin, time.time() + delay),
                    )
                raise RetryLater(f"HTTP {status}", delay)
            return Capture(original, current, status, kept_headers, bytes(body), time.time())
        raise PolicyDenied("redirect limit reached")

    def robots_allowed(self, url):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM robots WHERE origin=? AND expires>?", (origin, time.time())
            ).fetchone()
        if row:
            text = row["text"]
        else:
            response = self.raw_get(origin + "/robots.txt")
            if response.status in {401, 403}:
                text = "User-agent: *\nDisallow: /"
            elif response.status in {404, 410}:
                text = "User-agent: *\nAllow: /"
            elif response.status == 200:
                text = response.body.decode("utf-8", errors="replace")
            else:
                raise RetryLater("robots.txt could not be verified", 30)
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO robots VALUES(?,?,?,?) ON CONFLICT(origin) DO UPDATE SET text=excluded.text,expires=excluded.expires,status=excluded.status",
                    (origin, text, time.time() + 3600, response.status),
                )
        parser = Protego.parse(text)
        if not parser.can_fetch(url, self.settings.user_agent):
            raise PolicyDenied("robots.txt disallows acquisition")
        delay = parser.crawl_delay(self.settings.user_agent)
        rate = parser.request_rate(self.settings.user_agent)
        if rate and rate.requests > 0:
            delay = max(delay or 0, rate.seconds / rate.requests)
        if delay:
            self.throttle(url, max(delay, self.spec.limits.domain_delay))

    def fetch(self, url):
        url = canonical_url(url)
        with self.store.connection() as db:
            old = db.execute(
                "SELECT * FROM captures WHERE url=? AND dataset=? ORDER BY id DESC LIMIT 1",
                (url, self.spec.dataset),
            ).fetchone()
        headers = {}
        if old:
            import json

            cached = json.loads(old["headers"])
            if "etag" in cached:
                headers["If-None-Match"] = cached["etag"]
            elif "last-modified" in cached:
                headers["If-Modified-Since"] = cached["last-modified"]
        response = self.raw_get(url, headers=headers, check_robots=True)
        if response.status == 304:
            if not old:
                raise PolicyDenied("304 received without cached evidence")
            previous = self.store.capture(old["id"])
            response.body = previous["body"]
            response.headers = {**json.loads(previous["headers"]), **response.headers}
        elif response.status != 200:
            raise PolicyDenied(f"HTTP {response.status}")
        return response
