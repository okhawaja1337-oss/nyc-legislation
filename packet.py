#!/usr/bin/env python3
"""
packet.py — the "district packet": a one-click, printable briefing bundle for a
member or district. Assembles a profile, that member's bills, upcoming hearings
that touch them, and (optionally) the reps for a district into one clean
Markdown document you can print to PDF or hand across a desk.

Pure formatting — the caller gathers the pieces and passes them in. Rendering to
printable HTML reuses briefing.md_to_html / briefing.print_html.
"""


def build_packet_md(name, level, facts=None, glance="", bills=None,
                    hearings=None, reps=None, as_of=""):
    facts = facts or {}
    bills = bills or []
    hearings = hearings or []
    reps = reps or []
    L = []
    L.append(f"# District packet — {name}")
    sub = " · ".join(x for x in [level, facts.get("chamber", ""), facts.get("seat", "")] if x)
    if sub:
        L.append(f"_{sub}_")
    if as_of:
        L.append(f"_Prepared {as_of}._")
    L.append("")

    # Key facts
    L.append("## Key facts")
    for label, key in [("Party", "party"), ("District", "district"),
                       ("In office since", "since"), ("Term ends", "term_ends"),
                       ("Bills on", "bills_on"), ("As prime sponsor", "as_prime"),
                       ("As co-sponsor", "as_cosponsor"), ("Passed / enacted", "passed"),
                       ("Contact", "contact")]:
        v = facts.get(key)
        if v not in (None, "", []):
            L.append(f"- **{label}:** {v}")
    if facts.get("focus_areas"):
        L.append(f"- **Focus areas:** {', '.join(facts['focus_areas'])}")
    if facts.get("committees"):
        L.append(f"- **Committees:** {', '.join(facts['committees'])}")
    L.append("")

    if glance:
        L.append("## Record at a glance")
        L.append(glance.strip())
        L.append("")

    if reps:
        L.append("## Who represents this district")
        for r in reps:
            dist = f"District {r['district']}" if isinstance(r.get("district"), int) else (r.get("district") or "")
            who = r.get("member") or "(see official site)"
            L.append(f"- **{r.get('seat','')}** ({dist}): {who}")
        L.append("")

    L.append(f"## Bills ({len(bills)})")
    if not bills:
        L.append("- _No bills in the loaded set for this member._")
    for b in bills[:60]:
        file = b.get("File", "")
        title = (b.get("Title") or "")[:100]
        status = b.get("Status", "")
        L.append(f"- **{file}** — {title}  \n  _{status}_")
    if len(bills) > 60:
        L.append(f"- _…and {len(bills) - 60} more (see the full export)._")
    L.append("")

    if hearings:
        L.append(f"## Upcoming hearings ({len(hearings)})")
        for h in hearings[:25]:
            L.append(f"- **{h.get('Date','')}** — {h.get('Committee / Body', h.get('committee',''))}"
                     + (f" ({h.get('Location','')})" if h.get("Location") else ""))
        L.append("")

    L.append("---")
    L.append("_Facts from official sources (NYC Legistar / NY State / congress-legislators). "
             "Any 'record at a glance' is AI analysis grounded in the data above, not an official statement._")
    return "\n".join(L)


def packet_to_rows(name, facts, bills):
    """Rows for an Excel sheet of the member's bills."""
    rows = []
    for b in (bills or []):
        rows.append({"File": b.get("File", ""), "Type": b.get("Type", ""),
                     "Title": b.get("Title", ""), "Status": b.get("Status", ""),
                     "Prime sponsor": b.get("Prime Sponsor", ""),
                     "Web Link": b.get("Web Link", "")})
    return rows
