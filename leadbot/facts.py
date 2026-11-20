"""The fact sheet: the only thing the model ever sees.

Code builds a small JSON from the cache (Clay) and HubSpot. The model writes words around these facts; the
checker (check.py) rejects any name, number or date that is not in here.
"""
from __future__ import annotations

from datetime import date

from leadbot.hubspot import CrmState
from leadbot.store import Company, Prospect
from leadbot.suppression import Verdict


def tenure(started: date | None, today: date) -> str | None:
    """'1 year 3 months' in the current role, in whole months."""
    if started is None or started > today:
        return None
    months = (today.year - started.year) * 12 + (today.month - started.month)
    years, months = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months or not years:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    return " ".join(parts)


def _drop_empty(data: dict) -> dict:
    """Leave out what we do not know, so the model is never tempted to fill a blank."""
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


def build_facts(prospect: Prospect, company: Company | None, crm: CrmState, verdict: Verdict, *, today: date,
                seller_name: str, seller_pitch: str) -> dict:
    facts = {
        "seller": {"name": seller_name, "sells": seller_pitch},
        "person": _drop_empty({
            "name": prospect.name,
            "title": prospect.title,
            "time_in_role": tenure(prospect.role_started_on, today),
            "location": prospect.location,
        }),
        "company": _drop_empty({
            "name": company.name, "industry": company.industry,
            "employees": company.employee_count or company.employee_range,  # an exact count, else a band
            "country": company.country, "description": company.description,
        }) if company else {},
        "crm": _drop_empty({
            "contact_in_hubspot": crm.contact_id is not None,
            "company_in_hubspot": crm.company_id is not None,
            "lifecycle_stage": crm.lifecycle_stage or crm.company_lifecycle_stage,
            "last_contacted": crm.last_contacted.isoformat() if crm.last_contacted else None,
            "do_not_contact": crm.do_not_contact or None,
            "open_deals": [_drop_empty({"name": d.name, "stage": d.stage, "owner": d.owner}) for d in crm.open_deals],
        }),
        # Decided in code (suppression.py). The model must copy the verdict, never change it.
        "verdict": verdict.verdict,
    }
    if verdict.reason:
        facts["hold_reason"] = verdict.reason
    return facts
