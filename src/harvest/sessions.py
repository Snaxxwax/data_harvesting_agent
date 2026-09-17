"""Signed, stateless browser session capability, distinct from the operator's bearer token.

The API token itself is never placed in a cookie, rendered into HTML, or logged: a session
cookie is an HMAC-signed, time-limited capability keyed on the operator's token, verifiable
without server-side session storage. Losing this cookie does not hand over a string that can
be pasted directly as `Authorization: Bearer <token>` in the same way the raw token would.
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256

COOKIE_NAME = "harvest_session"
SESSION_SECONDS = 12 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _mac(api_token: str, payload: bytes) -> bytes:
    return hmac.new(api_token.encode(), payload, sha256).digest()


def issue(api_token: str, *, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + SESSION_SECONDS)
    payload = str(expires).encode()
    return _b64(payload) + "." + _b64(_mac(api_token, payload))


def verify(api_token: str, cookie_value: str | None, *, now: float | None = None) -> bool:
    if not cookie_value or "." not in cookie_value:
        return False
    payload_part, _, mac_part = cookie_value.partition(".")
    try:
        payload = _unb64(payload_part)
        mac = _unb64(mac_part)
    except Exception:
        return False
    if not hmac.compare_digest(mac, _mac(api_token, payload)):
        return False
    try:
        expires = int(payload.decode())
    except ValueError:
        return False
    return (now if now is not None else time.time()) < expires
