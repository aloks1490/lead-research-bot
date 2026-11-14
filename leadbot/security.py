"""Checks that an inbound webhook really came from Slack or Clay."""
from __future__ import annotations

import hashlib
import hmac
import time

SLACK_MAX_AGE_SECONDS = 300


def verify_slack_signature(signing_secret: str, timestamp: str | None, body: bytes, signature: str | None,
                           now: float | None = None) -> bool:
    """Slack signs each request: HMAC-SHA256 over "v0:<timestamp>:<raw body>" with the app's signing secret.
    A timestamp older than 5 minutes is refused, so a captured request cannot be replayed later."""
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        age = abs((now if now is not None else time.time()) - int(timestamp))
    except ValueError:
        return False
    if age > SLACK_MAX_AGE_SECONDS:
        return False
    expected = "v0=" + hmac.new(signing_secret.encode(), b"v0:" + timestamp.encode() + b":" + body,
                                hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def secrets_match(expected: str, given: str | None) -> bool:
    """Constant-time comparison. An unset secret never matches, so a missing config fails closed."""
    return bool(expected) and given is not None and hmac.compare_digest(expected.encode(), given.encode())
