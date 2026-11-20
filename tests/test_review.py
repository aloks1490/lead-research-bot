"""The reviewer (leadbot/review.py): a second model that lists claims the fact sheet does not support."""
from __future__ import annotations

import json

import httpx

from leadbot.brief import Briefer, ClaudeWriter
from leadbot.review import REVIEW_PATH, Reviewer
from leadbot.skill import load
from scripts.reviewer_cases import CASES
from tests.conftest import FakeClaude
from tests.test_units import make_http

BAD = next(c for c in CASES if c["name"] == "reid_startup_and_customers")
GOOD = next(c for c in CASES if c["name"] == "jane_clean")


def answer(unsupported: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"unsupported": unsupported})}}]})


def reviewer(responses: list):
    http, calls, _ = make_http(responses)
    return Reviewer(http, "https://nim.test/v1", "nvapi-test", "google/gemma-4-31b-it"), calls


def briefer_with(responses: list, case: dict):
    """A Briefer whose writer returns the case's brief and whose reviewer answers with `responses`."""
    claude = FakeClaude()
    claude.reply = lambda facts: case["brief"]
    review, calls = reviewer(responses)
    return Briefer(ClaudeWriter(claude, "claude-sonnet-5-5"), review), calls


# ---------- the reviewer on its own ----------

def test_the_review_skill_loads_and_its_examples_are_consistent():
    skill = load(REVIEW_PATH)
    assert skill.name == "review" and len(skill.examples) == 3
    for example in skill.examples:
        assert skill.validate(example.answer) == []
        brief_text = json.dumps(example.facts["brief"]).casefold()
        for claim in example.answer["unsupported"]:  # every example quote is really in its example brief
            assert claim["quote"].casefold() in brief_text


async def test_the_request_carries_facts_brief_and_the_schema():
    review, calls = reviewer([answer([])])
    result = await review.review(GOOD["facts"], GOOD["brief"])
    assert result.status == "passed" and result.model == "google/gemma-4-31b-it"
    body = json.loads(calls[0].content)
    sent = json.loads(body["messages"][-1]["content"])
    assert sent["facts"] == GOOD["facts"] and "verdict" not in sent["brief"]  # only the words are reviewed
    assert body["response_format"]["type"] == "json_schema" and body["temperature"] == 0


async def test_flagged_claims_reject_the_brief():
    review, _ = reviewer([answer([{"quote": "startup", "why": "no stage in facts"}])])
    result = await review.review(BAD["facts"], BAD["brief"])
    assert result.status == "rejected" and result.unsupported[0]["quote"] == "startup"


async def test_a_quote_that_is_not_in_the_brief_is_dropped():
    # The reviewer inventing a problem must not reject a good brief.
    review, _ = reviewer([answer([{"quote": "raised a Series B", "why": "not in facts"}])])
    result = await review.review(GOOD["facts"], GOOD["brief"])
    assert (result.status, result.dropped) == ("passed", 1)


async def test_quotes_match_despite_case_and_spacing():
    review, _ = reviewer([answer([{"quote": "WILL  NEED compliance automation", "why": "x"}])])
    assert (await review.review(BAD["facts"], BAD["brief"])).status == "rejected"


async def test_an_outage_skips_the_review_instead_of_blocking():
    review, calls = reviewer([httpx.Response(503, text="down")] * 4)
    result = await review.review(GOOD["facts"], GOOD["brief"])
    assert result.status == "skipped" and "503" in result.error


async def test_a_reply_that_breaks_the_schema_skips_the_review():
    bad = httpx.Response(200, json={"choices": [{"message": {"content": '{"problems": "many"}'}}]})
    review, _ = reviewer([bad])
    result = await review.review(GOOD["facts"], GOOD["brief"])
    assert result.status == "skipped" and "unsupported" in result.error


async def test_a_timeout_is_not_retried():
    review, calls = reviewer([httpx.ReadTimeout("slow"), answer([])])
    assert (await review.review(GOOD["facts"], GOOD["brief"])).status == "skipped"
    assert len(calls) == 1


# ---------- the reviewer inside the Briefer ----------

async def test_a_rejected_brief_becomes_the_template_with_the_claims_as_reason():
    briefer, _ = briefer_with([answer([{"quote": "early-stage biotechs use Northwall",
                                        "why": "facts name no Northwall customers"}])], BAD)
    result = await briefer.write(BAD["facts"])
    assert (result.source, result.check_passed) == ("template", False)
    assert result.fallback_reason.startswith('reviewer: "early-stage biotechs use Northwall"')
    assert result.review["status"] == "rejected"


async def test_a_passed_brief_keeps_the_model_brief_and_records_the_review():
    briefer, _ = briefer_with([answer([])], GOOD)
    result = await briefer.write(GOOD["facts"])
    assert (result.source, result.check_passed, result.review["status"]) == ("claude", True, "passed")


async def test_a_skipped_review_still_sends_the_model_brief():
    briefer, _ = briefer_with([httpx.Response(503, text="down")] * 4, GOOD)
    result = await briefer.write(GOOD["facts"])
    assert result.source == "claude" and result.review["status"] == "skipped"


async def test_the_reviewer_only_runs_after_the_checker_passes():
    bad_words = {**GOOD["brief"], "company": "Acme raised a Series B."}  # the word checker catches this
    briefer, calls = briefer_with([answer([])], {**GOOD, "brief": bad_words})
    result = await briefer.write(GOOD["facts"])
    assert result.fallback_reason.startswith("checker:") and calls == []


async def test_without_a_review_model_there_is_no_review():
    claude = FakeClaude()
    claude.reply = lambda facts: GOOD["brief"]
    result = await Briefer(ClaudeWriter(claude, "claude-sonnet-5-5")).write(GOOD["facts"])
    assert result.source == "claude" and result.review is None
