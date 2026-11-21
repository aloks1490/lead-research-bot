"""End-to-end tests: a signed Slack event goes in, and we check what reached Slack, Clay, HubSpot and the database."""
from __future__ import annotations

import json

import httpx

from leadbot.skill import load as load_skill
from leadbot.store import Status
from tests.conftest import JANE_NORM, faithful_brief


async def research_jane(world):
    """The full happy path for a profile nobody has asked about before."""
    await world.mention()
    await world.clay_calls_back()


# ---------- the happy path ----------

async def test_new_profile_is_enriched_briefed_and_written_to_hubspot(world):
    company_id = world.hubspot.add_company("acme.io", name="Acme")

    response = await world.mention()
    assert response.status_code == 200
    assert world.job().status == Status.ENRICHING
    assert world.clay_requests == [{"linkedin_url": f"https://www.{JANE_NORM}", "job_id": world.job().id}]
    assert "Researching" in world.thread_messages()[0]["text"]

    await world.clay_calls_back()
    job, brief = world.job(), world.brief()
    assert job.status == Status.DONE
    assert (brief.source, brief.check_passed, brief.fallback_reason) == ("claude", True, None)
    assert (brief.input_tokens, brief.output_tokens) == (420, 160)

    # The brief is a reply in the AE's thread.
    posted = world.thread_messages()[-1]
    assert posted["thread_ts"] == job.thread_ts
    decisions = posted["blocks"][0]["text"]["text"]
    assert "No conflict in HubSpot" in decisions
    assert "*Fit: strong*" in decisions and "Germany is a target market" in decisions
    assert "Jane Doe" in posted["blocks"][1]["text"]["text"]
    assert "Email: jane.doe@acme.io _(unverified)_" in posted["blocks"][2]["text"]["text"]

    # HubSpot: a new contact, and one note attached to both the contact and the company.
    contact_id, contact = next(iter(world.hubspot.contacts.items()))
    assert contact["email"] == "jane.doe@acme.io" and contact["jobtitle"] == "Head of Security"
    note = world.hubspot.notes[0]
    assert {a["to"]["id"] for a in note["associations"]} == {contact_id, company_id}
    assert "Jane Doe" in note["properties"]["hs_note_body"]
    assert brief.hubspot_note_id and brief.slack_ts


async def test_second_request_hits_the_cache_and_skips_clay(world):
    await research_jane(world)
    await world.mention("<@UBOT> linkedin.com/in/jane-doe")  # a different spelling of the same profile

    assert world.job().status == Status.DONE
    assert world.brief().suppression["cache"] == "hit"
    assert len(world.clay_requests) == 1  # still only the first call


async def test_cache_entry_older_than_30_days_is_enriched_again(world):
    await research_jane(world)
    world.advance(days=31)
    await world.mention()

    assert world.job().status == Status.ENRICHING
    assert len(world.clay_requests) == 2


async def test_two_requests_for_one_profile_share_one_clay_call(world):
    await world.mention()
    await world.mention()
    assert len(world.clay_requests) == 1

    response = await world.clay_calls_back()
    assert response.json()["jobs_resumed"] == 2
    assert [j.status for j in world.store.jobs.values()] == [Status.DONE, Status.DONE]


# ---------- inbound webhooks ----------

async def test_slack_resend_of_the_same_event_creates_one_job(world):
    first = await world.mention(event_id="Ev-same")
    second = await world.mention(event_id="Ev-same")

    assert first.status_code == second.status_code == 200
    assert second.json() == {"ok": True, "duplicate": True}
    assert len(world.store.jobs) == 1
    assert len(world.clay_requests) == 1


async def test_forged_slack_request_is_rejected(world, caplog):
    response = await world.mention(signature="v0=" + "0" * 64)
    assert response.status_code == 401
    assert world.store.jobs == {}
    rejected = next(r for r in caplog.records if r.getMessage() == "slack signature rejected")
    assert "does not match" in rejected.reason  # the log says why, so a 401 in production can be diagnosed


async def test_an_ignored_slack_event_is_logged(world, caplog):
    import logging
    caplog.set_level(logging.INFO)
    body = json.dumps({"type": "event_callback", "event_id": "Ev-msg",
                       "event": {"type": "message", "channel": "C_SALES", "text": "hi", "ts": "1.2"}}).encode()
    response = await world.post_slack(body)
    assert response.status_code == 200 and world.store.jobs == {}
    ignored = next(r for r in caplog.records if r.getMessage() == "slack event ignored")
    assert ignored.event_type == "message"


async def test_replayed_slack_request_is_rejected(world):
    response = await world.mention(timestamp="1600000000")  # correctly signed, but years old
    assert response.status_code == 401
    assert world.store.jobs == {}


async def test_slack_url_verification_handshake(world):
    response = await world.post_slack(b'{"type": "url_verification", "challenge": "abc123"}')
    assert response.json() == {"challenge": "abc123"}


async def test_clay_callback_with_a_wrong_secret_is_rejected(world):
    await world.mention()
    response = await world.clay_calls_back(secret="wrong")

    assert response.status_code == 401
    assert world.job().status == Status.ENRICHING
    assert world.store.prospects == {}


async def test_a_link_that_is_not_a_profile_gets_a_friendly_reply(world):
    await world.mention("<@UBOT> <https://www.linkedin.com/company/acme>")

    assert world.job().status == Status.REJECTED
    assert "LinkedIn profile link" in world.thread_messages()[-1]["text"]
    assert world.clay_requests == [] and world.alerts() == []


# ---------- suppression ----------

async def test_prospect_with_an_open_deal_gets_hold_with_the_reason_first(world):
    company_id = world.hubspot.add_company("acme.io", name="Acme")
    world.hubspot.add_deal(company_id, "Acme rollout", stage="proposal")
    world.hubspot.add_deal(company_id, "Old deal", stage="closedwon")  # closed deals do not suppress

    await research_jane(world)
    brief = world.brief()
    assert brief.brief["verdict"] == "hold"
    assert brief.suppression["reason"] == "Open deal, owner Priya, stage Proposal"
    first_block = world.thread_messages()[-1]["blocks"][0]["text"]["text"]
    assert "Hold" in first_block and "Open deal, owner Priya, stage Proposal" in first_block
    assert "Hold" in world.hubspot.notes[0]["properties"]["hs_note_body"]


async def test_existing_contact_is_not_overwritten(world):
    world.hubspot.add_contact("jane.doe@acme.io", jobtitle="Typed by a rep",
                              notes_last_contacted="2020-01-01T00:00:00Z")
    await research_jane(world)

    assert len(world.hubspot.contacts) == 1
    assert world.hubspot.contacts["100"]["jobtitle"] == "Typed by a rep"
    assert world.hubspot.notes[0]["associations"][0]["to"]["id"] == "100"


async def test_an_email_hubspot_rejects_does_not_fail_the_job(world):
    # Found in the first run against real HubSpot: an email it refused (INVALID_EMAIL) failed the job after the
    # brief was already in the thread, so the AE got the brief and then "Sorry, I could not finish".
    company_id = world.hubspot.add_company("acme.io", name="Acme")
    world.hubspot.reject_new_contacts = True
    await research_jane(world)

    assert world.job().status == Status.DONE
    assert world.alerts() == [] and "could not finish" not in world.thread_messages()[-1]["text"]
    assert world.hubspot.contacts == {}
    note = world.hubspot.notes[0]
    assert [a["to"]["id"] for a in note["associations"]] == [company_id]  # the note still lands, on the company


async def test_hold_text_has_one_full_stop(world):
    world.hubspot.add_company("acme.io", name="Acme", lifecyclestage="customer")
    world.claude.reply = lambda facts: {**faithful_brief(facts), "hold_reason": "Acme is already a customer."}
    await research_jane(world)
    text = world.thread_messages()[-1]["text"]
    assert text.startswith("HOLD: Acme is already a customer. ") and ".." not in text


# ---------- the model is never trusted ----------

async def test_invented_facts_are_rejected_and_the_template_brief_goes_out(world):
    def invents(facts):
        brief = faithful_brief(facts)
        brief["angle"] = "Congratulate them on the Series B and the move to Munich with 500 new hires."
        return brief

    world.claude.reply = invents
    await research_jane(world)

    brief = world.brief()
    assert world.job().status == Status.DONE  # the AE still gets a brief
    assert (brief.source, brief.check_passed) == ("template", False)
    assert "500" in brief.fallback_reason and "Munich" in brief.fallback_reason
    assert "Series B" not in str(world.thread_messages()[-1]["blocks"])


async def test_the_model_cannot_change_the_verdict(world):
    company_id = world.hubspot.add_company("acme.io", name="Acme")
    world.hubspot.add_deal(company_id, "Acme rollout", stage="proposal")
    world.claude.reply = lambda facts: {**faithful_brief(facts), "verdict": "reach_out", "hold_reason": ""}

    await research_jane(world)
    brief = world.brief()
    assert brief.source == "template" and "verdict changed" in brief.fallback_reason
    assert brief.brief["verdict"] == "hold"


async def test_claude_only_sees_the_fact_sheet(world):
    await research_jane(world)
    call = world.claude.calls[0]

    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "tool_choice" not in call and "temperature" not in call  # both are rejected by claude-sonnet-5-5
    assert call["system"] == load_skill().system                      # the prompt comes from the skill file
    assert len(call["messages"]) == 2 * len(load_skill().examples) + 1  # worked examples, then the real facts
    assert call["messages"][-2]["content"][0]["cache_control"] == {"type": "ephemeral"}  # cached up to here
    assert call["output_config"]["format"]["schema"] == load_skill().schema
    assert "jane.doe@acme.io" not in call["messages"][-1]["content"]  # the email is not needed to write a brief
    assert world.brief().prompt_version == load_skill().prompt_version
    assert world.brief().facts["person"]["name"] == "Jane Doe"


# ---------- failures ----------

async def test_hubspot_rate_limit_is_retried_after_the_given_wait(world):
    world.hubspot.fail_with = [httpx.Response(429, headers={"Retry-After": "2"}, json={})]
    await research_jane(world)

    assert world.job().status == Status.DONE
    assert world.sleeps == [2.0]


async def test_a_step_that_keeps_failing_fails_loudly(world):
    world.hubspot.fail_with = [httpx.Response(503, json={"message": "down"})] * 20
    await research_jane(world)

    job = world.job()
    assert job.status == Status.FAILED and "503" in job.last_error
    assert len(world.hubspot.requests) == 4  # 4 tries, then stop
    assert "could not finish" in world.thread_messages()[-1]["text"]
    alert = world.alerts()[0]["text"]
    assert job.id in alert and "503" in alert


async def test_clay_that_never_calls_back_is_timed_out_by_the_sweeper(world):
    await world.mention()
    headers = {"X-Internal-Secret": "sweep-secret"}

    world.advance(minutes=9)
    assert (await world.api.post("/internal/sweep", headers=headers)).json() == {"timed_out": 0, "resumed": 0}
    assert world.job().status == Status.ENRICHING

    world.advance(minutes=2)
    assert (await world.api.post("/internal/sweep", headers=headers)).json() == {"timed_out": 1, "resumed": 0}
    job = world.job()
    assert job.status == Status.FAILED and "Clay did not call back" in job.last_error
    assert job.id in world.alerts()[0]["text"]

    # A second sweep finds nothing new: no duplicate alert.
    await world.api.post("/internal/sweep", headers=headers)
    assert len(world.alerts()) == 1


async def test_sweep_endpoint_needs_its_secret(world):
    assert (await world.api.post("/internal/sweep")).status_code == 401


async def test_late_clay_callback_is_still_cached(world):
    await world.mention()
    world.advance(minutes=11)
    await world.runner.sweep()
    assert world.job().status == Status.FAILED

    response = await world.clay_calls_back()
    assert response.json()["jobs_resumed"] == 0
    await world.mention()  # asking again now costs no Clay credit
    assert world.job().status == Status.DONE
    assert len(world.clay_requests) == 1


async def test_profile_that_clay_cannot_find(world):
    await world.mention()
    await world.clay_calls_back(person={"full_name": "", "title": ""}, company={})

    assert world.job().status == Status.FAILED
    assert "could not find a person" in world.thread_messages()[-1]["text"]


# ---------- restarts ----------

async def test_a_restarted_process_resumes_a_job_from_its_last_status(world):
    await research_jane(world)  # fills the cache
    job = await world.store.create_job(slack_event_id="Ev-crash", raw_text=f"<https://{JANE_NORM}>",
                                       requested_by="U_AE", channel="C_SALES", thread_ts="1.1")
    # The old process got as far as the cache lookup, then died.
    await world.store.transition(job.id, Status.RECEIVED, Status.NORMALISED, linkedin_url_norm=JANE_NORM)
    await world.store.transition(job.id, Status.NORMALISED, Status.CACHE_HIT)

    restarted = world.new_runner()
    assert await restarted.resume_interrupted() == 1
    await restarted.drain()

    assert world.job().status == Status.DONE
    assert world.thread_messages()[-1]["thread_ts"] == "1.1"


async def test_a_resumed_delivery_does_not_post_the_brief_twice(world):
    await research_jane(world)
    done = world.job()
    brief = world.brief()
    # Pretend the process died after posting to Slack but before writing to HubSpot.
    world.store.jobs[done.id].status = Status.BRIEFED
    world.store.briefs[done.id] = brief.model_copy(update={"hubspot_note_id": None})
    messages_before, notes_before = len(world.slack_messages), len(world.hubspot.notes)

    restarted = world.new_runner()
    await restarted.resume_interrupted()
    await restarted.drain()

    assert world.job().status == Status.DONE
    assert len(world.slack_messages) == messages_before      # Slack: not again
    assert len(world.hubspot.notes) == notes_before + 1      # HubSpot: the missing half is done


async def test_a_job_that_keeps_crashing_is_given_up_on(world):
    await world.mention()
    job = world.job()
    world.store.jobs[job.id].status = Status.NORMALISED
    world.store.jobs[job.id].attempts = world.settings.max_attempts

    await world.runner.resume_interrupted()
    await world.runner.drain()
    assert world.job().status == Status.FAILED and "gave up" in world.job().last_error


# ---------- the in-process sweeper ----------

async def test_the_sweeper_keeps_running_after_a_failed_sweep(world):
    import asyncio
    calls = []

    async def sweep():
        calls.append(len(calls))
        if len(calls) == 2:
            raise RuntimeError("database blip")
        return {}

    world.runner.sweep = sweep
    task = asyncio.create_task(world.runner.sweep_forever(0.01))
    for _ in range(200):
        if len(calls) >= 4:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    assert len(calls) >= 4  # the failure on the second sweep did not stop the third and fourth


async def test_the_app_starts_and_stops_its_own_sweeper():
    from leadbot.main import create_app
    from tests.conftest import settings as make_settings

    app = create_app(make_settings().model_copy(update={"sweep_interval_seconds": 3600}))
    async with app.router.lifespan_context(app):
        sweeper = app.state.sweeper
        assert sweeper is not None and not sweeper.done()
    assert sweeper.cancelled()

    app = create_app(make_settings().model_copy(update={"sweep_interval_seconds": 0}))
    async with app.router.lifespan_context(app):
        assert app.state.sweeper is None  # 0 turns it off


# ---------- one thread per request ----------

async def test_everything_the_ae_sees_is_in_the_thread_of_their_mention(world):
    await world.mention("<@UBOT> <https://www.linkedin.com/company/acme>")      # rejected
    world.hubspot.fail_with = [httpx.Response(503, json={})] * 20
    await research_jane(world)                                                   # researching, then failure
    world.hubspot.fail_with = []
    await world.mention()                                                        # cache hit, then a brief

    for job in world.store.jobs.values():
        replies = [m for m in world.thread_messages() if m.get("thread_ts") == job.thread_ts]
        assert replies, f"no reply in the thread of {job.slack_event_id}"
    assert all(m.get("thread_ts") for m in world.thread_messages())  # nothing posted to the channel itself
    assert all("thread_ts" not in m for m in world.alerts())          # only the maintainer alert is outside


async def test_a_mention_inside_an_existing_thread_replies_in_that_thread(world):
    body = json.dumps({"type": "event_callback", "event_id": "Ev-in-thread",
                       "event": {"type": "app_mention", "user": "U_AE", "channel": "C_SALES",
                                 "text": "<@UBOT> <https://www.linkedin.com/company/acme>",
                                 "ts": "1700000500.000002", "thread_ts": "1700000400.000001"}}).encode()
    await world.post_slack(body)
    await world.runner.drain()
    assert world.thread_messages()[-1]["thread_ts"] == "1700000400.000001"
