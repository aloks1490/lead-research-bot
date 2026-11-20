"""The treg enrichment route: a signed Slack mention in, a brief out, with treg (faked) as the data source."""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from leadbot.clay import ClayCompany
from leadbot.store import Status
from leadbot.treg import _location, _short
from tests.conftest import JANE_NORM

JANE = f"https://www.{JANE_NORM}"
ACME_LI = "https://www.linkedin.com/company/acme"
ROLE = {"company_name": "Acme", "job_title": "Head of Security", "company_linkedin_url": ACME_LI,
        "company_domain": None, "job_start_date": "2024-08-01", "job_is_current": True}
ACME = {"name": "Acme", "domain": "acme.io", "industry": "Fintech", "employees": "201-500",
        "location": "Berlin DE", "description": "Payments for online shops. " + "More text. " * 60}


@pytest.fixture
def treg_world(world):
    world.settings.enrichment_provider = "treg"
    return world


async def test_a_new_profile_is_enriched_through_treg_in_one_run(treg_world):
    w = treg_world
    w.treg.add_person(JANE, "Jane Doe", [ROLE], email="Jane.Doe@acme.io")
    w.treg.companies[ACME_LI] = ACME
    await w.mention()

    assert w.job().status == Status.DONE  # no callback to wait for
    assert w.clay_requests == []
    assert [tool for tool, _ in w.treg.calls] == ["treg.people.enrich", "treg.people.email.find",
                                                  "treg.companies.enrich"]
    assert dict(w.treg.calls)["treg.companies.enrich"] == {"linkedin_url": ACME_LI}  # the company of the role

    prospect = w.store.prospects[JANE_NORM]
    assert (prospect.name, prospect.title, prospect.email) == ("Jane Doe", "Head of Security", "jane.doe@acme.io")
    assert prospect.role_started_on == date(2024, 8, 1) and prospect.location == "Berlin, DE"
    assert prospect.source == "treg" and prospect.raw["cost_micro"] == 1150
    company = w.store.companies["acme.io"]
    assert (company.employee_count, company.employee_range) == (None, "201-500")
    assert len(company.description) <= 300

    facts = w.brief().facts
    assert facts["company"]["employees"] == "201-500"
    assert "time_in_role" in facts["person"]
    assert "Filler" not in str(facts)  # the provider's "about" text never reaches the model
    assert w.brief().source == "claude" and w.brief().check_passed  # the size band passes the fact checker


async def test_a_second_request_hits_the_cache_and_skips_treg(treg_world):
    w = treg_world
    w.treg.add_person(JANE, "Jane Doe", [ROLE], email="jane.doe@acme.io")
    w.treg.companies[ACME_LI] = ACME
    await w.mention()
    calls = len(w.treg.calls)
    await w.mention()
    assert w.job().status == Status.DONE and len(w.treg.calls) == calls


async def test_nobody_found_tells_the_ae(treg_world):
    await treg_world.mention()
    assert treg_world.job().status == Status.FAILED
    assert "could not find a person" in treg_world.thread_messages()[-1]["text"]
    assert [tool for tool, _ in treg_world.treg.calls] == ["treg.people.enrich"]  # no email or company lookups


async def test_the_first_current_role_is_used(treg_world):
    w = treg_world
    old = {**ROLE, "company_name": "Oldco", "job_title": "Engineer", "job_is_current": False}
    side = {**ROLE, "company_name": "Sideco", "job_title": "Advisor", "company_linkedin_url": "https://x/sideco"}
    w.treg.add_person(JANE, "Jane Doe", [old, ROLE, side], email="jane.doe@acme.io")
    w.treg.companies[ACME_LI] = ACME
    await w.mention()
    assert w.store.prospects[JANE_NORM].title == "Head of Security"


async def test_no_company_record_falls_back_to_the_email_domain(treg_world):
    w = treg_world
    w.treg.add_person(JANE, "Jane Doe", [{**ROLE, "company_linkedin_url": None}], email="jane.doe@acme.io")
    w.treg.companies["acme.io"] = ACME
    await w.mention()
    assert dict(w.treg.calls)["treg.companies.enrich"] == {"domain": "acme.io"}
    assert w.store.prospects[JANE_NORM].company_domain == "acme.io"


async def test_a_free_email_domain_is_not_used_as_the_company(treg_world):
    w = treg_world
    w.treg.add_person(JANE, "Jane Doe", [{**ROLE, "company_linkedin_url": None}], email="jane@gmail.com")
    await w.mention()
    assert w.store.prospects[JANE_NORM].company_domain is None
    assert w.job().status == Status.DONE  # still briefed, from the person alone


async def test_a_failing_email_lookup_does_not_stop_the_brief(treg_world):
    w = treg_world
    w.treg.add_person(JANE, "Jane Doe", [ROLE], email="jane.doe@acme.io")
    w.treg.companies[ACME_LI] = ACME
    w.treg.fail["treg.people.email.find"] = httpx.Response(503, json={})
    await w.mention()
    assert w.job().status == Status.DONE
    assert w.store.prospects[JANE_NORM].email is None and w.alerts() == []


async def test_a_failing_person_lookup_fails_loudly(treg_world):
    treg_world.treg.fail["treg.people.enrich"] = httpx.Response(503, json={})
    await treg_world.mention()
    job = treg_world.job()
    assert job.status == Status.FAILED and "treg" in job.last_error
    assert job.id in treg_world.alerts()[0]["text"]


async def test_a_missing_token_fails_with_a_clear_error(treg_world):
    treg_world.runner.d.treg.token = ""
    await treg_world.mention()
    assert "TREG_TOKEN is not set" in treg_world.job().last_error
    assert treg_world.treg.calls == []


async def test_a_timed_out_email_lookup_is_not_retried(treg_world):
    # Found on the first real run: a routed email lookup that found nobody ran past the time limit, and retrying
    # it 4 times turned one slow miss into 6 minutes. treg already tries every provider inside one call.
    w = treg_world
    w.treg.add_person(JANE, "Jane Doe", [ROLE])
    w.treg.companies[ACME_LI] = ACME
    w.treg.fail["treg.people.email.find"] = httpx.ReadTimeout("slow")
    await w.mention()
    assert [tool for tool, _ in w.treg.calls].count("treg.people.email.find") == 1
    assert w.job().status == Status.DONE and w.store.prospects[JANE_NORM].email is None


async def test_a_timed_out_person_lookup_fails_after_one_try(treg_world):
    treg_world.treg.fail["treg.people.enrich"] = httpx.ReadTimeout("slow")
    await treg_world.mention()
    assert [tool for tool, _ in treg_world.treg.calls] == ["treg.people.enrich"]
    assert treg_world.job().status == Status.FAILED and "ReadTimeout" in treg_world.job().last_error


async def test_a_person_with_no_company_still_gets_a_model_brief(treg_world):
    # Also from the first real run: no company was found, the model rightly left "company" empty, and the
    # checker wrongly rejected that.
    w = treg_world
    w.treg.add_person(JANE, "Jane Doe", [{**ROLE, "company_linkedin_url": None, "company_name": None}])
    w.claude.reply = lambda facts: {
        "verdict": "reach_out", "hold_reason": "", "headline": "Jane Doe is Head of Security.",
        "person": "Head of Security in Berlin.", "company": "", "angle": "Ask how Jane handles audits today.",
        "questions": ["How do you prepare for audits?", "Who owns compliance?"]}
    await w.mention()
    assert w.brief().facts["company"] == {}
    assert (w.brief().source, w.brief().check_passed) == ("claude", True)
    person_block = w.thread_messages()[-1]["blocks"][2]["text"]["text"]
    assert "*Company*" not in person_block  # no empty heading in Slack


# ---------- small pieces ----------

@pytest.mark.parametrize("raw,expected", [
    ({"person": {"location": {"city": "Berlin", "country_code": "DE"}}}, "Berlin, DE"),     # BlitzAPI
    ({"data": {"location": {"city": "Pune City", "country_code": "IN"}}}, "Pune City, IN"),  # Crawl4AI
    ({"data": {"location": "somewhere"}}, "fallback"), ({}, "fallback"),
])
def test_location_is_read_from_either_provider_format(raw, expected):
    assert _location(raw, "fallback") == expected


@pytest.mark.parametrize("value,count,band", [
    ("51-200", None, "51-200"), ("10,001+", None, "10,001+"), ("1,200", 1200, None), (183, 183, None),
    ("201 - 500", None, "201-500"), ("lots", None, None),
])
def test_company_size_is_kept_as_a_number_or_a_band(value, count, band):
    company = ClayCompany(employee_count=value)
    assert (company.employee_count, company.employee_range) == (count, band)


def test_long_descriptions_are_cut_at_a_sentence():
    text = "First sentence here. " + "Second sentence is long. " * 30
    short = _short(text)
    assert len(short) <= 300 and short.endswith(".")
    assert _short("Short.") == "Short." and _short(None) is None
