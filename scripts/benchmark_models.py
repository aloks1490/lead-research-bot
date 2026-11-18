"""Benchmark brief-writing models on an OpenAI-compatible endpoint (NVIDIA NIM by default).

    uv run python scripts/benchmark_models.py                      # every LLM_MODEL line in .env, commented or not
    uv run python scripts/benchmark_models.py moonshotai/kimi-k3.1-pro z-ai/glm-5.3-flash
    uv run python scripts/benchmark_models.py --repeat 2 --out bench.jsonl

Each model writes the same four briefs (two "reach out", two "hold") twice: with its hidden reasoning on, and
with it switched off. It uses the bot's own system prompt, JSON parsing and fact checker, so "passed" means the
brief would have reached an AE. Responses are streamed to measure:

  ttft_ms         time to the first token of any kind (reasoning or answer)
  answer_ms       time to the first token of the answer itself: what an AE waits for
  total_ms        time to the last token
  output_tok_s    completion tokens / generation time (first token to last)
  reasoning_tok   tokens spent thinking (from the provider's usage report)
  passed          the brief passed the fact checker

Results are one JSON object per call (--out); a summary table is printed at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from leadbot.brief import JSON_INSTRUCTIONS, SYSTEM, parse_json_reply
from leadbot.check import check
from leadbot.config import Settings
from leadbot.facts import build_facts
from leadbot.hubspot import CrmState, OpenDeal
from leadbot.store import Company, Prospect
from leadbot.suppression import Context, evaluate

TODAY = date(2026, 11, 18)  # fixed, so the fact sheets are the same on every run
TIMEOUT = httpx.Timeout(240.0, connect=10.0)
# Models name their reasoning switch differently. Unknown template arguments are ignored, so both are sent.
THINKING_OFF = {"chat_template_kwargs": {"thinking": False, "enable_thinking": False}}


def cases(settings: Settings) -> dict[str, dict]:
    now = datetime.now(UTC)
    jane = Prospect(linkedin_url_norm="linkedin.com/in/jane-doe", name="Jane Doe", title="Head of Security",
                    location="Berlin", role_started_on=date(2024, 8, 1), company_domain="acme.io", enriched_at=now)
    acme = Company(domain="acme.io", name="Acme", industry="Fintech", employee_count=240, country="Germany",
                   description="Payments for online shops.")
    omar = Prospect(linkedin_url_norm="linkedin.com/in/omar-haddad", name="Omar Haddad", title="CTO",
                    location="Vienna", role_started_on=date(2022, 3, 1), company_domain="globex.com", enriched_at=now)
    globex = Company(domain="globex.com", name="Globex", industry="Logistics software", employee_count=85,
                     country="Austria", description="Route planning for couriers.")
    raw = {
        "reach_out": (jane, acme, CrmState()),
        "hold_open_deal": (jane, acme, CrmState(company_id="501",
                                                open_deals=[OpenDeal("Acme rollout", "Proposal", "Priya")])),
        "reach_out_2": (omar, globex, CrmState()),
        "hold_recent": (omar, globex, CrmState(contact_id="7", last_contacted=date(2026, 11, 5))),
    }
    return {name: build_facts(p, c, crm, evaluate(crm, Context(TODAY)), today=TODAY,
                              seller_name=settings.seller_name, seller_pitch=settings.seller_pitch)
            for name, (p, c, crm) in raw.items()}


def models_from_env(path: Path = Path(".env")) -> list[str]:
    """Every LLM_MODEL line in .env, active or commented out, in file order."""
    found = re.findall(r'^\s*#?\s*LLM_MODEL\s*=\s*["\']?([^"\'\s#]+)', path.read_text(), re.M)
    return list(dict.fromkeys(found))


async def call(client: httpx.AsyncClient, settings: Settings, model: str, facts: dict, thinking: bool) -> dict:
    """One streamed request. Retries a 429 a few times; drops JSON mode if the model rejects it."""
    body = {
        "model": model, "temperature": 0.2, "max_tokens": 4000, "stream": True,
        "stream_options": {"include_usage": True},
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM + JSON_INSTRUCTIONS},
                     {"role": "user", "content": json.dumps(facts, sort_keys=True, ensure_ascii=False)}],
        **({} if thinking else THINKING_OFF),
    }
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Accept": "text/event-stream"}
    result: dict = {"json_mode": True}
    for attempt in range(5):
        start = time.monotonic()
        first_any = first_answer = None
        answer, reasoning_chars, usage, finish, chunks = [], 0, {}, None, 0
        try:
            async with client.stream("POST", url, json=body, headers=headers, timeout=TIMEOUT) as response:
                if response.status_code == 429 and attempt < 4:
                    await asyncio.sleep(float(response.headers.get("Retry-After") or 5 * (attempt + 1)))
                    continue
                if response.status_code == 400 and "response_format" in body:
                    body.pop("response_format")
                    result["json_mode"] = False
                    continue
                if response.status_code != 200:
                    text = (await response.aread()).decode(errors="replace")
                    return {**result, "error": f"HTTP {response.status_code}: {text[:200]}"}
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    usage = event.get("usage") or usage
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        finish = choice.get("finish_reason") or finish
                        thought = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        text = delta.get("content") or ""
                        now = time.monotonic()
                        if (thought or text) and first_any is None:
                            first_any = now
                        if text and first_answer is None:
                            first_answer = now
                        if thought or text:
                            chunks += 1
                        reasoning_chars += len(thought)
                        answer.append(text)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            return {**result, "error": f"{type(e).__name__} after {time.monotonic() - start:.0f}s"}
        end = time.monotonic()
        break
    else:
        return {**result, "error": "rate limited (429) on every try"}

    completion = usage.get("completion_tokens") or chunks  # chunk count when the provider reports no usage
    reasoning_tok = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    generation = end - (first_any or end)
    content = "".join(answer)
    result.update({
        "ttft_ms": round(((first_any or end) - start) * 1000),
        "answer_ms": round(((first_answer or end) - start) * 1000),
        "total_ms": round((end - start) * 1000),
        "output_tok_s": round(completion / generation, 1) if generation > 0 else None,
        "prompt_tok": usage.get("prompt_tokens"),
        "completion_tok": completion,
        "reasoning_tok": reasoning_tok if reasoning_tok is not None else (None if reasoning_chars else 0),
        "reasoning_chars": reasoning_chars,
        "usage_reported": bool(usage),
        "finish_reason": finish,
        "content": content,
    })
    try:
        brief = parse_json_reply(content)
    except ValueError as e:
        result.update({"json_ok": False, "passed": False, "problems": [f"no JSON: {e}"]})
        return result
    problems = check(brief, facts)
    words = sum(len(str(v).split()) for k, v in brief.items() if k != "verdict") if isinstance(brief, dict) else None
    result.update({"json_ok": True, "passed": not problems, "problems": problems, "words": words, "brief": brief})
    return result


async def bench_model(client, settings, model, all_cases, repeat, sink) -> list[dict]:
    rows = []
    for thinking in (True, False):
        for name, facts in all_cases.items():
            for run in range(repeat):
                row = {"model": model, "thinking": thinking, "case": name, "run": run,
                       **await call(client, settings, model, facts, thinking)}
                rows.append(row)
                sink(row)
                status = row.get("error") or ("passed" if row.get("passed") else f"failed: {row.get('problems')}")
                print(f"{model:45} thinking={'on ' if thinking else 'off'} {name:15} "
                      f"total={row.get('total_ms', 0) / 1000:5.1f}s  {str(status)[:90]}", flush=True)
    return rows


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def summary(rows: list[dict]) -> str:
    lines = ["| Model | Thinking | Passed | Errors | Answer starts (s) | Total (s) | Output tok/s | "
             "Output tok | Reasoning tok | Words |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["model"], r["thinking"]), []).append(r)
    for (model, thinking), group in groups.items():
        ok = [r for r in group if "error" not in r]
        fmt = lambda v, scale=1, nd=1: "–" if v is None else f"{v / scale:.{nd}f}"  # noqa: E731
        lines.append(
            f"| {model} | {'on' if thinking else 'off'} | {sum(bool(r.get('passed')) for r in group)}/{len(group)} | "
            f"{len(group) - len(ok)} | {fmt(median([r['answer_ms'] for r in ok]), 1000)} | "
            f"{fmt(median([r['total_ms'] for r in ok]), 1000)} | {fmt(median([r['output_tok_s'] for r in ok]))} | "
            f"{fmt(median([r['completion_tok'] for r in ok]), nd=0)} | "
            f"{fmt(median([r['reasoning_tok'] for r in ok]), nd=0)} | "
            f"{fmt(median([r.get('words') for r in ok]), nd=0)} |")
    return "\n".join(lines) + "\n\nMedians over the successful calls. Passed = the brief passed the bot's fact checker."


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="*", help="model ids; default: every LLM_MODEL line in .env")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case and mode")
    parser.add_argument("--out", default="benchmark.jsonl", help="one JSON line per call")
    args = parser.parse_args()

    settings = Settings()
    models = args.models or models_from_env()
    all_cases = cases(settings)
    print(f"{len(models)} models x 2 modes x {len(all_cases)} cases x {args.repeat} runs, endpoint "
          f"{settings.llm_base_url}\n", flush=True)
    out = open(args.out, "w")

    def sink(row: dict) -> None:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        out.flush()

    async with httpx.AsyncClient() as client:  # models run in parallel; each model's calls run one at a time
        results = await asyncio.gather(*(bench_model(client, settings, m, all_cases, args.repeat, sink)
                                         for m in models))
    out.close()
    print("\n" + summary([r for rows in results for r in rows]))


if __name__ == "__main__":
    asyncio.run(main())
