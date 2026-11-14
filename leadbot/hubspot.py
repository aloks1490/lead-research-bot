"""HubSpot CRM client: read what the suppression check needs, write the contact and the note back.

The retry logic that the Signal Engine kept inside its HubSpot client now lives in the shared HttpClient (http.py).
Deal stages, owners and association types are read from the account, never hard-coded, because they differ
between HubSpot portals.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime

from leadbot.http import ApiError, HttpClient

TIMEOUT = 10.0
OK = (200, 201, 204, 207)


@dataclass
class OpenDeal:
    name: str
    stage: str
    owner: str | None = None


@dataclass
class CrmState:
    """Everything the suppression rules look at. Built by HubSpot.lookup, stored on the brief for the audit trail."""
    contact_id: str | None = None
    company_id: str | None = None
    lifecycle_stage: str | None = None          # of the contact
    company_lifecycle_stage: str | None = None
    last_contacted: date | None = None
    do_not_contact: bool = False
    open_deals: list[OpenDeal] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["last_contacted"] = self.last_contacted.isoformat() if self.last_contacted else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> CrmState:
        data = dict(data)
        data["open_deals"] = [OpenDeal(**d) for d in data.get("open_deals", [])]
        if data.get("last_contacted"):
            data["last_contacted"] = date.fromisoformat(data["last_contacted"])
        return cls(**data)


def _parse_day(value: str | None) -> date | None:
    """HubSpot returns datetimes as ISO strings on v3, and as epoch milliseconds on some older properties."""
    if not value:
        return None
    if value.isdigit():
        return datetime.fromtimestamp(int(value) / 1000, UTC).date()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


class HubSpot:
    def __init__(self, http: HttpClient, token: str, base_url: str = "https://api.hubapi.com",
                 dnc_property: str = "hs_email_optout", linkedin_property: str = "hs_linkedin_url"):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.dnc_property = dnc_property
        self.linkedin_property = linkedin_property
        # Small per-process caches for things that almost never change.
        self._stages: dict[str, dict] | None = None
        self._owners: dict[str, str | None] = {}
        self._association_types: dict[tuple[str, str], int] = {}

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        response = await self.http.request("hubspot", method, self.base_url + path, endpoint=path.split("?")[0],
                                           timeout=TIMEOUT, ok=OK, json=json, headers=self.headers)
        return response.json() if response.status_code != 204 and response.content else {}

    # ---------- reads ----------

    async def _find(self, object_type: str, prop: str, value: str, properties: list[str]) -> dict | None:
        body = {"filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}],
                "properties": properties, "limit": 1}
        results = (await self._request("POST", f"/crm/v3/objects/{object_type}/search", json=body)).get("results", [])
        return results[0] if results else None

    async def _associated_ids(self, from_type: str, object_id: str, to_type: str) -> list[str]:
        data = await self._request("GET", f"/crm/v4/objects/{from_type}/{object_id}/associations/{to_type}")
        return [str(r["toObjectId"]) for r in data.get("results", [])]

    async def _deal_stages(self) -> dict[str, dict]:
        """{stage id: {"label": "Proposal", "closed": False}} across all deal pipelines."""
        if self._stages is None:
            pipelines = (await self._request("GET", "/crm/v3/pipelines/deals")).get("results", [])
            self._stages = {
                stage["id"]: {"label": stage.get("label") or stage["id"],
                              "closed": str(stage.get("metadata", {}).get("isClosed", "false")).lower() == "true"}
                for pipeline in pipelines for stage in pipeline.get("stages", [])
            }
        return self._stages

    async def _owner_name(self, owner_id: str | None) -> str | None:
        if not owner_id:
            return None
        if owner_id not in self._owners:
            try:
                owner = await self._request("GET", f"/crm/v3/owners/{owner_id}")
                self._owners[owner_id] = owner.get("firstName") or owner.get("email")
            except ApiError as e:
                if e.status != 404:  # a deleted owner is not worth failing the job over
                    raise
                self._owners[owner_id] = None
        return self._owners[owner_id]

    async def _open_deals(self, deal_ids: list[str]) -> list[OpenDeal]:
        if not deal_ids:
            return []
        body = {"inputs": [{"id": i} for i in deal_ids[:100]],
                "properties": ["dealname", "dealstage", "hubspot_owner_id"]}
        deals = (await self._request("POST", "/crm/v3/objects/deals/batch/read", json=body)).get("results", [])
        stages = await self._deal_stages()
        out = []
        for deal in deals:
            props = deal.get("properties", {})
            stage = stages.get(props.get("dealstage") or "", {"label": props.get("dealstage") or "unknown",
                                                              "closed": False})
            if not stage["closed"]:
                out.append(OpenDeal(name=props.get("dealname") or "Unnamed deal", stage=stage["label"],
                                    owner=await self._owner_name(props.get("hubspot_owner_id"))))
        return out

    async def lookup(self, email: str | None, domain: str | None) -> CrmState:
        """Read the contact (by email) and the company (by domain), plus the deals attached to either."""
        state = CrmState()
        deal_ids: list[str] = []
        if email:
            contact = await self._find("contacts", "email", email,
                                       ["lifecyclestage", "notes_last_contacted", self.dnc_property])
            if contact:
                props = contact.get("properties", {})
                state.contact_id = str(contact["id"])
                state.lifecycle_stage = props.get("lifecyclestage") or None
                state.last_contacted = _parse_day(props.get("notes_last_contacted"))
                state.do_not_contact = str(props.get(self.dnc_property) or "").lower() == "true"
                deal_ids += await self._associated_ids("contacts", state.contact_id, "deals")
        if domain:
            company = await self._find("companies", "domain", domain, ["lifecyclestage", "name"])
            if company:
                state.company_id = str(company["id"])
                state.company_lifecycle_stage = company.get("properties", {}).get("lifecyclestage") or None
                deal_ids += await self._associated_ids("companies", state.company_id, "deals")
        state.open_deals = await self._open_deals(list(dict.fromkeys(deal_ids)))  # de-duplicated, order kept
        return state

    # ---------- writes ----------

    async def upsert_contact(self, state: CrmState, *, email: str | None, name: str | None, title: str | None,
                             company_name: str | None, linkedin_url: str) -> str | None:
        """Create the contact if HubSpot does not have it. An existing contact is left alone: the CRM is the
        source of truth and a rep may have edited it. Returns the contact id, or None when there is no email
        to key the contact on."""
        if state.contact_id:
            return state.contact_id
        if not email:
            return None
        first, _, last = (name or "").partition(" ")
        props = {"email": email, "firstname": first, "lastname": last, "jobtitle": title, "company": company_name}
        if self.linkedin_property:
            props[self.linkedin_property] = linkedin_url
        props = {k: v for k, v in props.items() if v}
        try:
            created = await self._request("POST", "/crm/v3/objects/contacts", json={"properties": props})
            return str(created["id"])
        except ApiError as e:
            if e.status != 409:
                raise
            # 409 = the contact appeared between our search and our create (a retried step, or a rep). Use it.
            existing = await self._find("contacts", "email", email, [])
            return str(existing["id"]) if existing else None

    async def _association_type_id(self, from_type: str, to_type: str) -> int:
        """HubSpot's own association type between two objects, read from the account (a Signal Engine lesson:
        the 'default association' endpoint answered OK without linking activities)."""
        key = (from_type, to_type)
        if key not in self._association_types:
            labels = (await self._request("GET", f"/crm/v4/associations/{from_type}/{to_type}/labels")).get(
                "results", [])
            defined = [item for item in labels if item.get("category") == "HUBSPOT_DEFINED"]
            if not defined:
                raise ApiError("hubspot", f"associations/{from_type}/{to_type}/labels", 200,
                               "no HUBSPOT_DEFINED association type")
            unlabeled = [item for item in defined if not item.get("label")]
            self._association_types[key] = int((unlabeled or defined)[0]["typeId"])
        return self._association_types[key]

    async def create_note(self, body_html: str, when: datetime, contact_id: str | None,
                          company_id: str | None) -> str:
        """One note, attached to the contact and the company in the same call."""
        associations = []
        for object_type, object_id in (("contacts", contact_id), ("companies", company_id)):
            if object_id:
                type_id = await self._association_type_id("notes", object_type)
                associations.append({"to": {"id": object_id},
                                     "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                                "associationTypeId": type_id}]})
        body = {"properties": {"hs_note_body": body_html, "hs_timestamp": when.isoformat()},
                "associations": associations}
        return str((await self._request("POST", "/crm/v3/objects/notes", json=body))["id"])
