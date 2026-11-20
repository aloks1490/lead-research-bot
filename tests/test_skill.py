"""The prompt skill file (leadbot/prompts/brief.md) and its loader (leadbot/skill.py).

The first tests guard the real file: if someone edits the prompt and breaks an example, CI fails before the
broken prompt reaches a model.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from leadbot.brief import Briefer, ClaudeWriter
from leadbot.check import check
from leadbot.config import Settings
from leadbot.facts import build_facts
from leadbot.fit import Fit, Target
from leadbot.fit import evaluate as evaluate_fit
from leadbot.hubspot import CrmState
from leadbot.skill import SkillError, load, parse
from leadbot.store import Company, Prospect
from leadbot.suppression import Verdict
from tests.conftest import FakeClaude

SKILL = load()

# ---------- the real skill file ----------


def test_the_brief_skill_loads():
    assert SKILL.name == "brief" and SKILL.version == "4"
    assert SKILL.prompt_version.startswith("brief@4+")
    assert len(SKILL.examples) >= 2
    assert {e.answer["verdict"] for e in SKILL.examples} == {"reach_out", "hold"}  # both kinds are shown


@pytest.mark.parametrize("example", SKILL.examples, ids=lambda e: e.title)
def test_every_example_is_a_brief_the_bot_would_accept(example):
    # A worked example that breaks the rules would teach the model to break them.
    assert SKILL.validate(example.answer) == []
    assert check(example.answer, example.facts) == []


@pytest.mark.parametrize("example", SKILL.examples, ids=lambda e: e.title)
def test_example_facts_look_like_real_fact_sheets(example):
    real = build_facts(Prospect(linkedin_url_norm="x", name="A B", title="CTO", enriched_at=datetime.now(UTC)),
                       Company(domain="x.io", name="X"), CrmState(), Verdict("reach_out"), Fit("unknown"),
                       today=date(2026, 11, 18),
                       seller_name="S", seller_pitch="p")
    assert set(example.facts) - {"hold_reason"} == set(real)  # same top-level keys as build_facts produces


@pytest.mark.parametrize("example", SKILL.examples, ids=lambda e: e.title)
def test_example_fit_is_what_the_code_would_decide(example):
    # The examples teach the model what a fit looks like; they must show exactly what fit.py would compute.
    person, company = example.facts["person"], example.facts["company"]
    employees = company.get("employees")
    fit = evaluate_fit(title=person.get("title"), person_location=person.get("location"),
                       company_name=company.get("name"), company_country=company.get("country"),
                       employees=employees if isinstance(employees, int) else None,
                       employee_band=employees if isinstance(employees, str) else None,
                       target=Target.from_settings(Settings(_env_file=None)))
    assert example.facts["fit"] == {"level": fit.level, "reasons": fit.reasons}


def test_weak_fit_examples_do_not_pitch():
    for example in SKILL.examples:
        if example.answer["verdict"] == "reach_out" and example.facts["fit"]["level"] != "strong":
            assert example.answer["angle"].lower().startswith("check fit"), example.title


def test_hold_examples_do_not_pitch():
    for example in SKILL.examples:
        if example.answer["verdict"] == "hold":
            angle = example.answer["angle"].lower()
            assert any(word in angle for word in ("do not contact", "wait", "ask ")), angle


# ---------- the loader ----------

GOOD = '''---
name: demo
version: 7
---
Notes for people.

## Instructions
Be brief.

## Output
Return JSON.

```json
{"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": false}
```

## Example: one

```json
{"x": 1}
```

```json
{"a": "hello"}
```
'''


def test_parse_reads_every_part():
    skill = parse(GOOD)
    assert (skill.name, skill.version) == ("demo", "7")
    assert skill.system == "Be brief.\n\nReturn JSON."  # the schema block and the notes are not sent as text
    assert skill.examples[0].facts == {"x": 1} and skill.examples[0].answer == {"a": "hello"}
    assert [m["role"] for m in skill.chat_messages({"x": 2})] == ["system", "user", "assistant", "user"]


def test_any_edit_changes_the_prompt_version():
    assert parse(GOOD).prompt_version != parse(GOOD.replace("Be brief.", "Be very brief.")).prompt_version


@pytest.mark.parametrize("broken,message", [
    (GOOD.replace("---\nname: demo\nversion: 7\n---\n", ""), "front matter"),
    (GOOD.replace("version: 7\n", ""), "version"),
    (GOOD.replace("## Instructions", "## Rules"), "Instructions"),
    (GOOD.replace('{"x": 1}', "{x: 1}"), "not valid JSON"),
    (GOOD.replace('{"a": "hello"}', '{"a": 5}'), "does not match the schema"),
    (GOOD.replace('```json\n{"x": 1}\n```\n\n', ""), "two ```json blocks"),
    (GOOD.replace('"type": "object"', '"type": "nonsense"'), "nonsense"),
])
def test_a_broken_skill_file_fails_at_load_time(broken, message):
    with pytest.raises(Exception) as error:
        parse(broken)
    assert message in str(error.value)
    assert isinstance(error.value, SkillError) or "nonsense" in message  # the last case is jsonschema's own error


def test_validate_names_each_problem():
    answer = {k: "x" for k in SKILL.schema["required"]} | {"questions": "not a list", "extra": 1}
    del answer["angle"]
    problems = " | ".join(SKILL.validate(answer))
    assert "'angle' is a required property" in problems
    assert "questions: 'not a list' is not of type 'array'" in problems
    assert "'extra' was unexpected" in problems
    assert "verdict: 'x' is not one of" in problems


# ---------- schema validation inside the Briefer ----------

FACTS = SKILL.examples[0].facts


async def test_an_answer_with_the_wrong_shape_is_rejected_before_fact_checking():
    claude = FakeClaude()
    claude.reply = lambda facts: {**SKILL.examples[0].answer, "summary": "an extra key"}
    result = await Briefer(ClaudeWriter(claude, "claude-sonnet-5-5")).write(FACTS)
    assert (result.source, result.check_passed) == ("template", False)
    assert result.fallback_reason.startswith("schema: ") and "summary" in result.fallback_reason
    assert result.prompt_version == SKILL.prompt_version  # recorded even when the answer was rejected
