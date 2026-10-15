#!/usr/bin/env python3
"""
Reading the code index.

Drafting a bill starts with three questions, and until now the office answered
all three by memory and asking around:

    Has anyone legislated on this section before?
    What happened to them?
    What else lives in this part of the code?

The third is a research task. The first two are a decision: if eleven bills
have amended § 27-2004 in a decade and two became law, the office is looking
at a hard section, and the useful next question is what the two that passed
had that the nine did not.

So ``precedent`` returns the base rate, not a list. A base rate is the single
most useful thing a legislative office can know before it commits staff time,
and it is the thing nobody has, because computing it by hand means reading a
decade of Legistar.

The base rate is honest about what it is. It reports how a section's bills
have fared historically. It cannot know whether this Council is different, and
says so rather than implying a forecast.
"""
from __future__ import annotations

from typing import Any, Iterable

from ..core.store import Store

SUBSTANTIVE = ("amended", "added", "repealed", "renumbered")
BODIES = {"admin_code": "Administrative Code", "charter": "City Charter",
          "rules": "Rules of the City of New York", "state": "State law"}


def _rows(store: Store, sql: str, params: Iterable = ()) -> list[dict]:
    return [dict(r) for r in store.q(sql, tuple(params))]


def title_name(title: str) -> str:
    from ..ingest.codes import TITLES
    return TITLES.get(str(title), "")


def section(store: Store, ref: str, limit: int = 60) -> dict:
    """Every bill that has touched one section, newest first."""
    ref = ref.strip().lstrip("§").strip()
    rows = _rows(store, """
        SELECT c.file, c.matter_id, c.action, c.year, c.stage,
               m.name, m.status, m.committee, m.local_law, m.n_sponsors,
               mb.name AS prime
        FROM code_refs c
        JOIN matters m ON m.matter_id = c.matter_id
        LEFT JOIN members mb ON mb.person_id = m.prime_id
        WHERE c.section = ? ORDER BY c.year DESC, c.file DESC LIMIT ?""",
        (ref, limit))
    if not rows:
        return {"section": ref, "bills": [], "found": False,
                "says": f"No bill in the corpus references § {ref}. Either "
                        f"nothing has been tried, or the section is written "
                        f"differently in the text — try a search instead."}
    title = ref.split("-")[0]
    return {"section": ref, "found": True, "bills": rows,
            "title": title, "title_name": title_name(title),
            "precedent": precedent(store, ref),
            "says": f"§ {ref} — {title_name(title) or 'Administrative Code'}. "
                    f"{len(rows)} bill(s) in the corpus reference it."}


def precedent(store: Store, ref: str) -> dict:
    """
    How bills that amended this section have fared.

    Only substantive references count. A bill that mentions a section in a
    definition is not an attempt to change it, and counting those would put
    the base rate somewhere between flattering and meaningless.
    """
    ref = ref.strip().lstrip("§").strip()
    rows = _rows(store, f"""
        SELECT c.action, c.stage, m.n_sponsors, m.year, m.local_law, m.file
        FROM code_refs c JOIN matters m ON m.matter_id = c.matter_id
        WHERE c.section = ? AND c.action IN ({','.join('?' * len(SUBSTANTIVE))})
        """, (ref, *SUBSTANTIVE))
    if not rows:
        return {"attempts": 0, "enacted": 0, "rate": None,
                "says": "No bill has tried to amend this section, so there is "
                        "no track record to read."}
    enacted = [r for r in rows if r["stage"] == "law"]
    live = [r for r in rows if r["stage"] == "live"]
    rate = len(enacted) / len(rows)
    won = sorted(r["n_sponsors"] or 0 for r in enacted)
    lost = sorted((r["n_sponsors"] or 0) for r in rows if r["stage"] == "dead")

    def med(xs):
        return xs[len(xs) // 2] if xs else None

    out = {"attempts": len(rows), "enacted": len(enacted),
           "live_now": len(live), "rate": round(rate, 3),
           "median_sponsors_enacted": med(won),
           "median_sponsors_failed": med(lost),
           "enacted_files": [r["file"] for r in enacted][:8]}
    hard = "hard" if rate < 0.25 else ("mixed" if rate < 0.5 else "receptive")
    out["says"] = (
        f"{len(rows)} bill(s) have tried to amend § {ref}; {len(enacted)} "
        f"became law ({rate:.0%}). Historically a {hard} section."
        + (f" The bills that passed carried a median of {med(won)} sponsors; "
           f"those that died, {med(lost)}." if won and lost else "")
        + " This is a base rate over the corpus, not a forecast for this "
          "Council.")
    return out


def in_title(store: Store, title: str, limit: int = 40) -> dict:
    """What the Council has legislated inside one title of the code."""
    title = str(title).strip()
    rows = _rows(store, """
        SELECT section, COUNT(*) refs,
               SUM(action IN ('amended','added','repealed')) amended,
               MAX(year) latest
        FROM code_refs WHERE body_of_law = 'admin_code' AND title = ?
        GROUP BY section ORDER BY amended DESC, refs DESC LIMIT ?""",
        (title, limit))
    return {"title": title, "name": title_name(title), "sections": rows,
            "says": f"Title {title} — {title_name(title)}. "
                    f"{len(rows)} section(s) legislated on in the corpus."}


def for_matter(store: Store, matter_id: Any) -> dict:
    """What law this bill touches, and what it does to each piece."""
    rows = _rows(store, """
        SELECT body_of_law, title, section, action FROM code_refs
        WHERE matter_id = ? ORDER BY
          action IN ('amended','added','repealed') DESC, body_of_law, section""",
        (matter_id,))
    amends = [r for r in rows if r["action"] in SUBSTANTIVE]
    return {"matter_id": matter_id, "refs": rows, "amends": amends,
            "cites": [r for r in rows if r["action"] == "cited"],
            "says": (f"Amends {len(amends)} section(s); cites "
                     f"{len(rows) - len(amends)} more."
                     if rows else "No law reference found in this bill's text.")}


def bodies(store: Store) -> list[dict]:
    """Coverage of the index, by body of law."""
    return _rows(store, """
        SELECT body_of_law, COUNT(*) refs, COUNT(DISTINCT section) sections,
               COUNT(DISTINCT matter_id) bills
        FROM code_refs GROUP BY body_of_law ORDER BY refs DESC""")


def find(store: Store, term: str, limit: int = 25) -> list[dict]:
    """
    Which sections govern a subject.

    Searches the names of the bills that touch each section rather than the
    code itself, because the office has the bills and does not have the code.
    It is an indirect route to the right answer and it is honest about that:
    a section that fifteen bills about street vending have amended is the
    street vending section, whatever it is called.
    """
    like = f"%{term.lower()}%"
    return _rows(store, """
        SELECT c.section, c.title, COUNT(*) bills,
               SUM(c.action IN ('amended','added','repealed')) amended,
               MAX(m.name) example
        FROM code_refs c JOIN matters m ON m.matter_id = c.matter_id
        WHERE c.body_of_law = 'admin_code' AND LOWER(m.name) LIKE ?
        GROUP BY c.section ORDER BY amended DESC, bills DESC LIMIT ?""",
        (like, limit))
