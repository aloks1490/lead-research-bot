"""The reviewer: a second model reads a brief for meaning and lists claims the fact sheet does not support.

Why: the fact checker (check.py) matches words, so it misses claims written in ordinary words, such as "a startup
that will need SOC 2 as it scales" or "biotechs like them use Northwall". Both reached briefs in real runs.

How:
  - the reviewer is a different model from the writer (a different family, so it does not share the writer's
    blind spots), on the same OpenAI-compatible endpoint, with its own skill file (prompts/review.md)
  - its answer must match the skill's schema, and the endpoint is asked to enforce it
  - each quote it returns must appear in the brief; a quote that does not is the reviewer inventing, and is dropped
  - any remaining quote means the template brief goes out instead (brief.py)
  - if the reviewer cannot answer (timeout, error, bad JSON), the brief goes out on the checker's word alone and
    the review is recorded as skipped: an outage of an extra check should not stop every brief
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field

from leadbot import skill as skills
from leadbot.http import ApiError, HttpClient
from leadbot.skill import parse_json_reply

log = logging.getLogger("leadbot.review")

REVIEW_PATH = skills.DEFAULT_PATH.parent / "review.md"
TIMEOUT = 45.0
REVIEWED_FIELDS = ("hold_reason", "headline", "person", "company", "angle", "questions")


@dataclass
class Review:
    status: str                       # "passed", "rejected" or "skipped"
    unsupported: list[dict] = field(default_factory=list)
    dropped: int = 0                  # quotes the reviewer returned that are not in the brief
    model: str | None = None
    duration_ms: int = 0
    error: str | None = None
    prompt_version: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'")).strip().casefold()


class Reviewer:
    def __init__(self, http: HttpClient, base_url: str, api_key: str, model: str, extra_body: dict | None = None,
                 skill: skills.Skill | None = None):
        self.http = http
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.model = model
        self.extra_body = extra_body or {}
        self.skill = skill or skills.load(REVIEW_PATH)

    async def review(self, facts: dict, brief: dict) -> Review:
        started = time.monotonic()
        shown = {k: brief[k] for k in REVIEWED_FIELDS if brief.get(k)}
        body = {
            "model": self.model, "temperature": 0, "max_tokens": 1000,
            "messages": self.skill.chat_messages({"facts": facts, "brief": shown}),
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "review", "schema": self.skill.schema, "strict": True}},
            **self.extra_body,
        }
        try:
            response = await self.http.request("review", "POST", self.url, endpoint="chat/completions",
                                               timeout=TIMEOUT, retry_timeouts=False, json=body,
                                               headers=self.headers)
            content = ((response.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            answer = parse_json_reply(content)
            problems = self.skill.validate(answer)
            if problems:
                raise ValueError("; ".join(problems))
        except (ApiError, ValueError) as e:  # ValueError covers bad JSON
            ms = round((time.monotonic() - started) * 1000)
            log.warning("review skipped", extra={"error_text": str(e)[:300], "duration_ms": ms})
            return Review("skipped", model=self.model, duration_ms=ms, error=str(e)[:300],
                          prompt_version=self.skill.prompt_version)

        text = _normal(json.dumps(shown, ensure_ascii=False))
        kept = [c for c in answer["unsupported"] if c["quote"].strip() and _normal(c["quote"]) in text]
        dropped = len(answer["unsupported"]) - len(kept)
        ms = round((time.monotonic() - started) * 1000)
        result = Review("rejected" if kept else "passed", kept, dropped, self.model, ms,
                        prompt_version=self.skill.prompt_version)
        log.info("review done", extra={"status": result.status, "unsupported": len(kept), "dropped": dropped,
                                       "duration_ms": ms})
        return result
