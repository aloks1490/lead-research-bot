"""The data layer: row models and the Store interface.

Two implementations share this interface: PostgresStore (Supabase, production) and MemoryStore (tests and
local demos). tests/test_store.py runs the same contract tests against both, so they cannot drift apart.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class Status(StrEnum):
    RECEIVED = "received"      # row inserted, Slack acknowledged
    NORMALISED = "normalised"  # URL is a valid profile, "Researching..." posted
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    ENRICHING = "enriching"    # sent to Clay, waiting for the callback
    ENRICHED = "enriched"
    CHECKED = "checked"        # HubSpot read, suppression verdict stored
    BRIEFED = "briefed"        # brief written and stored
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"      # not a LinkedIn profile URL; the user was told


TERMINAL = {Status.DONE, Status.FAILED, Status.REJECTED}
# Statuses where our own process is doing the work. A job left in one of these was interrupted and can be resumed.
# ENRICHING is different: there we wait for Clay, and the sweeper times it out instead.
RESUMABLE = set(Status) - TERMINAL - {Status.ENRICHING}


class Job(BaseModel):
    id: str
    slack_event_id: str
    status: Status
    raw_text: str
    linkedin_url_norm: str | None = None
    requested_by: str | None = None
    channel: str
    thread_ts: str
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class Company(BaseModel):
    domain: str
    name: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None
    description: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Prospect(BaseModel):
    id: str | None = None  # assigned by the store
    linkedin_url_norm: str
    name: str | None = None
    title: str | None = None
    email: str | None = None
    location: str | None = None
    role_started_on: date | None = None
    company_domain: str | None = None
    enriched_at: datetime
    source: str = "clay"
    raw: dict[str, Any] = Field(default_factory=dict)


class BriefRecord(BaseModel):
    job_id: str
    prospect_id: str | None = None
    suppression: dict[str, Any] | None = None
    facts: dict[str, Any] | None = None
    brief: dict[str, Any] | None = None
    source: str | None = None
    model: str | None = None
    check_passed: bool | None = None
    fallback_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    slack_ts: str | None = None
    hubspot_note_id: str | None = None


BRIEF_COLUMNS = tuple(name for name in BriefRecord.model_fields if name != "job_id")
JSON_COLUMNS = {"suppression", "facts", "brief"}


class Store(Protocol):
    async def create_job(self, *, slack_event_id: str, raw_text: str, requested_by: str | None, channel: str,
                         thread_ts: str) -> Job | None:
        """Insert a job. Returns None when this slack_event_id was already seen (a Slack resend)."""

    async def get_job(self, job_id: str) -> Job | None: ...

    async def claim(self, job_id: str) -> Job | None:
        """Count one more attempt to run this job and return it."""

    async def transition(self, job_id: str, from_status: Status, to_status: Status,
                         linkedin_url_norm: str | None = None) -> bool:
        """Move a job to its next status only if it is still in `from_status` (compare-and-set).
        False means something else already moved it, and the caller must stop working on it."""

    async def fail_job(self, job_id: str, error: str) -> bool:
        """Mark a job failed. False if it had already finished, so the caller does not alert twice."""

    async def jobs_in(self, statuses: set[Status], updated_before: datetime | None = None) -> list[Job]:
        """Jobs in one of `statuses`; with `updated_before`, only those untouched since then. Used for recovery."""

    async def enriching_jobs(self, linkedin_url_norm: str) -> list[Job]:
        """Jobs waiting on Clay for this profile."""

    async def get_prospect(self, linkedin_url_norm: str) -> Prospect | None: ...

    async def upsert_prospect(self, prospect: Prospect) -> Prospect: ...

    async def get_company(self, domain: str) -> Company | None: ...

    async def upsert_company(self, company: Company) -> None: ...

    async def get_brief(self, job_id: str) -> BriefRecord | None: ...

    async def upsert_brief(self, job_id: str, **columns: Any) -> None:
        """Create the brief row for a job, or update only the given columns."""

    async def close(self) -> None: ...
