"""Fill a HubSpot developer test account with one record per suppression verdict, then check each one.

    uv run python scripts/seed_hubspot.py           # create what is missing, then check
    uv run python scripts/seed_hubspot.py --check   # only check

Seeding needs more scopes than the bot, so it uses its own service key, HUBSPOT_SEED_TOKEN, with:
crm.objects.contacts.read/write, crm.objects.companies.read/write, crm.objects.deals.read/write,
crm.objects.owners.read and crm.schemas.contacts.read/write. The check uses the bot's key (HUBSPOT_TOKEN) and the
bot's own lookup and rules, so it shows what the bot will decide. Set HUBSPOT_DNC_PROPERTY=do_not_contact so the bot
reads the checkbox this script creates.

Safe to run again: each record is looked up by email, domain or name first and only created if missing.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, timedelta

import httpx

from leadbot.config import Settings
from leadbot.http import HttpClient
from leadbot.hubspot import HubSpot
from leadbot.suppression import Context, evaluate

DNC_PROPERTY = "do_not_contact"
DEAL_NAME = "Seed: open deal for Omar"

# (email, first name, last name, rule the bot should fire or None for reach_out, extra contact properties)
CONTACTS = [
    ("riya.reach@example.com", "Riya", "Reach", None, {}),
    ("dina.dnc@example.com", "Dina", "Dnc", "do_not_contact", {DNC_PROPERTY: "true"}),
    ("omar.deal@example.com", "Omar", "Deal", "open_deal", {}),
    ("cara.customer@example.com", "Cara", "Customer", "existing_customer", {"lifecyclestage": "customer"}),
    ("rahul.recent@example.com", "Rahul", "Recent", "recently_contacted", {}),
]
# A company that is a customer: a new prospect at this domain should be held even with no contact record.
COMPANY_DOMAIN, COMPANY_NAME = "customer-co.example", "Customer Co"


class Api:
    """Plain synchronous calls. A seed script runs once by hand, so an error stops it with HubSpot's message."""

    def __init__(self, client: httpx.Client, base_url: str, token: str):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, json: dict | None = None, allow_404: bool = False) -> dict | None:
        response = self.client.request(method, self.base_url + path, json=json, headers=self.headers, timeout=15)
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400:
            sys.exit(f"{method} {path} failed with {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    def find(self, object_type: str, prop: str, value: str) -> str | None:
        body = {"filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}], "limit": 1}
        results = self.call("POST", f"/crm/v3/objects/{object_type}/search", json=body)["results"]
        return results[0]["id"] if results else None

    def associate(self, from_type: str, from_id: str, to_type: str, to_id: str) -> None:
        """HubSpot's default association between two records. PUT, so repeating it changes nothing."""
        self.call("PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}")


def ensure_dnc_property(api: Api) -> None:
    if api.call("GET", f"/crm/v3/properties/contacts/{DNC_PROPERTY}", allow_404=True):
        return
    api.call("POST", "/crm/v3/properties/contacts", json={
        "name": DNC_PROPERTY, "label": "Do not contact", "type": "bool", "fieldType": "booleancheckbox",
        "groupName": "contactinformation",
        "options": [{"label": "Yes", "value": "true"}, {"label": "No", "value": "false"}]})
    print(f"created contact property {DNC_PROPERTY}")


def ensure_contact(api: Api, email: str, first: str, last: str, extra: dict) -> str:
    contact_id = api.find("contacts", "email", email)
    if contact_id:
        if extra:
            api.call("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": extra})
        return contact_id
    props = {"email": email, "firstname": first, "lastname": last, **extra}
    contact_id = api.call("POST", "/crm/v3/objects/contacts", json={"properties": props})["id"]
    print(f"created contact {email}")
    return contact_id


def ensure_company(api: Api) -> None:
    if api.find("companies", "domain", COMPANY_DOMAIN):
        return
    api.call("POST", "/crm/v3/objects/companies", json={"properties": {
        "domain": COMPANY_DOMAIN, "name": COMPANY_NAME, "lifecyclestage": "customer"}})
    print(f"created company {COMPANY_DOMAIN}")


def ensure_open_deal(api: Api, contact_id: str) -> None:
    deal_id = api.find("deals", "dealname", DEAL_NAME)
    if not deal_id:
        pipeline = api.call("GET", "/crm/v3/pipelines/deals")["results"][0]
        stage = next(s for s in pipeline["stages"] if s.get("metadata", {}).get("isClosed") != "true")
        owners = api.call("GET", "/crm/v3/owners?limit=1")["results"]
        props = {"dealname": DEAL_NAME, "pipeline": pipeline["id"], "dealstage": stage["id"], "amount": "12000"}
        if owners:
            props["hubspot_owner_id"] = owners[0]["id"]
        deal_id = api.call("POST", "/crm/v3/objects/deals", json={"properties": props})["id"]
        print(f"created deal '{DEAL_NAME}' in stage {stage['label']}")
    api.associate("deals", deal_id, "contacts", contact_id)


def ensure_recent_call(api: Api, contact_id: str) -> None:
    """A logged call is what moves HubSpot's 'last contacted' date; notes do not."""
    if api.call("GET", f"/crm/v4/objects/contacts/{contact_id}/associations/calls")["results"]:
        return
    when = datetime.now(UTC) - timedelta(days=5)
    call_id = api.call("POST", "/crm/v3/objects/calls", json={"properties": {
        "hs_timestamp": when.isoformat(), "hs_call_title": "Seed: intro call", "hs_call_body": "Seed data.",
        "hs_call_status": "COMPLETED", "hs_call_direction": "OUTBOUND"}})["id"]
    api.associate("calls", call_id, "contacts", contact_id)
    print("logged a call 5 days ago on rahul.recent@example.com")


def seed(settings: Settings, token: str) -> None:
    with httpx.Client() as client:
        api = Api(client, settings.hubspot_base_url, token)
        ensure_dnc_property(api)
        ids = {email: ensure_contact(api, email, first, last, extra) for email, first, last, _, extra in CONTACTS}
        ensure_company(api)
        ensure_open_deal(api, ids["omar.deal@example.com"])
        ensure_recent_call(api, ids["rahul.recent@example.com"])


async def check(settings: Settings) -> bool:
    cases = [(email, None, rule) for email, _, _, rule, _ in CONTACTS] + [(None, COMPANY_DOMAIN, "existing_customer")]
    all_ok = True
    async with httpx.AsyncClient() as client:
        hubspot = HubSpot(HttpClient(client), settings.hubspot_token, settings.hubspot_base_url,
                          settings.hubspot_dnc_property, settings.hubspot_linkedin_property)
        for email, domain, expected in cases:
            verdict = evaluate(await hubspot.lookup(email, domain), Context(today=date.today()))
            ok = verdict.rule == expected
            all_ok &= ok
            reason = f" ({verdict.reason})" if verdict.reason else ""
            print(f"{'ok  ' if ok else 'FAIL'} {email or domain:<28} expected {expected or 'reach_out':<19} "
                  f"got {verdict.rule or 'reach_out'}{reason}")
    if not all_ok:
        print("HubSpot sets 'last contacted' a few minutes after a call is logged; if only that row failed, "
              "run --check again shortly.")
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only check, create nothing")
    args = parser.parse_args()

    settings = Settings()
    if settings.hubspot_dnc_property != DNC_PROPERTY:
        sys.exit(f"Set HUBSPOT_DNC_PROPERTY={DNC_PROPERTY} in .env so the bot reads the checkbox this script sets.")
    if not args.check:
        if not settings.hubspot_seed_token:
            sys.exit("Set HUBSPOT_SEED_TOKEN in .env (a second service key; scopes are in this file's docstring).")
        seed(settings, settings.hubspot_seed_token)
    sys.exit(0 if asyncio.run(check(settings)) else 1)


if __name__ == "__main__":
    main()
