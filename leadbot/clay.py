"""Clay: send a profile for enrichment, and parse what Clay posts back.

Outbound: POST {linkedin_url, job_id} to the Clay table's webhook source.
Inbound:  an HTTP API column at the end of the Clay table posts the enriched row to POST /webhooks/clay.
          docs/clay-setup.md has the exact body to paste into that column.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from leadbot.http import HttpClient

TIMEOUT = 10.0


class Clay:
    def __init__(self, http: HttpClient, webhook_url: str, token: str = ""):
        self.http = http
        self.webhook_url = webhook_url
        self.headers = {"x-clay-webhook-auth": token} if token else {}

    async def enrich(self, linkedin_url_norm: str, job_id: str) -> None:
        # The webhook URL identifies the table and works like a secret, so only a label is logged.
        await self.http.request("clay", "POST", self.webhook_url, endpoint="table webhook", timeout=TIMEOUT,
                                ok=(200, 201, 202, 204), headers=self.headers,
                                json={"linkedin_url": f"https://www.{linkedin_url_norm}", "job_id": job_id})


# ---------- the callback payload ----------

def _blank_to_none(value: Any) -> Any:
    """Clay sends an empty string for a cell it could not fill."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class ClayPerson(BaseModel):
    full_name: str | None = None
    title: str | None = None
    email: str | None = None
    location: str | None = None
    role_started_on: date | None = None

    _clean = field_validator("*", mode="before")(_blank_to_none)

    @field_validator("role_started_on", mode="before")
    @classmethod
    def _parse_date(cls, value: Any) -> Any:
        """Accepts '2024-08-01', '2024-08' and full timestamps. Anything else becomes None instead of an error."""
        if not isinstance(value, str) or not value.strip():
            return value if isinstance(value, date) else None
        match = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", value.strip())
        if not match:
            return None
        try:
            return date(int(match[1]), int(match[2]), int(match[3] or 1))
        except ValueError:
            return None

    @field_validator("email")
    @classmethod
    def _lower_email(cls, value: str | None) -> str | None:
        return value.lower() if value and "@" in value else None


class ClayCompany(BaseModel):
    name: str | None = None
    domain: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None
    description: str | None = None

    _clean = field_validator("*", mode="before")(_blank_to_none)

    @field_validator("employee_count", mode="before")
    @classmethod
    def _parse_count(cls, value: Any) -> Any:
        """'1,200' -> 1200. A range such as '201-500' is not one number, so it becomes None (it stays in `raw`)."""
        if isinstance(value, str):
            digits = value.replace(",", "").strip()
            return int(digits) if digits.isdigit() else None
        return value

    @field_validator("domain")
    @classmethod
    def _bare_domain(cls, value: str | None) -> str | None:
        """'https://www.Acme.io/about' -> 'acme.io'. The bare domain is the company's unique key."""
        if not value:
            return None
        domain = re.sub(r"^https?://", "", value.lower()).split("/")[0]
        return domain.removeprefix("www.") or None


class ClayCallback(BaseModel):
    job_id: str | None = None
    linkedin_url: str
    person: ClayPerson = Field(default_factory=ClayPerson)
    company: ClayCompany = Field(default_factory=ClayCompany)
    received_at: datetime | None = None  # set by us, not by Clay
