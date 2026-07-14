#!/usr/bin/env python3
"""
people.py — the "every level of government that affects NYC" reference layer.

Design choice that matters: this file hardcodes STRUCTURE (which offices exist,
their term lengths, and the fixed rules for when they're on the ballot) but NOT
who currently holds them. Officeholders change; a PR tool that ships a stale
name is worse than one that links you to the live source. So current names are
filled from live data (Legistar for the Council, congress-legislators for the
federal delegation, the NY Senate API for Albany), and everything here stays
true across elections.

Pure functions only — no network, no Streamlit — so it's trivially testable.
"""

# ---------------------------------------------------------------------------
# Offices that govern NYC, by level. Election-year rules are deterministic.
# ---------------------------------------------------------------------------
# election_rule: (kind, base_year) where kind picks the cadence:
#   "muni4"  -> NYC municipal 4-yr cycle: base 2025, then every 4 (2029, 2033…)
#   "gov4"   -> NY statewide 4-yr cycle:  base 2026, then every 4 (2030, 2034…)
#   "even2"  -> every even year (state legislature, U.S. House)
#   "sen6:C" -> U.S. Senate class C (NY sr. seat): 2028, +6
#   "sen6:A" -> U.S. Senate class A (NY jr. seat): 2030, +6
OFFICES = [
    # NYC citywide
    {"office": "Mayor", "level": "NYC", "branch": "Executive", "term": 4,
     "rule": ("muni4", 2025), "source": "https://www.nyc.gov/office-of-the-mayor"},
    {"office": "Public Advocate", "level": "NYC", "branch": "Executive", "term": 4,
     "rule": ("muni4", 2025), "source": "https://pubadvocate.nyc.gov"},
    {"office": "Comptroller", "level": "NYC", "branch": "Executive", "term": 4,
     "rule": ("muni4", 2025), "source": "https://comptroller.nyc.gov"},
    {"office": "Borough President (×5)", "level": "NYC", "branch": "Executive", "term": 4,
     "rule": ("muni4", 2025), "source": "https://www.nyc.gov"},
    {"office": "District Attorney (×5)", "level": "NYC", "branch": "Justice", "term": 4,
     "rule": ("muni4", 2025), "source": "https://www.nyc.gov",
     "note": "DA cycles are staggered by county; confirm each borough's year."},
    {"office": "City Council (51 districts)", "level": "NYC", "branch": "Legislative", "term": 4,
     "rule": ("muni4", 2025), "source": "https://council.nyc.gov",
     "note": "Two-year terms are used in the election after a redistricting decade."},
    # NY State
    {"office": "Governor", "level": "NY State", "branch": "Executive", "term": 4,
     "rule": ("gov4", 2026), "source": "https://www.governor.ny.gov"},
    {"office": "Lieutenant Governor", "level": "NY State", "branch": "Executive", "term": 4,
     "rule": ("gov4", 2026), "source": "https://www.ny.gov"},
    {"office": "Attorney General", "level": "NY State", "branch": "Justice", "term": 4,
     "rule": ("gov4", 2026), "source": "https://ag.ny.gov"},
    {"office": "State Comptroller", "level": "NY State", "branch": "Executive", "term": 4,
     "rule": ("gov4", 2026), "source": "https://www.osc.ny.gov"},
    {"office": "State Senate (NYC seats)", "level": "NY State", "branch": "Legislative", "term": 2,
     "rule": ("even2", 0), "source": "https://www.nysenate.gov"},
    {"office": "State Assembly (NYC seats)", "level": "NY State", "branch": "Legislative", "term": 2,
     "rule": ("even2", 0), "source": "https://nyassembly.gov"},
    # Federal
    {"office": "U.S. House (NYC delegation)", "level": "Federal", "branch": "Legislative", "term": 2,
     "rule": ("even2", 0), "source": "https://www.house.gov"},
    {"office": "U.S. Senator (senior, NY)", "level": "Federal", "branch": "Legislative", "term": 6,
     "rule": ("sen6:C", 2028), "source": "https://www.senate.gov"},
    {"office": "U.S. Senator (junior, NY)", "level": "Federal", "branch": "Legislative", "term": 6,
     "rule": ("sen6:A", 2030), "source": "https://www.senate.gov"},
]

LEVELS = ["NYC", "NY State", "Federal"]
LEVEL_COLOR = {"NYC": "#3b82f6", "NY State": "#8b5cf6", "Federal": "#ef4444"}


def next_election_year(rule, from_year):
    """Next election year at or after `from_year` for a given office rule."""
    kind, base = rule
    y = int(from_year)
    if kind in ("muni4", "gov4"):
        step = 4
        while base < y:
            base += step
        return base
    if kind == "even2":
        return y if y % 2 == 0 else y + 1
    if kind.startswith("sen6"):
        step = 6
        while base < y:
            base += step
        return base
    return y


def election_calendar(from_year, years=8):
    """Rows of {year, level, office, in_N_years} for elections in a window."""
    from_year = int(from_year)
    rows = []
    for off in OFFICES:
        ney = next_election_year(off["rule"], from_year)
        # include repeat occurrences within the window
        y = ney
        step = {"muni4": 4, "gov4": 4, "even2": 2, "sen6": 6}[off["rule"][0].split(":")[0]]
        while y <= from_year + years:
            rows.append({
                "Year": y, "Level": off["level"], "Office": off["office"],
                "Branch": off["branch"], "In": y - from_year,
                "Term (yrs)": off["term"],
            })
            y += step
    rows.sort(key=lambda r: (r["Year"], LEVELS.index(r["Level"]) if r["Level"] in LEVELS else 9))
    return rows


def offices_up_this_year(year):
    """Which offices are on the ballot in `year` exactly."""
    year = int(year)
    return [o for o in OFFICES if next_election_year(o["rule"], year) == year]


# ---------------------------------------------------------------------------
# Unified member-profile shaping. The three sources have different shapes; the
# UI wants one card format. These merge them without any network calls.
# ---------------------------------------------------------------------------
UNIFIED_FIELDS = ["name", "level", "chamber", "seat", "district", "party",
                  "contact", "term_end", "source", "extra"]


def council_profile(name, district=None):
    """A minimal unified profile for a NYC Council member (name from Legistar)."""
    return {
        "name": name, "level": "NYC", "chamber": "City Council",
        "seat": f"District {district}" if district else "Council Member",
        "district": district, "party": "", "contact": "",
        "term_end": "", "source": "https://council.nyc.gov", "extra": {},
    }


def state_profile(m):
    """Unify a sources.nystate member dict."""
    return {
        "name": m.get("name", ""), "level": "NY State", "chamber": m.get("chamber", ""),
        "seat": f"District {m.get('district')}" if m.get("district") else "",
        "district": m.get("district"), "party": _party(m.get("party", "")),
        "contact": "", "term_end": "",
        "source": "https://www.nysenate.gov", "extra": {"short": m.get("short", "")},
    }


def federal_profile(p):
    """Unify a sources.congress delegation profile dict."""
    contact = " · ".join(x for x in [p.get("phone"), p.get("office")] if x)
    return {
        "name": p.get("name", ""), "level": "Federal", "chamber": p.get("chamber", ""),
        "seat": p.get("seat", ""), "district": p.get("district"),
        "party": _party(p.get("party", "")), "contact": contact,
        "term_end": p.get("term_end", ""), "source": p.get("url", ""),
        "extra": {"since": p.get("since", ""), "twitter": p.get("twitter", ""),
                  "bioguide": p.get("bioguide", "")},
    }


def _party(p):
    return {"Democrat": "Democratic", "D": "Democratic", "Republican": "Republican",
            "R": "Republican", "Independent": "Independent", "I": "Independent"}.get(p, p)


def match_reps(districts, federal_delegation=None, nys_members=None):
    """Map resolved district numbers -> the official who holds each seat.

    `districts` is {council, state_senate, state_assembly, congress} of ints/None.
    Federal is resolved from the delegation dataset (no key); state is resolved
    if a members list is provided; council links out to the official page.
    Returns an ordered list of row dicts for display.
    """
    districts = districts or {}
    fed = federal_delegation or []
    nys = nys_members or []
    rows = []

    cd = districts.get("council")
    rows.append({
        "level": "NYC", "seat": "City Council",
        "district": cd, "member": None,
        "link": f"https://council.nyc.gov/district-{cd}/" if cd else "https://council.nyc.gov",
    })

    ss = districts.get("state_senate")
    ss_member = next((m["name"] for m in nys
                      if str(m.get("district")) == str(ss) and "Senate" in m.get("chamber", "")), None)
    rows.append({
        "level": "NY State", "seat": "State Senate",
        "district": ss, "member": ss_member,
        "link": "https://www.nysenate.gov/find-my-senator",
    })

    sa = districts.get("state_assembly")
    sa_member = next((m["name"] for m in nys
                      if str(m.get("district")) == str(sa) and "Assembly" in m.get("chamber", "")), None)
    rows.append({
        "level": "NY State", "seat": "State Assembly",
        "district": sa, "member": sa_member,
        "link": "https://nyassembly.gov/mem/search/",
    })

    cong = districts.get("congress")
    cong_member = next((d["name"] for d in fed
                        if str(d.get("district")) == str(cong) and "House" in d.get("chamber", "")), None)
    cong_link = next((d.get("source") for d in fed
                      if str(d.get("district")) == str(cong)), "https://www.house.gov")
    rows.append({
        "level": "Federal", "seat": "U.S. House",
        "district": cong, "member": cong_member,
        "link": cong_link or "https://www.house.gov",
    })

    # Both NY senators cover every NYC address.
    for d in fed:
        if d.get("chamber") == "U.S. Senate":
            rows.append({"level": "Federal", "seat": "U.S. Senate", "district": "statewide",
                         "member": d["name"], "link": d.get("source") or "https://www.senate.gov"})
    return rows


def branch_summary():
    """Counts of offices by level and branch — for the Command Center tiles."""
    out = {}
    for o in OFFICES:
        out.setdefault(o["level"], {}).setdefault(o["branch"], 0)
        out[o["level"]][o["branch"]] += 1
    return out
