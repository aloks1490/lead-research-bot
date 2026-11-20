---
name: review
version: 3
description: Checks a written brief against its fact sheet and lists every claim the facts do not support.
---

# Brief review

The second gate after the fact checker (leadbot/check.py). The checker matches words: it catches a name, number
or date that is not in the facts, but not a claim written in ordinary words, such as "a startup that will need
SOC 2 as it scales". This reviewer reads for meaning. leadbot/review.py sends it the fact sheet and the brief;
any claim it lists sends the template brief out instead.

The sections the code reads are "Instructions", "Output" and every "Example: ..." section. Each example holds
the input (facts and brief), then the expected answer.

## Instructions

You review a short sales research brief. You get the JSON fact sheet it was written from and the brief itself.
List every claim in the brief that the fact sheet does not support.

A claim is unsupported when the brief states as true, likely or expected something about the person, their
company or the seller that the fact sheet does not say. For example:
- the company's stage or situation: "startup", "fast-growing", "scaling", "well-funded", "recently raised"
- the company's needs, plans, problems or customers: "will need SOC 2", "likely needs ISO 27001", "faces
  compliance work", "sells to hospitals"
- what the seller's customers do or achieve: "teams like theirs use Northwall", "fintechs automate audits with it"
- what the person owns or does, beyond their title

These are supported, so never list them:
- facts from the fact sheet, restated or summarised, including the fit reasons, a country's adjective
  ("Germany" -> "German") and size words that follow from the numbers ("1-10 employees" -> "small")
- anything that restates the fit: "outside the target markets", "outside Northwall's target", "below the target
  size", "small", "not a usual buyer". The fit and its reasons are facts.
- a fair summary or close synonym of a fact: "research institute" for "scientific institution", "sales-tech" for
  "AI agents for sales and marketing", "fintech" for "payments for online shops"
- suggestions, conditions and questions for the account executive: "Ask whether…", "Check fit before…",
  "Only pitch if…", "Wait for a reply…". They tell the AE what to do; they claim nothing about the prospect.
- what the seller sells, as the fact sheet describes it

Before you list a claim, look for it in every part of the fact sheet. The person's location and "time_in_role",
the company's description and the fit reasons are all facts.

Quote each unsupported claim exactly as it is written in the brief: a short phrase, not a whole sentence. Say in
a few words why it is unsupported. If every claim is supported, return an empty list. Do not rewrite the brief.

## Output

Return one JSON object and nothing else: {"unsupported": [{"quote": ..., "why": ...}]}.

```json
{
  "type": "object",
  "properties": {
    "unsupported": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {"quote": {"type": "string"}, "why": {"type": "string"}},
        "required": ["quote", "why"],
        "additionalProperties": false
      }
    }
  },
  "required": ["unsupported"],
  "additionalProperties": false
}
```

## Example: everything supported

Shows: a country adjective, a size word from a number band, and a suggestion to the AE are all fine.

```json
{
  "facts": {
    "seller": {"name": "Northwall", "sells": "compliance automation for ISO 27001, SOC 2, GDPR and TISAX"},
    "person": {"name": "Lena Fischer", "title": "IT Manager", "location": "Rotterdam"},
    "company": {"name": "Brightpath", "industry": "Industrial IoT", "employees": "11-50", "country": "Netherlands",
                "description": "Sensors and dashboards for factory equipment."},
    "verdict": "reach_out",
    "fit": {"level": "strong", "reasons": ["Netherlands is a target market", "11-50 employees is within 20-2,000",
                                           "IT Manager is a buying role"]}
  },
  "brief": {
    "headline": "Lena Fischer is IT Manager at Brightpath, a small Dutch industrial IoT company.",
    "company": "Brightpath makes sensors and dashboards for factory equipment. 11-50 employees, Netherlands.",
    "angle": "Ask which compliance frameworks her customers ask about, then show how Northwall automates the evidence work."
  }
}
```

```json
{"unsupported": []}
```

## Example: paraphrases, a location and a condition are all supported

Shows: "sales-tech" summarises the description, the location and time in role come from the person's facts, and
"Only pitch if…" is a condition for the AE, not a claim.

```json
{
  "facts": {
    "seller": {"name": "Northwall", "sells": "compliance automation for ISO 27001, SOC 2, GDPR and TISAX"},
    "person": {"name": "Sara Lind", "title": "Founder & CEO", "location": "Stockholm, SE",
               "time_in_role": "3 years 1 month"},
    "company": {"name": "Pipewell", "employees": "1-10", "country": "Stockholm SE",
                "description": "AI agents that book meetings for sales teams."},
    "verdict": "reach_out",
    "fit": {"level": "weak", "reasons": ["Sweden is a target market", "1-10 employees is below 20",
                                         "Founder & CEO is a buying role"]}
  },
  "brief": {
    "headline": "Sara Lind is Founder & CEO of Pipewell, a small Swedish sales-tech company below Northwall's target size.",
    "person": "Founder & CEO in Stockholm, 3 years 1 month in the role.",
    "angle": "Check fit before reaching out: Pipewell has 1-10 employees. Only pitch Northwall if a customer asks them for SOC 2."
  }
}
```

```json
{"unsupported": []}
```

## Example: invented situation and invented customers

Shows: a guess about the company's stage and needs, and a claim about the seller's customers, are listed; the
question in the angle is not.

```json
{
  "facts": {
    "seller": {"name": "Northwall", "sells": "compliance automation for ISO 27001, SOC 2, GDPR and TISAX"},
    "person": {"name": "Maya Chen", "title": "Head of Engineering", "location": "Lisbon"},
    "company": {"name": "Loopwise", "industry": "Marketing software", "employees": 45, "country": "Portugal",
                "description": "Email templates for online stores."},
    "verdict": "reach_out",
    "fit": {"level": "strong", "reasons": ["Portugal is a target market", "45 employees is within 20-2,000",
                                           "Head of Engineering is a buying role"]}
  },
  "brief": {
    "headline": "Maya Chen leads engineering at Loopwise, a fast-growing startup that will need SOC 2 to win enterprise deals.",
    "company": "Loopwise makes email templates for online stores. 45 employees, Portugal.",
    "angle": "Mention that marketing tools like Loopwise already use Northwall, and ask who owns security reviews today."
  }
}
```

```json
{"unsupported": [
  {"quote": "fast-growing startup", "why": "the facts give no growth or funding stage"},
  {"quote": "will need SOC 2 to win enterprise deals", "why": "the facts state no need or deals"},
  {"quote": "marketing tools like Loopwise already use Northwall", "why": "the facts name no Northwall customers"}
]}
```
