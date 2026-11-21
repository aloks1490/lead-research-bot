"""The fit rules (leadbot/fit.py): is this person a likely buyer of what the seller sells?"""
from __future__ import annotations

import pytest

from leadbot.brief import template_brief
from leadbot.check import check
from leadbot.config import Settings
from leadbot.fit import Fit, Target, country_code, evaluate, headcount_bounds

TARGET = Target.from_settings(Settings(_env_file=None))


def fit(**kw) -> Fit:
    base = dict(title="Head of Security", person_location="Berlin, DE", company_name="Acme",
                company_country="Germany", employees=240, employee_band=None)
    return evaluate(target=TARGET, **{**base, **kw})


@pytest.mark.parametrize("text,code", [
    ("Berlin, DE", "DE"), ("Los Angeles US", "US"), ("Pune City IN", "IN"), ("Germany", "DE"),
    ("Zurich, Switzerland", "CH"), ("London, UK", "GB"), ("San Francisco, United States", "US"),
    ("Rotterdam", None), ("", None), (None, None),
])
def test_country_is_read_from_any_location_text(text, code):
    assert country_code(text) == code


@pytest.mark.parametrize("count,band,bounds", [
    (240, None, (240, 240)), (None, "51-200", (51, 200)), (None, "10,001+", (10001, 10**9)), (None, None, None),
])
def test_headcount_bounds(count, band, bounds):
    assert headcount_bounds(count, band) == bounds


def test_a_buyer_at_a_target_company_is_a_strong_fit():
    result = fit()
    assert result.level == "strong" and result.checks == {"market": "pass", "size": "pass", "role": "pass"}
    assert result.reasons == ["Germany is a target market", "240 employees is within 20-2,000",
                              "Head of Security is a buying role"]


@pytest.mark.parametrize("change,check_name,reason", [
    ({"company_country": "Los Angeles US"}, "market", "United States is outside the target markets"),
    ({"employees": None, "employee_band": "1-10"}, "size", "1-10 employees is below 20"),
    ({"employees": 50000}, "size", "50000 employees is above 2,000"),
    ({"title": "Marketing Manager"}, "role", "Marketing Manager is not a usual buying role"),
])
def test_any_failed_check_makes_a_weak_fit(change, check_name, reason):
    result = fit(**change)
    assert result.level == "weak" and result.checks[check_name] == "fail" and reason in result.reasons


@pytest.mark.parametrize("title,buyer", [
    ("Head of IT", True), ("CISO", True), ("Founder & CEO", True), ("Co-Founder, Executive Board Chair", True),
    ("VP Engineering", True), ("Data Protection Officer", True), ("Geschäftsführer", True),
    ("AI x GTM Engineer", False), ("Account Executive", False), ("Marketing Manager", False), ("CFO", False),
    ("Digital Marketing Lead", False),  # "it" must be a whole word, not part of "digital"
])
def test_buying_roles(title, buyer):
    assert (fit(title=title).checks["role"] == "pass") is buyer


def test_with_no_company_the_person_location_is_used_and_fit_is_not_strong():
    # The real case that started this: an engineer in India with no company found.
    result = fit(company_name=None, company_country=None, employees=None, person_location="Pune City IN",
                 title="AI x GTM Engineer")
    assert result.level == "weak" and "India is outside the target markets" in result.reasons
    assert "No company found" in result.reasons
    assert fit(company_name=None, company_country=None, employees=None).level == "unknown"  # a buyer in DE


def test_zero_employees_is_an_unknown_size_not_a_small_company():
    # Found on the live bot: treg gave 0 for a company with staff, and the fit said "0 employees is below 20".
    result = fit(employees=0)
    assert result.checks["size"] == "unknown" and not any("0 employees" in r for r in result.reasons)


def test_too_little_data_is_unknown_not_strong():
    assert fit(company_country=None, person_location=None, employees=None).level == "unknown"


def test_the_target_comes_from_settings():
    us_seller = Target.from_settings(Settings(_env_file=None, fit_countries="US,CA", fit_min_employees=1))
    result = evaluate(title="Founder & CEO", person_location="San Francisco, US", company_name="X",
                      company_country="US", employees=None, employee_band="1-10", target=us_seller)
    assert result.level == "strong"


def test_a_weak_fit_template_says_what_to_check_and_passes_the_checker():
    facts = {"seller": {"name": "Northwall", "sells": "compliance automation"},
             "person": {"name": "Nico Druelle", "title": "Founder & CEO"},
             "company": {"name": "The Revenue Architects", "employees": "1-10", "country": "Los Angeles US"},
             "crm": {}, "verdict": "reach_out",
             "fit": {"level": "weak", "reasons": ["United States is outside the target markets",
                                                  "1-10 employees is below 20"]}}
    brief = template_brief(facts)
    assert brief["angle"].startswith("Check fit before reaching out: United States is outside")
    assert check(brief, facts) == []
