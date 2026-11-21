"""The fact checker: rejects a model-written brief that says anything the fact sheet does not.

Ported from the Signal Engine (activation/narrate.py, `check`) and adapted to the brief schema. Rules:
  1. the shape is right, and the verdict equals the one computed in code
  2. every number in the brief appears in the fact sheet (dates count as their parts)
  3. every capitalised word that is not at the start of a sentence is a name or term from the fact sheet,
     or an ordinary word from COMMON_WORDS
  4. the whole brief is at most MAX_WORDS words
An empty result means accepted. Any problem means the template brief is used instead.

It is deliberately strict: a wrongly rejected brief costs nothing (the template still goes out), while a
wrongly accepted one puts an invented fact in front of an AE.
"""
from __future__ import annotations

import json
import re

MAX_WORDS = 120
MAX_HEADLINE_CHARS = 160
TEXT_FIELDS = ("headline", "person", "company", "angle")

# Capitalised words that are fine anywhere in a sentence without being in the fact sheet.
COMMON_WORDS = {
    "i", "ok", "ceo", "cto", "cfo", "coo", "cmo", "cro", "ciso", "cio", "cpo", "vp", "svp", "evp", "it", "hr",
    "crm", "hubspot", "linkedin", "saas", "b2b", "smb", "smbs", "ae", "aes", "sdr", "bdr", "q1", "q2", "q3", "q4",
}

# A country in the facts also allows its adjective: "Germany" -> "German". The fit names every country, target or
# not ("Australia is outside the target markets"), so the brief may too. A country missing here only costs a
# template brief; add it to the list.
COUNTRY_ADJECTIVES = {
    # target markets: EU, EEA, UK, Switzerland
    "germany": "german", "austria": "austrian", "switzerland": "swiss", "france": "french",
    "netherlands": "dutch", "belgium": "belgian", "luxembourg": "luxembourgish", "spain": "spanish",
    "portugal": "portuguese", "italy": "italian", "ireland": "irish", "united kingdom": "british",
    "denmark": "danish", "sweden": "swedish", "norway": "norwegian", "finland": "finnish", "poland": "polish",
    "czechia": "czech", "czech republic": "czech", "estonia": "estonian", "latvia": "latvian",
    "lithuania": "lithuanian", "slovakia": "slovak", "slovenia": "slovenian", "hungary": "hungarian",
    "romania": "romanian", "bulgaria": "bulgarian", "croatia": "croatian", "greece": "greek", "cyprus": "cypriot",
    "malta": "maltese", "iceland": "icelandic",
    # outside the target markets, but common in real profiles
    "united states": "american", "canada": "canadian", "australia": "australian", "new zealand": "new zealander",
    "india": "indian", "singapore": "singaporean", "israel": "israeli", "japan": "japanese", "china": "chinese",
    "brazil": "brazilian", "mexico": "mexican", "south africa": "south african", "turkey": "turkish",
    "ukraine": "ukrainian", "united arab emirates": "emirati",
}

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|[&'’.-])*")  # letters in any script, so "Müller" is one word
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SENTENCE_END = re.compile(r"(?<=[.!?:])\s+")


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)  # keep real characters: ASCII escapes like ü hold digits


def _numbers(value) -> set[str]:
    return set(_NUMBER.findall(_dump(value)))


def _words(value) -> set[str]:
    return {_bare(w) for w in _WORD.findall(_dump(value))}


def _bare(word: str) -> str:
    return word.casefold().strip(".'’-&")


def _fact_numbers(facts: dict) -> set[str]:
    """Numbers in the facts plus harmless rewordings: '09' -> '9', '4.0' -> '4', and the parts of ISO dates."""
    numbers = _numbers(facts)
    out = set(numbers)
    for n in numbers:
        out.add(str(int(float(n))) if float(n) == int(float(n)) else n)
    for year, month, day in _ISO_DATE.findall(_dump(facts)):
        out |= {year, str(int(month)), str(int(day))}
    return out


def _fact_words(facts: dict) -> set[str]:
    """Words in the facts plus harmless rewordings: the month names of their dates ('2026-11-02' allows
    'November' and 'Nov'), and the adjective of a country they name ('Germany' allows 'German')."""
    words = _words(facts)
    for _, month, _ in _ISO_DATE.findall(_dump(facts)):
        name = MONTHS[int(month) - 1]
        words |= {name, name[:3]}
    text = _dump(facts).casefold()
    words |= {w for country, adjective in COUNTRY_ADJECTIVES.items() if country in text for w in adjective.split()}
    return words


def _is_known(word: str, known: set[str]) -> bool:
    """Known as written, or as a plain grammatical form: possessive (Acme's), plural (CTOs), or every
    capitalised part of a hyphenated word (ISO-certified -> check 'ISO'; 'certified' is ordinary English)."""
    bare = re.sub(r"['’]s$", "", _bare(word))
    if bare in known or (bare.endswith("s") and bare[:-1] in known):
        return True
    parts = [p for p in word.strip(".'’-").split("-") if p]
    return len(parts) > 1 and all(not p[0].isupper() or len(p) == 1 or _is_known(p, known) for p in parts)


def _shape_problems(out: dict, facts: dict) -> list[str]:
    problems = []
    for name in TEXT_FIELDS:
        if not isinstance(out.get(name), str):
            problems.append(f"{name} missing")
        elif not out[name].strip() and not (name == "company" and not facts.get("company")):
            problems.append(f"{name} missing")  # empty is only right for "company", when no company is known
    if isinstance(out.get("headline"), str) and len(out["headline"]) > MAX_HEADLINE_CHARS:
        problems.append("headline too long")
    questions = out.get("questions")
    if not isinstance(questions, list) or len(questions) != 2 or not all(isinstance(q, str) and q.strip()
                                                                         for q in questions):
        problems.append("questions must be exactly 2 strings")
    if out.get("verdict") != facts["verdict"]:
        problems.append(f"verdict changed: code said {facts['verdict']}, model said {out.get('verdict')}")
    hold_reason = out.get("hold_reason")
    if not isinstance(hold_reason, str):
        problems.append("hold_reason must be a string")
    elif facts["verdict"] == "hold" and not hold_reason.strip():
        problems.append("hold_reason missing for a hold verdict")
    elif facts["verdict"] == "reach_out" and hold_reason.strip():
        problems.append("hold_reason given for a reach_out verdict")
    return problems


def check(out: dict, facts: dict) -> list[str]:
    """Reasons to reject the model's brief. Empty list = accepted."""
    problems = _shape_problems(out, facts)
    if problems:
        return problems

    texts = [out[name] for name in TEXT_FIELDS] + list(out["questions"]) + [out["hold_reason"]]
    word_count = sum(len(t.split()) for t in texts)
    if word_count > MAX_WORDS:
        problems.append(f"too long: {word_count} words (max {MAX_WORDS})")

    invented_numbers = _numbers(texts) - _fact_numbers(facts)
    if invented_numbers:
        problems.append(f"numbers not in facts: {sorted(invented_numbers)}")

    known = _fact_words(facts) | COMMON_WORDS
    invented_words = []
    for text in texts:
        for sentence in _SENTENCE_END.split(text):
            words = _WORD.findall(sentence)
            for word in words[1:]:  # the first word of a sentence is capitalised anyway
                shown = word.strip(".'’-&")
                if word[0].isupper() and not _is_known(word, known) and shown not in invented_words:
                    invented_words.append(shown)
    if invented_words:
        problems.append(f"names or terms not in facts: {invented_words}")
    return problems
