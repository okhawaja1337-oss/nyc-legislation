#!/usr/bin/env python3
"""
Seed the workspace with the office's real shape.

An empty project tool is a chore. This one opens with the programs District 49
actually runs, the sections work actually moves through, and the deadlines the
City Charter actually imposes -- so the first thing a staffer sees is their own
job, not a blank board.

The budget program is generated from the fiscal calendar rather than typed, so
next year's cycle is one command, not a re-creation. Dates follow the Charter's
sequence (Preliminary in January, Response in March, Executive in April,
adoption by the end of June) and are marked as office-planned, not statutory
deadlines, because the exact day moves each year.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..core.config import PILLARS, STAFF_INITIALS
from . import model, schema

# The office roster, from the initials the calendar already uses.
PEOPLE = [
    {"name": "Kamillah Hanks", "initials": "KH", "role": "Council Member",
     "color": "#16306B", "weekly_hours": 45},
    {"name": "Omar Khawaja", "initials": "OK",
     "role": "Legislative & Budget Director", "color": "#BF9000",
     "weekly_hours": 40},
    {"name": "Chief of Staff", "initials": "COS", "role": "Chief of Staff",
     "color": "#2E6B34", "weekly_hours": 40},
    {"name": "Scheduler", "initials": "AS", "role": "Scheduler & Community",
     "color": "#8A6D00", "weekly_hours": 35},
    {"name": "Community Liaison", "initials": "MB", "role": "Community Liaison",
     "color": "#9A4E12", "weekly_hours": 35},
    {"name": "Constituent Services", "initials": "TP",
     "role": "Constituent Services", "color": "#8E2F2A", "weekly_hours": 35},
    {"name": "Education Liaison", "initials": "EB", "role": "Education Liaison",
     "color": "#3B66A3", "weekly_hours": 35},
    {"name": "Operations", "initials": "OO", "role": "Operations",
     "color": "#57544B", "weekly_hours": 35},
]

# Programs, each with its projects. Sections are created per project.
PROGRAMS: list[dict] = [
    {
        "key": "budget", "name": "Budget & Discretionary Funding",
        "icon": "◈", "color": "#BF9000", "pillar": "economic_development",
        "purpose": ("The fiscal year end to end: the Preliminary response, the "
                    "Executive hearings, adoption, Schedule C designations and "
                    "the Transparency Resolutions that move money afterwards."),
        "projects": [
            ("Preliminary Budget Response",
             "The Council's asks, and District 49's place in them."),
            ("Executive Budget hearings",
             "Testimony, questions and the record for each hearing."),
            ("Schedule C designations",
             "Every District 49 designation from intake to MOCS clearance."),
            ("MOCS pipeline",
             "Awards moving through clearance; the exposure report."),
            ("Transparency Resolutions",
             "In-year movement, tiered by whether it is confirmed money."),
            ("Capital §254",
             "Council capital adds and the projects behind them."),
        ],
    },
    {
        "key": "legislation", "name": "Legislative Program",
        "icon": "⚖", "color": "#16306B", "pillar": "si_parity",
        "purpose": ("Bills the Member sponsors, sign-on decisions, hearing "
                    "preparation and the whip work behind each vote."),
        "projects": [
            ("Prime-sponsored bills", "Drafting through enactment."),
            ("Sign-on decisions", "Bills to join, hold, or seek amendments to."),
            ("Hearing preparation", "Questions, witnesses and the record."),
            ("Oversight & follow-up", "What the agency promised, and whether it happened."),
        ],
    },
    {
        "key": "land_use", "name": "Land Use & Development",
        "icon": "▦", "color": "#2E6B34", "pillar": "neighborhood_development",
        "purpose": ("ULURP applications, rezonings and capital projects on the "
                    "North Shore, tested against the office's land-use doctrine: "
                    "ownership over rental, affordability at or below 80% AMI, "
                    "and infrastructure that lands with the units."),
        "projects": [
            ("Active ULURP applications", "Certification through Council vote."),
            ("Bay Street corridor", "The corridor's projects in one place."),
            ("Community Board 1 coordination", "Positions, testimony and timing."),
            ("Infrastructure commitments", "What was promised with each approval."),
        ],
    },
    {
        "key": "constituent", "name": "Constituent Services",
        "icon": "◉", "color": "#8E2F2A", "pillar": "public_safety",
        "purpose": ("Casework, referrals and the recurring problems that should "
                    "become legislation or a budget ask."),
        "projects": [
            ("Open casework", "Active constituent matters and their referrals."),
            ("Agency escalations", "Cases that need a call above the front line."),
            ("Recurring problems", "Patterns worth a bill or a budget line."),
        ],
    },
    {
        "key": "comms", "name": "Communications & Press",
        "icon": "▧", "color": "#A3403B", "pillar": "arts_culture",
        "purpose": ("Statements, talking points, newsletter and the public "
                    "record — every deliverable sourced before it ships."),
        "projects": [
            ("Press statements", "Drafts through approval and release."),
            ("Talking points", "Hearing, event and reporter preparation."),
            ("Newsletter & district mail", "What goes to the district list."),
            ("Media monitoring", "Coverage, clips and the quotable record."),
        ],
    },
    {
        "key": "district", "name": "District Operations & Events",
        "icon": "▤", "color": "#57544B", "pillar": "neighborhood_development",
        "purpose": "Events, community boards, civic associations and the calendar.",
        "projects": [
            ("Community events", "What the office attends, and who staffs it."),
            ("Civic & community boards", "Standing meetings and commitments."),
            ("Office operations", "Internal, administrative and staffing work."),
        ],
    },
]


def _fy_dates(fy: int) -> list[tuple[str, str, str]]:
    """The budget cycle for a fiscal year, as (date, title, note).

    NYC fiscal year FY2028 runs July 2027 to June 2028, so its Preliminary
    Budget lands in January 2027. These are the office's planning dates, not
    statutory deadlines -- the Charter fixes the sequence, the Mayor fixes the
    day.
    """
    cal = fy - 1
    return [
        (f"{cal}-01-16", f"FY{fy} Preliminary Budget released",
         "Mayor's Preliminary Budget; Council preliminary hearings follow."),
        (f"{cal}-03-10", f"FY{fy} Preliminary Budget Response adopted",
         "The Council's asks. District 49 priorities must be in before this."),
        (f"{cal}-04-26", f"FY{fy} Executive Budget released",
         "Executive hearings follow through May."),
        (f"{cal}-06-10", f"FY{fy} Schedule C designations due",
         "Member designations finalised for adoption."),
        (f"{cal}-06-30", f"FY{fy} Budget adopted",
         "Adoption vote; Schedule C fixes the designations."),
        (f"{fy}-07-15", f"FY{fy} Transparency Resolution #1",
         "First in-year movement after adoption."),
    ]


def seed(store, fy: int | None = None, actor: str = "seed") -> dict:
    """Create the office's programs, projects, people and cycle milestones."""
    schema.apply(store.conn)
    fy = fy or (date.today().year + 1 if date.today().month >= 7
                else date.today().year)

    created = {"people": 0, "programs": 0, "projects": 0, "milestones": 0}

    existing_people = {p["name"] for p in model.people(store, active_only=False)}
    for person in PEOPLE:
        if person["name"] not in existing_people:
            model.save_person(store, {**person, "actor": actor})
            created["people"] += 1

    existing_programs = {p["name"] for p in model.programs(store, True)}
    for i, spec in enumerate(PROGRAMS):
        if spec["name"] in existing_programs:
            continue
        program = model.save_program(store, {
            "name": spec["name"], "purpose": spec["purpose"],
            "pillar": spec["pillar"], "color": spec["color"],
            "icon": spec["icon"], "fy": fy, "position": i,
            "lead": "Omar Khawaja", "actor": actor,
        })
        created["programs"] += 1
        for j, (name, purpose) in enumerate(spec["projects"]):
            model.save_project(store, {
                "program_id": program["id"], "name": name, "purpose": purpose,
                "lead": "Omar Khawaja", "position": j, "actor": actor,
            })
            created["projects"] += 1

        # The budget program carries the cycle as dated milestones.
        if spec["key"] == "budget":
            cycle = model.save_project(store, {
                "program_id": program["id"], "name": f"FY{fy} cycle calendar",
                "purpose": ("Charter-sequenced dates for this fiscal year. "
                            "Office planning dates, not statutory deadlines."),
                "position": 99, "default_view": "timeline", "actor": actor,
            })
            created["projects"] += 1
            for k, (when, title, note) in enumerate(_fy_dates(fy)):
                model.save_task(store, {
                    "title": title, "project_id": cycle["id"],
                    "due": f"{when}T17:00:00", "milestone": True,
                    "status": "Intake", "priority": "High",
                    "owner": "Omar Khawaja", "description": note,
                    "position": k, "actor": actor,
                })
                created["milestones"] += 1

    store.set_meta("workspace.seeded", {"fy": fy, "created": created})
    return {"fy": fy, **created,
            "note": ("Programs, projects and cycle milestones created. "
                     "Existing items were left alone.")}


def demo_tasks(store, actor: str = "seed") -> dict:
    """A handful of representative tasks, so every view has something in it."""
    projects = {p["name"]: p["id"] for p in model.projects(store)}
    today = date.today()
    rows = [
        ("Reconcile the SI ledger to 100% of lines", "Schedule C designations",
         "Omar Khawaja", 3, "Urgent",
         "Loaded detail covers part of the 712 lines. Pull the full export and "
         "tie every line out before the next Transparency Resolution."),
        ("Call lapsed grantees before they call us", "Schedule C designations",
         "Community Liaison", 5, "High",
         "Organizations funded three years running that are not in this year's "
         "book will call. Reach them first."),
        ("Draft FY28 Preliminary Budget Response asks", "Preliminary Budget Response",
         "Omar Khawaja", 21, "High",
         "District 49 priorities for the Council's response, with the per-resident "
         "delivery argument."),
        ("Sign-on review: pending health bills", "Sign-on decisions",
         "Omar Khawaja", 7, "Normal",
         "Run the sign-on engine and bring the Member a short list with reasons."),
        ("Bay Street corridor: infrastructure commitments", "Bay Street corridor",
         "Chief of Staff", 14, "High",
         "What was promised with each approval, and whether it has been funded."),
        ("Media library: attach hearing transcripts", "Media monitoring",
         "Education Liaison", 10, "Normal",
         "Nothing can be quoted for release without a stored transcript."),
    ]
    made = 0
    for title, project, owner, days, priority, body in rows:
        pid = projects.get(project)
        if not pid:
            continue
        if store.scalar("SELECT 1 FROM workspace_tasks WHERE title=?", [title]):
            continue
        model.save_task(store, {
            "title": title, "project_id": pid, "owner": owner,
            "due": (today + timedelta(days=days)).isoformat() + "T17:00:00",
            "priority": priority, "status": "Intake", "description": body,
            "actor": actor,
        })
        made += 1
    return {"tasks": made}
