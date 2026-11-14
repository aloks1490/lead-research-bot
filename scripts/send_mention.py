"""Send the bot a signed Slack app_mention event, exactly as Slack would.

    uv run python scripts/send_mention.py https://www.linkedin.com/in/jane-doe
    uv run python scripts/send_mention.py https://www.linkedin.com/in/jane-doe --event-id Ev1
    (the same --event-id twice simulates a Slack resend)

Reads SLACK_SIGNING_SECRET from the environment or .env.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
import uuid

import httpx

from leadbot.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("linkedin_url")
    parser.add_argument("--bot", default="http://localhost:8000")
    parser.add_argument("--event-id", default=None, help="reuse an id to simulate a Slack resend")
    args = parser.parse_args()

    secret = Settings().slack_signing_secret
    ts = f"{time.time():.6f}"
    body = json.dumps({
        "type": "event_callback", "event_id": args.event_id or f"Ev{uuid.uuid4().hex[:10]}",
        "event": {"type": "app_mention", "user": "U_LOCAL", "text": f"<@UBOT> <{args.linkedin_url}>",
                  "channel": "C_LOCAL", "ts": ts},
    }).encode()
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    started = time.monotonic()
    response = httpx.post(f"{args.bot}/slack/events", content=body, headers={
        "X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature, "Content-Type": "application/json"})
    print(f"{response.status_code} {response.text}  ({(time.monotonic() - started) * 1000:.0f} ms)")


if __name__ == "__main__":
    main()
