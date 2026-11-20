"""treg (treg.to): one API key in front of many enrichment providers. Replaces Clay, whose free plan dropped the
webhook source and the HTTP API column this bot used.

Three calls per new prospect, each `POST https://treg.to/call/<tool>` with an `X-Treg-Token` header:

    treg.people.enrich       LinkedIn URL      -> name, location, every role (title, company, start date, current?)
    treg.people.email.find   LinkedIn URL      -> work email
    treg.companies.enrich    company LinkedIn  -> name, domain, industry, size, location, description
                             URL, or a domain

treg "routes" each call: it asks providers one by one until one answers, and bills only answers. Its own
response says who answered (`_treg.served_by`) and what that cost (header `X-Treg-Cost-Micro`), both logged.

Unlike Clay, treg answers in the same request, so there is no callback to wait for. A miss (nobody found)
is an HTTP 200 with empty output; it can take ~40 s while treg tries every provider.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from leadbot.clay import ClayCompany, ClayPerson
from leadbot.http import ApiError, HttpClient

log = logging.getLogger("leadbot.treg")

# A call that finds nobody makes treg try every provider in turn: 40 s for a person, minutes for an email. So each
# call gets one try (no retry after a timeout: it would repeat the whole provider list) and a fixed budget.
# Measured on a real profile: retrying a timed-out email lookup 4 times turned a 90 s miss into 6 minutes.
PERSON_TIMEOUT = 90.0
OPTIONAL_TIMEOUT = 45.0  # email and company: the brief can be written without them
FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com",
            "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.de", "gmx.net", "web.de"}
MAX_DESCRIPTION = 300  # providers return whole "about" pages; the brief needs one or two sentences


@dataclass
class Enrichment:
    person: ClayPerson
    company: ClayCompany
    raw: dict = field(default_factory=dict)  # every treg response, kept to backfill new fields for free
    cost_micro: int = 0                      # what the lookups cost, in millionths of a dollar


def _short(text: str | None) -> str | None:
    """The first sentences of a long description, cut at a sentence end, at most MAX_DESCRIPTION characters."""
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= MAX_DESCRIPTION:
        return text
    cut = text[:MAX_DESCRIPTION]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > 0 else cut.rsplit(" ", 1)[0]


def _current_role(raw: dict) -> dict:
    """The current role from a provider's raw profile, if it lists roles (BlitzAPI does). People can hold several
    current roles; the first one, at the top of the profile, is the one treg's own summary uses too."""
    experiences = ((raw or {}).get("person") or {}).get("experiences") or []
    return next((e for e in experiences if e.get("job_is_current")), {})


def _location(raw: dict, fallback: str | None) -> str | None:
    profile = (raw or {}).get("person") or (raw or {}).get("data") or {}  # BlitzAPI or Crawl4AI format
    place = profile.get("location") or {}
    if not isinstance(place, dict):
        return fallback
    parts = [place.get("city"), place.get("country_code")]
    return ", ".join(p for p in parts if p) or fallback


class Treg:
    def __init__(self, http: HttpClient, token: str, base_url: str = "https://treg.to"):
        self.http = http
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Treg-Token": token, "Content-Type": "application/json"}

    async def _call(self, tool: str, body: dict, timeout: float = PERSON_TIMEOUT) -> tuple[dict, int]:
        """One routed call. Returns (response JSON, cost in micro-dollars). Errors and retries: the shared client."""
        response = await self.http.request("treg", "POST", f"{self.base_url}/call/{tool}", endpoint=tool,
                                           timeout=timeout, retry_timeouts=False, json=body, headers=self.headers)
        data = response.json()
        cost = int(response.headers.get("X-Treg-Cost-Micro") or 0)
        log.info("treg answered", extra={"tool": tool, "outcome": (data.get("_treg") or {}).get("outcome"),
                                         "served_by": response.headers.get("X-Treg-Served-By"),
                                         "cost_micro": cost})
        return data, cost

    async def enrich(self, linkedin_url_norm: str) -> Enrichment:
        """Person first (it says which company to look up), then the email and the company at the same time.
        Only the person lookup is essential: if the email or company lookup fails, the brief is still written
        from what was found, and the failure is logged."""
        if not self.token:
            raise ApiError("treg", "config", None, "TREG_TOKEN is not set")
        url = f"https://www.{linkedin_url_norm}"
        person_data, cost = await self._call("treg.people.enrich", {"linkedin_url": url})
        found = person_data.get("output") or {}
        raw_person = person_data.get("raw") or {}
        if not found.get("full_name"):
            return Enrichment(ClayPerson(), ClayCompany(), {"person": person_data}, cost)  # nobody behind the URL

        role = _current_role(raw_person)
        email_task = self._optional("treg.people.email.find", {"linkedin_url": url})
        company_body = ({"linkedin_url": role["company_linkedin_url"]} if role.get("company_linkedin_url")
                        else {"domain": found["company_domain"]} if found.get("company_domain") else None)
        company_task = self._optional("treg.companies.enrich", company_body) if company_body else _nothing()
        (email_data, email_cost), (company_data, company_cost) = await asyncio.gather(email_task, company_task)

        email = (email_data.get("output") or {}).get("email")
        company_found = company_data.get("output") or {}
        email_domain = email.split("@")[-1].lower() if email and "@" in email else None
        if not company_found.get("name") and email_domain and email_domain not in FREEMAIL:
            # No company record, but a work email says where they work: look the company up by its domain.
            company_data, extra = await self._optional("treg.companies.enrich", {"domain": email_domain})
            company_cost += extra
            company_found = company_data.get("output") or {}

        person = ClayPerson(
            full_name=found.get("full_name"),
            title=role.get("job_title") or found.get("title"),
            email=email,
            location=_location(raw_person, found.get("location")),
            role_started_on=role.get("job_start_date"),
        )
        company = ClayCompany(
            name=company_found.get("name") or role.get("company_name") or found.get("company"),
            domain=company_found.get("domain") or found.get("company_domain") or role.get("company_domain")
            or (email_domain if email_domain not in FREEMAIL else None),
            industry=company_found.get("industry"),
            employee_count=company_found.get("employees"),
            country=company_found.get("location"),
            description=_short(company_found.get("description")),
        )
        raw = {"person": person_data, "email": email_data, "company": company_data}
        return Enrichment(person, company, raw, cost + email_cost + company_cost)

    async def _optional(self, tool: str, body: dict) -> tuple[dict, int]:
        try:
            return await self._call(tool, body, timeout=OPTIONAL_TIMEOUT)
        except ApiError as e:
            log.warning("optional treg lookup failed, continuing without it",
                        extra={"tool": tool, "error_text": str(e)})
            return {}, 0


async def _nothing() -> tuple[dict, int]:
    return {}, 0

