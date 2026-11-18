"""Contract tests for the data layer. The same tests run against both stores:

- MemoryStore: always.
- PostgresStore: when TEST_DATABASE_URL is set (CI starts a Postgres container; locally:
  TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest tests/test_store.py).
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from leadbot.store import Company, Prospect, Status
from leadbot.store.memory import MemoryStore

MIGRATIONS = sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))


@pytest.fixture(params=["memory", "postgres"])
async def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    from leadbot.store.postgres import PostgresStore
    pg = await PostgresStore.connect(url)
    async with pg.pool.connection() as conn:
        for migration in MIGRATIONS:
            await conn.execute(migration.read_text())
        await conn.execute("truncate briefs, research_jobs, prospects, companies")
    yield pg
    await pg.close()


async def new_job(store, event_id="Ev1"):
    return await store.create_job(slack_event_id=event_id, raw_text="<url>", requested_by="U1", channel="C1",
                                  thread_ts="1.0")


async def test_same_slack_event_inserts_only_once(store):
    first = await new_job(store)
    assert first.status == Status.RECEIVED and first.attempts == 0
    assert await new_job(store) is None
    assert (await new_job(store, "Ev2")).id != first.id


async def test_transition_is_compare_and_set(store):
    job = await new_job(store)
    assert await store.transition(job.id, Status.RECEIVED, Status.NORMALISED, linkedin_url_norm="linkedin.com/in/a")
    # A second worker that still thinks the job is `received` loses.
    assert not await store.transition(job.id, Status.RECEIVED, Status.NORMALISED)
    assert await store.transition(job.id, Status.NORMALISED, Status.CACHE_MISS)
    stored = await store.get_job(job.id)
    assert stored.status == Status.CACHE_MISS
    assert stored.linkedin_url_norm == "linkedin.com/in/a"  # kept when a later transition does not pass it


async def test_claim_counts_attempts(store):
    job = await new_job(store)
    assert (await store.claim(job.id)).attempts == 1
    assert (await store.claim(job.id)).attempts == 2


async def test_fail_job_only_once_and_never_after_done(store):
    job = await new_job(store)
    assert await store.fail_job(job.id, "boom")
    assert not await store.fail_job(job.id, "boom again")
    stored = await store.get_job(job.id)
    assert (stored.status, stored.last_error) == (Status.FAILED, "boom")

    done = await new_job(store, "Ev2")
    await store.transition(done.id, Status.RECEIVED, Status.DONE)
    assert not await store.fail_job(done.id, "too late")


async def test_jobs_in_filters_by_status_and_age(store):
    job = await new_job(store)
    await store.transition(job.id, Status.RECEIVED, Status.ENRICHING, linkedin_url_norm="linkedin.com/in/a")
    future, past = datetime.now(UTC) + timedelta(minutes=1), datetime.now(UTC) - timedelta(minutes=1)
    assert [j.id for j in await store.jobs_in({Status.ENRICHING}, future)] == [job.id]
    assert await store.jobs_in({Status.ENRICHING}, past) == []
    assert await store.jobs_in({Status.RECEIVED}, future) == []
    assert [j.id for j in await store.jobs_in({Status.ENRICHING})] == [job.id]  # no age filter
    assert [j.id for j in await store.enriching_jobs("linkedin.com/in/a")] == [job.id]
    assert await store.enriching_jobs("linkedin.com/in/b") == []


async def test_prospect_and_company_upserts_overwrite(store):
    await store.upsert_company(Company(domain="acme.io", name="Acme", employee_count=200, raw={"v": 1}))
    await store.upsert_company(Company(domain="acme.io", name="Acme", employee_count=240, raw={"v": 2}))
    company = await store.get_company("acme.io")
    assert (company.employee_count, company.raw) == (240, {"v": 2})

    when = datetime(2026, 11, 1, tzinfo=UTC)
    first = await store.upsert_prospect(Prospect(
        linkedin_url_norm="linkedin.com/in/a", name="Jane", company_domain="acme.io", enriched_at=when,
        role_started_on=date(2024, 8, 1), raw={"v": 1}))
    second = await store.upsert_prospect(Prospect(
        linkedin_url_norm="linkedin.com/in/a", name="Jane Doe", company_domain="acme.io",
        enriched_at=when + timedelta(days=1)))
    assert first.id == second.id  # same person, same row
    stored = await store.get_prospect("linkedin.com/in/a")
    assert stored.name == "Jane Doe" and stored.enriched_at == when + timedelta(days=1)
    assert await store.get_prospect("linkedin.com/in/nobody") is None


async def test_brief_upsert_updates_only_the_given_columns(store):
    job = await new_job(store)
    await store.upsert_brief(job.id, suppression={"verdict": "hold"})
    await store.upsert_brief(job.id, brief={"headline": "x"}, source="claude", check_passed=True, input_tokens=10)
    await store.upsert_brief(job.id, slack_ts="1.2", prompt_version="brief@2+abcd1234")
    record = await store.get_brief(job.id)
    assert record.suppression == {"verdict": "hold"} and record.brief == {"headline": "x"}
    assert (record.source, record.check_passed, record.input_tokens, record.slack_ts) == ("claude", True, 10, "1.2")
    assert record.prompt_version == "brief@2+abcd1234" and record.hubspot_note_id is None
    with pytest.raises(ValueError):
        await store.upsert_brief(job.id, not_a_column=1)
