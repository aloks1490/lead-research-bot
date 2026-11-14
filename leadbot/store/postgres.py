"""Postgres Store (Supabase). Plain SQL through psycopg, so every query can be read and run by hand.

Deduplication and idempotency live in the database, not in Python:
  - `on conflict (unique key) do nothing / do update` on every insert
  - status changes are compare-and-set: `update ... where id = %s and status = %s`
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from leadbot.store import BRIEF_COLUMNS, JSON_COLUMNS, TERMINAL, BriefRecord, Company, Job, Prospect, Status

_TERMINAL = [s.value for s in TERMINAL]


def _job(row: dict | None) -> Job | None:
    return Job(**{**row, "id": str(row["id"])}) if row else None


class PostgresStore:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    @classmethod
    async def connect(cls, database_url: str, max_size: int = 5) -> PostgresStore:
        # prepare_threshold=None: Supabase's pooler runs in transaction mode, which cannot keep prepared statements.
        pool = AsyncConnectionPool(database_url, min_size=1, max_size=max_size, open=False,
                                   kwargs={"row_factory": dict_row, "prepare_threshold": None, "autocommit": True})
        await pool.open(wait=True, timeout=15)
        return cls(pool)

    async def _one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone() if cur.description else None

    async def _all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def _count(self, sql: str, params: tuple | dict = ()) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    # ---------- jobs ----------

    async def create_job(self, *, slack_event_id, raw_text, requested_by, channel, thread_ts) -> Job | None:
        return _job(await self._one(
            """insert into research_jobs (slack_event_id, raw_text, requested_by, channel, thread_ts)
               values (%s, %s, %s, %s, %s)
               on conflict (slack_event_id) do nothing
               returning *""",
            (slack_event_id, raw_text, requested_by, channel, thread_ts)))

    async def get_job(self, job_id: str) -> Job | None:
        return _job(await self._one("select * from research_jobs where id = %s", (job_id,)))

    async def claim(self, job_id: str) -> Job | None:
        return _job(await self._one(
            "update research_jobs set attempts = attempts + 1 where id = %s returning *", (job_id,)))

    async def transition(self, job_id, from_status, to_status, linkedin_url_norm=None) -> bool:
        return await self._count(
            """update research_jobs
               set status = %s, updated_at = now(), linkedin_url_norm = coalesce(%s, linkedin_url_norm)
               where id = %s and status = %s""",
            (to_status.value, linkedin_url_norm, job_id, from_status.value)) == 1

    async def fail_job(self, job_id: str, error: str) -> bool:
        return await self._count(
            """update research_jobs set status = 'failed', last_error = %s, updated_at = now()
               where id = %s and status <> all(%s)""",
            (error, job_id, _TERMINAL)) == 1

    async def jobs_in(self, statuses: set[Status], updated_before: datetime | None = None) -> list[Job]:
        rows = await self._all(
            """select * from research_jobs
               where status = any(%s) and (%s::timestamptz is null or updated_at < %s)
               order by created_at""",
            ([s.value for s in statuses], updated_before, updated_before))
        return [_job(r) for r in rows]

    async def enriching_jobs(self, linkedin_url_norm: str) -> list[Job]:
        rows = await self._all(
            "select * from research_jobs where status = 'enriching' and linkedin_url_norm = %s", (linkedin_url_norm,))
        return [_job(r) for r in rows]

    # ---------- cache ----------

    async def get_prospect(self, linkedin_url_norm: str) -> Prospect | None:
        row = await self._one("select * from prospects where linkedin_url_norm = %s", (linkedin_url_norm,))
        return Prospect(**{**row, "id": str(row["id"])}) if row else None

    async def upsert_prospect(self, prospect: Prospect) -> Prospect:
        params = prospect.model_dump(exclude={"id"})
        params["raw"] = Jsonb(params["raw"])
        row = await self._one(
            """insert into prospects (linkedin_url_norm, name, title, email, location, role_started_on,
                                      company_domain, enriched_at, source, raw)
               values (%(linkedin_url_norm)s, %(name)s, %(title)s, %(email)s, %(location)s, %(role_started_on)s,
                       %(company_domain)s, %(enriched_at)s, %(source)s, %(raw)s)
               on conflict (linkedin_url_norm) do update set
                   name = excluded.name, title = excluded.title, email = excluded.email,
                   location = excluded.location, role_started_on = excluded.role_started_on,
                   company_domain = excluded.company_domain, enriched_at = excluded.enriched_at,
                   source = excluded.source, raw = excluded.raw
               returning *""",
            params)
        return Prospect(**{**row, "id": str(row["id"])})

    async def get_company(self, domain: str) -> Company | None:
        row = await self._one("select * from companies where domain = %s", (domain,))
        return Company(**{k: v for k, v in row.items() if k != "updated_at"}) if row else None

    async def upsert_company(self, company: Company) -> None:
        params = company.model_dump()
        params["raw"] = Jsonb(params["raw"])
        await self._count(
            """insert into companies (domain, name, industry, employee_count, country, description, raw)
               values (%(domain)s, %(name)s, %(industry)s, %(employee_count)s, %(country)s, %(description)s, %(raw)s)
               on conflict (domain) do update set
                   name = excluded.name, industry = excluded.industry, employee_count = excluded.employee_count,
                   country = excluded.country, description = excluded.description, raw = excluded.raw,
                   updated_at = now()""",
            params)

    # ---------- briefs ----------

    async def get_brief(self, job_id: str) -> BriefRecord | None:
        row = await self._one("select * from briefs where job_id = %s", (job_id,))
        if row is None:
            return None
        row = {k: (str(v) if k in ("job_id", "prospect_id") and v else v) for k, v in row.items()}
        return BriefRecord(**{k: row[k] for k in ("job_id", *BRIEF_COLUMNS)})

    async def upsert_brief(self, job_id: str, **columns: Any) -> None:
        unknown = set(columns) - set(BRIEF_COLUMNS)
        if unknown:
            raise ValueError(f"unknown brief columns: {sorted(unknown)}")
        names = list(columns)  # column names come from BRIEF_COLUMNS above, never from user input
        values = [Jsonb(columns[n]) if n in JSON_COLUMNS and columns[n] is not None else columns[n] for n in names]
        insert_cols = ", ".join(["job_id", *names])
        placeholders = ", ".join(["%s"] * (len(names) + 1))
        updates = ", ".join([f"{n} = excluded.{n}" for n in names] + ["updated_at = now()"])
        await self._count(
            f"insert into briefs ({insert_cols}) values ({placeholders}) "
            f"on conflict (job_id) do update set {updates}",
            (job_id, *values))

    async def close(self) -> None:
        await self.pool.close()
