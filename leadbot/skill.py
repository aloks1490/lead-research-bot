"""Loads a prompt "skill" file: the instructions, output schema and worked examples a model gets for one task.

The file is Markdown (see leadbot/prompts/brief.md):

    ---                          front matter: name and version
    name: brief
    version: 2
    ---
    ## Instructions              the rules, sent as the system prompt
    ## Output                    field guidance (also sent) and one ```json block: the output's JSON schema
    ## Example: <title>          two ```json blocks each: a fact sheet, then the ideal answer for it

Keeping the prompt in a file, not in Python strings, means it can be read, reviewed and versioned on its own, and
every stored brief can say which version wrote it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator

DEFAULT_PATH = Path(__file__).parent / "prompts" / "brief.md"

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


class SkillError(ValueError):
    """The skill file is malformed. Raised at startup, so a broken prompt never reaches production traffic."""


@dataclass(frozen=True)
class Example:
    title: str
    facts: dict
    answer: dict


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    system: str              # Instructions + Output guidance, ready to send as the system prompt
    schema: dict             # JSON schema of one answer
    examples: tuple[Example, ...]
    content_hash: str        # changes whenever the file changes, even if someone forgot to raise `version`

    @property
    def prompt_version(self) -> str:
        """Stored with every brief, e.g. 'brief@2+3f9a1c2d'."""
        return f"{self.name}@{self.version}+{self.content_hash}"

    def chat_messages(self, facts: dict) -> list[dict]:
        """System prompt, then each example as a user/assistant pair, then the real fact sheet.
        Showing whole worked answers fixes the format and tone far more reliably than describing them."""
        messages = [{"role": "system", "content": self.system}]
        for example in self.examples:
            messages += [{"role": "user", "content": _dump(example.facts)},
                         {"role": "assistant", "content": _dump(example.answer)}]
        messages.append({"role": "user", "content": _dump(facts)})
        return messages

    def validate(self, answer: object) -> list[str]:
        """Schema problems with one answer: missing keys, extra keys, wrong types. Empty list = valid."""
        validator = Draft202012Validator(self.schema)
        problems = []
        for error in sorted(validator.iter_errors(answer), key=lambda e: list(e.absolute_path)):
            where = "/".join(str(p) for p in error.absolute_path) or "answer"
            problems.append(f"{where}: {error.message}")
        return problems


def parse_json_reply(text: str) -> dict:
    """The JSON object in a model reply that may carry code fences or stray prose around it."""
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    out = json.loads(text[start:end + 1])
    if not isinstance(out, dict):
        raise ValueError("reply is not a JSON object")
    return out


def _dump(value: dict) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)  # byte-stable, so prompt caching can work


def _sections(body: str) -> dict[str, str]:
    """{heading: text} for every '## ' heading."""
    parts = re.split(r"^## (.+)$", body, flags=re.M)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def _json_blocks(text: str, where: str) -> list:
    try:
        return [json.loads(block) for block in _JSON_BLOCK.findall(text)]
    except json.JSONDecodeError as e:
        raise SkillError(f"{where}: a ```json block is not valid JSON: {e}") from e


def parse(text: str) -> Skill:
    match = _FRONT_MATTER.match(text)
    if not match:
        raise SkillError("missing front matter (--- name / version ---)")
    meta = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    meta = {k.strip(): v.strip() for k, v in meta.items()}
    for key in ("name", "version"):
        if not meta.get(key):
            raise SkillError(f"front matter needs '{key}'")

    sections = _sections(text[match.end():])
    for heading in ("Instructions", "Output"):
        if heading not in sections:
            raise SkillError(f"missing section '## {heading}'")

    schemas = _json_blocks(sections["Output"], "Output")
    if len(schemas) != 1:
        raise SkillError("'## Output' must hold exactly one ```json block: the schema")
    schema = schemas[0]
    Draft202012Validator.check_schema(schema)
    guidance = _JSON_BLOCK.sub("", sections["Output"]).strip()

    examples = []
    for heading, body in sections.items():
        if not heading.startswith("Example:"):
            continue
        blocks = _json_blocks(body, heading)
        if len(blocks) != 2:
            raise SkillError(f"'## {heading}' must hold two ```json blocks: the facts, then the answer")
        examples.append(Example(heading.removeprefix("Example:").strip(), blocks[0], blocks[1]))
        problems = Draft202012Validator(schema).iter_errors(blocks[1])
        if any(True for _ in problems):
            raise SkillError(f"'## {heading}': the example answer does not match the schema")

    return Skill(
        name=meta["name"],
        version=meta["version"],
        system=sections["Instructions"] + "\n\n" + guidance,
        schema=schema,
        examples=tuple(examples),
        content_hash=hashlib.sha256(text.encode()).hexdigest()[:8],
    )


@cache
def load(path: str | Path = DEFAULT_PATH) -> Skill:
    path = Path(path)
    try:
        return parse(path.read_text(encoding="utf-8"))
    except SkillError as e:
        raise SkillError(f"{path}: {e}") from e
