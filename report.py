#!/usr/bin/env python3
"""
report.py — fuse findings from anywhere in the app into one combined report.

The whole tool produces artifacts — briefings, law wikis, enforcement reports,
predictions, data extracts, notes. This stitches a chosen set of them into a
single titled document with a table of contents, ready to export as Markdown or
a printable page. Pure string assembly; rendering to HTML reuses
briefing.md_to_html / briefing.print_html.
"""


def build_report(title, sections, intro="", as_of=""):
    """Fuse sections into one Markdown document.

    sections: [{"heading": str, "body": str, "kind": str (optional)}]
    """
    title = title or "Legislative Report"
    lines = [f"# {title}"]
    meta = " · ".join(x for x in [f"Prepared {as_of}" if as_of else "",
                                  f"{len(sections)} section(s)"] if x)
    if meta:
        lines.append(f"_{meta}._")
    if intro.strip():
        lines += ["", intro.strip()]
    if len(sections) > 1:
        lines += ["", "## Contents"]
        for i, s in enumerate(sections, 1):
            tag = f" ({s['kind']})" if s.get("kind") else ""
            lines.append(f"{i}. {s.get('heading', 'Section')}{tag}")
    for s in sections:
        lines += ["", "---", "", f"## {s.get('heading', 'Section')}", "", (s.get("body") or "").strip()]
    lines += ["", "---",
              "_Compiled by NYC Legislative Intelligence. AI-written sections are analysis, not official "
              "statements; verify figures against the cited sources._"]
    return "\n".join(lines)


def report_manifest_rows(sections):
    """Rows describing the report's contents, for an Excel manifest sheet."""
    return [{"#": i, "Section": s.get("heading", ""), "Kind": s.get("kind", ""),
             "Chars": len(s.get("body", "") or "")} for i, s in enumerate(sections, 1)]
