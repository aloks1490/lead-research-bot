"""The web service: two inbound webhooks (Slack, Clay), a sweep endpoint and a health check.

Run locally:  uv run uvicorn leadbot.main:app --reload
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from leadbot import logs
from leadbot.brief import Briefer
from leadbot.clay import Clay, ClayCallback
from leadbot.config import Settings
from leadbot.http import HttpClient
from leadbot.hubspot import HubSpot
from leadbot.jobs import Deps, Runner
from leadbot.security import secrets_match, verify_slack_signature
from leadbot.slack import Slack
from leadbot.store import Store
from leadbot.store.memory import MemoryStore

log = logging.getLogger("leadbot.main")


def build_runner(settings: Settings, store: Store, http: HttpClient, briefer: Briefer | None = None, **deps) -> Runner:
    """Wire the clients together. Tests call this with a fake transport, a MemoryStore and a fake Claude client."""
    return Runner(Deps(
        settings=settings,
        store=store,
        slack=Slack(http, settings.slack_bot_token, settings.slack_base_url),
        clay=Clay(http, settings.clay_webhook_url, settings.clay_webhook_token),
        hubspot=HubSpot(http, settings.hubspot_token, settings.hubspot_base_url, settings.hubspot_dnc_property,
                        settings.hubspot_linkedin_property),
        briefer=briefer or Briefer.from_settings(settings, http),
        **deps,
    ))


def create_app(settings: Settings | None = None, runner: Runner | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logs.configure(settings.log_level)
        if runner is not None:  # tests pass a ready runner
            app.state.runner = runner
            yield
            await runner.drain()
            return
        if settings.database_url:
            from leadbot.store.postgres import PostgresStore
            store: Store = await PostgresStore.connect(settings.database_url)
        else:
            log.warning("DATABASE_URL is not set: using the in-memory store, state is lost on restart")
            store = MemoryStore()
        async with httpx.AsyncClient() as client:
            app.state.runner = build_runner(settings, store, HttpClient(client))
            resumed = await app.state.runner.resume_interrupted()  # jobs a restart left mid-way
            log.info("service started", extra={"jobs_resumed": resumed,
                                              "model": app.state.runner.d.briefer.model or "none (template only)"})
            yield
            await app.state.runner.drain()
        await store.close()

    app = FastAPI(title="lead-research-bot", lifespan=lifespan)
    if runner is not None:
        app.state.runner = runner

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/slack/events")
    async def slack_events(request: Request):
        """Slack wants a 200 within 3 seconds or it sends the event again. So this handler only verifies,
        stores the job and returns; the research runs in the background."""
        body = await request.body()
        if not verify_slack_signature(settings.slack_signing_secret, request.headers.get("X-Slack-Request-Timestamp"),
                                      body, request.headers.get("X-Slack-Signature")):
            log.warning("slack signature rejected")
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        if payload.get("type") == "url_verification":  # Slack's one-time check when the URL is saved
            return {"challenge": payload.get("challenge")}

        event = payload.get("event") or {}
        if payload.get("type") != "event_callback" or event.get("type") != "app_mention" or event.get("bot_id"):
            return {"ok": True}  # an event we do not handle: acknowledge it so Slack does not resend

        runner_: Runner = request.app.state.runner
        job = await runner_.d.store.create_job(
            slack_event_id=payload["event_id"], raw_text=event.get("text", ""), requested_by=event.get("user"),
            channel=event["channel"], thread_ts=event.get("thread_ts") or event["ts"])
        if job is None:
            # Same event_id as before: Slack is resending because an earlier reply was slow. Nothing to do.
            log.info("duplicate slack event ignored", extra={"slack_event_id": payload["event_id"],
                                                             "retry_num": request.headers.get("X-Slack-Retry-Num")})
            return {"ok": True, "duplicate": True}
        log.info("job received", extra={"job_id": job.id, "slack_event_id": payload["event_id"]})
        runner_.submit(job.id)
        return {"ok": True}

    @app.post("/webhooks/clay")
    async def clay_callback(request: Request):
        if not secrets_match(settings.clay_callback_secret, request.headers.get("X-Clay-Secret")):
            log.warning("clay callback rejected: bad secret")
            return JSONResponse({"error": "invalid secret"}, status_code=401)
        try:
            payload = ClayCallback.model_validate_json(await request.body())
            resumed = await request.app.state.runner.on_clay_callback(payload)
        except (ValidationError, ValueError) as e:
            log.warning("clay callback rejected: bad payload", extra={"error_text": str(e)[:500]})
            return JSONResponse({"error": "invalid payload"}, status_code=422)
        return {"ok": True, "jobs_resumed": resumed}

    @app.post("/internal/sweep")
    async def sweep(request: Request):
        if not secrets_match(settings.internal_secret, request.headers.get("X-Internal-Secret")):
            return JSONResponse({"error": "invalid secret"}, status_code=401)
        return await request.app.state.runner.sweep()

    return app


app = create_app()
