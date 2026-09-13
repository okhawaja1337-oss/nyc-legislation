#!/usr/bin/env python3
"""
Write a brief, without touching the terminal beyond answering three questions.

The same contract runs either way: the evidence is assembled from the record
first, every figure must trace back to it, and a brief that fails a blocking
check is saved as a draft rather than filed. This just puts a door on it for
anyone who would rather not type a command.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

KINDS = [
    ("member", "A Councilmember's record",
     "Which member? (surname is enough, e.g. Hanks)", "Hanks"),
    ("fiscal", "The District 49 fiscal position",
     "Which fiscal year? (Enter for 2027)", ""),
    ("matter", "One bill or resolution",
     "Legistar matter id (e.g. 77634)", ""),
]


def ask(prompt: str, default: str = "") -> str:
    try:
        got = input(f"  {prompt}\n  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        raise SystemExit(0)
    return got or default


def main() -> int:
    from universe.ai import process
    from universe.core import pipeline as P
    from universe.core.store import Store

    print("\n  WRITE A BRIEF — District 49\n  " + "=" * 40 + "\n")
    for i, (_, label, _, _) in enumerate(KINDS, 1):
        print(f"    {i}. {label}")
    print()
    choice = ask("Which one? (1, 2 or 3)", "1")
    try:
        kind, label, question, default = KINDS[int(choice) - 1]
    except (ValueError, IndexError):
        print("\n  That was not 1, 2 or 3. Nothing was written.")
        return 1

    print()
    subject = ask(question, default)
    print()
    what = ask("What is the question this answers? (Enter to skip)")
    print()
    council = ask("Run the LLM Council — five perspectives, "
                  "slower? (y/N)", "n").lower().startswith("y")

    kwargs = {}
    if kind == "fiscal" and subject.isdigit():
        kwargs["fy"] = int(subject)
        subject = ""
    if kind == "matter" and not subject.isdigit():
        print("\n  A bill brief needs the numeric matter id. Nothing was written.")
        return 1

    print("\n  Assembling the evidence…")
    with Store() as store:
        made = process.run(store, kind, subject, ask=what or None,
                           with_council=council, write=True, **kwargs)

    receipt = made.receipt
    print("\n  " + "=" * 52)
    print(f"  {(receipt.verdict() if receipt else made.status).upper()}")
    print("  " + "=" * 52)
    if receipt:
        print(textwrap.fill(receipt.why(), 70,
                            initial_indent="  ", subsequent_indent="  "))
        for gate in receipt.blockers:
            print(f"\n  BLOCKED — {gate.asks}\n    {gate.found}")
        for gate in receipt.warnings:
            print(f"\n  Note — {gate.asks}\n    {gate.found}")

    out = HERE / "out" / "universe"
    print(f"\n  Filed as {made.deliverable_id}")
    print(f"  Saved in {out}")
    print("\n  Open the workspace and look under 'Run a brief' to read it.\n")
    try:
        input("  Press Enter to close. ")
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
