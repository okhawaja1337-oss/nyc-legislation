#!/usr/bin/env python3
"""
citydata.py — the City-Hall-first layer: who holds which citywide office, and
rich per-district context (demographics, languages, neighborhoods).

Same principle as elsewhere: we hardcode the STABLE structure (which offices
exist, their remit, and the official links) but never the volatile current
occupant — those come from live sources. District demographics are produced as a
SOURCED, web-grounded snapshot (via the web-search LLM) and clearly marked to
verify, rather than baked into a table that goes stale after every ACS release.
"""

# ---------------------------------------------------------------------------
# Citywide + boroughwide elected offices (structure + official links only)
# ---------------------------------------------------------------------------
CITYWIDE_OFFICIALS = [
    {"office": "Mayor", "scope": "Citywide", "branch": "Executive",
     "remit": "Runs city government and its agencies; proposes the budget; appoints commissioners.",
     "link": "https://www.nyc.gov/office-of-the-mayor/"},
    {"office": "Public Advocate", "scope": "Citywide", "branch": "Executive",
     "remit": "Watchdog for residents; first in line of succession to the Mayor; a non-voting Council member.",
     "link": "https://pubadvocate.nyc.gov/"},
    {"office": "Comptroller", "scope": "Citywide", "branch": "Executive",
     "remit": "Chief fiscal officer; audits agencies, manages pension funds, registers contracts.",
     "link": "https://comptroller.nyc.gov/"},
    {"office": "Speaker of the Council", "scope": "Citywide", "branch": "Legislative",
     "remit": "Leads the City Council, sets the legislative agenda, and negotiates the budget with the Mayor.",
     "link": "https://council.nyc.gov/"},
]

BOROUGHS = ["Manhattan", "Bronx", "Brooklyn", "Queens", "Staten Island"]

BOROUGH_OFFICES = [
    {"office": "Borough President", "branch": "Executive",
     "remit": "Advocates for the borough; advises on land use (ULURP) and budget; appoints community board members.",
     "link": "https://www.nyc.gov/"},
    {"office": "District Attorney", "branch": "Justice",
     "remit": "Chief local prosecutor for the county; charges and tries criminal cases.",
     "link": "https://www.nyc.gov/"},
]

# District attorney office sites (one per county; stable URLs).
DA_SITES = {
    "Manhattan": "https://www.manhattanda.org/",
    "Bronx": "https://bronxda.nyc.gov/",
    "Brooklyn": "https://www.brooklynda.org/",
    "Queens": "https://queensda.org/",
    "Staten Island": "https://www.rcda.nyc/",
}
BP_SITES = {
    "Manhattan": "https://www.manhattanbp.nyc.gov/",
    "Bronx": "https://bronxboropres.nyc.gov/",
    "Brooklyn": "https://www.brooklynbp.nyc.gov/",
    "Queens": "https://queensbp.org/",
    "Staten Island": "https://www.statenislandusa.com/",
}


def district_links(n):
    """Authoritative links for a Council district."""
    return {
        "Council member page": f"https://council.nyc.gov/district-{int(n)}/",
        "Find your district (map)": "https://council.nyc.gov/districts/",
        "NYC Population FactFinder": "https://popfactfinder.planning.nyc.gov/",
        "Census QuickFacts (NYC)": "https://www.census.gov/quickfacts/newyorkcitynewyork",
    }


def citywide_rows():
    """Flat list for the City Officials directory (offices + links; names live)."""
    rows = list(CITYWIDE_OFFICIALS)
    for b in BOROUGHS:
        rows.append({"office": f"{b} Borough President", "scope": b, "branch": "Executive",
                     "remit": BOROUGH_OFFICES[0]["remit"], "link": BP_SITES.get(b, "https://www.nyc.gov/")})
    for b in BOROUGHS:
        rows.append({"office": f"{b} District Attorney", "scope": b, "branch": "Justice",
                     "remit": BOROUGH_OFFICES[1]["remit"], "link": DA_SITES.get(b, "https://www.nyc.gov/")})
    return rows


# ---------------------------------------------------------------------------
# Per-district demographic/language snapshot — web-grounded, not hardcoded.
# ---------------------------------------------------------------------------
DISTRICT_PROFILE_PROMPT = """Produce a concise demographic & community profile of **New
York City Council District {district}**. Use web search for current, sourced
figures — prefer NYC Planning (Population FactFinder / district profiles), the
U.S. Census / American Community Survey, and the City Council's own district
page. Do NOT invent numbers; cite the source and year for each figure, and if a
figure isn't available say so.

Output Markdown with these short sections:
**Snapshot** — borough(s), approximate population, and the neighborhoods the district covers.
**Who lives here** — race/ethnicity mix and notable communities (with source + year).
**Languages** — the most spoken languages and any significant limited-English-proficiency share (with source).
**Economics** — median household income and any notable housing/poverty context (with source + year).
**What to know** — 2–3 bullets a Council staffer should keep in mind about this district's makeup.

End with: "_Figures are from public sources as noted and may lag; verify against the linked profiles._"
"""


def district_profile(llm, district):
    """Web-grounded demographic snapshot for a district; '' if no LLM key."""
    if not (llm and llm.ready):
        return ""
    try:
        return llm.complete(DISTRICT_PROFILE_PROMPT.format(district=int(district)),
                            max_tokens=1200, allow_web=True)
    except Exception as e:
        return f"_(couldn't build the profile: {e})_"
