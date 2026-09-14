#!/usr/bin/env python3
"""
The dashboard register.

Some authoritative sources are interactive dashboards rather than APIs: the
Council's own Power BI budget reports, the Comptroller's capital projects
tracker, Checkbook NYC. They render client-side and publish no JSON endpoint,
so the Universe cannot pull numbers from them -- but the office still needs to
know they exist, what each one answers, and how to get to the right view fast.

This module keeps that register and produces the deep links a staffer opens
when a briefing needs a figure the lake does not carry.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Dashboard:
    key: str
    name: str
    publisher: str
    url: str
    answers: str
    source_id: str
    interactive_only: bool = True
    notes: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


COUNCIL_BUDGET_BI = Dashboard(
    key="council_budget_bi",
    name="NYC Council Budget Dashboard",
    publisher="NYC Council Finance Division",
    url=("https://app.powerbigov.us/view?r=eyJrIjoiMTkwYWMyNGEtMDNiZC00OTY4"
         "LTk4YjEtYzI0MzhlOTA3MzllIiwidCI6IjM1YzgyODE2LTZjNTYtNDQzYi1iYWY2"
         "LTgzMTIxNjNjYWRjMSJ9"),
    answers=("Adopted and modified expense budget by agency, unit of "
             "appropriation and object class; capital commitments; headcount. "
             "The Council's own presentation of the numbers it negotiated."),
    source_id="COUNCIL_BI",
    notes=("Power BI embed (report 190ac24a-03bd-4968-98b1-c2438e90739e, "
           "tenant 35c82816-6c56-443b-baf6-8312163cadc1). Renders client-side "
           "with no public JSON endpoint -- read figures in the dashboard, then "
           "tie them out against Schedule C or the Open Data expense budget "
           "before putting them in a brief."),
)

REGISTER: tuple[Dashboard, ...] = (
    COUNCIL_BUDGET_BI,
    Dashboard("council_budget_hub", "Council Budget Dashboards hub",
              "NYC Council Finance Division",
              "https://council.nyc.gov/budget/dashboards/",
              "Index of every dashboard the Finance Division publishes.",
              "COUNCIL_BI_HUB"),
    Dashboard("checkbook", "Checkbook NYC", "NYC Comptroller",
              "https://www.checkbooknyc.com/",
              "Every city payment, contract and payroll line, to the penny. "
              "Use it to confirm an organization actually got paid.",
              "CHECKBOOK", interactive_only=False,
              notes="Has a legacy API; the site itself is the practical entry point."),
    Dashboard("capital_projects", "NYC Capital Projects Dashboard",
              "NYC Comptroller",
              "https://comptroller.nyc.gov/services/for-the-public/nyc-capital-projects/",
              "Budget, schedule and phase for capital projects -- including the "
              "§254 adds the Council made.",
              "COMPTROLLER"),
    Dashboard("zap", "Zoning Application Portal (ZAP)",
              "NYC Department of City Planning", "https://zap.planning.nyc.gov/",
              "Every ULURP and CEQR application with status, documents and "
              "community board. The land-use early-warning system.",
              "ZAP"),
    Dashboard("ibo_summary", "IBO Citywide Budget Summary",
              "NYC Independent Budget Office",
              "https://ibo.nyc.ny.us/RevenueSpending/citywide-summary.html",
              "Independent read on revenue and spending trends.",
              "IBO"),
)

BY_KEY = {d.key: d for d in REGISTER}


def all_dashboards() -> list[dict]:
    return [d.to_dict() for d in REGISTER]


def get(key: str) -> Dashboard | None:
    return BY_KEY.get(key)
