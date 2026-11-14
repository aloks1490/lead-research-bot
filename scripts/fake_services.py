"""Local stand-ins for Slack, Clay and HubSpot, so the whole bot can run on a laptop with no accounts.

    uv run uvicorn scripts.fake_services:app --port 8001

Point the bot at it with the values in .env.example ("Local demo" block), then send a mention with
scripts/send_mention.py. Slack messages are printed here instead of posted. Clay "enriches" after one second
and calls the bot back, like the real HTTP API column does. HubSpot knows one company with an open deal
(globex.com); everything else is new to it.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os

import httpx
from fastapi import FastAPI, Request

BOT_URL = os.environ.get("BOT_URL", "http://localhost:8000")
CLAY_CALLBACK_SECRET = os.environ.get("CLAY_CALLBACK_SECRET", "local-clay-secret")

print = functools.partial(print, flush=True)  # show output at once, also when piped to a file

app = FastAPI(title="fake Slack + Clay + HubSpot")
state = {"contacts": {}, "notes": [], "messages": 0}

PEOPLE = {  # what "Clay" knows, keyed by profile slug; unknown slugs get a generic person
    "jane-doe": ({"full_name": "Jane Doe", "title": "Head of Security", "email": "jane.doe@acme.io",
                  "location": "Berlin", "role_started_on": "2024-08-01"},
                 {"name": "Acme", "domain": "acme.io", "industry": "Fintech", "employee_count": 240,
                  "country": "Germany", "description": "Payments for online shops."}),
    "omar-haddad": ({"full_name": "Omar Haddad", "title": "CTO", "email": "omar@globex.com",
                     "location": "Vienna", "role_started_on": "2022-03-01"},
                    {"name": "Globex", "domain": "globex.com", "industry": "Logistics software",
                     "employee_count": 85, "country": "Austria", "description": "Route planning for couriers."}),
    "nobody-here": ({"full_name": ""}, {}),  # a profile Clay cannot find
}


# ---------- Slack ----------

@app.post("/slack/chat.postMessage")
async def post_message(request: Request):
    body = await request.json()
    state["messages"] += 1
    print(f"\n--- Slack message to {body['channel']} (thread {body.get('thread_ts')}) ---\n{body['text']}")
    for block in body.get("blocks", []):
        text = block.get("text", {}).get("text") or " ".join(e["text"] for e in block.get("elements", []))
        print(f"  | {text}".replace("\n", "\n  | "))
    return {"ok": True, "ts": f"1700000000.{state['messages']:06d}"}


# ---------- Clay ----------

@app.post("/clay/webhook")
async def clay_webhook(request: Request):
    body = await request.json()
    asyncio.get_running_loop().create_task(_call_back(body))
    return {"status": "accepted"}


async def _call_back(body: dict) -> None:
    await asyncio.sleep(1)
    slug = body["linkedin_url"].rstrip("/").rsplit("/", 1)[-1]
    person, company = PEOPLE.get(slug, (
        {"full_name": slug.replace("-", " ").title(), "title": "Engineering Manager", "email": f"{slug}@example.org"},
        {"name": "Example Org", "domain": "example.org", "industry": "Software", "employee_count": 120,
         "country": "Netherlands"}))
    payload = {"job_id": body["job_id"], "linkedin_url": body["linkedin_url"], "person": person, "company": company}
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{BOT_URL}/webhooks/clay", json=payload,
                                     headers={"X-Clay-Secret": CLAY_CALLBACK_SECRET})
    print(f"\n--- Clay called back for {slug}: bot answered {response.status_code} {response.text}")


# ---------- HubSpot ----------

GLOBEX = {"id": "501", "properties": {"domain": "globex.com", "name": "Globex", "lifecyclestage": "opportunity"}}


@app.post("/hubspot/crm/v3/objects/{object_type}/search")
async def search(object_type: str, request: Request):
    flt = (await request.json())["filterGroups"][0]["filters"][0]
    if object_type == "companies" and flt["value"] == "globex.com":
        return {"results": [GLOBEX]}
    if object_type == "contacts" and flt["value"] in state["contacts"]:
        return {"results": [state["contacts"][flt["value"]]]}
    return {"results": []}


@app.get("/hubspot/crm/v4/objects/{from_type}/{object_id}/associations/{to_type}")
async def associations(from_type: str, object_id: str, to_type: str):
    linked = from_type == "companies" and object_id == "501" and to_type == "deals"
    return {"results": [{"toObjectId": 901}] if linked else []}


@app.post("/hubspot/crm/v3/objects/deals/batch/read")
async def deals():
    return {"results": [{"id": "901", "properties": {"dealname": "Globex rollout", "dealstage": "proposal",
                                                     "hubspot_owner_id": "77"}}]}


@app.get("/hubspot/crm/v3/pipelines/deals")
async def pipelines():
    return {"results": [{"id": "default", "stages": [
        {"id": "proposal", "label": "Proposal", "metadata": {"isClosed": "false"}},
        {"id": "closedwon", "label": "Closed won", "metadata": {"isClosed": "true"}}]}]}


@app.get("/hubspot/crm/v3/owners/{owner_id}")
async def owner(owner_id: str):
    return {"id": owner_id, "firstName": "Priya", "email": "priya@seller.test"}


@app.post("/hubspot/crm/v3/objects/contacts", status_code=201)
async def create_contact(request: Request):
    props = (await request.json())["properties"]
    contact = {"id": str(100 + len(state["contacts"])), "properties": props}
    state["contacts"][props["email"]] = contact
    print(f"\n--- HubSpot contact created: {json.dumps(props)}")
    return contact


@app.get("/hubspot/crm/v4/associations/notes/{to_type}/labels")
async def labels(to_type: str):
    return {"results": [{"category": "HUBSPOT_DEFINED", "typeId": {"contacts": 202, "companies": 190}[to_type],
                         "label": None}]}


@app.post("/hubspot/crm/v3/objects/notes", status_code=201)
async def create_note(request: Request):
    body = await request.json()
    state["notes"].append(body)
    linked = [a["to"]["id"] for a in body["associations"]]
    print(f"\n--- HubSpot note created, attached to {linked}:\n  {body['properties']['hs_note_body']}")
    return {"id": str(7000 + len(state["notes"]))}
