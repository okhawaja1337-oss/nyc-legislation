#!/usr/bin/env python3
"""
Configuration, canonical constants, and the District 49 doctrine.

Everything in the Universe that is *stable* lives here: paths, fiscal-year
conventions, the D49 geography, and the Councilmember's standing priorities.
Volatile facts (who holds an office, what a bill's status is, how much an
organization got) are NEVER hardcoded -- they come from the data packs.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- paths ----
PKG_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PKG_ROOT.parent
DATA_DIR = Path(os.environ.get("UNIVERSE_DATA", REPO_ROOT / "data" / "universe"))
PACK_DIR = DATA_DIR / "packs"          # normalized, versioned data packs
CACHE_DIR = DATA_DIR / "cache"         # live-pull cache (TTL'd)
JOURNAL_DIR = DATA_DIR / "journal"     # append-only change journal
OUT_DIR = Path(os.environ.get("UNIVERSE_OUT", REPO_ROOT / "out" / "universe"))
DB_PATH = DATA_DIR / "universe.sqlite"

for _d in (DATA_DIR, PACK_DIR, CACHE_DIR, JOURNAL_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------ identities ----
MEMBER_NAME = "Kamillah Hanks"
MEMBER_LAST = "Hanks"
DISTRICT = 49
BOROUGH = "Staten Island"
DISTRICT_LABEL = "District 49 (North Shore, Staten Island)"

# Staten Island's three council districts. Names are looked up live; the
# district numbers themselves are stable structure.
SI_DISTRICTS = (49, 50, 51)

# North Shore ZIPs that define "in-district" for funding reconciliation.
D49_ZIPS = ("10301", "10302", "10303", "10304", "10310")

# Neighborhoods used for constituent-affairs routing and land-use tagging.
D49_NEIGHBORHOODS = (
    "St. George", "Stapleton", "Tompkinsville", "Clifton", "Park Hill",
    "New Brighton", "West Brighton", "Port Richmond", "Mariners Harbor",
    "Elm Park", "Randall Manor", "Silver Lake", "Grymes Hill", "Rosebank",
    "Arlington", "Graniteville", "Castleton Corners", "Sunnyside",
)

# ------------------------------------------------------- fiscal calendar ----
CURRENT_FY = 2027
FISCAL_YEARS = (2022, 2023, 2024, 2025, 2026, 2027)

# The NYC budget cycle. Stable structure -- drives the "what's next" clock.
BUDGET_CALENDAR = [
    ("Nov", "November Financial Plan Modification", "OMB publishes the first in-year modification."),
    ("Jan", "Preliminary Budget", "Mayor releases the Preliminary Budget; Council prelim hearings follow."),
    ("Mar", "Preliminary Budget Response", "Council adopts its Preliminary Budget Response (the Council's asks)."),
    ("Apr", "Executive Budget", "Mayor releases the Executive Budget; Council executive hearings follow."),
    ("Jun", "Adopted Budget", "Council votes adoption; Schedule C fixes member designations."),
    ("Jul+", "Transparency Resolutions", "In-year designation changes move by Transparency Resolution."),
]

# ------------------------------------------------- the five issue pillars ----
# CM Hanks' standing budget-and-policy priorities. Every brief scores against
# these, so that analysis is anchored to what the office actually works on.
PILLARS = {
    "arts_culture": {
        "label": "Arts & Cultural",
        "keywords": ("cultural", "arts", "museum", "theatre", "theater", "music",
                     "CASA", "heritage", "history", "artist", "performance",
                     "library", "humanities", "film"),
        "initiatives": ("Cultural After-School Adventure (CASA)",
                        "Cultural Immigrant Initiative",
                        "Coalition Theaters of Color",
                        "Su-CASA"),
    },
    "neighborhood_development": {
        "label": "Neighborhood Development",
        "keywords": ("neighborhood", "housing", "land use", "ULURP", "rezoning",
                     "homeowner", "preservation", "streetscape", "BID",
                     "main street", "vacant", "brownfield", "waterfront",
                     "infrastructure", "resiliency", "flood", "sewer"),
        "initiatives": ("Neighborhood Development Grant Initiative",
                        "Community Housing Preservation Strategies",
                        "Homeowner Stabilization Services",
                        "Community Land Trust"),
    },
    "economic_development": {
        "label": "Economic Development",
        "keywords": ("economic", "small business", "workforce", "jobs", "MWBE",
                     "entrepreneur", "chamber", "commerce", "industrial",
                     "incubator", "tourism", "retail", "storefront", "apprentice"),
        "initiatives": ("Chamber on the Go and Small Business Assistance",
                        "Citywide Young Adult Entrepreneurship Program",
                        "Create New Technology Incubators",
                        "Five Borough Chamber Alliance",
                        "MWBE Leadership Associations"),
    },
    "public_safety": {
        "label": "Crisis Management / Public Safety",
        "keywords": ("public safety", "violence", "crisis", "police", "NYPD",
                     "emergency", "victim", "trafficking", "domestic violence",
                     "cure violence", "reentry", "diversion", "hate crime",
                     "fire", "EMS", "preparedness"),
        "initiatives": ("Community Safety and Victim Services Initiative",
                        "Domestic Violence and Empowerment (DoVE)",
                        "Violence Prevention and Intervention",
                        "Hate Crimes Prevention",
                        "Alternatives to Incarceration and Reentry Programs"),
    },
    "health_hospitals": {
        "label": "Health & Hospitals",
        "keywords": ("health", "hospital", "mental health", "opioid", "overdose",
                     "clinic", "H+H", "maternal", "cancer", "HIV", "substance",
                     "medicaid", "behavioral", "nursing", "public health"),
        "initiatives": ("Access Health Initiative", "Healthy Beginnings",
                        "Opioid Prevention and Treatment",
                        "HIV/AIDS Pathways to Care",
                        "Older Adults Mental Health",
                        "Mental Health Services for Vulnerable Populations"),
    },
    "si_parity": {
        "label": "Staten Island Parity & Independence",
        "keywords": ("Staten Island", "borough", "ferry", "Richmond", "parity",
                     "North Shore", "MTA", "express bus", "borough president",
                     "boroughwide", "commute", "toll", "Verrazzano"),
        "initiatives": ("Boroughwide Needs Initiative",
                        "Older Adults Across the Boroughs"),
    },
}

# ------------------------------------------------------- land-use doctrine ----
# The CM's land-use test. Applied to every ULURP / development brief.
LAND_USE_DOCTRINE = {
    "ownership_over_rental": (
        "Prefer homeownership and equity-building tenure over pure rental. "
        "Ask: how many units convey ownership, and on what terms?"
    ),
    "affordability_floor": (
        "Affordability should reach at or below 80% AMI, with meaningful depth "
        "near the poverty line -- not an 80%-AMI-average that skews high."
    ),
    "working_class_youth": (
        "Housing and jobs must serve working-class young households who can "
        "plausibly move toward the middle and grow income over time."
    ),
    "aging_in_place": (
        "The aging population must be able to stay: accessible units, senior "
        "services, and healthcare within reach."
    ),
    "population_replacement": (
        "Development must replace the population that leaves, ages out, or "
        "cannot contribute -- net new contributing households, not churn."
    ),
    "smart_growth": (
        "Density where transit and sewer capacity exist; infrastructure "
        "commitments must land before or with the units, not after."
    ),
    "bipartisan_transition": (
        "The North Shore transition should be legible and acceptable to "
        "Republican colleagues on the Island; avoid framing that reads as "
        "imposition rather than shared growth."
    ),
    "anti_stereotype": (
        "Reject analysis that treats Staten Island's racialized history as a "
        "ceiling on its economic or political potential."
    ),
}

AMI_TARGET_CEILING = 0.80  # 80% AMI
OWNERSHIP_PREFERENCE = True

# ------------------------------------------------------- house style ----
# CM Hanks' briefing format. Every deliverable renders to this shape.
HOUSE_STYLE = {
    "name": "Bulletpoints for Bureaucrats",
    "order": ["bottom_line", "details", "d49_impact", "questions", "recommendation"],
    "bottom_line_sentences": (2, 4),
    "detail_bullets": (4, 8),
    "questions": 3,
    "rules": (
        "Lead with a short paragraph that states the answer, not the background.",
        "Then bullets -- each one fact or one decision, never a paragraph.",
        "Always carry a Staten Island / District 49 impact read.",
        "Exactly three questions, each one the CM could ask out loud in a hearing.",
        "Every number carries a source. Unknown figures are tagged [verify: source].",
        "No invented quotes, no invented statistics, ever.",
    ),
}

# ---------------------------------------------------- provenance tiers ----
# How much weight a fact carries. Used by the citation registry.
# How much weight a source carries, lowest number first. The academic tiers
# are kept apart on purpose. A peer-reviewed article has been through
# referees; an NBER or IZA working paper has not, and working papers are the
# workhorse of policy economics -- most of what gets cited in a budget hearing
# as "the research shows" is a working paper. Collapsing the two would let the
# office cite an unrefereed result with the authority of a refereed one, which
# is precisely the error that gets a Councilmember corrected in public.
PROVENANCE = {
    "OFFICIAL_PRIMARY": 1,     # the City's own book, the statute, the roll call
    "PEER_REVIEWED": 2,        # refereed journal article
    "OFFICIAL_DERIVED": 2,     # a faithful mirror or parse of a primary source
    "INSTITUTIONAL": 3,        # IBO, Comptroller, a think tank, a university centre
    "WORKING_PAPER": 4,        # NBER, IZA, SSRN -- not refereed, say so when citing
    "MARKET": 4,               # rating agencies, EMMA disclosure, bank research
    "PRESS": 5,
    "INTERNAL": 6,
    "DELIBERATIVE": 7,
}

# Tiers that must carry a caveat when a brief leans on them.
NEEDS_CAVEAT = {
    "WORKING_PAPER": "not peer reviewed",
    "PRESS": "press reporting, not a primary source",
    "DELIBERATIVE": "deliberative — the office's own reasoning, not evidence",
    "MARKET": "market or rating-agency research, which has a seller",
}

# ------------------------------------------------------------ live feeds ----
LEGISTAR_BASE = "https://webapi.legistar.com/v1/nyc"
SOCRATA_BASE = "https://data.cityofnewyork.us/resource"
GEOSEARCH_BASE = "https://geosearch.planninglabs.nyc/v2"
GPP_BASE = "https://a860-gpp.nyc.gov"

# Socrata datasets the refresher pulls. id -> (label, provenance)
OPEN_DATASETS = {
    "erm2-nwe9": ("311 Service Requests", "OFFICIAL_DERIVED"),
    "ujre-m2tj": ("Discretionary Award Tracker", "OFFICIAL_DERIVED"),
    "mwzb-yiwb": ("Expense Budget", "OFFICIAL_DERIVED"),
    "39g5-gbp3": ("Adopted Budget (all funds)", "OFFICIAL_DERIVED"),
    "ugzk-a6x4": ("Revenue Budget", "OFFICIAL_DERIVED"),
    "2cmn-uidm": ("Capital Commitments", "OFFICIAL_DERIVED"),
    "qyyg-4tf5": ("Contract Awards", "OFFICIAL_DERIVED"),
    "k397-673e": ("Citywide Payroll", "OFFICIAL_DERIVED"),
    "872g-cjhh": ("Council District Boundaries", "OFFICIAL_DERIVED"),
    "rjkp-yttg": ("Campaign Finance Contributions", "OFFICIAL_DERIVED"),
    "ye4r-qpmp": ("ACS Demographics by District", "OFFICIAL_DERIVED"),
}

# Credentials resolve through core.keys: environment first, then a config file
# under the user's home directory. Nothing here reads a secret out of the repo,
# and these module attributes stay dynamic so a key set after import is seen.
from . import keys as _keys


def anthropic_key(index: int = 0) -> str | None:
    return _keys.get("anthropic_api_key", index=index)


def anthropic_keys() -> list[str]:
    return _keys.get_all("anthropic_api_key")


SOCRATA_APP_TOKEN = (_keys.get("socrata_app_token")
                     or os.environ.get("NYC_APP_TOKEN"))
ANTHROPIC_API_KEY = _keys.get("anthropic_api_key")
LLM_COUNCIL_URL = _keys.setting("llm_council_url", "http://localhost:8001")

D49_CALENDAR_ID = _keys.setting(
    "d49_calendar_id", "g32f5p7pd3lfm8bie06acv5s0o@group.calendar.google.com"
)

# Staff initials seen on the D49 calendar, for ownership routing.
STAFF_INITIALS = {
    "KH": "Councilmember Kamillah Hanks",
    "OK": "Omar Khawaja (Legislative & Budget Director)",
    "AS": "Scheduler / Community",
    "MB": "Community Liaison",
    "TP": "Community Liaison",
    "EB": "Education Liaison",
    "OO": "Operations",
}
