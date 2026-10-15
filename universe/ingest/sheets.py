#!/usr/bin/env python3
"""
Ingest the office's own spreadsheets.

Two workbooks do the real operational work in District 49 and neither is in
any public dataset:

1. **The discretionary tracker** — every award in the MOCS pipeline with its
   preliminary and final MOCS ID, the Council analyst, the determination
   (Pending / Cleared / CBO Defunded), and the change codes that explain why a
   line moved. This is the only place the *initiative* a line belongs to is
   named; adopted Schedule C only says "Local" or "Citywide".

2. **The Staten Island reconciliation** — every SI funding line after the
   Transparency Resolutions, by channel and pot, tied out to the adopted books.

Both arrive from the Google Drive connector as pipe-delimited markdown. This
module parses that shape, finds the real header row wherever it sits, and
normalizes into the same ``funding`` table the adopted books land in — so a
single query spans adopted, moved, and in-pipeline money.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..core.config import D49_ZIPS
from ..core.store import Store
from .fiscal import clean_ein, org_key, pillar_for

MONEY_RE = re.compile(r"-?\$?\s*([\d,]+(?:\.\d+)?)")
ZIP_RE = re.compile(r"\b(1\d{4})\b")


# ------------------------------------------------------------- parsing ----
def unescape(cell: str) -> str:
    """Undo the connector's markdown escaping."""
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|$])", r"\1", cell or "").strip()


def rows_from_markdown(text: str) -> Iterator[list[str]]:
    """Yield each pipe-delimited row as a list of cleaned cells."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [unescape(c) for c in line.strip("|").split("|")]
        if all(set(c) <= set(":- ") for c in cells):   # alignment row
            continue
        yield cells


def money(cell: str) -> float | None:
    """Parse a currency cell, honouring leading minus and parenthesised negatives."""
    if not cell:
        return None
    s = cell.strip()
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    m = MONEY_RE.search(s)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return -val if neg else val


def find_header(rows: list[list[str]], *required: str,
                max_cell: int = 60) -> tuple[int, dict[str, int]]:
    """Locate a header row by the column names it must contain.

    A header cell is short and each required term must land in its own cell --
    otherwise a merged title row ("ROLLUP BY CHANNEL AND POT (adopted ...)")
    matches every term at once and hijacks the parse.
    """
    want = [r.lower() for r in required]
    for i, cells in enumerate(rows):
        low = [c.lower().strip() for c in cells]
        used: set[int] = set()
        ok = True
        for w in want:
            hit = next((j for j, c in enumerate(low)
                        if j not in used and c and len(c) <= max_cell and w in c), None)
            if hit is None:
                ok = False
                break
            used.add(hit)
        if not ok:
            continue
        idx: dict[str, int] = {}
        for j, c in enumerate(cells):
            key = c.strip().lower()
            if key and len(key) <= max_cell and key not in idx:
                idx[key] = j
        return i, idx
    raise ValueError(f"header row with {required} not found")


def col(cells: list[str], idx: dict[str, int], *names: str) -> str:
    """Read a cell by any of several header spellings; exact match wins."""
    def at(j: int) -> str:
        return cells[j].strip() if 0 <= j < len(cells) else ""
    for n in names:
        n = n.lower()
        if n in idx:
            return at(idx[n])
    for n in names:
        n = n.lower()
        for key, j in idx.items():
            if key.startswith(n):
                return at(j)
    return ""


def _lid(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}-" + hashlib.sha1(raw.encode()).hexdigest()[:14]


# ------------------------------------------------ the MOCS tracker ----
DETERMINATION_MAP = {
    "cleared mocs": "cleared",
    "pending mocs": "pending MOCS",
    "cbo defunded": "defunded",
    "denied": "denied",
    "on hold": "on hold",
    "withdrawn": "withdrawn",
}


def ingest_mocs_tracker(store: Store, text: str,
                        source_id: str = "D49_MOCS_TRACKER") -> dict:
    """Load the per-award MOCS pipeline rows.

    The workbook stacks a tab per fiscal year, so the header repeats; each
    repeat re-binds the column map rather than corrupting the rows beneath it.
    """
    rows = list(rows_from_markdown(text))
    try:
        hdr_i, idx = find_header(rows, "mocs id", "legal name", "council member")
    except ValueError:
        return {"mocs_rows": 0, "error": "header not found"}

    out: list[dict] = []
    codes: dict[str, str] = {}

    with store.tx() as c:
        c.execute("DELETE FROM funding WHERE source_id = ?", (source_id,))

    for ordinal, cells in enumerate(rows[hdr_i + 1:]):
        low = [c.lower() for c in cells]
        if any(c == "legal name" for c in low) and any("mocs id" in c for c in low):
            idx = {c.strip().lower(): j for j, c in enumerate(cells)
                   if c.strip() and len(c.strip()) <= 60}
            continue

        org = col(cells, idx, "legal name")
        if not org:
            continue
        fy_raw = re.sub(r"\D", "", col(cells, idx, "fiscal year"))
        if len(fy_raw) < 4:
            continue
        fy = int(fy_raw[:4])

        ein = clean_ein(col(cells, idx, "tax id"))
        initiative = col(cells, idx, "source") or None
        mocs_state = col(cells, idx, "mocs status").lower()
        cc_state = col(cells, idx, "status")
        status = next((v for k, v in DETERMINATION_MAP.items() if k in mocs_state),
                      None)
        if not status:
            clearance = (col(cells, idx, "mocs clearance status") + " " +
                         col(cells, idx, "city council clearance status")).lower()
            status = next((v for k, v in DETERMINATION_MAP.items() if k in clearance),
                          "in pipeline")

        dist_raw = re.sub(r"\D", "", col(cells, idx, "council district"))
        district = int(dist_raw) if dist_raw else None
        zipc = re.sub(r"\D", "", col(cells, idx, "postal code"))[:5] or None
        program_area = col(cells, idx, "program area") or None
        purpose = col(cells, idx, "cm purpose of funds") or None

        out.append({
            "line_id": _lid("MOCS", fy, ordinal, col(cells, idx, "mocs id") or org,
                            initiative, col(cells, idx, "amount")),
            "fy": fy,
            "channel": "expense",
            "pot": initiative,
            "member": col(cells, idx, "council member") or None,
            "person_id": None,
            "district": district,
            "borough": "Staten Island" if district in (49, 50, 51) else None,
            "org": org,
            "org_key": org_key(org, ein),
            "ein": ein,
            "program": col(cells, idx, "program name") or program_area,
            "agency": col(cells, idx, "agency") or None,
            "amount": money(col(cells, idx, "amount")),
            "section": program_area,
            "purpose": purpose,
            "status": status,
            "mocs_id": col(cells, idx, "mocs id") or None,
            "analyst": col(cells, idx, "analyst", "staff contact") or None,
            "in_d49": 1 if (district == 49 or zipc in D49_ZIPS) else 0,
            "pillar": pillar_for(org, initiative, program_area, purpose),
            "source_id": source_id,
            "locator": (f"FY{fy} discretionary tracker"
                        + (f"; MOCS {col(cells, idx, 'mocs id')}" if col(cells, idx, "mocs id") else "")),
            "updated": None,
        })

        # Budget-line coordinates are what Finance actually needs to move money.
        ua, bc, oc = (col(cells, idx, "u/a"), col(cells, idx, "budget code"),
                      col(cells, idx, "object code"))
        conduit = col(cells, idx, "fiscal conduit")
        if any((ua, bc, oc, conduit, cc_state)):
            out[-1]["purpose"] = " | ".join(
                p for p in (purpose,
                            f"U/A {ua}" if ua else "",
                            f"BC {bc}" if bc else "",
                            f"OC {oc}" if oc else "",
                            f"conduit: {conduit}" if conduit else "",
                            f"Council: {cc_state}" if cc_state else "") if p) or None

    # Legend tabs define initiative abbreviations and the change-code vocabulary.
    for cells in rows[:hdr_i]:
        vals = [c for c in cells if c]
        if len(vals) >= 2 and re.fullmatch(r"[A-Z][A-Z0-9]{0,5}", vals[0]) and len(vals[1]) > 4:
            codes[vals[0]] = vals[1]

    n = store.upsert("funding", out)
    store.set_meta("mocs.initiative_codes", codes)
    store.journal("ingest.mocs_tracker", {"rows": n, "codes": len(codes)})
    return {"mocs_rows": n, "initiative_codes": len(codes)}


def enrich_pots_from_tracker(store: Store) -> int:
    """Backfill initiative names onto adopted Schedule C lines.

    Adopted Schedule C only records Local vs Citywide. The tracker names the
    actual initiative. Where the same organization appears in the same fiscal
    year for the same member, carry the initiative across so pot-level
    analysis works on the adopted books too.
    """
    pairs = store.q("""
        SELECT DISTINCT t.org_key, t.fy, t.member, t.pot
        FROM funding t
        WHERE t.source_id = 'D49_MOCS_TRACKER' AND t.pot IS NOT NULL AND t.pot != ''
    """)
    n = 0
    with store.tx() as c:
        for p in pairs:
            cur = c.execute("""
                UPDATE funding SET pot = ?
                WHERE org_key = ? AND fy = ? AND member = ?
                  AND source_id = 'SCHEDULE_C'
                  AND (pot IS NULL OR pot IN ('Local', 'Citywide'))
            """, (p["pot"], p["org_key"], p["fy"], p["member"]))
            n += cur.rowcount
    store.journal("enrich.pots", {"updated": n})
    return n


# -------------------------------------------- the SI reconciliation ----
def ingest_si_rollup(store: Store, text: str, fy: int = 2027,
                     source_id: str = "SI_ROLLUP") -> dict:
    """Load every Staten Island funding line, post-Transparency-Resolution."""
    rows = list(rows_from_markdown(text))

    with store.tx() as c:
        c.execute("DELETE FROM funding WHERE source_id = ? AND fy = ?",
                  (source_id, fy))

    # --- part 1: the channel/pot rollup, kept as a reconciliation target ---
    rollup: list[dict] = []
    totals: dict[str, Any] = {}
    try:
        hdr_i, idx = find_header(rows, "channel", "pot", "adopted")
        for cells in rows[hdr_i + 1:]:
            ch = col(cells, idx, "channel")
            if not ch:
                continue
            if ch.upper().startswith("TOTAL"):
                totals = {
                    "lines": int(money(col(cells, idx, "lines")) or 0) or None,
                    "adopted": money(col(cells, idx, "adopted")),
                    "tr_movement": money(col(cells, idx, "tr movement")),
                    "note": col(cells, idx, "note") or None,
                }
                break
            rollup.append({
                "channel": ch,
                "pot": col(cells, idx, "pot"),
                "lines": money(col(cells, idx, "lines")),
                "adopted": money(col(cells, idx, "adopted")),
                "tr_movement": money(col(cells, idx, "tr movement")),
            })
    except ValueError:
        pass

    # --- part 2: the line-level detail -------------------------------------
    detail: list[dict] = []
    try:
        hdr_i, idx = find_header(rows, "organization", "ein", "amount")
        for ordinal, cells in enumerate(rows[hdr_i + 1:]):
            org = col(cells, idx, "organization")
            if not org or org.lower().startswith("channel"):
                continue
            amt = money(col(cells, idx, "amount"))
            if amt is None:
                continue
            channel = col(cells, idx, "channel")
            pot = col(cells, idx, "pot")
            ein = clean_ein(col(cells, idx, "ein"))
            flag = col(cells, idx, "flag", "d49 status")
            zm = ZIP_RE.search(" ".join(cells))
            is_capital = "capital" in (channel or "").lower()
            detail.append({
                "line_id": _lid("SI", fy, ordinal, channel, pot, org, amt),
                "fy": fy,
                "channel": "capital" if is_capital else "expense",
                "pot": pot or channel or None,
                "member": col(cells, idx, "sponsor", "attribution") or None,
                "person_id": None,
                "district": 49 if "IN D49" in flag.upper() else None,
                "borough": "Staten Island",
                "org": org,
                "org_key": org_key(org, ein),
                "ein": ein,
                "program": org.split(" - ")[1] if " - " in org else None,
                "agency": None,
                "amount": amt,
                "section": channel or None,
                "purpose": col(cells, idx, "note") or None,
                "status": ("TR-add" if "TR ADJUSTMENT" in (channel or "").upper()
                           else "adopted"),
                "mocs_id": None,
                "analyst": None,
                "in_d49": 1 if ("IN D49" in flag.upper()
                                or (zm and zm.group(1) in D49_ZIPS)) else 0,
                "pillar": pillar_for(org, pot, channel),
                "source_id": source_id,
                "locator": col(cells, idx, "source ref") or f"FY{fy} SI reconciliation",
            })
    except ValueError:
        pass

    n = store.upsert("funding", detail)
    store.set_meta(f"si_rollup.fy{fy}", {"rollup": rollup, "totals": totals,
                                         "lines": len(detail)})
    store.journal("ingest.si_rollup", {"rows": n, "rollup_pots": len(rollup),
                                       "totals": totals})
    return {"si_rows": n, "rollup_pots": len(rollup), "totals": totals}


def load_connector_json(path: Path | str) -> str:
    """Read a Drive-connector result file ({'fileContent': '...'})."""
    raw = json.loads(Path(path).read_text())
    return raw.get("fileContent", "") if isinstance(raw, dict) else str(raw)


# ------------------------------------------- the Transparency Reso ledger ----
# The office's own tier vocabulary. These are not decorative: a
# ADOPTED-PENDING-MOD line does not take effect until a budget modification
# passes, so it must never be mixed into a confirmed total, and a line
# REVERSED by a later resolution nets to zero.
TIERS = ("ADOPTED-IMPLEMENTATION", "ADOPTED-PENDING-MOD", "REVERSED", "CONTEXT")
CONFIRMED_TIER = "ADOPTED-IMPLEMENTATION"


def _tier_of(raw: str) -> str:
    up = (raw or "").upper()
    if "PENDING-MOD" in up or "PENDING MOD" in up:
        return "ADOPTED-PENDING-MOD"
    if "REVERSED" in up:
        return "REVERSED"
    if "CONTEXT" in up:
        return "CONTEXT"
    if "IMPLEMENTATION" in up:
        return "ADOPTED-IMPLEMENTATION"
    return up or "UNSPECIFIED"


def ingest_tr_ledger(store: Store, text: str, fy: int = 2027,
                     source_id: str = "SI_TR_LEDGER") -> dict:
    """Load the line-by-line Transparency Resolution ledger.

    Every Staten Island line moved by a Transparency Resolution, with the
    provenance tier that decides whether it counts. The ledger's own reading
    is the analytically important part and is preserved as metadata: the
    Speaker block nets to zero only because a *pending* line offsets confirmed
    rescissions, so the confirmed Speaker channel is down even though the
    arithmetic looks flat.
    """
    rows = list(rows_from_markdown(text))
    try:
        hdr_i, idx = find_header(rows, "reso", "initiative", "organization",
                                 "amount")
    except ValueError:
        return {"tr_ledger_rows": 0, "error": "header not found"}

    with store.tx() as c:
        c.execute("DELETE FROM funding WHERE source_id = ? AND fy = ?",
                  (source_id, fy))

    out: list[dict] = []
    total_row: dict | None = None
    for ordinal, cells in enumerate(rows[hdr_i + 1:]):
        reso = col(cells, idx, "reso")
        if not reso:
            continue
        if reso.upper().startswith("TOTAL"):
            total_row = {"net": money(col(cells, idx, "amount")),
                         "label": col(cells, idx, "initiative")}
            continue

        org = col(cells, idx, "organization")
        if not org:
            continue
        amt = money(col(cells, idx, "amount"))
        if amt is None:
            continue
        ein = clean_ein(col(cells, idx, "ein"))
        tier = _tier_of(col(cells, idx, "tier"))
        d49 = col(cells, idx, "d49 status").upper()
        initiative = col(cells, idx, "initiative")
        body = col(cells, idx, "member/body", "member")
        out.append({
            "line_id": _lid("TRL", fy, reso, ordinal, org, amt, initiative),
            "fy": fy,
            "channel": "expense",
            "pot": initiative or None,
            "member": body or None,
            "person_id": None,
            "district": 49 if d49.startswith("IN D49") else None,
            "borough": "Staten Island",
            "org": org,
            "org_key": org_key(org, ein),
            "ein": ein,
            "program": org.split(" — ")[1] if " — " in org else None,
            "agency": col(cells, idx, "agy", "agency") or None,
            "amount": amt,
            "section": f"Transparency Reso {reso}",
            "purpose": col(cells, idx, "note / effect", "note") or None,
            "status": "TR-add" if amt > 0 else "TR-cut" if amt < 0 else "TR-neutral",
            "tier": tier,
            "reso": reso,
            "mocs_id": None,
            "analyst": None,
            "in_d49": 1 if d49.startswith("IN D49") or d49.startswith("TO D49") else 0,
            "pillar": pillar_for(org, initiative),
            "source_id": source_id,
            "locator": f"FY{fy} Transparency Reso {reso}, chart "
                       f"{col(cells, idx, 'chart')}",
            "updated": None,
        })

    n = store.upsert("funding", out)
    by_tier: dict[str, dict] = {}
    for r in out:
        slot = by_tier.setdefault(r["tier"], {"lines": 0, "amount": 0.0})
        slot["lines"] += 1
        slot["amount"] += r["amount"] or 0
    store.set_meta(f"tr_ledger.fy{fy}", {
        "lines": n, "by_tier": by_tier, "stated_net": (total_row or {}).get("net"),
        "confirmed_net": by_tier.get(CONFIRMED_TIER, {}).get("amount"),
        "reading": [c for c in (rows[-1] if rows else []) if len(c) > 120][:1],
    })
    store.journal("ingest.tr_ledger", {"rows": n, "by_tier": by_tier})
    return {"tr_ledger_rows": n, "by_tier": by_tier,
            "stated_net": (total_row or {}).get("net")}


def ingest_channel_rollup(store: Store, text: str, fy: int = 2027,
                          source_id: str = "SI_ROLLUP") -> dict:
    """Load the channel/pot rollup, keeping capital and expense separate.

    The office's rule, stated on the sheet itself: capital and expense are
    separate measures and are never merged. The grand total exists only as two
    components shown side by side, and any analysis that adds them is wrong.
    """
    rows = list(rows_from_markdown(text))
    try:
        hdr_i, idx = find_header(rows, "channel", "pot", "lines", "adopted")
    except ValueError:
        return {"rollup_pots": 0, "error": "header not found"}

    pots: list[dict] = []
    subtotals: dict[str, dict] = {}
    grand: dict | None = None
    for cells in rows[hdr_i + 1:]:
        ch = col(cells, idx, "channel")
        if not ch:
            continue
        rec = {"channel": ch, "pot": col(cells, idx, "pot / initiative", "pot"),
               "lines": int(money(col(cells, idx, "lines")) or 0),
               "adopted": money(col(cells, idx, "adopted")) or 0.0,
               "tr_movement": money(col(cells, idx, "tr movement")) or 0.0}
        up = ch.upper()
        if up.startswith("GRAND"):
            grand = rec
        elif "SUBTOTAL" in up:
            subtotals[up.replace(" SUBTOTAL", "").strip()] = rec
        else:
            pots.append(rec)

    capital = [p for p in pots if p["channel"].upper().startswith("CAPITAL")]
    expense = [p for p in pots if not p["channel"].upper().startswith("CAPITAL")]

    def agg(rows_: list[dict]) -> dict:
        return {"lines": sum(r["lines"] for r in rows_),
                "adopted": round(sum(r["adopted"] for r in rows_), 2),
                "tr_movement": round(sum(r["tr_movement"] for r in rows_), 2)}

    payload = {
        "pots": pots,
        "capital": {**agg(capital), "pots": capital},
        "expense": {**agg(expense), "pots": expense},
        "stated_subtotals": subtotals,
        "grand": grand,
        "rule": ("Capital and expense are separate measures and are never "
                 "merged. The grand total exists only as two components."),
    }
    store.set_meta(f"si_channel_rollup.fy{fy}", payload)
    store.journal("ingest.channel_rollup",
                  {"pots": len(pots), "capital": payload["capital"]["adopted"],
                   "expense": payload["expense"]["adopted"]})
    return {"rollup_pots": len(pots),
            "capital": payload["capital"], "expense": payload["expense"]}
