"""Test world: the real app and job runner, with every outside service replaced by a fake.

- Slack, Clay and HubSpot are mocked at the HTTP layer with respx, so the real clients, retries and parsing run.
- Claude is a fake client object (the Anthropic SDK has its own HTTP stack); tests control what it "writes".
- Storage is the in-memory store. tests/test_store.py proves it behaves like the Postgres one.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx

from leadbot.brief import Briefer, ClaudeWriter
from leadbot.config import Settings
from leadbot.http import HttpClient
from leadbot.main import build_runner, create_app
from leadbot.store.memory import MemoryStore

SLACK = "https://slack.test/api"
HUBSPOT = "https://hubspot.test"
CLAY_WEBHOOK = "https://clay.test/webhook/table-123"
JANE_URL = "https://www.linkedin.com/in/Jane-Doe/?utm_source=share"
JANE_NORM = "linkedin.com/in/jane-doe"


def settings() -> Settings:
    return Settings(
        _env_file=None,
        slack_signing_secret="slack-secret", slack_bot_token="xoxb-test", slack_base_url=SLACK,
        clay_webhook_url=CLAY_WEBHOOK, clay_callback_secret="clay-secret",
        hubspot_token="hs-test", hubspot_base_url=HUBSPOT,
        anthropic_api_key="test-key", internal_secret="sweep-secret", database_url="",
    )


def faithful_brief(facts: dict) -> dict:
    """What a well-behaved model returns: only words and numbers from the fact sheet."""
    person, company = facts["person"], facts.get("company", {})
    return {
        "verdict": facts["verdict"],
        "hold_reason": facts.get("hold_reason", ""),
        "headline": f"{person['name']} is {person.get('title', 'a contact')} at {company.get('name', 'a company')}.",
        "person": f"{person.get('title', 'Unknown role')}, {person.get('time_in_role', 'new')} in the role.",
        "company": f"{company.get('name')} has {company.get('employees')} employees in {company.get('country')}.",
        "angle": f"Ask how {company.get('name')} prepares for audits.",
        "questions": ["How do you handle audits today?", "Who owns compliance on your team?"],
    }


class FakeClaude:
    """Stands in for anthropic.AsyncAnthropic. Set `.reply` to control the model's output, `.error` to make it fail."""

    def __init__(self):
        self.calls: list[dict] = []
        self.reply = faithful_brief
        self.stop_reason = "end_turn"
        self.error: Exception | None = None
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        facts = json.loads(kwargs["messages"][-1]["content"])  # the real fact sheet comes after the examples
        content = [SimpleNamespace(type="thinking", thinking=""),
                   SimpleNamespace(type="text", text=json.dumps(self.reply(facts)))]
        return SimpleNamespace(stop_reason=self.stop_reason, content=content,
                               usage=SimpleNamespace(input_tokens=420, output_tokens=160, cache_read_input_tokens=0))


class FakeHubSpot:
    """A tiny in-memory HubSpot behind the real REST paths."""

    def __init__(self, router: respx.MockRouter):
        self.contacts: dict[str, dict] = {}   # id -> properties
        self.companies: dict[str, dict] = {}  # id -> properties
        self.deals: dict[str, dict] = {}      # id -> properties
        self.links: dict[tuple[str, str, str], list[str]] = {}  # (from type, id, to type) -> ids
        self.owners = {"77": {"firstName": "Priya", "email": "priya@seller.test"}}
        self.notes: list[dict] = []
        self.fail_with: list[httpx.Response] = []  # responses to serve before behaving normally
        self.requests: list[httpx.Request] = []
        router.route(url__startswith=HUBSPOT).mock(side_effect=self._handle)

    def add_company(self, domain: str, **props) -> str:
        company_id = str(500 + len(self.companies))
        self.companies[company_id] = {"domain": domain, **props}
        return company_id

    def add_contact(self, email: str, **props) -> str:
        contact_id = str(100 + len(self.contacts))
        self.contacts[contact_id] = {"email": email, **props}
        return contact_id

    def add_deal(self, company_id: str, name: str, stage: str, owner_id: str = "77") -> str:
        deal_id = str(900 + len(self.deals))
        self.deals[deal_id] = {"dealname": name, "dealstage": stage, "hubspot_owner_id": owner_id}
        self.links.setdefault(("companies", company_id, "deals"), []).append(deal_id)
        return deal_id

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return self.fail_with.pop(0)
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        parts = path.strip("/").split("/")
        if path.endswith("/search"):
            table = self.contacts if parts[3] == "contacts" else self.companies
            flt = body["filterGroups"][0]["filters"][0]
            hits = [{"id": i, "properties": p} for i, p in table.items() if p.get(flt["propertyName"]) == flt["value"]]
            return httpx.Response(200, json={"results": hits[:1]})
        if "/associations/" in path and path.endswith("/labels"):
            type_id = {"contacts": 202, "companies": 190}[parts[4]]
            return httpx.Response(200, json={"results": [{"category": "HUBSPOT_DEFINED", "typeId": type_id,
                                                          "label": None}]})
        if "/associations/" in path:  # /crm/v4/objects/{from}/{id}/associations/{to}
            ids = self.links.get((parts[3], parts[4], parts[6]), [])
            return httpx.Response(200, json={"results": [{"toObjectId": int(i)} for i in ids]})
        if path == "/crm/v3/objects/deals/batch/read":
            wanted = [i["id"] for i in body["inputs"]]
            return httpx.Response(200, json={"results": [{"id": i, "properties": self.deals[i]} for i in wanted]})
        if path == "/crm/v3/pipelines/deals":
            stages = [{"id": "proposal", "label": "Proposal", "metadata": {"isClosed": "false"}},
                      {"id": "closedwon", "label": "Closed won", "metadata": {"isClosed": "true"}}]
            return httpx.Response(200, json={"results": [{"id": "default", "stages": stages}]})
        if path.startswith("/crm/v3/owners/"):
            owner = self.owners.get(parts[-1])
            return httpx.Response(200, json=owner) if owner else httpx.Response(404, json={})
        if path == "/crm/v3/objects/contacts" and method == "POST":
            contact_id = self.add_contact(**body["properties"])
            return httpx.Response(201, json={"id": contact_id, "properties": body["properties"]})
        if path == "/crm/v3/objects/notes" and method == "POST":
            self.notes.append(body)
            return httpx.Response(201, json={"id": str(7000 + len(self.notes))})
        return httpx.Response(404, json={"message": f"fake hubspot has no route for {method} {path}"})


class World:
    def __init__(self):
        self.settings = settings()
        self.store = MemoryStore()
        self.now = datetime.now(UTC)
        self.sleeps: list[float] = []
        self.router = respx.MockRouter(assert_all_called=False)
        self.slack_messages: list[dict] = []
        self.clay_requests: list[dict] = []
        self.slack_route = self.router.post(f"{SLACK}/chat.postMessage").mock(side_effect=self._slack)
        self.clay_route = self.router.post(CLAY_WEBHOOK).mock(side_effect=self._clay)
        self.hubspot = FakeHubSpot(self.router)
        self.claude = FakeClaude()
        self.http_client = httpx.AsyncClient(transport=httpx.MockTransport(self.router.async_handler))
        self.runner = self.new_runner()
        self.app = create_app(self.settings, self.runner)
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://bot.test")
        self._events = 0

    def new_runner(self):
        """A fresh runner on the same database: what a restarted process looks like."""
        async def no_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        http = HttpClient(self.http_client, sleep=no_sleep, rng=lambda: 1.0)
        briefer = Briefer(ClaudeWriter(self.claude, self.settings.claude_model, self.settings.claude_max_tokens,
                                       self.settings.claude_effort))
        return build_runner(self.settings, self.store, http, briefer, now=lambda: self.now)

    def _slack(self, request: httpx.Request) -> httpx.Response:
        self.slack_messages.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "ts": f"1700000000.{len(self.slack_messages):06d}"})

    def _clay(self, request: httpx.Request) -> httpx.Response:
        self.clay_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "accepted"})

    # ---------- driving the bot ----------

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)

    async def mention(self, text: str = f"<@UBOT> <{JANE_URL}>", event_id: str | None = None,
                      signature: str | None = None, timestamp: str | None = None) -> httpx.Response:
        """Send a signed Slack app_mention event, then wait for the background work to settle."""
        self._events += 1
        event_id = event_id or f"Ev{self._events:04d}"
        body = json.dumps({"type": "event_callback", "event_id": event_id,
                           "event": {"type": "app_mention", "user": "U_AE", "text": text, "channel": "C_SALES",
                                     "ts": f"1699999999.{self._events:06d}"}}).encode()
        response = await self.post_slack(body, signature=signature, timestamp=timestamp)
        await self.runner.drain()
        return response

    async def post_slack(self, body: bytes, signature: str | None = None, timestamp: str | None = None):
        timestamp = timestamp or str(int(time.time()))
        signature = signature or "v0=" + hmac.new(b"slack-secret", f"v0:{timestamp}:".encode() + body,
                                                  hashlib.sha256).hexdigest()
        return await self.api.post("/slack/events", content=body, headers={
            "X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature,
            "Content-Type": "application/json"})

    async def clay_calls_back(self, secret: str = "clay-secret", **overrides) -> httpx.Response:
        """Clay's HTTP API column posts the enriched row, then wait for the resumed jobs to settle."""
        payload = {
            "job_id": "ignored-here", "linkedin_url": JANE_URL,
            "person": {"full_name": "Jane Doe", "title": "Head of Security", "email": "Jane.Doe@acme.io",
                       "location": "Berlin", "role_started_on": "2024-08-01"},
            "company": {"name": "Acme", "domain": "https://www.acme.io/", "industry": "Fintech",
                        "employee_count": "240", "country": "Germany", "description": "Payments for online shops."},
        }
        payload.update(overrides)
        response = await self.api.post("/webhooks/clay", json=payload, headers={"X-Clay-Secret": secret})
        await self.runner.drain()
        return response

    # ---------- reading the result ----------

    def job(self, index: int = -1):
        return list(self.store.jobs.values())[index]

    def brief(self, index: int = -1):
        return self.store.briefs[self.job(index).id]

    def thread_messages(self) -> list[dict]:
        return [m for m in self.slack_messages if m["channel"] == "C_SALES"]

    def alerts(self) -> list[dict]:
        return [m for m in self.slack_messages if m["channel"] == self.settings.slack_alert_channel]


@pytest.fixture
async def world():
    w = World()
    yield w
    await w.runner.drain()
    await w.api.aclose()
    await w.http_client.aclose()
