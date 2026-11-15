"""Brief generation. A model writes the words; code decides everything else.

Flow: fact sheet -> model -> fact checker -> brief.
If there is no API key, the call fails, the model declines, or the checker rejects the output, the brief is
built from a template instead and the reason is stored as `fallback_reason`. A model problem never blocks an AE.

Two model providers, chosen by which key is set (see Briefer.from_settings):
  - OpenAICompatWriter: any OpenAI-compatible chat endpoint, for example NVIDIA NIM or OpenRouter
  - ClaudeWriter: the Anthropic API, with the reply constrained to BRIEF_SCHEMA
Both give the Briefer a draft; the same checker and the same template fallback apply to both.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import anthropic

from leadbot.check import check
from leadbot.http import ApiError, HttpClient

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
    source: str                 # "claude", "llm" (OpenAI-compatible endpoint) or "template"
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

class ModelError(Exception):
    """The model call failed or returned nothing usable. The message is stored as `fallback_reason`."""

    def __init__(self, reason: str, usage: dict | None = None):
        super().__init__(reason)
        self.usage = usage or {}


@dataclass
class Draft:
    """What a model wrote, before the checker has looked at it."""
    out: dict
    usage: dict = field(default_factory=dict)  # input_tokens, output_tokens, cache_read_tokens


class ClaudeWriter:
    source = "claude"

    def __init__(self, client: anthropic.AsyncAnthropic, model: str, max_tokens: int = 2000, effort: str = "low"):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort

    async def draft(self, facts: dict) -> Draft:
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # The system prompt is identical on every call, so it is marked for prompt caching. Note: Claude
                # only caches a prefix of at least 512 tokens on this model. This prompt is shorter, so today the
                # marker does nothing. It starts paying off, with no code change, once the prompt grows past that.
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                # sort_keys keeps the request byte-stable for the same facts.
                messages=[{"role": "user", "content": json.dumps(facts, sort_keys=True, ensure_ascii=False)}],
                # Structured output: the reply is guaranteed to be JSON that matches BRIEF_SCHEMA. (A forced tool
                # call would do the same on older models; claude-sonnet-5-5 rejects forced tool_choice.)
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
            )
        # Most specific first. Every branch ends in the template: the AE still gets a brief.
        except anthropic.RateLimitError as e:
            raise ModelError(f"claude rate limit after retries: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise ModelError(f"claude api error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise ModelError(f"claude connection error: {type(e).__name__}") from e

        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens,
                 "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0}
        # A response is not always an answer: check why the model stopped before reading the content.
        if response.stop_reason == "refusal":
            raise ModelError("claude declined the request", usage)
        if response.stop_reason == "max_tokens":
            raise ModelError("claude output cut off at max_tokens", usage)
        try:
            text = next(block.text for block in response.content if block.type == "text")
            return Draft(json.loads(text), usage)
        except (StopIteration, json.JSONDecodeError) as e:
            raise ModelError(f"claude returned no valid JSON: {type(e).__name__}", usage) from e


# These endpoints cannot be held to a schema, so the shape is spelled out in the prompt and verified by the checker.
JSON_INSTRUCTIONS = """

Return one JSON object and nothing else, with exactly these keys:
  "verdict": "reach_out" or "hold"
  "hold_reason": string
  "headline": string
  "person": string
  "company": string
  "angle": string
  "questions": a list of exactly two strings"""


def parse_json_reply(text: str) -> dict:
    """The JSON object in a reply that may carry code fences or stray prose around it."""
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    out = json.loads(text[start:end + 1])
    if not isinstance(out, dict):
        raise ValueError("reply is not a JSON object")
    return out


class OpenAICompatWriter:
    """Any endpoint that speaks the OpenAI chat-completions format (NVIDIA NIM, OpenRouter, ...).
    Calls go through the shared HttpClient, so they get the same timeout, retries and logging as HubSpot."""
    source = "llm"

    def __init__(self, http: HttpClient, base_url: str, api_key: str, model: str, max_tokens: int = 2000,
                 extra_body: dict | None = None):
        self.http = http
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.model = model
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}

    async def _post(self, body: dict):
        return await self.http.request("llm", "POST", self.url, endpoint="chat/completions", timeout=TIMEOUT_SECONDS,
                                       json=body, headers=self.headers)

    async def draft(self, facts: dict) -> Draft:
        body = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": SYSTEM + JSON_INSTRUCTIONS},
                         {"role": "user", "content": json.dumps(facts, sort_keys=True, ensure_ascii=False)}],
            **self.extra_body,
        }
        try:
            try:
                response = await self._post({**body, "response_format": {"type": "json_object"}})
            except ApiError as e:
                if e.status != 400:
                    raise
                response = await self._post(body)  # some models reject JSON mode: ask again without it
        except ApiError as e:
            raise ModelError(f"llm api error {e.status or 'network'}: {e.detail[:200]}") from e

        data = response.json()
        reported = data.get("usage") or {}
        usage = {"input_tokens": reported.get("prompt_tokens") or 0,
                 "output_tokens": reported.get("completion_tokens") or 0, "cache_read_tokens": 0}
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        if not content:
            # A reasoning model can spend the whole token budget thinking and return no answer.
            raise ModelError(f"llm returned no content (finish_reason={choice.get('finish_reason')})", usage)
        try:
            return Draft(parse_json_reply(content), usage)
        except ValueError as e:  # includes json.JSONDecodeError
            raise ModelError(f"llm returned no valid JSON: {e}", usage) from e


class Briefer:
    def __init__(self, writer: ClaudeWriter | OpenAICompatWriter | None):
        self.writer = writer

    @property
    def model(self) -> str | None:
        return self.writer.model if self.writer else None

    @classmethod
    def from_settings(cls, settings, http: HttpClient) -> Briefer:
        """LLM_API_KEY + LLM_MODEL select the OpenAI-compatible endpoint. Otherwise ANTHROPIC_API_KEY selects
        Claude. With neither, every brief comes from the template."""
        if settings.llm_api_key and settings.llm_model:
            extra = json.loads(settings.llm_extra_body) if settings.llm_extra_body.strip() else None
            return cls(OpenAICompatWriter(http, settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                                          settings.llm_max_tokens, extra))
        if settings.anthropic_api_key:
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=TIMEOUT_SECONDS,
                                              max_retries=MAX_RETRIES)
            return cls(ClaudeWriter(client, settings.claude_model, settings.claude_max_tokens,
                                    settings.claude_effort))
        return cls(None)

    async def write(self, facts: dict) -> BriefResult:
        if self.writer is None:
            return self._fallback(facts, "no model API key configured", check_passed=None)
        try:
            draft = await self.writer.draft(facts)
        except ModelError as e:
            return self._fallback(facts, str(e), check_passed=None, **e.usage)
        problems = check(draft.out, facts)
        if problems:
            log.warning("brief rejected by checker", extra={"problems": problems})
            return self._fallback(facts, "checker: " + "; ".join(problems), check_passed=False, **draft.usage)
        return BriefResult(draft.out, self.writer.source, self.model, True, None, **draft.usage)

    def _fallback(self, facts: dict, reason: str, *, check_passed: bool | None, **usage) -> BriefResult:
        log.info("using template brief", extra={"fallback_reason": reason})
        return BriefResult(template_brief(facts), "template", self.model, check_passed, reason, **usage)
