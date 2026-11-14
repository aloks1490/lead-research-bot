"""Unit tests for the small pure pieces: URL handling, signatures, the retry client, suppression rules, the checker."""
from __future__ import annotations

import hashlib
import hmac
from datetime import date

import anthropic
import httpx
import httpx2
import pytest

from leadbot.brief import Briefer, template_brief
from leadbot.check import check
from leadbot.clay import ClayCallback
from leadbot.facts import tenure
from leadbot.http import ApiError, HttpClient
from leadbot.hubspot import CrmState, OpenDeal
from leadbot.security import secrets_match, verify_slack_signature
from leadbot.suppression import Context, evaluate
from leadbot.urls import extract_url, normalise_linkedin_url
from tests.conftest import FakeClaude

# ---------- URLs ----------


@pytest.mark.parametrize("raw", [
    "https://www.linkedin.com/in/Jane-Doe/?utm_source=share",
    "http://linkedin.com/in/jane-doe",
    "linkedin.com/in/jane-doe/",
    "https://de.linkedin.com/in/jane-doe/details/experience/",
    "  HTTPS://WWW.LINKEDIN.COM/in/JANE-DOE#about  ",
])
def test_every_spelling_of_a_profile_gives_one_cache_key(raw):
    assert normalise_linkedin_url(raw) == "linkedin.com/in/jane-doe"


@pytest.mark.parametrize("raw", [
    "https://www.linkedin.com/company/acme", "https://linkedin.com/in/", "https://example.com/in/jane-doe",
    "https://notlinkedin.com/in/jane-doe", "https://www.linkedin.com/posts/jane-doe_123", "not a url",
])
def test_non_profiles_are_refused(raw):
    assert normalise_linkedin_url(raw) is None


def test_non_ascii_profile_slug_has_one_form():
    assert normalise_linkedin_url("linkedin.com/in/José-Müller") == normalise_linkedin_url(
        "linkedin.com/in/jos%C3%A9-m%C3%BCller")


@pytest.mark.parametrize("text,expected", [
    ("<@UBOT> <https://www.linkedin.com/in/jane-doe>", "https://www.linkedin.com/in/jane-doe"),
    ("<@UBOT> <https://www.linkedin.com/in/jane-doe|Jane Doe> please", "https://www.linkedin.com/in/jane-doe"),
    ("<@UBOT> linkedin.com/in/jane-doe thanks", "linkedin.com/in/jane-doe"),
    ("<@UBOT> <linkedin.com/in/jane-doe>", "linkedin.com/in/jane-doe"),  # found by the live demo run
    ("<@UBOT> hello", None),
])
def test_link_is_pulled_out_of_a_slack_message(text, expected):
    assert extract_url(text) == expected


# ---------- webhook security ----------

def _sign(secret: str, timestamp: str, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()


def test_slack_signature():
    body, now = b'{"a": 1}', 1_700_000_000
    good = _sign("secret", str(now), body)
    assert verify_slack_signature("secret", str(now), body, good, now=now)
    assert not verify_slack_signature("secret", str(now), body + b" ", good, now=now)        # body changed
    assert not verify_slack_signature("other", str(now), body, good, now=now)                # wrong secret
    assert not verify_slack_signature("secret", str(now), body, good, now=now + 301)         # too old
    assert not verify_slack_signature("secret", None, body, good, now=now)                   # header missing
    assert not verify_slack_signature("", str(now), body, _sign("", str(now), body), now=now)  # secret not configured


def test_an_unset_secret_never_matches():
    assert secrets_match("s3cret", "s3cret")
    assert not secrets_match("s3cret", "nope") and not secrets_match("s3cret", None)
    assert not secrets_match("", "")


# ---------- the shared retry client ----------

def make_http(responses: list):
    """An HttpClient whose server answers with `responses` in order. Returns (client, calls, sleeps)."""
    calls, sleeps = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = HttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleep, rng=lambda: 1.0)
    return client, calls, sleeps


async def get(client: HttpClient):
    return await client.request("hubspot", "GET", "https://api.test/x", endpoint="/x", timeout=1)


async def test_429_waits_for_retry_after_then_succeeds():
    client, calls, sleeps = make_http([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200)])
    assert (await get(client)).status_code == 200
    assert len(calls) == 2 and sleeps == [3.0]


async def test_without_retry_after_the_backoff_doubles():
    client, calls, sleeps = make_http([httpx.Response(503)] * 3 + [httpx.Response(200)])
    await get(client)
    assert sleeps == [0.5, 1.0, 2.0]  # rng fixed at 1.0, so these are the upper bounds of the jitter


async def test_it_stops_after_four_tries():
    client, calls, _ = make_http([httpx.Response(500, text="boom")] * 10)
    with pytest.raises(ApiError) as error:
        await get(client)
    assert len(calls) == 4 and error.value.status == 500 and "boom" in str(error.value)


async def test_a_client_error_is_not_retried():
    client, calls, sleeps = make_http([httpx.Response(400, text="bad property")])
    with pytest.raises(ApiError):
        await get(client)
    assert len(calls) == 1 and sleeps == []


async def test_a_timeout_is_retried():
    client, calls, _ = make_http([httpx.ReadTimeout("slow"), httpx.ConnectError("down"), httpx.Response(200)])
    assert (await get(client)).status_code == 200
    assert len(calls) == 3


# ---------- suppression rules ----------

TODAY = Context(today=date(2026, 11, 14))


def test_no_crm_history_means_reach_out():
    assert evaluate(CrmState(), TODAY).verdict == "reach_out"


@pytest.mark.parametrize("state,rule,reason", [
    (CrmState(do_not_contact=True), "do_not_contact", "Marked do-not-contact in HubSpot"),
    (CrmState(open_deals=[OpenDeal("Rollout", "Proposal", "Priya")]), "open_deal",
     "Open deal, owner Priya, stage Proposal"),
    (CrmState(open_deals=[OpenDeal("A", "Demo"), OpenDeal("B", "Proposal")]), "open_deal",
     "Open deal, stage Demo (+1 more)"),
    (CrmState(company_lifecycle_stage="customer"), "existing_customer", "Already a customer"),
    (CrmState(last_contacted=date(2026, 11, 2)), "recently_contacted", "Contacted 12 days ago, on 2026-11-02"),
])
def test_each_rule_holds_with_its_reason(state, rule, reason):
    verdict = evaluate(state, TODAY)
    assert (verdict.verdict, verdict.rule, verdict.reason) == ("hold", rule, reason)


def test_contact_30_days_ago_no_longer_suppresses():
    assert evaluate(CrmState(last_contacted=date(2026, 10, 15)), TODAY).verdict == "reach_out"  # exactly 30 days
    assert evaluate(CrmState(last_contacted=date(2026, 10, 16)), TODAY).verdict == "hold"       # 29 days


def test_do_not_contact_outranks_everything():
    state = CrmState(do_not_contact=True, open_deals=[OpenDeal("A", "Demo")], lifecycle_stage="customer")
    assert evaluate(state, TODAY).rule == "do_not_contact"


def test_crm_state_survives_storage():
    state = CrmState(contact_id="1", last_contacted=date(2026, 11, 2), open_deals=[OpenDeal("A", "Demo", "Priya")])
    assert CrmState.from_dict(state.to_dict()) == state


# ---------- fact sheet helpers and the Clay payload ----------

@pytest.mark.parametrize("started,expected", [
    (date(2024, 8, 1), "2 years 3 months"), (date(2025, 11, 1), "1 year"), (date(2026, 10, 20), "1 month"),
    (date(2026, 11, 1), "0 months"), (date(2027, 1, 1), None), (None, None),
])
def test_tenure(started, expected):
    assert tenure(started, date(2026, 11, 14)) == expected


def test_clay_payload_is_cleaned():
    payload = ClayCallback.model_validate({
        "linkedin_url": "https://linkedin.com/in/jane-doe",
        "person": {"full_name": " Jane Doe ", "title": "", "email": "Jane@Acme.IO", "role_started_on": "2024-08"},
        "company": {"domain": "https://www.Acme.io/about", "employee_count": "1,200", "industry": ""},
    })
    assert payload.person.full_name == "Jane Doe" and payload.person.title is None
    assert payload.person.email == "jane@acme.io" and payload.person.role_started_on == date(2024, 8, 1)
    assert payload.company.domain == "acme.io" and payload.company.employee_count == 1200
    assert payload.company.industry is None


def test_clay_payload_with_unusable_values_does_not_crash():
    payload = ClayCallback.model_validate({
        "linkedin_url": "x", "person": {"email": "not-an-email", "role_started_on": "last summer"},
        "company": {"employee_count": "201-500", "domain": ""}})
    assert payload.person.email is None and payload.person.role_started_on is None
    assert payload.company.employee_count is None and payload.company.domain is None


# ---------- the fact checker ----------

FACTS = {
    "seller": {"name": "Northwall", "sells": "compliance automation for ISO 27001, SOC 2, GDPR and TISAX"},
    "person": {"name": "Jörg Müller-Schmidt", "title": "Head of Security", "time_in_role": "2 years 3 months"},
    "company": {"name": "Acme", "industry": "Fintech", "employees": 240, "country": "Germany",
                "description": "Payments for online shops."},
    "crm": {"company_in_hubspot": True, "last_contacted": "2026-09-02"},
    "verdict": "reach_out",
}
GOOD = {
    "verdict": "reach_out", "hold_reason": "",
    "headline": "Jörg Müller-Schmidt leads security at Acme, a Fintech with 240 employees.",
    "person": "Head of Security for 2 years 3 months. Senior, likely owns audits.",
    "company": "Acme builds payments for online shops in Germany. Last contacted on 2 September 2026.",
    "angle": "Ask how Acme's team prepares for ISO 27001 and SOC 2 audits, and offer Northwall as a shortcut.",
    "questions": ["Who runs your ISO 27001 audit today?", "Is GDPR evidence collected by hand?"],
}


def test_a_brief_built_only_from_the_facts_passes():
    assert check(GOOD, FACTS) == []


@pytest.mark.parametrize("field,text,expected", [
    ("company", "Acme has 300 employees.", "numbers not in facts: ['300']"),
    ("angle", "Mention their recent Series B.", "names or terms not in facts: ['Series', 'B']"),
    ("headline", "A former Google engineer now at Acme.", "names or terms not in facts: ['Google']"),
    ("company", "Acme opened an office in Munich in 2025.", "numbers not in facts: ['2025']"),
    ("angle", "word " * 121, "too long"),
    ("headline", "", "headline missing"),
])
def test_anything_not_in_the_facts_is_rejected(field, text, expected):
    problems = check({**GOOD, field: text}, FACTS)
    assert any(expected in p for p in problems), problems


def test_verdict_and_hold_reason_must_match_the_code():
    hold_facts = {**FACTS, "verdict": "hold", "hold_reason": "Already a customer"}
    assert any("verdict changed" in p for p in check(GOOD, hold_facts))
    assert any("hold_reason missing" in p for p in check({**GOOD, "verdict": "hold"}, hold_facts))
    assert check({**GOOD, "verdict": "hold", "hold_reason": "Already a customer."}, hold_facts) == []
    assert any("hold_reason given" in p for p in check({**GOOD, "hold_reason": "Because."}, FACTS))


def test_questions_must_be_exactly_two():
    assert check({**GOOD, "questions": ["Only one?"]}, FACTS) == ["questions must be exactly 2 strings"]


def test_the_template_brief_always_passes_the_checker():
    for facts in (FACTS, {**FACTS, "verdict": "hold", "hold_reason": "Open deal, owner Priya, stage Proposal",
                          "crm": {"open_deals": [{"name": "Rollout", "stage": "Proposal", "owner": "Priya"}]}},
                  {"seller": FACTS["seller"], "person": {"name": "Jane Doe"}, "company": {}, "crm": {},
                   "verdict": "reach_out"}):
        assert check(template_brief(facts), facts) == []


# ---------- the Claude wrapper ----------

async def test_without_an_api_key_the_template_is_used():
    result = await Briefer(None, "claude-sonnet-5-5").write(FACTS)
    assert (result.source, result.check_passed, result.model) == ("template", None, None)
    assert "no Claude API key" in result.fallback_reason


async def test_a_refusal_falls_back_to_the_template():
    claude = FakeClaude()
    claude.stop_reason = "refusal"
    result = await Briefer(claude, "claude-sonnet-5-5").write(FACTS)
    assert result.source == "template" and "declined" in result.fallback_reason
    assert result.input_tokens == 420  # tokens are still recorded: the call was paid for


async def test_a_claude_outage_falls_back_to_the_template():
    claude = FakeClaude()
    claude.error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    result = await Briefer(claude, "claude-sonnet-5-5").write(FACTS)
    assert result.source == "template" and "connection error" in result.fallback_reason


async def test_a_good_reply_is_accepted():
    claude = FakeClaude()
    claude.reply = lambda facts: GOOD
    result = await Briefer(claude, "claude-sonnet-5-5").write(FACTS)
    assert (result.source, result.check_passed, result.fallback_reason) == ("claude", True, None)
    assert result.brief == GOOD
