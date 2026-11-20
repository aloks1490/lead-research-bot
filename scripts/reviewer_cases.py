"""Labelled briefs for choosing and checking the reviewer model (scripts/eval_reviewer.py).

"bad" cases carry claims the facts do not support, taken from real briefs the bot wrote between 2026-11-19 and
2026-11-21 (two are written to cover a gap: funding and the seller's customers). Each entry in `must_flag` is one
invented claim, given as words any of which in a returned quote counts as catching it.
"good" cases are briefs that must pass: restated facts, fit reasons, country adjectives, size words, suggestions.
"""
from __future__ import annotations

SELLER = {"name": "Northwall", "sells": "compliance automation for ISO 27001, SOC 2, GDPR and TISAX"}
CRM_EMPTY = {"contact_in_hubspot": False, "company_in_hubspot": False}


def facts(person: dict, company: dict, fit: dict, verdict: str = "reach_out", **extra) -> dict:
    return {"seller": SELLER, "person": person, "company": company, "crm": CRM_EMPTY, "verdict": verdict,
            "fit": fit, **extra}


def brief(headline: str, person: str, company: str, angle: str, questions: list[str] | None = None,
          verdict: str = "reach_out", hold_reason: str = "") -> dict:
    return {"verdict": verdict, "hold_reason": hold_reason, "headline": headline, "person": person,
            "company": company, "angle": angle,
            "questions": questions or ["Which compliance frameworks matter most to you?",
                                       "Who owns compliance on your team today?"]}


WEAK_US = {"level": "weak", "reasons": ["United States is outside the target markets", "1-10 employees is below 20",
                                        "Founder & CEO is a buying role"]}
REID = facts({"name": "Reid Hoffman", "title": "Co-Founder, Executive Board Chair", "location": "US",
              "time_in_role": "1 year 10 months"},
             {"name": "Manas AI", "industry": "Biotechnology Research", "employees": "1-10", "country": "US",
              "description": "Manas AI works on biotechnology research."},
             {"level": "weak", "reasons": ["United States is outside the target markets", "1-10 employees is below 20",
                                           "Co-Founder, Executive Board Chair is a buying role"]})
ALOK = facts({"name": "Alok Singh", "title": "AI x GTM Engineer", "location": "Pune City IN"}, {},
             {"level": "weak", "reasons": ["India is outside the target markets", "No company found",
                                           "AI x GTM Engineer is not a usual buying role"]})
ARC = facts({"name": "Patrick Collison", "title": "Cofounder", "location": "San Francisco, US",
             "time_in_role": "5 years 10 months"},
            {"name": "Arc Institute", "industry": "Biotechnology", "employees": "51-200", "country": "Palo Alto US",
             "description": "Arc Institute is a new scientific institution that conducts curiosity-driven basic "
                            "science and technology development. Headquartered in Palo Alto, California, Arc is a "
                            "non-profit."},
            {"level": "weak", "reasons": ["United States is outside the target markets",
                                          "51-200 employees is within 20-2,000", "Cofounder is a buying role"]})
RIYA = facts({"name": "Riya Reach", "title": "Head of IT", "location": "Munich", "time_in_role": "3 years 6 months"},
             {"name": "Reach Systems", "industry": "Software", "employees": 150, "country": "Germany",
              "description": "Scheduling software for clinics."},
             {"level": "strong", "reasons": ["Germany is a target market", "150 employees is within 20-2,000",
                                             "Head of IT is a buying role"]})
NICO = facts({"name": "Nico Druelle", "title": "Founder & CEO", "location": "San Francisco, US",
              "time_in_role": "2 years 10 months"},
             {"name": "The Revenue Architects", "industry": "Technology; Information and Internet", "employees": "1-10",
              "country": "Los Angeles US",
              "description": "Grow your Pipeline with AI Agents and Human Experts. We combine Agentic workflows with "
                             "real human expertise to scale your sales and marketing"}, WEAK_US)
JANE = facts({"name": "Jane Doe", "title": "Head of Security", "location": "Berlin",
              "time_in_role": "2 years 3 months"},
             {"name": "Acme", "industry": "Fintech", "employees": 240, "country": "Germany",
              "description": "Payments for online shops."},
             {"level": "strong", "reasons": ["Germany is a target market", "240 employees is within 20-2,000",
                                             "Head of Security is a buying role"]})
OMAR = facts({"name": "Omar Haddad", "title": "CTO", "location": "Vienna", "time_in_role": "4 years 8 months"},
             {"name": "Globex", "industry": "Logistics software", "employees": 85, "country": "Austria",
              "description": "Route planning for couriers."},
             {"level": "strong", "reasons": ["Austria is a target market", "85 employees is within 20-2,000",
                                             "CTO is a buying role"]},
             verdict="hold", hold_reason="Contacted 13 days ago, on 2026-11-05")

CASES = [
    # ---------- bad: real briefs ----------
    {"name": "reid_startup_and_customers", "facts": REID, "brief": brief(
        "Reid Hoffman is Co-Founder and Executive Board Chair at Manas AI, a US biotech startup that will need "
        "compliance automation as it scales.",
        "Co-Founder, Executive Board Chair in the US, 1 year 10 months in the role.",
        "Manas AI does biotechnology research. 1-10 employees, US.",
        "Mention that early-stage biotechs use Northwall to get ISO 27001 and SOC 2 ready before enterprise deals "
        "arrive."),
     "must_flag": [["startup", "scales", "will need"], ["use Northwall", "early-stage biotechs"]]},
    {"name": "alok_team_faces", "facts": ALOK, "brief": brief(
        "Alok Singh is an AI x GTM Engineer in Pune City, IN, and Northwall can automate the compliance work his "
        "team faces.", "AI x GTM Engineer in Pune City, IN.", "",
        "Ask which compliance frameworks his customers ask about, then show how Northwall automates the evidence "
        "work."),
     "must_flag": [["team faces", "compliance work his"]]},
    {"name": "arc_will_need", "facts": ARC, "brief": brief(
        "Patrick Collison is Cofounder at Arc Institute, a US biotech non-profit that will need research-grade "
        "security certifications.", "Cofounder in San Francisco, US, 5 years 10 months in the role.",
        "Arc Institute does curiosity-driven basic science and technology development. Biotechnology, 51-200 "
        "employees, Palo Alto, California.",
        "Ask which of ISO 27001, SOC 2, GDPR or TISAX their funders or partners require."),
     "must_flag": [["will need", "research-grade"]]},
    {"name": "riya_likely_needs", "facts": RIYA, "brief": brief(
        "Riya Reach is Head of IT at Reach Systems, a German clinic-scheduling software company that likely needs "
        "ISO 27001 or GDPR proof for hospital customers.", "Head of IT in Munich, 3 years 6 months in the role.",
        "Reach Systems makes scheduling software for clinics. Software, 150 employees, Germany.",
        "Ask how Reach Systems prepares for ISO 27001 audits today."),
     "must_flag": [["likely needs", "hospital"]]},
    {"name": "nico_may_need", "facts": NICO, "brief": brief(
        "Nico Druelle is Founder & CEO at The Revenue Architects, a small US tech firm that may need compliance "
        "automation as it sells to larger buyers.", "Founder & CEO in San Francisco, 2 years 10 months in the role.",
        "The Revenue Architects combines AI agents with human experts for sales and marketing. 1-10 employees.",
        "Check fit before reaching out: the company is outside the target markets."),
     "must_flag": [["may need", "larger buyers"]]},
    # ---------- bad: written to cover gaps ----------
    {"name": "jane_funding", "facts": JANE, "brief": brief(
        "Jane Doe is Head of Security at Acme, a German fintech that recently raised new funding.",
        "Head of Security in Berlin, 2 years 3 months in the role.",
        "Acme builds payments for online shops. Fintech, 240 employees, Germany.",
        "Ask which of ISO 27001, SOC 2, GDPR and TISAX her customers ask about."),
     "must_flag": [["raised", "funding"]]},
    {"name": "jane_seller_customers", "facts": JANE, "brief": brief(
        "Jane Doe is Head of Security at Acme, a German fintech with 240 employees.",
        "Head of Security in Berlin, 2 years 3 months in the role.",
        "Acme builds payments for online shops. Fintech, 240 employees, Germany.",
        "Mention that most fintechs her size already automate audits with Northwall."),
     "must_flag": [["already automate", "most fintechs"]]},
    # ---------- good ----------
    {"name": "nico_v4", "facts": NICO, "brief": brief(
        "Nico Druelle is Founder & CEO at The Revenue Architects, a small US sales-tech company outside Northwall's "
        "target markets.", "Founder & CEO in San Francisco, 2 years 10 months in the role.",
        "The Revenue Architects combines AI agents with human experts to scale sales and marketing. Technology, "
        "1-10 employees, Los Angeles US.",
        "Check fit before reaching out: the company has 1-10 employees and is outside target markets. Only pitch "
        "Northwall if a customer is asking them for ISO 27001, SOC 2, GDPR or TISAX."),
     "must_flag": []},
    {"name": "alok_v4", "facts": ALOK, "brief": brief(
        "Alok Singh is an AI x GTM Engineer in Pune, India, with no company data and a role that is not a usual "
        "buyer of compliance tools.", "AI x GTM Engineer in Pune City, IN.", "",
        "Check fit before reaching out: India is outside the target markets, no company is known, and the role is "
        "not a usual buying role. Only contact if Alok says he owns ISO 27001, SOC 2, GDPR or TISAX work."),
     "must_flag": []},
    {"name": "jane_clean", "facts": JANE, "brief": brief(
        "Jane Doe is Head of Security at Acme, a German fintech with 240 employees.",
        "Head of Security in Berlin, 2 years 3 months in the role.",
        "Acme builds payments for online shops. Fintech, 240 employees, Germany.",
        "Ask which of ISO 27001, SOC 2, GDPR and TISAX her customers ask about, then show how Northwall automates "
        "the evidence work."),
     "must_flag": []},
    {"name": "omar_hold_clean", "facts": OMAR, "brief": brief(
        "Omar Haddad is CTO of Globex, an Austrian route-planning software company for couriers.",
        "CTO in Vienna, 4 years 8 months in the role.",
        "Globex builds route planning for couriers. Logistics software, 85 employees, Austria.",
        "Wait for a reply to the earlier contact. When you follow up, ask how Globex handles ISO 27001 or SOC 2 "
        "evidence today.", verdict="hold", hold_reason="Omar was contacted 13 days ago, on 5 November 2026."),
     "must_flag": []},
    {"name": "riya_clean", "facts": RIYA, "brief": brief(
        "Riya Reach is Head of IT at Reach Systems, a German company making scheduling software for clinics.",
        "Head of IT in Munich, 3 years 6 months in the role.",
        "Reach Systems makes scheduling software for clinics. Software, 150 employees, Germany.",
        "Ask how Reach Systems handles ISO 27001 and GDPR evidence today, then show how Northwall automates it."),
     "must_flag": []},
    {"name": "arc_clean", "facts": ARC, "brief": brief(
        "Patrick Collison is Cofounder at Arc Institute, a non-profit research institute in Palo Alto.",
        "Cofounder in San Francisco, US, 5 years 10 months in the role.",
        "Arc Institute does curiosity-driven basic science and technology development. Biotechnology, 51-200 "
        "employees, Palo Alto US.",
        "Check fit before reaching out: Arc Institute is outside the target markets. Only pitch if a partner asks "
        "them for ISO 27001 or SOC 2."),
     "must_flag": []},
]
