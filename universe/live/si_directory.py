#!/usr/bin/env python3
"""
The Staten Island contact directory.

Constituent services is a routing problem. A staffer with a name and a direct
line resolves a case in one call; a staffer with a main switchboard number
spends a morning and loses the constituent. This module is the office's routing
table for the whole island, across every level of government plus the civic and
institutional partners the office actually works with.

**One rule governs this file: nothing here is guessed.** Every phone number,
address and named officeholder below was read off a published page, and every
record carries the URL it came from and the date it was confirmed. A fabricated
extension is worse than a blank one -- a blank prompts a lookup, a wrong number
sends a constituent to the wrong agency and the office never finds out. Where a
source gave a name but no reachable contact, the contact field is empty and the
confidence says so. Where a published page truncated a value, it is recorded as
truncated rather than completed from guesswork.

Confidence levels, and what each licenses:

  ``published``   read off the office's own or the City's own page. Dial it.
  ``listed``      from a reputable directory, not the body's own site. Confirm
                  the person before using their name in writing.
  ``unverified``  a name or role with no contact confirmed. Look it up first.

The authoritative source for City agency contacts is the Green Book
(a856-gbol.nyc.gov). Where a Green Book record id is known it is stored, so a
staffer can click straight through to the current entry rather than trusting
this snapshot -- and ``universe connect`` will refresh from it directly once
that host is reachable.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Iterable

from ..core.store import Store

CHECKED = "2026-09-13"
GREENBOOK = "https://a856-gbol.nyc.gov/GBOLWebsite/GreenBook/Details?orgId={id}"

# Sources consulted, so a record's provenance is one lookup away.
SOURCES = {
    "SICHAMBER": "https://www.sichamber.com/elected-officials",
    "CB1_ELECTED": "https://www.nyc.gov/site/statenislandcb1/resources/elected-officials.page",
    "CAU_BOARDS": "https://home.nyc.gov/site/cau/community-boards/staten-island-boards.page",
    "BP_NUMBERS": "https://www.statenislandusa.com/important-numbers.html",
    "BP_LOCAL": "https://www.statenislandusa.com/localgovernment.html",
    "RCDA": "https://www.statenislandda.org/",
    "DOT_SI": "https://www.nyc.gov/html/dot/html/contact/QA_contact-form.shtml?routing=si",
    "D31": "https://www.district31nyc.org/superintendent.html",
    "CEC31": "http://www.cec31.org/officials.html",
    "PROJHOSP": "https://projecthospitality.org/executive-staff/",
    "SIARTS": "https://statenislandarts.org/about-us/",
    "RUMC": "https://rumcsi.org/about/executive-leadership/",
    "SIUH": "https://siuh.northwell.edu/about",
    "SIEDC": "https://www.siedc.org/staff",
    "COUNCIL": "https://council.nyc.gov/",
    "HPD_BSC": "https://www.nyc.gov/site/hpd/contact/borough-service-centers.page",
    "HRA_LOC": "https://www.nyc.gov/site/hra/locations/job-locations-and-service-centers.page",
    "DFTA_EJ": "https://www.nyc.gov/assets/dfta/downloads/pdf/services/EJ-Providers-Intake-numbers-and-addresses-FY2024gr.pdf",
    "DOB_CONTACT": "https://www.nyc.gov/site/buildings/dob/contact-us.page",
    "DOB_OFFICES": "https://www.nyc.gov/site/buildings/dob/office-locations.page",
    "SBS": "https://www.nyc.gov/site/sbs/index.page",
    "DEP": "https://www.nyc.gov/site/dep/about/contact-us.page",
}

# level, agency, office, title, person, phone, email, address, zip, district,
# service_area, url, source, confidence, note
Row = dict[str, Any]


def _r(level: str, agency: str, office: str, **kw) -> Row:
    return {"level": level, "agency": agency, "office": office, **kw}


# ------------------------------------------------------------- federal ----
FEDERAL: tuple[Row, ...] = (
    _r("federal", "U.S. House", "NY-11 district office",
       title="Member of Congress", person="Nicole Malliotakis",
       phone="(718) 568-2870", address="1698 Victory Boulevard, Staten Island, NY",
       zip="10314", district="NY-11", confidence="published", source="SICHAMBER",
       service_area="federal casework: immigration, VA, SSA, IRS, passports, "
                    "federal grants"),
    _r("federal", "U.S. House", "NY-11 Washington office",
       title="Member of Congress", person="Nicole Malliotakis",
       phone="(202) 225-3371", address="417 Cannon House Office Building, Washington, DC",
       zip="20515", district="NY-11", confidence="published", source="SICHAMBER",
       service_area="federal legislation, appropriations requests"),
)

# ---------------------------------------------------------------- state ----
STATE: tuple[Row, ...] = (
    _r("state", "NY Senate", "Senate District 23 district office",
       title="State Senator", person="Jessica Scarcella-Spanton",
       phone="(718) 727-9406",
       address="36 Richmond Terrace, Room 112, Staten Island, NY", zip="10301",
       district="SD-23", confidence="published", source="SICHAMBER",
       note="Fax (718) 727-9426. North Shore overlap with District 49 — the "
            "closest state partner for D49 work.",
       service_area="state budget asks, SUNY/CUNY, state agencies, "
                    "North Shore capital"),
    _r("state", "NY Senate", "Senate District 24 district office",
       title="State Senator", person="Andrew Lanza",
       phone="(718) 984-4073",
       address="3845 Richmond Avenue, Suite 2A, Staten Island, NY", zip="10312",
       district="SD-24", confidence="published", source="SICHAMBER"),
    _r("state", "NY Assembly", "Assembly District 61 district office",
       title="Assembly Member", person="Charles Fall",
       phone="(718) 442-9932",
       address="853 Forest Avenue, Staten Island, NY", zip="10310",
       district="AD-61", confidence="published", source="SICHAMBER",
       note="AD-61 covers the North Shore — the Assembly counterpart to D49.",
       service_area="state legislation and member items on the North Shore"),
    _r("state", "NY Assembly", "Assembly District 62 district office",
       title="Assembly Member", person="Michael Reilly",
       phone="(718) 967-5194",
       address="7001 Amboy Road, Suite 202 E, Staten Island, NY", zip="10307",
       district="AD-62", confidence="published", source="CEC31"),
    _r("state", "NY Assembly", "Assembly District 63 district office",
       title="Assembly Member", person="Sam Pirozzolo",
       phone="(718) 370-1384",
       address="2090 Victory Blvd., Staten Island, NY", zip="10314",
       district="AD-63", confidence="published", source="SICHAMBER"),
    _r("state", "NY Assembly", "Assembly District 64 district office",
       title="Assembly Member", person="Michael Tannousis",
       phone="(718) 987-0197",
       address="11 Maplewood Place, Staten Island, NY", zip="10306",
       district="AD-64", confidence="published", source="SICHAMBER"),
)

# ----------------------------------------------------------------- city ----
CITY_ELECTED: tuple[Row, ...] = (
    _r("city", "Borough President", "Staten Island Borough Hall",
       title="Borough President", person="Vito J. Fossella",
       phone="(718) 816-2000",
       address="10 Richmond Terrace, Staten Island, NY", zip="10301",
       confidence="published", source="SICHAMBER",
       service_area="borough capital allocations, ULURP recommendations, "
                    "community board appointments, borough board"),
    _r("city", "City Council", "District 49 district office",
       title="Council Member, Majority Whip", person="Kamillah M. Hanks",
       phone="(718) 556-7370",
       address="130 Stuyvesant Place, 6th Floor, Staten Island, NY", zip="10301",
       district="49", confidence="published", source="CB1_ELECTED",
       note="This office. Chair, Committee on Public Safety. Majority Whip "
            "since 15 January 2026 — the first from Staten Island.",
       service_area="North Shore constituent services, discretionary funding, "
                    "land use, public safety oversight"),
    _r("city", "City Council", "District 50 district office",
       title="Council Member, Minority Leader", person="David M. Carr",
       phone="(718) 980-1017",
       address="900 South Avenue, Suite 403, Staten Island, NY", zip="10314",
       district="50", confidence="published", source="CB1_ELECTED",
       note="Minority Leader since 7 January 2026. With D49 holding the "
            "Majority Whip, Staten Island holds both floor leadership seats.",
       service_area="Mid-Island; bipartisan borough asks"),
    _r("city", "City Council", "District 51 district office",
       title="Council Member", person="Frank Morano",
       phone="(718) 984-5151",
       address="2955 Veterans Road West, Suite 2, Staten Island, NY", zip="10309",
       district="51", confidence="published", source="CB1_ELECTED",
       service_area="South Shore; third seat of the borough delegation"),
    _r("city", "District Attorney", "Richmond County District Attorney",
       title="District Attorney", person="Michael E. McMahon",
       phone="(718) 556-7050", email="mcmahonda@rcda.nyc.gov",
       address="130 Stuyvesant Place, 9th Floor, Room 919, Staten Island, NY",
       zip="10301", confidence="published", source="RCDA",
       note="Same building as the D49 district office.",
       service_area="prosecution policy, diversion programmes, "
                    "public safety partnerships"),
)

CITY_AGENCIES: tuple[Row, ...] = (
    _r("city", "DOT", "Staten Island Borough Commissioner",
       title="Borough Commissioner", person="Roseann Caruana",
       address="Staten Island", confidence="published", source="DOT_SI",
       url="https://www.nyc.gov/html/dot/html/contact/QA_contact-form.shtml?routing=si",
       note="DOT routes borough correspondence through the form at the URL "
            "above; no direct line is published. Use the form or the Green Book.",
       service_area="potholes, resurfacing, traffic signals, bus stops, "
                    "sidewalks, street lights, parking regulations"),
    _r("city", "NYPD", "Patrol Borough Staten Island",
       title="Borough Commander", person="Chief Christopher Hancock",
       phone="(718) 571-6429",
       address="970 Richmond Avenue, Staten Island, NY", zip="10314",
       confidence="listed", source="BP_NUMBERS", greenbook=4100,
       note="Confirm the commander before naming them in writing — borough "
            "commands rotate.",
       service_area="precinct concerns, quality of life, traffic safety, "
                    "community affairs"),
    _r("city", "NYPD", "120th Precinct (North Shore)", title="Commanding Officer",
       address="78 Richmond Terrace, Staten Island, NY", zip="10301",
       confidence="published", source="BP_NUMBERS",
       url="https://www.nyc.gov/site/nypd/bureaus/patrol/precincts/120th-precinct.page",
       note="The D49 precinct. Confirm the CO and the Community Affairs "
            "officer by name each term.",
       service_area="North Shore policing, community council, NCO officers"),
    _r("city", "NYPD", "122nd Precinct", title="Commanding Officer",
       confidence="published", source="BP_NUMBERS",
       url="https://www.nyc.gov/site/nypd/bureaus/patrol/precincts/122nd-precinct.page"),
    _r("city", "NYPD", "123rd Precinct", title="Commanding Officer",
       confidence="published", source="BP_NUMBERS",
       url="https://www.nyc.gov/site/nypd/bureaus/patrol/precincts/123rd-precinct.page"),
    _r("city", "DPR", "NYC Parks — Staten Island Borough Office",
       title="Borough Commissioner", phone="(718) 390-2080",
       address="1150 Clove Road, Staten Island, NY", zip="10301",
       confidence="listed", source="BP_NUMBERS",
       note="Commissioner name not confirmed from a Parks page — look up "
            "before using.",
       service_area="park maintenance, street trees, playgrounds, "
                    "tree pruning, greenstreets"),
    _r("city", "DSNY", "Staten Island Borough Office",
       title="Borough Chief", address="2500 Richmond Avenue, Staten Island, NY",
       confidence="listed", source="BP_NUMBERS", greenbook=3107,
       note="Address listed; no borough phone confirmed. Use the Green Book "
            "record or 311 escalation.",
       service_area="missed collection, illegal dumping, street cleaning, "
                    "containerisation, snow"),
    _r("city", "FDNY", "Staten Island — Division 8 / borough offices",
       phone="(718) 981-3031",
       address="1688 Victory Boulevard, Staten Island, NY", zip="10314",
       confidence="listed", source="BP_NUMBERS", greenbook=2903,
       note="Listed line; confirm the division and the chief before writing.",
       service_area="fire safety inspections, EMS response, hydrants"),
    _r("city", "DOE", "Community School District 31",
       title="Superintendent", person="Roderick Palton", phone="(718) 420-5657",
       address="715 Ocean Terrace, Building A, Staten Island, NY", zip="10301",
       confidence="listed", source="D31",
       note="District 31 is the whole borough — one district, unusually.",
       service_area="school placement, IEP and special education, busing, "
                    "school capital"),
    _r("city", "DOE", "Community Education Council 31", title="CEC 31",
       url="http://www.cec31.org/contact.html", confidence="published",
       source="CEC31",
       service_area="parent body on school policy; a standing venue for "
                    "education asks"),
    _r("city", "311 / Mayor's Office", "Staten Island Customer Service Borough Office",
       confidence="listed", source="BP_NUMBERS", greenbook=5801,
       note="Green Book record; the escalation path when 311 stalls.",
       service_area="311 escalation, service request follow-up"),
)

# ------------------------------------------------------- community boards ----
COMMUNITY_BOARDS: tuple[Row, ...] = (
    _r("community_board", "Community Board 1", "Staten Island CB1 (North Shore)",
       title="District Manager", person="Joan Cusack", phone="(718) 981-6900",
       address="1 Edgewater Plaza, Suite 217, Staten Island, NY", zip="10305",
       confidence="published", source="CAU_BOARDS",
       url="https://www.nyc.gov/site/statenislandcb1/contact/contact.page",
       note="CB1 is the District 49 board. Every ULURP action on the North "
            "Shore passes through it first.",
       service_area="ULURP, liquor licences, capital and expense budget "
                    "priorities, land use"),
    _r("community_board", "Community Board 2", "Staten Island CB2 (Mid-Island)",
       title="District Manager", person="Debra Derrico", phone="(718) 568-3581",
       address="900 South Avenue, Suite 28, Staten Island, NY", zip="10314",
       confidence="published", source="CAU_BOARDS"),
    _r("community_board", "Community Board 3", "Staten Island CB3 (South Shore)",
       title="District Manager", person="Charlene Wagner", phone="(718) 356-7900",
       address="1243 Woodrow Road, 2nd Floor, Staten Island, NY", zip="10309",
       confidence="published", source="CAU_BOARDS"),
)

# ------------------------------------------------- nonprofits and anchors ----
NONPROFIT: tuple[Row, ...] = (
    _r("nonprofit", "Nonprofit Staten Island", "borough nonprofit network",
       title="Executive Director", person="Tatiana Arguello",
       confidence="unverified", source="SIEDC",
       url="https://www.linkedin.com/company/nonprofit-staten-island",
       note="HIGHEST-LEVERAGE SINGLE CONTACT. One call reaches the borough's "
            "nonprofit sector — the fastest route for Capacity Building "
            "certification outreach to the sub-$25,000 discretionary cohort. "
            "Contact details not confirmed from an organisational page.",
       service_area="nonprofit capacity, sector convening, discretionary "
                    "funding compliance"),
    _r("nonprofit", "Project Hospitality", "homelessness, food, health services",
       title="President and CEO", person="Rev. Dr. Terry Troia",
       email="tetroia@projecthospitality.org",
       confidence="published", source="PROJHOSP",
       url="https://projecthospitality.org/executive-staff/",
       note="A 2026 CEO search notice is circulating — confirm current "
            "leadership before addressing correspondence. Largest D49 "
            "discretionary recipient: $400,000 across 4 lines in FY2027.",
       service_area="homeless services, food security, HIV services, "
                    "immigrant services, reentry"),
    _r("nonprofit", "Northfield Community LDC", "community and economic development",
       confidence="unverified", source="SIEDC",
       url="https://www.facebook.com/Northfield.ldc/",
       note="$220,000 across 2 FY2027 lines via HPD. Leadership not confirmed "
            "from an organisational page.",
       service_area="housing quality, local economy, human needs, "
                    "small business assistance"),
    _r("nonprofit", "Staten Island Arts", "borough arts council",
       title="Executive Director", person="Pia Agrawal",
       phone="(718) 447-3329 x1002",
       address="23 Navy Pier Court, Staten Island, NY", zip="10304",
       confidence="published", source="SIARTS",
       note="Email published but TRUNCATED in the source and deliberately not "
            "reconstructed here. Look it up before writing. Staten Island Arts "
            "is the DCLA regranting body — the gateway for small arts "
            "organisations to city money.",
       service_area="arts regranting, cultural funding, public art, "
                    "arts in education"),
    _r("nonprofit", "La Colmena", "day labourer and immigrant worker centre",
       title="Executive Director", person="Yesenia Mata",
       confidence="unverified", source="SIEDC",
       note="Named in the D49 calendar (7th Annual Mexican Independence "
            "event). Contact not confirmed from an organisational page.",
       service_area="immigrant workers, wage theft, day labour, "
                    "workforce access"),
    _r("nonprofit", "The Staten Island Foundation", "private borough funder",
       confidence="unverified", source="SIEDC",
       note="The borough's principal private grantmaker — the natural match "
            "partner for public dollars. Leadership names appear in public "
            "sources but were not confirmed from the Foundation's own page.",
       service_area="private grants, match funding, capacity building"),
    _r("nonprofit", "New York Center for Interpersonal Development",
       "youth and community services", confidence="unverified",
       source="SIEDC",
       note="$160,000 across 2 FY2027 lines via DYCD.",
       service_area="youth development, mediation, LGBTQ services"),
)

INSTITUTION: tuple[Row, ...] = (
    _r("institution", "Richmond University Medical Center", "hospital — West Brighton",
       title="President and CEO", person="Daniel J. Messina, PhD, FACHE, LNHA",
       phone="(718) 818-1234",
       address="355 Bard Avenue, Building 2, Staten Island, NY", zip="10310",
       confidence="published", source="RUMC",
       note="In District 49. The borough delegation jointly announced $1.4M "
            "for a new RUMC building — an existing bipartisan win to build on.",
       service_area="hospital capital, health access, emergency care, "
                    "behavioural health"),
    _r("institution", "Staten Island University Hospital", "hospital — Ocean Breeze",
       title="President", person="Meagan Sills, MBA", phone="(718) 226-9000",
       address="475 Seaview Avenue, Staten Island, NY", zip="10305",
       confidence="published", source="SIUH",
       note="Northwell Health. Second borough hospital system.",
       service_area="hospital services, health workforce, community health"),
    _r("institution", "SIEDC", "Staten Island Economic Development Corporation",
       title="President and CEO", person="Mike Cusick",
       confidence="listed", source="SIEDC", url="https://www.siedc.org/staff",
       note="Former Assembly member; the borough's main business-development "
            "convener. Direct contact not published on the staff page.",
       service_area="business attraction, workforce, commercial corridors, "
                    "borough economic strategy"),
    _r("institution", "Staten Island Chamber of Commerce", "business membership body",
       confidence="published", source="SICHAMBER",
       url="https://www.sichamber.com/elected-officials",
       note="Also maintains the borough's public elected-officials directory, "
            "which is a useful cross-check on this file.",
       service_area="small business, commercial corridors, member outreach"),
)


# The human-services and housing desks. A North Shore district office calls
# these more than anything else on this list -- heat and hot water, benefits,
# older adults, illegal conversions -- and a routing table without them is not
# a routing table.
CITY_SERVICES: tuple[Row, ...] = (
    _r("city", "HPD", "Staten Island Borough Service Center",
       title="Borough Service Center",
       confidence="published", source="HPD_BSC",
       url="https://www.nyc.gov/site/hpd/contact/borough-service-centers.page",
       note="HPD publishes borough service centre numbers on the page above "
            "rather than in a static directory; read the current number there. "
            "This is the single most-called agency in North Shore casework.",
       service_area="heat and hot water, housing code violations, Section 8, "
                    "affordable housing lotteries, landlord harassment, "
                    "emergency repairs"),
    _r("city", "DSS/HRA", "Staten Island — Human Resources Administration",
       phone="(718) 557-1399", confidence="listed", source="HRA_LOC",
       url="https://www.nyc.gov/site/hra/locations/job-locations-and-service-centers.page",
       note="Listed general line. Job Centre and SNAP Centre locations are on "
            "the page above and should be confirmed per case.",
       service_area="SNAP, cash assistance, Medicaid, rental arrears, "
                    "one-shot deals, eviction prevention"),
    _r("city", "DFTA", "Community Agency for Senior Citizens (CASC) — SI CD1-3",
       title="Borough aging services provider", phone="(718) 981-6226 x146",
       address="120 Stuyvesant Place, Suite 409, Staten Island, NY", zip="10301",
       confidence="published", source="DFTA_EJ",
       note="Open M-F 9:00-17:00. Covers all three Staten Island community "
            "districts. In District 49 — two blocks from the D49 office.",
       service_area="older adult centres, home-delivered meals, caregiver "
                    "support, benefits screening, aging in place"),
    _r("city", "DOB", "Staten Island Borough Office — plumbing",
       phone="(718) 420-5416", confidence="published", source="DOB_CONTACT",
       url="https://www.nyc.gov/site/buildings/dob/office-locations.page",
       service_area="plumbing permits and inspections"),
    _r("city", "DOB", "Staten Island Borough Office — electrical",
       phone="(718) 420-5411", confidence="published", source="DOB_CONTACT",
       url="https://www.nyc.gov/site/buildings/dob/office-locations.page",
       note="Borough office address and general line on the page above. "
            "Illegal conversions are a standing North Shore issue.",
       service_area="construction permits, illegal conversions, stop work "
                    "orders, facade and scaffolding, elevator complaints"),
    _r("city", "DEP", "Environmental Protection — borough contact",
       confidence="unverified", source="DEP",
       url="https://www.nyc.gov/site/dep/about/contact-us.page",
       note="No Staten Island borough line confirmed. DEP matters on the "
            "North Shore are strategic, not routine -- sewer and stormwater "
            "capacity is the gating question in the NYCEDC Action Plan, so "
            "route it to the commissioner's office, not 311.",
       service_area="water bills, sewer backups, catch basins, "
                    "sewer and stormwater capacity, noise"),
    _r("city", "SBS", "Small Business Services — borough support",
       confidence="unverified", source="SBS",
       url="https://www.nyc.gov/site/sbs/index.page",
       note="Confirm the Staten Island Workforce1 centre and the commercial "
            "corridor contact; both are standing D49 economic asks.",
       service_area="small business support, commercial leases, MWBE "
                    "certification, Workforce1"),
)

ALL: tuple[Row, ...] = (FEDERAL + STATE + CITY_ELECTED + CITY_AGENCIES
                        + CITY_SERVICES + COMMUNITY_BOARDS + NONPROFIT
                        + INSTITUTION)


# ------------------------------------------------------------------ load ----
def _contact_id(row: Row) -> str:
    base = f"{row['level']}:{row['agency']}:{row['office']}"
    return base.lower().replace(" ", "-").replace("—", "-")[:120]


def normalize(rows: Iterable[Row]) -> list[dict]:
    out = []
    for row in rows:
        url = row.get("url") or (GREENBOOK.format(id=row["greenbook"])
                                 if row.get("greenbook") else
                                 SOURCES.get(row.get("source", ""), ""))
        out.append({
            "contact_id": _contact_id(row),
            "level": row["level"], "agency": row["agency"],
            "office": row["office"], "title": row.get("title"),
            "person": row.get("person"), "email": row.get("email"),
            "phone": row.get("phone"), "address": row.get("address"),
            "borough": "Staten Island", "district": row.get("district"),
            "zip": row.get("zip"), "service_area": row.get("service_area"),
            "parent": row.get("parent"), "url": url,
            "source_id": "SI_DIRECTORY",
            "updated": CHECKED,
            "verified": CHECKED,
            "verified_at": SOURCES.get(row.get("source", ""), url),
            "confidence": row.get("confidence", "unverified"),
            "note": row.get("note"),
        })
    return out


def load(store: Store, rows: Iterable[Row] | None = None) -> dict:
    """Load the curated directory. Idempotent; replaces its own records only."""
    payload = normalize(rows if rows is not None else ALL)
    store.conn.execute("DELETE FROM contacts WHERE source_id='SI_DIRECTORY'")
    store.upsert("contacts", payload)
    store.index_many([
        (
            "contact", r["contact_id"],
            " — ".join(x for x in (r["person"], r["office"]) if x) or r["agency"],
            " ".join(str(x) for x in (r["agency"], r["title"], r["service_area"],
                                      r["address"], r["note"]) if x),
            " ".join(str(x) for x in (r["phone"], r["email"], r["district"]) if x),
        ) for r in payload])
    store.conn.commit()
    counts: dict[str, int] = {}
    for r in payload:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    store.journal("directory.load", {"contacts": len(payload), "by_level": counts})
    return {
        "loaded": len(payload), "by_level": counts,
        "by_confidence": {
            c: sum(1 for r in payload if r["confidence"] == c)
            for c in ("published", "listed", "unverified")},
        "with_phone": sum(1 for r in payload if r["phone"]),
        "checked": CHECKED,
    }


# ---------------------------------------------------------------- reading ----
def directory(store: Store, level: str = "", query: str = "",
              confidence: str = "", limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM contacts WHERE 1=1"
    params: list = []
    if level:
        sql += " AND level = ?"
        params.append(level)
    if confidence:
        sql += " AND confidence = ?"
        params.append(confidence)
    if query:
        sql += (" AND (person LIKE ? OR agency LIKE ? OR office LIKE ? "
                "OR service_area LIKE ? OR title LIKE ?)")
        params += [f"%{query}%"] * 5
    sql += (" ORDER BY CASE level WHEN 'federal' THEN 1 WHEN 'state' THEN 2 "
            "WHEN 'city' THEN 3 WHEN 'community_board' THEN 4 "
            "WHEN 'institution' THEN 5 ELSE 6 END, agency, office LIMIT ?")
    params.append(limit)
    return [dict(r) for r in store.q(sql, params)]


def route(store: Store, problem: str, limit: int = 6) -> dict:
    """
    Who to call about a constituent problem.

    Matches the problem against each contact's service area. Returns what it
    matched on, so a staffer can see why an office was suggested rather than
    trusting a ranked list they cannot interrogate.
    """
    words = [w.lower() for w in problem.split() if len(w) > 3]
    hits = []
    for row in directory(store, limit=500):
        blob = " ".join(str(x or "").lower() for x in
                        (row["service_area"], row["agency"], row["office"]))
        matched = [w for w in words if w in blob]
        if matched:
            hits.append({**row, "matched_on": matched, "score": len(matched)})
    hits.sort(key=lambda r: (-r["score"], r["confidence"] != "published"))
    return {
        "problem": problem,
        "matches": hits[:limit],
        "note": ("Contacts marked 'listed' or 'unverified' must be confirmed "
                 "before the office uses the name in writing."
                 if any(h["confidence"] != "published" for h in hits[:limit])
                 else None),
    }


def gaps(store: Store) -> dict:
    """What the directory still does not know. The to-do list for a staffer."""
    rows = directory(store, limit=500)
    return {
        "total": len(rows),
        "no_phone_or_email": [
            {"office": r["office"], "agency": r["agency"], "note": r["note"]}
            for r in rows if not r["phone"] and not r["email"]],
        "unverified_people": [
            {"office": r["office"], "person": r["person"]}
            for r in rows if r["confidence"] == "unverified"],
        "how_to_close": (
            "The Green Book (a856-gbol.nyc.gov) is authoritative for every City "
            "agency record here. Run `universe load --greenbook` from a network "
            "that can reach it and these fill in automatically. Nonprofit "
            "leadership is best confirmed from each organisation's own staff "
            "page, not a directory aggregator."),
    }
