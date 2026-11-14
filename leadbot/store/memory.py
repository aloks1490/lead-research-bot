"""In-memory Store for tests and local demos. Same behaviour as PostgresStore, but state is lost on restart."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from leadbot.store import BRIEF_COLUMNS, TERMINAL, BriefRecord, Company, Job, Prospect, Status


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryStore:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.event_ids: dict[str, str] = {}
        self.prospects: dict[str, Prospect] = {}
        self.companies: dict[str, Company] = {}
        self.briefs: dict[str, BriefRecord] = {}

    async def create_job(self, *, slack_event_id, raw_text, requested_by, channel, thread_ts) -> Job | None:
        if slack_event_id in self.event_ids:
            return None
        now = _now()
        job = Job(id=str(uuid.uuid4()), slack_event_id=slack_event_id, status=Status.RECEIVED, raw_text=raw_text,
                  requested_by=requested_by, channel=channel, thread_ts=thread_ts, created_at=now, updated_at=now)
        self.jobs[job.id] = job
        self.event_ids[slack_event_id] = job.id
        return job.model_copy()

    async def get_job(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        return job.model_copy() if job else None

    async def claim(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        job.attempts += 1
        return job.model_copy()

    async def transition(self, job_id, from_status, to_status, linkedin_url_norm=None) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status != from_status:
            return False
        job.status = to_status
        job.updated_at = _now()
        if linkedin_url_norm is not None:
            job.linkedin_url_norm = linkedin_url_norm
        return True

    async def fail_job(self, job_id: str, error: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status in TERMINAL:
            return False
        job.status, job.last_error, job.updated_at = Status.FAILED, error, _now()
        return True

    async def jobs_in(self, statuses, updated_before=None) -> list[Job]:
        return [j.model_copy() for j in self.jobs.values()
                if j.status in statuses and (updated_before is None or j.updated_at < updated_before)]

    async def enriching_jobs(self, linkedin_url_norm: str) -> list[Job]:
        return [j.model_copy() for j in self.jobs.values()
                if j.status == Status.ENRICHING and j.linkedin_url_norm == linkedin_url_norm]

    async def get_prospect(self, linkedin_url_norm: str) -> Prospect | None:
        prospect = self.prospects.get(linkedin_url_norm)
        return prospect.model_copy() if prospect else None

    async def upsert_prospect(self, prospect: Prospect) -> Prospect:
        existing = self.prospects.get(prospect.linkedin_url_norm)
        stored = prospect.model_copy(update={"id": existing.id if existing else str(uuid.uuid4())})
        self.prospects[stored.linkedin_url_norm] = stored
        return stored.model_copy()

    async def get_company(self, domain: str) -> Company | None:
        company = self.companies.get(domain)
        return company.model_copy() if company else None

    async def upsert_company(self, company: Company) -> None:
        self.companies[company.domain] = company.model_copy()

    async def get_brief(self, job_id: str) -> BriefRecord | None:
        brief = self.briefs.get(job_id)
        return brief.model_copy() if brief else None

    async def upsert_brief(self, job_id: str, **columns: Any) -> None:
        unknown = set(columns) - set(BRIEF_COLUMNS)
        if unknown:
            raise ValueError(f"unknown brief columns: {sorted(unknown)}")
        current = self.briefs.get(job_id) or BriefRecord(job_id=job_id)
        self.briefs[job_id] = current.model_copy(update=columns)

    async def close(self) -> None:
        pass
