"""Score reviewer models on labelled briefs (scripts/reviewer_cases.py).

    uv run python scripts/eval_reviewer.py mistralai/mistral-large-4-1t-a52b google/gemma-4-31b-it
    uv run python scripts/eval_reviewer.py --repeat 2 --out docs/benchmarks/reviewer.jsonl <models>

Per model: how many invented claims it caught (each "must_flag" entry in a bad case), how many clean briefs it
wrongly rejected, how often it could not answer, and how long it took. The reviewer code is the bot's own
(leadbot/review.py), so this measures exactly what production would do, including dropping quotes that are not
in the brief.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics

import httpx

from leadbot.config import Settings
from leadbot.http import HttpClient
from leadbot.review import Reviewer
from scripts.reviewer_cases import CASES


def caught(review, words: list[str]) -> bool:
    quotes = " | ".join(c["quote"].casefold() for c in review.unsupported)
    return any(w.casefold() in quotes for w in words)


async def run_model(http: HttpClient, settings: Settings, model: str, repeat: int, sink) -> dict:
    extra = json.loads(settings.llm_extra_body) if settings.llm_extra_body.strip() else None
    reviewer = Reviewer(http, settings.llm_base_url, settings.llm_api_key, model, extra)
    claims = hits = clean = false_alarms = skipped = dropped = 0
    times = []
    for case in CASES:
        for run in range(repeat):
            review = await reviewer.review(case["facts"], case["brief"])
            times.append(review.duration_ms)
            dropped += review.dropped
            if review.status == "skipped":
                skipped += 1
            if case["must_flag"]:
                claims += len(case["must_flag"])
                hits += sum(caught(review, words) for words in case["must_flag"]) if review.status != "skipped" else 0
            else:
                clean += 1
                false_alarms += review.status == "rejected"
            row = {"model": model, "case": case["name"], "run": run, **review.to_dict()}
            sink(row)
            mark = "SKIP" if review.status == "skipped" else ("flag" if review.status == "rejected" else "pass")
            print(f"{model:40} {case['name']:28} {mark}  {review.duration_ms / 1000:5.1f}s  "
                  f"{[c['quote'] for c in review.unsupported]}"[:230], flush=True)
    return {"model": model, "caught": f"{hits}/{claims}", "false_alarms": f"{false_alarms}/{clean}",
            "skipped": skipped, "dropped_quotes": dropped,
            "median_s": round(statistics.median(times) / 1000, 1), "max_s": round(max(times) / 1000, 1)}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="+")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", default="reviewer-eval.jsonl")
    args = parser.parse_args()
    settings = Settings()
    out = open(args.out, "w")

    def sink(row: dict) -> None:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        out.flush()

    async with httpx.AsyncClient() as client:
        http = HttpClient(client, max_tries=2)
        # One model at a time: running several at once against a free endpoint trips its rate limit (429) and
        # times calls out, which measures the endpoint's load, not the model.
        results = [await run_model(http, settings, m, args.repeat, sink) for m in args.models]
    out.close()
    print("\n| Model | Invented claims caught | Clean briefs wrongly rejected | Could not answer | "
          "Quotes not in brief | Median | Slowest |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        print(f"| {r['model']} | {r['caught']} | {r['false_alarms']} | {r['skipped']} | {r['dropped_quotes']} | "
              f"{r['median_s']} s | {r['max_s']} s |")


if __name__ == "__main__":
    asyncio.run(main())
