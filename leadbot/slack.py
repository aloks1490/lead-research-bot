"""Slack Web API client and the Block Kit layout of a brief."""
from __future__ import annotations

from leadbot.http import ApiError, HttpClient

TIMEOUT = 10.0


class Slack:
    def __init__(self, http: HttpClient, token: str, base_url: str = "https://slack.com/api"):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}

    async def post(self, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> str:
        """Post a message and return its timestamp (Slack's message id)."""
        body = {"channel": channel, "text": text, "unfurl_links": False}
        if thread_ts:
            body["thread_ts"] = thread_ts
        if blocks:
            body["blocks"] = blocks
        response = await self.http.request("slack", "POST", f"{self.base_url}/chat.postMessage",
                                           endpoint="chat.postMessage", timeout=TIMEOUT, json=body,
                                           headers=self.headers)
        data = response.json()
        if not data.get("ok"):  # Slack reports most errors as HTTP 200 with ok=false
            raise ApiError("slack", "chat.postMessage", response.status_code, str(data.get("error")))
        return str(data.get("ts", ""))


# ---------- how a brief looks in the thread ----------

def brief_text(brief: dict) -> str:
    """Plain-text version: used for notifications and as the fallback when blocks cannot render."""
    lead = f"HOLD: {brief['hold_reason']}" if brief["verdict"] == "hold" else "OK to reach out"
    return f"{lead}. {brief['headline']}"


def brief_blocks(brief: dict, footer: str) -> list[dict]:
    """A suppressed prospect opens with the reason not to reach out: the AE needs that more than the research."""
    if brief["verdict"] == "hold":
        verdict = f":no_entry: *Hold: do not reach out.* {brief['hold_reason']}"
    else:
        verdict = ":white_check_mark: *OK to reach out*"
    questions = "\n".join(f"• {q}" for q in brief["questions"])
    return [
        _section(verdict),
        _section(f"*{brief['headline']}*"),
        _section(f"*Person*\n{brief['person']}\n\n*Company*\n{brief['company']}"),
        _section(f"*Angle*\n{brief['angle']}\n\n*Questions for the first call*\n{questions}"),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
    ]


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}
