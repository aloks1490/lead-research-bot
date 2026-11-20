# Clay table setup

> **Only for a paid Clay plan.** Clay's free plan does not include the webhook source or the HTTP API column, so
> the bot uses treg by default (`ENRICHMENT_PROVIDER=treg`, see the README). Set `ENRICHMENT_PROVIDER=clay` to use
> the setup below.

The bot sends a row to a Clay table and Clay posts the enriched row back. One table, three parts.

## 1. Source: a webhook

Create a table with a **Webhook** source. Copy its URL into `CLAY_WEBHOOK_URL`.
(If you turn on the webhook's auth token, put it in `CLAY_WEBHOOK_TOKEN`.)

The bot posts this body, so the table gets two columns:

```json
{"linkedin_url": "https://www.linkedin.com/in/jane-doe", "job_id": "0b6f6c1e-..."}
```

## 2. Enrichment columns

Add the enrichments you want, keyed on `linkedin_url`: one for the person (name, title, work email, location,
start date of the current role) and one for the company (name, domain, industry, employee count, country,
short description). If you already have a table from an earlier workflow, reuse its columns.

## 3. Last column: HTTP API, back to the bot

Add an **HTTP API** column that runs after the enrichments:

- Method: `POST`
- URL: `https://<your-host>/webhooks/clay`
- Headers: `Content-Type: application/json` and `X-Clay-Secret: <the value of CLAY_CALLBACK_SECRET>`
- Body (map each value to your own column names):

```json
{
  "job_id": "{{job_id}}",
  "linkedin_url": "{{linkedin_url}}",
  "person": {
    "full_name": "{{Full Name}}",
    "title": "{{Job Title}}",
    "email": "{{Work Email}}",
    "location": "{{Location}}",
    "role_started_on": "{{Current Role Start Date}}"
  },
  "company": {
    "name": "{{Company Name}}",
    "domain": "{{Company Domain}}",
    "industry": "{{Industry}}",
    "employee_count": "{{Employee Count}}",
    "country": "{{Country}}",
    "description": "{{Company Description}}"
  }
}
```

The bot is forgiving about this payload ([leadbot/clay.py](../leadbot/clay.py)):

- an empty string counts as "unknown"
- `employee_count` may be a number or a string such as `"1,200"`; a range such as `"201-500"` is ignored
- `role_started_on` may be `2024-08-01`, `2024-08` or a full timestamp
- `domain` may be a full URL; it is cut down to `acme.io`
- only `linkedin_url` is required. If `full_name` is empty, the bot tells the AE that nobody was found

The whole payload is stored in `prospects.raw`, so a field you add later can be backfilled from there.

## Check it

Mention the bot with a new profile and look for these log lines with the same `job_id`:
`api call` with `"service": "clay"`, then `clay callback stored` with `"jobs_resumed": 1`.
If the callback never arrives, the bot's sweeper (every 5 minutes) fails the job after 10 minutes and posts an alert.
