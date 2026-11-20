"""Checks that an inbound webhook really came from Slack or Clay."""
from __future__ import annotations

import hashlib
import hmac
import time

SLACK_MAX_AGE_SECONDS = 300


def slack_signature_problem(signing_secret: str, timestamp: str | None, body: bytes, signature: str | None,
                            now: float | None = None) -> str | None:
    """Why a request does not carry a valid Slack signature, or None if it does.

    Slack signs each request: HMAC-SHA256 over "v0:<timestamp>:<raw body>" with the app's signing secret.
    A timestamp older than 5 minutes is refused, so a captured request cannot be replayed later.
    The reason is logged when a request is refused; it never contains the secret or the signature."""
    if not signing_secret:
        return "SLACK_SIGNING_SECRET is not set"
    if not timestamp or not signature:
        return "missing X-Slack-Request-Timestamp or X-Slack-Signature header"
    try:
        age = round((now if now is not None else time.time()) - int(timestamp))
    except ValueError:
        return "timestamp header is not a number"
    if abs(age) > SLACK_MAX_AGE_SECONDS:
        return f"timestamp is {age} s old, the limit is {SLACK_MAX_AGE_SECONDS} s (negative = in the future)"
    expected = "v0=" + hmac.new(signing_secret.encode(), b"v0:" + timestamp.encode() + b":" + body,
                                hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return "signature does not match: a different SLACK_SIGNING_SECRET, or the body changed on the way"
    return None


def verify_slack_signature(signing_secret: str, timestamp: str | None, body: bytes, signature: str | None,
                           now: float | None = None) -> bool:
    return slack_signature_problem(signing_secret, timestamp, body, signature, now) is None


def secrets_match(expected: str, given: str | None) -> bool:
    """Constant-time comparison. An unset secret never matches, so a missing config fails closed."""
    return bool(expected) and given is not None and hmac.compare_digest(expected.encode(), given.encode())
