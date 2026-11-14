"""Should the AE reach out at all? Decided here, in plain code, never by the model.

Each rule is a small function that returns a reason to hold, or None. RULES is checked in order and the first
reason wins. To add a rule: write one function and add it to RULES (and a test in tests/test_suppression.py).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from leadbot.hubspot import CrmState


@dataclass(frozen=True)
class Verdict:
    verdict: str              # "reach_out" or "hold"
    reason: str | None = None  # shown to the AE when the verdict is "hold"
    rule: str | None = None    # which rule fired, for logs and metrics


@dataclass(frozen=True)
class Context:
    today: date
    recent_contact_days: int = 30


Rule = Callable[[CrmState, Context], str | None]


def do_not_contact(state: CrmState, ctx: Context) -> str | None:
    return "Marked do-not-contact in HubSpot" if state.do_not_contact else None


def open_deal(state: CrmState, ctx: Context) -> str | None:
    if not state.open_deals:
        return None
    deal = state.open_deals[0]
    owner = f", owner {deal.owner}" if deal.owner else ""
    more = f" (+{len(state.open_deals) - 1} more)" if len(state.open_deals) > 1 else ""
    return f"Open deal{owner}, stage {deal.stage}{more}"


def existing_customer(state: CrmState, ctx: Context) -> str | None:
    if "customer" in (state.lifecycle_stage, state.company_lifecycle_stage):
        return "Already a customer"
    return None


def recently_contacted(state: CrmState, ctx: Context) -> str | None:
    if state.last_contacted is None:
        return None
    days = (ctx.today - state.last_contacted).days
    if 0 <= days < ctx.recent_contact_days:
        return f"Contacted {days} days ago, on {state.last_contacted.isoformat()}"
    return None


RULES: list[Rule] = [do_not_contact, open_deal, existing_customer, recently_contacted]


def evaluate(state: CrmState, ctx: Context) -> Verdict:
    for rule in RULES:
        reason = rule(state, ctx)
        if reason:
            return Verdict("hold", reason, rule.__name__)
    return Verdict("reach_out")
