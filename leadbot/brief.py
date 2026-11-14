"""Brief generation. Claude writes the words; code decides everything else.

Flow: fact sheet -> Claude (JSON constrained to BRIEF_SCHEMA) -> fact checker -> brief.
If there is no API key, the call fails, the model declines, or the checker rejects the output, the brief is
built from a template instead and the reason is stored as `fallback_reason`. A model problem never blocks an AE.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import anthropic

from leadbot.check import check

log = logging.getLogger("leadbot.brief")

TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3  # 4 tries in total. The SDK retries 429, 5xx (529 overloaded included) and network errors,
#                  waits for Retry-After when the API sends it, and backs off with jitter otherwise.

SYSTEM = """You write short research briefs for account executives (AEs) before they contact a prospect.

You get one JSON fact sheet about a person, their company, what the CRM knows about them, and a verdict that \
was already decided: "reach_out" or "hold".

Rules:
- Use ONLY the facts in the fact sheet. Never add a person, company, product, number, date, place or event that \
is not in it. If a fact is missing, leave it out; do not guess and do not say that it is missing.
- Copy "verdict" exactly as given. You do not decide it.
- If the verdict is "hold", put the given hold reason into "hold_reason" in plain English. If the verdict is \
"reach_out", "hold_reason" is an empty string.
- "headline": one line on who this person is and why they matter to the seller.
- "person": their role, seniority and time in the role.
- "company": what the company does, its size and where it is.
- "angle": one suggested way to open the conversation, tied to the seller's offer and based only on the facts.
- "questions": exactly two discovery questions for a first call.
- Plain English, short sentences. At most 110 words across all fields. No greetings, no markdown, no emojis."""

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["reach_out", "hold"]},
        "hold_reason": {"type": "string"},
        "headline": {"type": "string"},
        "person": {"type": "string"},
        "company": {"type": "string"},
        "angle": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "hold_reason", "headline", "person", "company", "angle", "questions"],
    "additionalProperties": False,
}


@dataclass
class BriefResult:
    brief: dict
    source: str                 # "claude" or "template"
    model: str | None
    check_passed: bool | None   # None when the model was never called
    fallback_reason: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


# ---------- template fallback ----------

def template_brief(facts: dict) -> dict:
    """A plain brief built only from the fact sheet. Always passes the checker, because it adds nothing."""
    person, company, seller = facts.get("person", {}), facts.get("company", {}), facts["seller"]
    name = person.get("name") or "This prospect"
    title = person.get("title")
    company_name = company.get("name")
    who = name + (f", {title}" if title else "") + (f" at {company_name}" if company_name else "")
    person_line = (title or "Role unknown") + (f", {person['time_in_role']} in the role" if person.get(
        "time_in_role") else "")
    company_bits = [company.get("industry"),
                    f"{company['employees']} employees" if company.get("employees") else None,
                    company.get("country")]
    company_line = ", ".join(str(b) for b in company_bits if b) or "No company details found"
    target = company_name or "their company"
    return {
        "verdict": facts["verdict"],
        "hold_reason": facts.get("hold_reason", ""),
        "headline": who,
        "person": person_line,
        "company": (f"{company_name}: " if company_name else "") + company_line,
        "angle": f"Ask how {target} handles the work that {seller['name']} covers: {seller['sells']}.",
        "questions": [
            f"How does {target} handle this today?",
            "What would have to be true for a change to be worth it this year?",
        ],
    }


# ---------- the model call ----------

class Briefer:
    def __init__(self, client: anthropic.AsyncAnthropic | None, model: str, max_tokens: int = 2000,
                 effort: str = "low"):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort

    @classmethod
    def from_settings(cls, settings) -> Briefer:
        client = None
        if settings.anthropic_api_key:
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=TIMEOUT_SECONDS,
                                              max_retries=MAX_RETRIES)
        return cls(client, settings.claude_model, settings.claude_max_tokens, settings.claude_effort)

    async def write(self, facts: dict) -> BriefResult:
        if self.client is None:
            return self._fallback(facts, "no Claude API key configured", check_passed=None)
        try:
            response = await self._call(facts)
        # Most specific first. Every branch ends in the template: the AE still gets a brief.
        except anthropic.RateLimitError as e:
            return self._fallback(facts, f"claude rate limit after retries: {e.message}", check_passed=None)
        except anthropic.APIStatusError as e:
            return self._fallback(facts, f"claude api error {e.status_code}: {e.message}", check_passed=None)
        except anthropic.APIConnectionError as e:  # includes timeouts
            return self._fallback(facts, f"claude connection error: {type(e).__name__}", check_passed=None)

        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens,
                 "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0}
        # A response is not always an answer: check why the model stopped before reading the content.
        if response.stop_reason == "refusal":
            return self._fallback(facts, "claude declined the request", check_passed=None, **usage)
        if response.stop_reason == "max_tokens":
            return self._fallback(facts, "claude output cut off at max_tokens", check_passed=None, **usage)
        try:
            text = next(block.text for block in response.content if block.type == "text")
            out = json.loads(text)
        except (StopIteration, json.JSONDecodeError) as e:
            return self._fallback(facts, f"claude returned no valid JSON: {type(e).__name__}", check_passed=None,
                                  **usage)

        problems = check(out, facts)
        if problems:
            log.warning("brief rejected by checker", extra={"problems": problems})
            return self._fallback(facts, "checker: " + "; ".join(problems), check_passed=False, **usage)
        return BriefResult(out, "claude", self.model, True, None, **usage)

    async def _call(self, facts: dict):
        return await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            # The system prompt is identical on every call, so it is marked for prompt caching. Note: Claude
            # only caches a prefix of at least 512 tokens on this model. This prompt is shorter, so today the
            # marker does nothing. It starts paying off, with no code change, once the prompt grows past that.
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            # sort_keys keeps the request byte-stable for the same facts.
            messages=[{"role": "user", "content": json.dumps(facts, sort_keys=True, ensure_ascii=False)}],
            # Structured output: the reply is guaranteed to be JSON that matches BRIEF_SCHEMA.
            # (A forced tool call would do the same on older models; claude-sonnet-5-5 rejects forced tool_choice.)
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
        )

    def _fallback(self, facts: dict, reason: str, *, check_passed: bool | None, **usage) -> BriefResult:
        log.info("using template brief", extra={"fallback_reason": reason})
        return BriefResult(template_brief(facts), "template", self.model if self.client else None, check_passed,
                           reason, **usage)
