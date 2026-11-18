# Model benchmark: which free model writes the briefs

Run on 2026-11-18 against NVIDIA NIM's free endpoint with [scripts/benchmark_models.py](../scripts/benchmark_models.py).
Each model wrote the same four briefs (two "reach out", two "hold") twice, with its hidden reasoning on and off:
8 calls per row. The prompt, JSON parsing and fact checker are the bot's own, so "passed" means the brief would
have reached an AE.

**Result: Kimi K3.1 Pro with thinking off.** It passed 8 of 8 and wrote the best briefs, in a median of 19 s.
Turning thinking on made every model 2 to 9 times slower and did not improve the pass rate.

| Model | Thinking | Passed | Answer starts (median) | Total (median) | Total (slowest) | Output tokens | Of which reasoning | Stream speed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Kimi K3.1 Pro | off | 8/8 | 14.1 s | 19.3 s | 27.9 s | 152 | 0 | 40 tok/s |
| Kimi K3.1 Pro | on | 7/8 ¹ | 35.0 s | 38.8 s | 81.6 s | 1001 | 841 | 37 tok/s |
| Nemotron 3 Ultra (550B) | off | 7/8 | 4.0 s | 4.0 s | 7.5 s | 148 | 0 | not streamed ² |
| Nemotron 3 Ultra (550B) | on | 8/8 | 15.5 s | 15.5 s | 42.2 s | 814 | 664 | 55 tok/s |
| Nemotron 3.5 Super VL (120B) | off | 7/8 | 1.3 s | 1.3 s | 1.9 s | 140 | 0 | not streamed ² |
| Nemotron 3.5 Super VL (120B) | on | 8/8 | 11.7 s | 11.7 s | 14.1 s | 1778 | 1628 | 158 tok/s |

Prompt size was 550 to 630 tokens in every case.

¹ The one miss was a connection timeout to the endpoint, not a bad brief.
² NIM returned these answers in one block instead of streaming them, so a tokens-per-second figure would be
meaningless. With reasoning on, the reasoning streams and the answer still arrives in one block at the end.

**Not tested.** DeepSeek V4.1 Flash reached end of life on NIM on 2026-11-16 (the API answers "410 Gone").
GLM-5.3 Flash is listed in the catalog but did not answer at all: even a two-word request timed out after 90 s.

## Brief quality

I read every brief. All three models kept to the facts well enough to pass the checker; they differ in judgement.

| Model | Strengths | Weaknesses |
| --- | --- | --- |
| Kimi K3.1 Pro | The only model whose hold briefs act on the hold: "Align with Priya on the Acme rollout before…", "When outreach resumes…". Rewrites the hold reason as a sentence. Headlines say why the person matters to the seller. | Slowest. Most of its time is spent waiting before the first token (about 14 s), not generating. |
| Nemotron 3 Ultra | Sharpest discovery questions ("How do you manage evidence collection for audits today?"). Short. | Twice left the person's name out of the headline. Adds small unsupported claims: "a growing SaaS platform", "logistics data handling", "the next 12 months" (the last was caught by the checker). |
| Nemotron 3.5 Super VL | Fastest by far. Plain and safe. | Generic angles ("Discuss compliance automation for ISO 27001, SOC 2, GDPR and TISAX"). Pitches even on a hold. Copies the hold reason word for word. Once returned an empty angle and no questions. |

## What this found about the bot

- **The checker only catches names, numbers and dates.** "A growing SaaS platform" passed although nothing in the
  facts says the company is growing. Claims written in lower-case words are not checked. A stricter check would
  need a second model call to judge the brief against the facts.
- **Reasoning is wasted on this task.** A 70 to 90 word brief from a 600-token fact sheet gains nothing from it,
  and it can push a call past the bot's 60-second timeout (it did once with Kimi).
- **Free endpoints change underneath you.** Two of the five models were unusable on the day of the test. The
  template fallback means a dead model costs a plain brief, not a failed job.

## Run it again

```bash
uv run python scripts/benchmark_models.py                  # every LLM_MODEL line in .env, commented or not
uv run python scripts/benchmark_models.py --repeat 3 moonshotai/kimi-k3.1-pro
```
