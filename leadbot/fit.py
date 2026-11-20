"""Is this person a likely buyer of what the seller sells? Decided here, in plain code, never by the model.

The CRM check (suppression.py) answers "is there a reason NOT to contact them?". It cannot say whether they are
worth contacting: anyone missing from HubSpot passes it. This module answers the second question from the
enrichment data, with three checks against the seller's target customer (set in config.py):

    market    the company's country (or the person's, if no company is known) is a target market
    size      the company's headcount is inside the target range
    role      the person's title is one that buys or influences the purchase

Result: "strong" (every known check passes), "weak" (any check fails) or "unknown" (too little data to say).
Each check adds one plain-English reason, shown to the AE next to the CRM verdict.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# ISO country code -> name, for the countries a location string is likely to mention. Codes not listed here still
# work (they are compared as codes); names not listed here are treated as unknown.
COUNTRIES = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "HR": "Croatia", "CY": "Cyprus", "CZ": "Czechia",
    "DK": "Denmark", "EE": "Estonia", "FI": "Finland", "FR": "France", "DE": "Germany", "GR": "Greece",
    "HU": "Hungary", "IE": "Ireland", "IT": "Italy", "LV": "Latvia", "LT": "Lithuania", "LU": "Luxembourg",
    "MT": "Malta", "NL": "Netherlands", "PL": "Poland", "PT": "Portugal", "RO": "Romania", "SK": "Slovakia",
    "SI": "Slovenia", "ES": "Spain", "SE": "Sweden", "GB": "United Kingdom", "CH": "Switzerland", "NO": "Norway",
    "IS": "Iceland", "LI": "Liechtenstein", "US": "United States", "CA": "Canada", "IN": "India",
    "AU": "Australia", "SG": "Singapore", "AE": "United Arab Emirates", "IL": "Israel", "BR": "Brazil",
    "JP": "Japan", "CN": "China",
}
ALIASES = {"uk": "GB", "england": "GB", "great britain": "GB", "usa": "US", "united states of america": "US",
           "czech republic": "CZ", "deutschland": "DE", "schweiz": "CH", "österreich": "AT"}
_BY_NAME = {name.casefold(): code for code, name in COUNTRIES.items()} | ALIASES


@dataclass(frozen=True)
class Target:
    """The seller's target customer. Built from settings, so another seller only changes configuration."""
    countries: frozenset[str]
    min_employees: int
    max_employees: int
    buyer_titles: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings) -> Target:
        return cls(
            countries=frozenset(c.strip().upper() for c in settings.fit_countries.split(",") if c.strip()),
            min_employees=settings.fit_min_employees,
            max_employees=settings.fit_max_employees,
            buyer_titles=tuple(t.strip().casefold() for t in settings.fit_buyer_titles.split(",") if t.strip()),
        )


@dataclass
class Fit:
    level: str                                   # "strong", "weak" or "unknown"
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)  # market/size/role -> "pass", "fail" or "unknown"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> Fit:
        return cls(**data) if data else cls("unknown", ["Fit was not assessed"])


# ---------- reading the data ----------

def country_code(location: str | None) -> str | None:
    """'Berlin, DE' / 'Los Angeles US' / 'Germany' / 'Zurich, Switzerland' -> a two-letter code, or None."""
    if not location:
        return None
    text = location.strip()
    last = re.split(r"[\s,]+", text)[-1]
    if re.fullmatch(r"[A-Z]{2}", last):
        return "GB" if last == "UK" else last
    lowered = text.casefold()
    for name, code in sorted(_BY_NAME.items(), key=lambda item: -len(item[0])):  # longest first: "united states"
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return code
    return None


def headcount_bounds(count: int | None, band: str | None) -> tuple[int, int] | None:
    """An exact count, or a band like '51-200' or '10,001+', as (low, high). None if unknown."""
    if count is not None:
        return count, count
    if not band:
        return None
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", band)]
    if band.strip().endswith("+") and numbers:
        return numbers[0], 10**9
    return (numbers[0], numbers[-1]) if numbers else None


def _is_buyer(title: str, target: Target) -> bool:
    lowered = title.casefold()
    return any(re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", lowered) for word in target.buyer_titles)


# ---------- the checks ----------

def evaluate(*, title: str | None, person_location: str | None, company_name: str | None,
             company_country: str | None, employees: int | None, employee_band: str | None,
             target: Target) -> Fit:
    reasons, checks = [], {}

    # Market: the company's country, or the person's when no company is known.
    code = country_code(company_country) or country_code(person_location)
    where = COUNTRIES.get(code, code) if code else None
    if code is None:
        checks["market"] = "unknown"
        reasons.append("Country not known")
    elif code in target.countries:
        checks["market"] = "pass"
        reasons.append(f"{where} is a target market")
    else:
        checks["market"] = "fail"
        reasons.append(f"{where} is outside the target markets")

    # Size: the band must overlap the target range.
    bounds = headcount_bounds(employees, employee_band)
    size_text = f"{employees}" if employees is not None else employee_band
    if bounds is None:
        checks["size"] = "unknown"
        reasons.append("Company size not known" if company_name else "No company found")
    elif bounds[1] < target.min_employees:
        checks["size"] = "fail"
        reasons.append(f"{size_text} employees is below {target.min_employees}")
    elif bounds[0] > target.max_employees:
        checks["size"] = "fail"
        reasons.append(f"{size_text} employees is above {target.max_employees:,}")
    else:
        checks["size"] = "pass"
        reasons.append(f"{size_text} employees is within {target.min_employees}-{target.max_employees:,}")

    # Role: someone who buys or influences a compliance purchase.
    if not title:
        checks["role"] = "unknown"
        reasons.append("Role not known")
    elif _is_buyer(title, target):
        checks["role"] = "pass"
        reasons.append(f"{title} is a buying role")
    else:
        checks["role"] = "fail"
        reasons.append(f"{title} is not a usual buying role")

    if "fail" in checks.values():
        level = "weak"
    elif list(checks.values()).count("unknown") >= 2 or not company_name:
        level = "unknown"
    else:
        level = "strong"
    return Fit(level, reasons, checks)
