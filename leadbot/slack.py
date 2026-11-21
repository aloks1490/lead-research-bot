"""Slack Web API client and the Block Kit layout of a brief."""
from __future__ import annotations

from leadbot.fit import Fit
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

FIT_ICONS = {"strong": ":dart:", "weak": ":warning:", "unknown": ":grey_question:"}


def brief_text(brief: dict, fit: Fit) -> str:
    """Plain-text version: used for notifications and as the fallback when blocks cannot render."""
    # The model usually ends its hold reason with a full stop; strip it so the text never shows "customer..".
    lead = f"HOLD: {brief['hold_reason'].rstrip('. ')}" if brief["verdict"] == "hold" else "No conflict in HubSpot"
    return f"{lead}. Fit: {fit.level}. {brief['headline']}"


def brief_blocks(brief: dict, footer: str, fit: Fit, email: str | None = None) -> list[dict]:
    """The two decisions made in code come first: is there a reason not to reach out (HubSpot), and is this a
    likely buyer at all (fit)? A hold opens with its reason: the AE needs that more than the research.
    The email comes from the enrichment lookup, not the model, and nothing checks that it delivers, so it is
    marked unverified."""
    if brief["verdict"] == "hold":
        verdict = f":no_entry: *Hold: do not reach out.* {brief['hold_reason']}"
    else:
        verdict = ":white_check_mark: *No conflict in HubSpot*"
    reasons = "\n".join(f"• {r}" for r in fit.reasons)
    verdict += f"\n{FIT_ICONS.get(fit.level, '')} *Fit: {fit.level}*\n{reasons}"
    questions = "\n".join(f"• {q}" for q in brief["questions"])
    return [
        _section(verdict),
        _section(f"*{brief['headline']}*"),
        # The company part is left out when nothing is known about the company (the model returns "").
        _section(f"*Person*\n{brief['person']}\n{_email_line(email)}"
                 + (f"\n\n*Company*\n{brief['company']}" if brief["company"].strip() else "")),
        _section(f"*Angle*\n{brief['angle']}\n\n*Questions for the first call*\n{questions}"),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
    ]


def _email_line(email: str | None) -> str:
    return f"Email: {email} _(unverified)_" if email else "Email: not found"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}
