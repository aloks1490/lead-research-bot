"""The job runner: one request moves through a status machine, one step at a time.

    received -> normalised -> cache_hit ----------------------------> checked -> briefed -> done
                           -> cache_miss -> enriching -> enriched --/
    any step can end in `failed`; a bad URL ends in `rejected`

Each step does its work, then moves the job to the next status with a compare-and-set. Because the status is in
Postgres and every write is an upsert, a crashed or restarted process picks a job up at its last status instead
of starting over. Delivery is at-least-once: see `_deliver`.
"""
from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from leadbot import slack as slack_ui
from leadbot.brief import Briefer
from leadbot.clay import Clay, ClayCallback
from leadbot.config import Settings
from leadbot.facts import build_facts
from leadbot.hubspot import CrmState, HubSpot
from leadbot.logs import job_id_var, timed_step
from leadbot.slack import Slack
from leadbot.store import RESUMABLE, Company, Job, Prospect, Status, Store
from leadbot.suppression import Context, Verdict, evaluate
from leadbot.urls import extract_url, normalise_linkedin_url

log = logging.getLogger("leadbot.jobs")


class EnrichmentEmpty(Exception):
    """Clay answered, but found no person behind the URL."""


@dataclass
class Deps:
    settings: Settings
    store: Store
    slack: Slack
    clay: Clay
    hubspot: HubSpot
    briefer: Briefer
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


class Runner:
    def __init__(self, deps: Deps):
        self.d = deps
        self._limit = asyncio.Semaphore(deps.settings.max_concurrent_jobs)
        self._tasks: dict[str, asyncio.Task] = {}
        # status -> (step name for logs, function). A status with no entry is terminal or waiting on Clay.
        self._steps = {
            Status.RECEIVED: ("normalise", self._normalise),
            Status.NORMALISED: ("cache_lookup", self._cache_lookup),
            Status.CACHE_MISS: ("enrich", self._enrich),
            Status.CACHE_HIT: ("check_crm", self._check_crm),
            Status.ENRICHED: ("check_crm", self._check_crm),
            Status.CHECKED: ("write_brief", self._write_brief),
            Status.BRIEFED: ("deliver", self._deliver),
        }

    # ---------- scheduling ----------

    def submit(self, job_id: str) -> None:
        """Run a job in the background. A job that is already running in this process is not started twice."""
        if job_id in self._tasks:
            return
        task = asyncio.create_task(self._guarded(job_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    async def drain(self) -> None:
        """Wait for all background jobs. Used on shutdown and in tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def _guarded(self, job_id: str) -> None:
        async with self._limit:  # the concurrency cap: at most N jobs call HubSpot and Claude at once
            await self.run(job_id)

    # ---------- the loop ----------

    async def run(self, job_id: str) -> None:
        token = job_id_var.set(job_id)
        try:
            job = await self.d.store.claim(job_id)
            if job is None:
                return
            if job.attempts > self.d.settings.max_attempts:
                raise RuntimeError(f"gave up after {self.d.settings.max_attempts} attempts (last: {job.status})")
            while True:
                job = await self.d.store.get_job(job_id)
                step = self._steps.get(job.status)
                if step is None:
                    return
                name, function = step
                with timed_step(log, name):
                    keep_going = await function(job)
                if not keep_going:
                    return
        except Exception as e:
            await self.fail(job_id, e)
        finally:
            job_id_var.reset(token)

    async def _move(self, job: Job, to: Status, **fields) -> bool:
        moved = await self.d.store.transition(job.id, job.status, to, **fields)
        if not moved:
            log.warning("job was moved by someone else, stopping", extra={"from": job.status, "to": to})
        return moved

    # ---------- steps ----------

    async def _normalise(self, job: Job) -> bool:
        url = extract_url(job.raw_text)
        url_norm = normalise_linkedin_url(url) if url else None
        if url_norm is None:
            await self.d.slack.post(
                job.channel,
                "I need a LinkedIn profile link, like `linkedin.com/in/jane-doe`. "
                "Company pages and other links will not work.",
                thread_ts=job.thread_ts)
            return await self._move(job, Status.REJECTED) and False
        await self.d.slack.post(job.channel, f"Researching `{url_norm}` ...", thread_ts=job.thread_ts)
        return await self._move(job, Status.NORMALISED, linkedin_url_norm=url_norm)

    async def _cache_lookup(self, job: Job) -> bool:
        prospect = await self.d.store.get_prospect(job.linkedin_url_norm)
        max_age = timedelta(days=self.d.settings.cache_ttl_days)
        fresh = prospect is not None and self.d.now() - prospect.enriched_at < max_age
        log.info("cache lookup", extra={"cache": "hit" if fresh else "miss"})
        return await self._move(job, Status.CACHE_HIT if fresh else Status.CACHE_MISS)

    async def _enrich(self, job: Job) -> bool:
        # Is another job already waiting on Clay for this same profile? Then wait with it: one Clay call serves both.
        already_waiting = await self.d.store.enriching_jobs(job.linkedin_url_norm)
        # Move to `enriching` BEFORE calling Clay, so a fast callback always finds the job in the right status.
        if not await self._move(job, Status.ENRICHING):
            return False
        if already_waiting:
            log.info("enrichment already in flight, sharing it", extra={"shared_with": already_waiting[0].id})
        else:
            await self.d.clay.enrich(job.linkedin_url_norm, job.id)
        return False  # nothing more to do until POST /webhooks/clay resumes the job

    async def _check_crm(self, job: Job) -> bool:
        prospect = await self.d.store.get_prospect(job.linkedin_url_norm)
        if prospect is None or not prospect.name:
            raise EnrichmentEmpty(f"no person found for {job.linkedin_url_norm}")
        crm = await self.d.hubspot.lookup(prospect.email, prospect.company_domain)
        verdict = evaluate(crm, Context(today=self.d.now().date(),
                                        recent_contact_days=self.d.settings.recent_contact_days))
        log.info("suppression verdict", extra={"verdict": verdict.verdict, "rule": verdict.rule})
        await self.d.store.upsert_brief(job.id, prospect_id=prospect.id, suppression={
            "verdict": verdict.verdict, "reason": verdict.reason, "rule": verdict.rule, "crm": crm.to_dict(),
            "cache": "hit" if job.status == Status.CACHE_HIT else "miss"})
        return await self._move(job, Status.CHECKED)

    async def _write_brief(self, job: Job) -> bool:
        prospect = await self.d.store.get_prospect(job.linkedin_url_norm)
        company = await self.d.store.get_company(prospect.company_domain) if prospect.company_domain else None
        suppression = (await self.d.store.get_brief(job.id)).suppression
        facts = build_facts(
            prospect, company, CrmState.from_dict(suppression["crm"]),
            Verdict(suppression["verdict"], suppression["reason"], suppression["rule"]),
            today=self.d.now().date(), seller_name=self.d.settings.seller_name,
            seller_pitch=self.d.settings.seller_pitch)
        result = await self.d.briefer.write(facts)
        log.info("brief written", extra={"source": result.source, "check_passed": result.check_passed,
                                         "input_tokens": result.input_tokens,
                                         "output_tokens": result.output_tokens})
        await self.d.store.upsert_brief(
            job.id, facts=facts, brief=result.brief, source=result.source, model=result.model,
            check_passed=result.check_passed, fallback_reason=result.fallback_reason,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens)
        return await self._move(job, Status.BRIEFED)

    async def _deliver(self, job: Job) -> bool:
        """Post to Slack, then write to HubSpot. Each half records that it happened (slack_ts, hubspot_note_id),
        so a job resumed after a crash skips the half that is already done. A crash between an API call and
        saving its marker can still repeat that one call: delivery is at-least-once, not exactly-once."""
        record = await self.d.store.get_brief(job.id)
        brief, suppression = record.brief, record.suppression
        if not record.slack_ts:
            written_by = record.model if record.source == "claude" else "template"
            footer = f"cache {suppression['cache']} · written by {written_by} · job `{job.id}`"
            ts = await self.d.slack.post(job.channel, slack_ui.brief_text(brief), thread_ts=job.thread_ts,
                                         blocks=slack_ui.brief_blocks(brief, footer))
            await self.d.store.upsert_brief(job.id, slack_ts=ts)
        if not record.hubspot_note_id:
            prospect = await self.d.store.get_prospect(job.linkedin_url_norm)
            company = await self.d.store.get_company(prospect.company_domain) if prospect.company_domain else None
            crm = CrmState.from_dict(suppression["crm"])
            contact_id = await self.d.hubspot.upsert_contact(
                crm, email=prospect.email, name=prospect.name, title=prospect.title,
                company_name=company.name if company else None,
                linkedin_url=f"https://www.{prospect.linkedin_url_norm}")
            if contact_id or crm.company_id:
                note_id = await self.d.hubspot.create_note(note_html(brief, job), self.d.now(), contact_id,
                                                           crm.company_id)
                await self.d.store.upsert_brief(job.id, hubspot_note_id=note_id)
            else:
                log.info("no HubSpot record to attach the note to (no email, company not in HubSpot)")
        return await self._move(job, Status.DONE)

    # ---------- the Clay callback ----------

    async def on_clay_callback(self, payload: ClayCallback) -> int:
        """Store what Clay found, then resume every job waiting on that profile. Returns how many were resumed.
        The data is stored even when no job is waiting any more (a late callback): it was paid for."""
        url_norm = normalise_linkedin_url(payload.linkedin_url)
        if url_norm is None:
            raise ValueError(f"callback carries no LinkedIn profile URL: {payload.linkedin_url!r}")
        if payload.company.domain:
            await self.d.store.upsert_company(Company(**payload.company.model_dump(),
                                                      raw=payload.company.model_dump(mode="json")))
        await self.d.store.upsert_prospect(Prospect(
            linkedin_url_norm=url_norm, name=payload.person.full_name, title=payload.person.title,
            email=payload.person.email, location=payload.person.location,
            role_started_on=payload.person.role_started_on, company_domain=payload.company.domain,
            enriched_at=self.d.now(), source="clay", raw=payload.model_dump(mode="json")))
        resumed = 0
        for job in await self.d.store.enriching_jobs(url_norm):
            if await self.d.store.transition(job.id, Status.ENRICHING, Status.ENRICHED):
                self.submit(job.id)
                resumed += 1
        log.info("clay callback stored", extra={"linkedin_url_norm": url_norm, "jobs_resumed": resumed})
        return resumed

    # ---------- failure ----------

    async def fail(self, job_id: str, error: Exception | str) -> None:
        """Fail loudly: mark the job, tell the AE in the thread, alert the channel. Never raises."""
        text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        log.error("job failed", extra={"job_id": job_id, "error_text": text},
                  exc_info=error if isinstance(error, Exception) else None)
        try:
            if not await self.d.store.fail_job(job_id, text[:2000]):
                return  # already finished or already failed: do not alert twice
            job = await self.d.store.get_job(job_id)
        except Exception:
            log.exception("could not record the failure", extra={"job_id": job_id})
            return
        if isinstance(error, EnrichmentEmpty):
            apology = "I could not find a person behind that LinkedIn link. Please check the URL and try again."
        else:
            apology = f"Sorry, I could not finish this one. The team has been alerted (job `{job_id}`)."
        alert = (f":rotating_light: lead-research job failed\n*job_id:* `{job_id}`\n"
                 f"*profile:* {job.linkedin_url_norm or 'unknown'}\n*requested by:* <@{job.requested_by}>\n"
                 f"*error:* {text[:500]}")
        for channel, message, thread in ((job.channel, apology, job.thread_ts),
                                         (self.d.settings.slack_alert_channel, alert, None)):
            try:
                await self.d.slack.post(channel, message, thread_ts=thread)
            except Exception:
                log.exception("could not post failure message", extra={"job_id": job_id, "channel": channel})

    # ---------- recovery ----------

    async def resume_interrupted(self, idle_for: timedelta | None = None) -> int:
        """Restart jobs that a crash or restart left mid-way. Returns how many were submitted.
        At startup every unfinished job qualifies. The sweeper passes `idle_for`, so it leaves alone the jobs
        that another worker is still moving."""
        jobs = await self.d.store.jobs_in(RESUMABLE, self.d.now() - idle_for if idle_for is not None else None)
        for job in jobs:
            self.submit(job.id)
        return len(jobs)

    async def sweep(self) -> dict:
        """Run every few minutes by an outside scheduler (POST /internal/sweep).
        1. Clay never called back: fail the job after `enrich_timeout_minutes`, so the AE is not left waiting.
        2. A job stopped mid-way (the process died): run it again from its last status."""
        timeout = timedelta(minutes=self.d.settings.enrich_timeout_minutes)
        timed_out = await self.d.store.jobs_in({Status.ENRICHING}, self.d.now() - timeout)
        for job in timed_out:
            await self.fail(job.id, f"TimeoutError: Clay did not call back within "
                                    f"{self.d.settings.enrich_timeout_minutes} minutes")
        resumed = await self.resume_interrupted(timedelta(minutes=self.d.settings.stuck_job_minutes))
        result = {"timed_out": len(timed_out), "resumed": resumed}
        log.info("sweep done", extra=result)
        return result


def note_html(brief: dict, job: Job) -> str:
    """The brief as a HubSpot note body (HubSpot notes take simple HTML)."""
    e = html.escape
    lead = (f"<strong>Hold: do not reach out.</strong> {e(brief['hold_reason'])}" if brief["verdict"] == "hold"
            else "<strong>OK to reach out</strong>")
    questions = "".join(f"<li>{e(q)}</li>" for q in brief["questions"])
    return (f"<p><strong>Lead research brief</strong></p><p>{lead}</p><p>{e(brief['headline'])}</p>"
            f"<p><strong>Person:</strong> {e(brief['person'])}<br><strong>Company:</strong> {e(brief['company'])}</p>"
            f"<p><strong>Angle:</strong> {e(brief['angle'])}</p><ul>{questions}</ul>"
            f"<p>Requested in Slack by {e(job.requested_by or 'unknown')} · job {e(job.id)}</p>")
