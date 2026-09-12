#!/usr/bin/env python3
"""
The citation registry.

Every fact that enters the Universe carries a source. This module is the
single place that decides what a citation looks like, how strong it is, and
how it renders in a brief, a footnote, or an export.

The rule the office runs on: a figure without a citation is not a figure, it
is a claim. Claims get tagged ``[verify: source]`` and never ship as fact.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Iterable

from .config import PACK_DIR, PROVENANCE

VERIFY_TAG = "[verify: source]"


@dataclass
class Source:
    """One citable source."""
    source_id: str
    name: str
    publisher: str
    url: str | None = None
    accessed: str | None = None
    coverage: str | None = None
    tier: str = "OFFICIAL_DERIVED"
    locator: str | None = None   # page, chart, dataset column, section
    notes: str | None = None

    @property
    def rank(self) -> int:
        return PROVENANCE.get(self.tier, 9)

    def cite(self, locator: str | None = None) -> str:
        """Render a compact inline citation."""
        loc = locator or self.locator
        bits = [self.name]
        if loc:
            bits.append(str(loc))
        if self.publisher and self.publisher not in self.name:
            bits.append(self.publisher)
        if self.accessed:
            bits.append(f"accessed {self.accessed}")
        return "(" + "; ".join(b for b in bits if b) + ")"

    def footnote(self, n: int, locator: str | None = None) -> str:
        loc = locator or self.locator
        parts = [f"[{n}] {self.name}"]
        if loc:
            parts.append(loc)
        if self.publisher:
            parts.append(self.publisher)
        if self.url:
            parts.append(self.url)
        if self.accessed:
            parts.append(f"accessed {self.accessed}")
        return " — ".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Fact:
    """A value bound to its source. The atom of every deliverable."""
    value: Any
    source_id: str | None = None
    locator: str | None = None
    label: str | None = None
    unit: str | None = None
    as_of: str | None = None
    derived: bool = False        # True when computed rather than read
    method: str | None = None    # how it was computed, if derived

    @property
    def verified(self) -> bool:
        return bool(self.source_id)

    def render(self, registry: "CitationRegistry | None" = None) -> str:
        v = self.value
        if isinstance(v, (int, float)) and self.unit == "usd":
            v = f"${v:,.0f}"
        elif isinstance(v, float):
            v = f"{v:,.4g}"
        elif isinstance(v, int):
            v = f"{v:,}"
        text = f"{self.label}: {v}" if self.label else str(v)
        if not self.verified:
            return f"{text} {VERIFY_TAG}"
        if registry:
            src = registry.get(self.source_id)
            if src:
                return f"{text} {src.cite(self.locator)}"
        return f"{text} [{self.source_id}]"


class CitationRegistry:
    """Holds every source the system knows about, and mints new ones."""

    def __init__(self, sources: Iterable[Source] = ()):
        self._by_id: dict[str, Source] = {}
        for s in sources:
            self.add(s)

    # ---------------------------------------------------------- mutation --
    def add(self, source: Source) -> Source:
        self._by_id[source.source_id] = source
        return source

    def mint(self, name: str, publisher: str, url: str | None = None,
             tier: str = "OFFICIAL_DERIVED", **kw) -> Source:
        """Create a deterministic source id from the identifying fields."""
        key = f"{name}|{publisher}|{url or ''}".encode()
        sid = kw.pop("source_id", None) or "S" + hashlib.sha1(key).hexdigest()[:8].upper()
        src = Source(source_id=sid, name=name, publisher=publisher, url=url,
                     tier=tier, accessed=kw.pop("accessed", date.today().isoformat()),
                     **kw)
        return self.add(src)

    # ------------------------------------------------------------ access --
    def get(self, source_id: str | None) -> Source | None:
        return self._by_id.get(source_id) if source_id else None

    def __contains__(self, sid: str) -> bool:
        return sid in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def all(self) -> list[Source]:
        return sorted(self._by_id.values(), key=lambda s: (s.rank, s.name))

    def by_tier(self, tier: str) -> list[Source]:
        return [s for s in self._by_id.values() if s.tier == tier]

    # ------------------------------------------------------ bibliography --
    def bibliography(self, used: Iterable[str] | None = None) -> list[str]:
        """Numbered footnotes for the sources actually used."""
        ids = list(dict.fromkeys(used)) if used is not None else [s.source_id for s in self.all()]
        out = []
        for i, sid in enumerate(ids, 1):
            src = self.get(sid)
            if src:
                out.append(src.footnote(i))
        return out

    # -------------------------------------------------------- persistence --
    def save(self, path=None) -> None:
        path = path or (PACK_DIR / "sources.json")
        path.write_text(json.dumps(
            {"sources": [s.to_dict() for s in self.all()]}, indent=2))

    @classmethod
    def load(cls, path=None) -> "CitationRegistry":
        path = path or (PACK_DIR / "sources.json")
        if not path.exists():
            return cls(DEFAULT_SOURCES)
        raw = json.loads(path.read_text())
        return cls(Source(**s) for s in raw.get("sources", []))


# --------------------------------------------------------------------------
# The standing source list: the authoritative places NYC fiscal and
# legislative facts actually come from. Ingest adds document-level sources on
# top of these.
# --------------------------------------------------------------------------
DEFAULT_SOURCES: tuple[Source, ...] = (
    Source("LEGISTAR", "NYC Council Legistar Web API", "New York City Council",
           "https://webapi.legistar.com/v1/nyc", tier="OFFICIAL_PRIMARY",
           coverage="Bills, sponsors, action histories, roll-call votes, committee events"),
    Source("LEGISTAR_UI", "NYC Council Legislative Research Center", "New York City Council",
           "https://legistar.council.nyc.gov/", tier="OFFICIAL_PRIMARY",
           coverage="Public bill and hearing record"),
    Source("SCHEDULE_C", "Adopted Budget Schedule C (member designations)",
           "NYC Council Finance Division", "https://council.nyc.gov/budget/schedule-c/",
           tier="OFFICIAL_PRIMARY",
           coverage="Every discretionary designation, by member and initiative, FY2008+"),
    Source("SEC254", "Section 254 Capital Budget Changes & Supporting Detail",
           "NYC Council Finance Division", "https://council.nyc.gov/budget/",
           tier="OFFICIAL_PRIMARY",
           coverage="Council capital adds by member and project"),
    Source("TRANSPARENCY_RESO", "Transparency Resolutions (in-year designation changes)",
           "New York City Council", "https://legistar.council.nyc.gov/",
           tier="OFFICIAL_PRIMARY",
           coverage="Adds, cuts and reallocations after adoption"),
    Source("COUNCIL_BUDGET", "Council Budget Publications (Financial Plan Overview, "
           "Preliminary Budget Response, tax forecast)", "NYC Council Finance Division",
           "https://council.nyc.gov/budget/", tier="OFFICIAL_PRIMARY",
           coverage="Council's own revenue forecast and budget positions"),
    Source("OMB", "Financial Plans and Budget Publications", "NYC Office of Management & Budget",
           "https://www.nyc.gov/site/omb/publications/publications.page",
           tier="OFFICIAL_PRIMARY",
           coverage="Preliminary, Executive, Adopted budgets and in-year modifications"),
    Source("COMPTROLLER", "Budget analysis, ACFR, New York by the Numbers",
           "NYC Comptroller", "https://comptroller.nyc.gov/reports/",
           tier="OFFICIAL_DERIVED", coverage="Independent fiscal analysis and monthly economic outlook"),
    Source("CHECKBOOK", "Checkbook NYC", "NYC Comptroller", "https://www.checkbooknyc.com/",
           tier="OFFICIAL_DERIVED", coverage="Contracts, spending and payroll at payment level"),
    Source("IBO", "Independent Budget Office", "NYC Independent Budget Office",
           "https://www.ibo.nyc.ny.us/", tier="INSTITUTIONAL",
           coverage="Nonpartisan fiscal history, revenue and spending analysis"),
    Source("OPENDATA", "NYC Open Data (Socrata)", "City of New York",
           "https://data.cityofnewyork.us/", tier="OFFICIAL_DERIVED",
           coverage="311, budget, contracts, payroll, boundaries, demographics"),
    Source("CHARTER", "New York City Charter", "New York City",
           "https://codelibrary.amlegal.com/codes/newyorkcity/latest/NYCcharter/",
           tier="OFFICIAL_PRIMARY", coverage="Structure of city government, ULURP, budget process"),
    Source("ADMIN_CODE", "New York City Administrative Code", "New York City",
           "https://codelibrary.amlegal.com/codes/newyorkcity/latest/NYCadmin/",
           tier="OFFICIAL_PRIMARY", coverage="Codified local law"),
    Source("RCNY", "Rules of the City of New York", "New York City",
           "https://codelibrary.amlegal.com/codes/newyorkcity/latest/NYCrules/",
           tier="OFFICIAL_PRIMARY", coverage="Agency rulemaking"),
    Source("NYS_SENATE", "NY State Open Legislation", "New York State Senate",
           "https://legislation.nysenate.gov/", tier="OFFICIAL_PRIMARY",
           coverage="State bills, sponsors, votes, laws"),
    Source("CONGRESS", "Congress.gov API", "U.S. Library of Congress",
           "https://api.congress.gov/", tier="OFFICIAL_PRIMARY",
           coverage="Federal bills and the NYC delegation's record"),
    Source("CENSUS_ACS", "American Community Survey", "U.S. Census Bureau",
           "https://data.census.gov/", tier="OFFICIAL_DERIVED",
           coverage="Population, income, tenure, age, language by tract and district"),
    Source("FRED", "Federal Reserve Economic Data", "Federal Reserve Bank of St. Louis",
           "https://fred.stlouisfed.org/", tier="INSTITUTIONAL",
           coverage="Employment, wages, rates, regional economic series"),
    Source("PROPUBLICA_990", "Nonprofit Explorer (IRS Form 990)", "ProPublica",
           "https://projects.propublica.org/nonprofits/", tier="INSTITUTIONAL",
           coverage="Filed financials for funded organizations, by EIN"),
    Source("CFB", "Follow the Money", "NYC Campaign Finance Board",
           "https://www.nyccfb.info/follow-the-money/", tier="OFFICIAL_DERIVED",
           coverage="Contributions and expenditures by candidate"),
    Source("EMMA", "Electronic Municipal Market Access", "MSRB",
           "https://emma.msrb.org/", tier="OFFICIAL_DERIVED",
           coverage="NYC GO and TFA bond disclosures, yields, ratings"),
    Source("ZAP", "Zoning Application Portal", "NYC Department of City Planning",
           "https://zap.planning.nyc.gov/", tier="OFFICIAL_PRIMARY",
           coverage="ULURP and CEQR applications, status and documents"),
    Source("COUNCIL_BI", "NYC Council Budget Dashboards (Power BI)",
           "NYC Council Finance Division",
           "https://app.powerbigov.us/view?r=eyJrIjoiMTkwYWMyNGEtMDNiZC00OTY4LTk4YjEtYzI0MzhlOTA3MzllIiwidCI6IjM1YzgyODE2LTZjNTYtNDQzYi1iYWY2LTgzMTIxNjNjYWRjMSJ9",
           tier="OFFICIAL_PRIMARY",
           coverage="Council's own interactive budget dashboard: expense and capital "
                    "by agency, unit of appropriation and council district",
           locator="report 190ac24a-03bd-4968-98b1-c2438e90739e, tenant 35c82816-6c56-443b-baf6-8312163cadc1",
           notes="Power BI embed -- interactive only; it renders client-side and "
                 "exposes no server-side JSON endpoint, so figures must be read "
                 "in the dashboard or tied out against Schedule C and Open Data."),
    Source("COUNCIL_BI_HUB", "NYC Council Budget Dashboards hub",
           "NYC Council Finance Division", "https://council.nyc.gov/budget/dashboards/",
           tier="OFFICIAL_PRIMARY", coverage="Index of every published Council budget dashboard"),
    Source("GPP", "NYC Government Publications Portal",
           "NYC Department of Records & Information Services",
           "https://a860-gpp.nyc.gov/", tier="OFFICIAL_PRIMARY",
           coverage="The city's permanent archive of published agency documents: "
                    "capital project detail data, budget books, agency reports, "
                    "filterable by fiscal year and borough",
           notes="Blacklight catalog -- every search view also returns JSON by "
                 "appending format=json, so it can be queried programmatically."),
    Source("COUNCIL_RECORD", "The Council Record", "Council Member Hanks, District 49",
           None, tier="OFFICIAL_DERIVED",
           coverage="Every Council member since 1992, voting fingerprints, ideal "
                    "points, election history, career transitions, and the full "
                    "matter table, compiled from Legistar, Wikidata and the "
                    "Board of Elections",
           notes="Derived analytical compilation. Counts trace to official "
                 "sources; scores and forecasts within it are model output and "
                 "are labeled as inference."),
    Source("GREENBOOK", "NYC Official Directory (the Green Book)",
           "NYC Department of Citywide Administrative Services",
           "https://a856-gbol.nyc.gov/GBOLWebsite/GreenBook/City",
           tier="OFFICIAL_PRIMARY",
           coverage="Every city agency, office, senior official, address and "
                    "phone number; also borough, state and federal offices "
                    "serving the city. The authoritative referral directory.",
           notes="The canonical answer to 'who do I send this constituent to'."),
    Source("CITY_CLERK_LOBBYING", "Lobbying Bureau filings", "NYC City Clerk",
           "https://lobbyistsearch.nyc.gov/", tier="OFFICIAL_PRIMARY",
           coverage="Registered lobbyists, clients, targets and compensation"),
    Source("COIB", "Conflicts of Interest Board", "City of New York",
           "https://www.nyc.gov/site/coib/index.page", tier="OFFICIAL_PRIMARY",
           coverage="Financial disclosure, advisory opinions, enforcement dispositions"),
    Source("OFFICIAL_SITE", "Official office websites", "Various",
           None, tier="OFFICIAL_PRIMARY",
           coverage="Public schedules, staff rosters, press releases"),
    Source("D49_MOCS_TRACKER", "District 49 discretionary tracker (MOCS pipeline)",
           "Council Member Hanks, District 49", None, tier="INTERNAL",
           coverage="Per-award MOCS status, analyst, change codes, FY26-FY27"),
    Source("D49_CALENDAR", "District 49 office calendar", "Council Member Hanks, District 49",
           None, tier="INTERNAL", coverage="Hearings, community events, staff assignments"),
    Source("SI_ROLLUP", "FY27 Staten Island funding reconciliation",
           "Council Member Hanks, District 49", None, tier="INTERNAL",
           coverage="Every SI funding line post-Transparency-Resolution, by channel and pot"),
)
