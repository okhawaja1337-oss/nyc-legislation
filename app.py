#!/usr/bin/env python3
"""
legistar_sync.py  —  NYC Council legislation tracker (1:1 with Legistar)

Pulls legislation directly from the NYC Legistar Web API (the same data store the
legistar.council.nyc.gov pages render from). Detects what changed since the last run.
Optionally adds AI-drafted impact bullets (cached so re-runs only pay for new/changed bills).

  DATA layer  (exact, from API): Matters, Sponsors, Histories, Attachments, Text.
  ANALYSIS layer (interpretive, labeled): keyword tags and/or AI-drafted impact bullets.

CLI runs ONE profile. For multiple scoped workbooks on a schedule, use run_sync.py + config.json.

  python3 legistar_sync.py --file "Int 0225-2026" --enrich --out int225.xlsx
  python3 legistar_sync.py --since 2024-01-01 --sponsor Hanks --enrich --text --impact ai --out hanks.xlsx
"""

import argparse, hashlib, json, os, re, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None  # allowed; only needed for live runs, not offline tests

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

API_BASE = "https://webapi.legistar.com/v1/nyc"
# Verified-stable public link: the gateway 302-redirects to the correct LegislationDetail page.
WEB_BASE = "https://legistar.council.nyc.gov/gateway.aspx?m=l&id="
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
PAGE = 1000


def _ver_rank(v):
    """NYC matter versions are '*' (introduced) then 'A','B',...  Rank '*' lowest, letters ascending.
    (The stock scraper assumes integer versions and would crash on NYC's letters.)"""
    if v is None:
        return (-1, "")
    return (0, "") if str(v) == "*" else (1, str(v))


# ============================================================================
# DATA LAYER — Legistar Web API client
# ============================================================================
class LegistarClient:
    def __init__(self, token=None, pause=0.0):
        self.token = token or os.environ.get("LEGISTAR_TOKEN")
        self.pause = pause
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json"})

    def _get(self, path, params=None):
        params = dict(params or {})
        if self.token:
            params["token"] = self.token
        last_err = None
        for attempt in range(6):
            try:
                r = self.s.get(f"{API_BASE}/{path}", params=params, timeout=90)
            except requests.exceptions.RequestException as e:
                last_err = e
                time.sleep(min(2 ** attempt, 20))  # ride out dropped connections
                continue
            if r.status_code == 200:
                if self.pause:
                    time.sleep(self.pause)
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 20)); continue
            r.raise_for_status()
        if last_err:
            raise last_err
        r.raise_for_status()

    def matters(self, odata_filter=None, top=PAGE):
        # Page WITHOUT a volatile $orderby and de-dupe by MatterId, exactly as the
        # production NYC scraper does: ordering by a non-unique field while paging by
        # $skip can drop/duplicate rows at page boundaries.
        out, seen, skip = [], set(), 0
        while True:
            params = {"$top": top, "$skip": skip}
            if odata_filter:
                params["$filter"] = odata_filter
            batch = self._get("matters", params)
            for m in batch:
                mid = m.get("MatterId")
                if mid not in seen:
                    seen.add(mid)
                    out.append(m)
            if len(batch) < top:
                return out
            skip += top

    def sponsors(self, mid):     return self._get(f"matters/{mid}/sponsors")
    def histories(self, mid):    return self._get(f"matters/{mid}/histories")
    def attachments(self, mid):  return self._get(f"matters/{mid}/attachments")

    def text_plain(self, mid, matter_version=None):
        # Verified shape: /matters/{id}/versions -> [{"Key":<text id>, "Value":<'*'|'A'|...>}],
        # pick the version whose Value == the matter's MatterVersion (else highest), then
        # /matters/{id}/texts/{Key} -> MatterTextPlain.
        try:
            versions = self._get(f"matters/{mid}/versions")
        except Exception:
            return ""
        if not versions:
            return ""
        chosen = None
        if matter_version is not None:
            chosen = next((v for v in versions if str(v.get("Value")) == str(matter_version)), None)
        if chosen is None:
            chosen = max(versions, key=lambda v: _ver_rank(v.get("Value")))
        try:
            t = self._get(f"matters/{mid}/texts/{chosen.get('Key')}")
            if isinstance(t, list):
                t = t[-1] if t else {}
            return (t or {}).get("MatterTextPlain") or ""
        except Exception:
            return ""


# ============================================================================
# DATA LAYER — pure transforms (no network; unit-testable)
# ============================================================================
def web_url(m):
    return f"{WEB_BASE}{m.get('MatterId')}"


def current_sponsors(matter, sponsor_json):
    # Mirror the production scraper: keep sponsors at the latest version, sorted by sequence.
    # Prefer the matter's MatterVersion; else the highest sponsor version (NYC-safe rank).
    rows = [s for s in (sponsor_json or []) if (s.get("MatterSponsorName") or "").strip()]
    if not rows:
        return []
    target = matter.get("MatterVersion")
    present = {s.get("MatterSponsorMatterVersion") for s in rows}
    if target not in present:
        target = max(present, key=_ver_rank)
    scoped = [s for s in rows if s.get("MatterSponsorMatterVersion") == target] or rows
    by_name = {}
    for s in scoped:
        name = s["MatterSponsorName"].strip()
        seq = s.get("MatterSponsorSequence", 9999)
        if name not in by_name or seq < by_name[name].get("MatterSponsorSequence", 9999):
            by_name[name] = s
    return sorted(by_name.values(), key=lambda s: s.get("MatterSponsorSequence", 9999))


def _date(s):
    if not s:
        return ""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")).strftime("%-m/%-d/%Y")
    except Exception:
        return str(s)[:10]


def normalize_matter(m, sponsors=None, histories=None, attachments=None):
    sp = current_sponsors(m, sponsors) if sponsors is not None else None
    prime = next((s["MatterSponsorName"] for s in (sp or []) if s.get("MatterSponsorSequence") == 0), "")
    if not prime and sp:
        prime = sp[0]["MatterSponsorName"]
    names = [s["MatterSponsorName"] for s in (sp or [])]
    latest = None
    if histories:
        dated = [h for h in histories if h.get("MatterHistoryActionDate")]
        latest = max(dated, key=lambda h: h["MatterHistoryActionDate"], default=None)
    return {
        "File": m.get("MatterFile", ""), "Type": m.get("MatterTypeName", ""),
        "Name": m.get("MatterName", ""), "Title": m.get("MatterTitle", ""),
        "Status": m.get("MatterStatusName", ""), "Committee/Body": m.get("MatterBodyName", ""),
        "Intro Date": _date(m.get("MatterIntroDate")), "Agenda Date": _date(m.get("MatterAgendaDate")),
        "Passed Date": _date(m.get("MatterPassedDate")), "Enacted Date": _date(m.get("MatterEnactmentDate")),
        "Law #": m.get("MatterEnactmentNumber", ""),
        "Sponsors (#)": len(names) if sp is not None else "",
        "Prime Sponsor": prime,
        "Hanks?": "Y" if any("Hanks" in n for n in names) else ("" if sp is not None else "?"),
        "Latest Action": (latest or {}).get("MatterHistoryActionName", ""),
        "Latest Action Date": _date((latest or {}).get("MatterHistoryActionDate")),
        "Attachments (#)": len(attachments) if attachments is not None else "",
        "Last Modified (UTC)": m.get("MatterLastModifiedUtc", ""),
        "MatterId": m.get("MatterId", ""), "Web Link": web_url(m),
        "_sponsor_names": names, "_prime": prime,
        "_sponsor_objs": sp or [], "_status_raw": m.get("MatterStatusName", ""),
    }


# ============================================================================
# ANALYSIS LAYER (1) — heuristic keyword tagging (free, transparent)
# ============================================================================
PILLARS = {
    "Arts & Culture": ["arts", "cultural", "museum", "artist", "theater", "library", "heritage", "public art", "DCLA", "humanities"],
    "Neighborhood Dev": ["zoning", "land use", "rezon", "ULURP", "housing", "landmark", "preservation", "neighborhood", "construction", "building", "tenant"],
    "Economic Dev": ["economic", "small business", "commercial", "workforce", "employment", "job", "tourism", "EDC", "tax credit", "women-owned"],
    "Public Safety/Crisis": ["NYPD", "police", "FDNY", "fire", "emergency", "crime", "traffic", "tow", "911", "B-HEARD", "shelter", "flood", "storm"],
    "Health & Hospitals": ["health", "hospital", "H+H", "clinic", "medical", "DOHMH", "mental health", "substance", "care", "wellness"],
}
SI_TERMS = ["staten island", "north shore", "verrazzano", "district 49", "st. george", "stapleton",
            "port richmond", "tompkinsville", "ferry", "tow pound", "borough of richmond"]
BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]


def keyword_tags(row, text=""):
    blob = " ".join([row.get("Title", ""), row.get("Name", ""), text]).lower()
    pillars = [p for p, kws in PILLARS.items() if any(k.lower() in blob for k in kws)]
    si = [t for t in SI_TERMS if t in blob]
    bor = [b for b in BOROUGHS if b.lower() in blob]
    return {"Pillar Tags": "; ".join(pillars),
            "SI/D-49 Signal": f"{len(si)} ({', '.join(si)})" if si else "0",
            "Boroughs Named": "; ".join(bor) or "(citywide / none named)"}


# ============================================================================
# ANALYSIS LAYER (2) — AI-drafted impact bullets (Anthropic API, cached)
# ============================================================================
PROMPT = """You are a legislative analyst for NYC Council Member Kamillah Hanks (District 49, \
North Shore Staten Island; majority whip). Given a NYC Council bill's file number, title, and text, \
write a tight impact read. Ground every statement ONLY in the provided text; do not invent specifics.

Return ONLY a JSON object with keys "purpose", "affects", "d49":
- purpose: <=25 words, plain language, what the bill actually does.
- affects: <=30 words, who/what across the city it touches and how.
- d49: <=35 words, the Staten Island / District 49 angle tied to CM Hanks's pillars (Arts & Culture, \
Neighborhood Development, Economic Development, Public Safety/Crisis, Health & Hospitals, SI parity/independence). \
If the bill has little direct D-49 nexus, say so plainly rather than stretching.

File: {file}
Type: {type}
Title: {title}
Text: {text}

JSON:"""


def text_hash(*parts):
    return hashlib.sha1("||".join(p or "" for p in parts).encode("utf-8", "ignore")).hexdigest()[:16]


class AIImpact:
    def __init__(self, model, api_key=None, pause=0.0):
        self.model = model
        self.key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.pause = pause
        self.s = requests.Session() if requests else None

    def draft(self, row, text):
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        prompt = PROMPT.format(file=row.get("File", ""), type=row.get("Type", ""),
                               title=row.get("Title", ""), text=(text or row.get("Name", ""))[:6000])
        body = {"model": self.model, "max_tokens": 320,
                "messages": [{"role": "user", "content": prompt}]}
        r = self.s.post(ANTHROPIC_URL, headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json=body, timeout=90)
        r.raise_for_status()
        data = r.json()
        txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if self.pause:
            time.sleep(self.pause)
        return parse_bullets(txt)


def parse_bullets(txt):
    s = re.sub(r"^```(json)?|```$", "", (txt or "").strip(), flags=re.MULTILINE).strip()
    try:
        d = json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
    return {"purpose": d.get("purpose", ""), "affects": d.get("affects", ""), "d49": d.get("d49", "")}


# ============================================================================
# CHANGE DETECTION + caches — local snapshot
# ============================================================================
def fingerprint(row):
    return {"File": row["File"], "Status": row["Status"], "SponsorCount": row["Sponsors (#)"],
            "Sponsors": sorted(row.get("_sponsor_names", [])), "Prime": row.get("_prime", ""),
            "Attachments": row["Attachments (#)"], "LatestAction": row["Latest Action"],
            "LastModified": row["Last Modified (UTC)"]}


class Snapshot:
    def __init__(self, path="legistar_state.db"):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (matter_id INTEGER PRIMARY KEY, fp TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS ai (matter_id INTEGER, thash TEXT, model TEXT, "
                        "bullets TEXT, created TEXT, PRIMARY KEY (matter_id, thash, model))")
        self.db.commit()

    def load(self):
        return {mid: json.loads(fp) for mid, fp in self.db.execute("SELECT matter_id, fp FROM state")}

    def save(self, rows):
        self.db.executemany("INSERT OR REPLACE INTO state VALUES (?,?)",
                            [(r["MatterId"], json.dumps(fingerprint(r))) for r in rows])
        self.db.commit()

    def ai_get(self, mid, thash, model):
        row = self.db.execute("SELECT bullets FROM ai WHERE matter_id=? AND thash=? AND model=?",
                              (mid, thash, model)).fetchone()
        return json.loads(row[0]) if row else None

    def ai_put(self, mid, thash, model, bullets):
        self.db.execute("INSERT OR REPLACE INTO ai VALUES (?,?,?,?,?)",
                        (mid, thash, model, json.dumps(bullets),
                         datetime.now(timezone.utc).isoformat()))
        self.db.commit()


def diff(old, rows):
    changes = []
    for r in rows:
        mid, new = r["MatterId"], fingerprint(r)
        prev = old.get(mid)
        if prev is None:
            changes.append((r["File"], "NEW", "—", "—", f"{r['Type']}: {r['Title'][:80]}")); continue
        if prev["Status"] != new["Status"]:
            changes.append((r["File"], "STATUS", "Status", prev["Status"], new["Status"]))
        added = set(new["Sponsors"]) - set(prev["Sponsors"])
        dropped = set(prev["Sponsors"]) - set(new["Sponsors"])
        if added:
            changes.append((r["File"], "SPONSOR +", "Sponsors", f'{prev["SponsorCount"]} sponsors', "; ".join(sorted(added))))
        if dropped:
            changes.append((r["File"], "SPONSOR -", "Sponsors", "; ".join(sorted(dropped)), f'now {new["SponsorCount"]}'))
        if prev["Prime"] != new["Prime"]:
            changes.append((r["File"], "PRIME", "Prime Sponsor", prev["Prime"], new["Prime"]))
        if prev["LatestAction"] != new["LatestAction"]:
            changes.append((r["File"], "ACTION", "Latest Action", prev["LatestAction"], new["LatestAction"]))
        if prev["Attachments"] != new["Attachments"]:
            changes.append((r["File"], "ATTACH", "Attachments (#)", prev["Attachments"], new["Attachments"]))
    return changes


# ============================================================================
# OUTPUT — house-style workbook
# ============================================================================
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
BODY = Font(name="Arial", size=10)
AI_FONT = Font(name="Arial", size=10, italic=True)
CHANGE_FILL = PatternFill("solid", fgColor="FFF2CC")
AI_FILL = PatternFill("solid", fgColor="EAF1FB")
THIN = Border(*(Side(style="thin", color="D9D9D9"),) * 4)


def _sheet(wb, title, headers, rows, widths=None, fill=None, font=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(1, c); cell.font, cell.fill = HDR_FONT, HDR_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=len(headers)):
        for cell in row:
            cell.font = font or BODY; cell.border = THIN
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if fill:
                cell.fill = fill
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    for i, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    return ws


def build_workbook(bundle, path):
    rows = bundle["rows"]
    sponsors_map, histories_map = bundle["sponsors_map"], bundle["histories_map"]
    attach_map, text_map = bundle["attach_map"], bundle["text_map"]
    ai_map, changes, run_info = bundle["ai_map"], bundle["changes"], bundle["run_info"]
    impact_mode = bundle["impact_mode"]

    wb = Workbook(); wb.remove(wb.active)

    cols = ["File", "Type", "Title", "Status", "Committee/Body", "Intro Date", "Latest Action",
            "Latest Action Date", "Sponsors (#)", "Prime Sponsor", "Hanks?", "Law #",
            "Last Modified (UTC)", "MatterId", "Web Link"]
    add_kw = impact_mode in ("keyword", "ai")
    if add_kw:
        cols += ["Pillar Tags", "SI/D-49 Signal", "Boroughs Named"]
    body = []
    for r in rows:
        line = [r.get(c, "") for c in cols if c not in ("Pillar Tags", "SI/D-49 Signal", "Boroughs Named")]
        if add_kw:
            t = keyword_tags(r, (text_map.get(r["MatterId"], "") or "")[:4000])
            line += [t["Pillar Tags"], t["SI/D-49 Signal"], t["Boroughs Named"]]
        body.append(line)
    _sheet(wb, "Matters", cols, body,
           widths=[16, 15, 58, 15, 24, 11, 26, 12, 11, 22, 7, 9, 22, 10, 50] + ([22, 26, 22] if add_kw else []))

    if impact_mode == "ai":
        ai_rows = []
        for r in rows:
            b = ai_map.get(r["MatterId"])
            if b:
                ai_rows.append([r["File"], r["Title"], b.get("purpose", ""), b.get("affects", ""),
                                b.get("d49", ""), run_info.get("AI model", "")])
        _sheet(wb, "Impact (AI-drafted)",
               ["File", "Title", "Purpose", "Who it affects", "D-49 angle", "Model"],
               ai_rows, widths=[16, 46, 40, 40, 44, 22], fill=AI_FILL, font=AI_FONT)

    sp_rows = []
    for r in rows:
        for s in r.get("_sponsor_objs", []):
            seq = s.get("MatterSponsorSequence", "")
            sp_rows.append([r["File"], r["Type"], s.get("MatterSponsorName", ""),
                            seq, "Prime" if seq == 0 else "Co-sponsor", r["MatterId"]])
    _sheet(wb, "Sponsors", ["File", "Type", "Sponsor", "Seq", "Role", "MatterId"], sp_rows,
           widths=[16, 15, 28, 6, 12, 10])

    h_rows = []
    for r in rows:
        seen_h = set()
        for h in sorted(histories_map.get(r["MatterId"], []), key=lambda x: x.get("MatterHistoryActionDate") or ""):
            d = h.get("MatterHistoryActionDate")
            a = (h.get("MatterHistoryActionName") or "").strip()
            b = h.get("MatterHistoryActionBodyName") or ""
            if not d or not a:
                continue
            k = (d, a, b)
            if k in seen_h:  # duplicate data-entry rows happen; drop them
                continue
            seen_h.add(k)
            h_rows.append([r["File"], _date(d), a, b, h.get("MatterHistoryPassedFlagName", "")])
    _sheet(wb, "History", ["File", "Action Date", "Action", "Action By", "Result"], h_rows,
           widths=[16, 12, 40, 30, 12])

    a_rows = []
    for r in rows:
        for a in attach_map.get(r["MatterId"], []):
            a_rows.append([r["File"], a.get("MatterAttachmentName", ""), a.get("MatterAttachmentHyperlink", "")])
    _sheet(wb, "Attachments", ["File", "Attachment", "Link"], a_rows, widths=[16, 40, 70])

    if text_map:
        t_rows = [[r["File"], r["Title"], (text_map.get(r["MatterId"], "") or "")[:30000]]
                  for r in rows if text_map.get(r["MatterId"])]
        _sheet(wb, "Bill Text", ["File", "Title", "Plain Text (truncated 30k)"], t_rows,
               widths=[16, 50, 120])

    hdr = ["File", "Change", "Field", "Was", "Now"]
    if changes:
        _sheet(wb, "Changes Since Last Sync", hdr, changes, widths=[16, 12, 16, 40, 40], fill=CHANGE_FILL)
    else:
        msg = run_info.get("Changes placeholder", "(no changes since last sync)")
        _sheet(wb, "Changes Since Last Sync", hdr, [[msg, "", "", "", ""]], widths=[16, 12, 16, 40, 40])

    _sheet(wb, "Run Info", ["Field", "Value"], [[k, v] for k, v in run_info.items()], widths=[26, 80])
    wb.move_sheet("Matters", -(len(wb.sheetnames) - 1))
    wb.save(path)
    return path


# ============================================================================
# ORCHESTRATION — shared by CLI and the scheduled runner
# ============================================================================
def assemble(client, ai, snap, old, profile):
    """Run one profile. Returns a bundle dict ready for build_workbook. Does NOT save snapshot."""
    f = profile.get("filter", {})
    clauses = []
    if f.get("file"):  clauses.append(f"MatterFile eq '{f['file']}'")
    if f.get("since"): clauses.append(f"MatterIntroDate ge datetime'{f['since']}'")
    if f.get("until"): clauses.append(f"MatterIntroDate lt datetime'{f['until']}'")
    if f.get("type"):  clauses.append(f"MatterTypeName eq '{f['type']}'")
    odata = " and ".join(clauses) or None

    raw = client.matters(odata)
    raw.sort(key=lambda m: m.get("MatterIntroDate") or "", reverse=True)  # safe in-memory display order
    if profile.get("limit"):
        raw = raw[: profile["limit"]]

    enrich = profile.get("enrich", False)
    enrich_status = set(profile.get("enrich_status") or [])  # empty => enrich all
    want_text = profile.get("text", False)
    workers = profile.get("workers", 4)
    sponsor_filter = (f.get("sponsor") or "").lower()

    sponsors_map, histories_map, attach_map, text_map = {}, {}, {}, {}

    def _pool(items, fn, label):
        total = len(items)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(fn, m) for m in items]):
                fut.result()
                done += 1
                if total > 40 and done % 25 == 0:
                    print(f"   {label}: {done}/{total}")

    # Phase A — sponsor scan (cheap: 1 call/matter) only when filtering by sponsor
    if sponsor_filter:
        print(f"Scanning {len(raw)} bills for sponsor '{f['sponsor']}' — this is the slow part, please wait...")
        _pool(raw, lambda m: sponsors_map.__setitem__(m["MatterId"], client.sponsors(m["MatterId"])), "scanned")
        raw = [m for m in raw
               if any(sponsor_filter in (s.get("MatterSponsorName") or "").lower()
                      for s in current_sponsors(m, sponsors_map.get(m["MatterId"], [])))]
        print(f"   matched {len(raw)} bills sponsored by '{f['sponsor']}'")

    # Phase B — heavy detail (histories/attachments/text) only on the matters we keep
    if enrich:
        targets = [m for m in raw if not enrich_status or m.get("MatterStatusName") in enrich_status]

        def heavy(m):
            mid = m["MatterId"]
            if mid not in sponsors_map:
                sponsors_map[mid] = client.sponsors(mid)
            histories_map[mid] = client.histories(mid)
            attach_map[mid] = client.attachments(mid)
            if want_text:
                tx = client.text_plain(mid, m.get("MatterVersion"))
                if tx:
                    text_map[mid] = tx

        if targets:
            print(f"Pulling full details for {len(targets)} bills...")
            _pool(targets, heavy, "details")

    rows = []
    for m in raw:
        mid = m["MatterId"]
        rows.append(normalize_matter(
            m,
            sponsors_map.get(mid) if mid in sponsors_map else None,
            histories_map.get(mid),
            attach_map.get(mid) if mid in attach_map else None))

    impact_mode = profile.get("impact", "none")
    ai_map = {}
    ai_drafted = ai_cached = 0
    if impact_mode == "ai" and ai is not None:
        cap = profile.get("ai_max_bills", 0)
        consider = rows[:cap] if cap else rows
        for r in consider:
            mid = r["MatterId"]
            th = text_hash(r.get("Title", ""), text_map.get(mid, ""))
            cached = snap.ai_get(mid, th, ai.model)
            if cached:
                ai_map[mid] = cached; ai_cached += 1; continue
            try:
                b = ai.draft(r, text_map.get(mid, ""))
                snap.ai_put(mid, th, ai.model, b); ai_map[mid] = b; ai_drafted += 1
            except Exception as e:
                ai_map[mid] = {"purpose": f"[AI error: {e}]", "affects": "", "d49": ""}

    changes = diff(old, rows) if old else []
    run_info = {
        "Profile": profile.get("name", "(cli)"),
        "Run timestamp (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "API base": API_BASE, "Filter": odata or "(none)",
        "Sponsor filter": f.get("sponsor", "(none)"),
        "Enrich": enrich, "Enrich status gate": ", ".join(enrich_status) or "(all)",
        "Full text pulled": bool(text_map),
        "Impact mode": impact_mode, "AI model": (ai.model if (impact_mode == "ai" and ai) else "(n/a)"),
        "AI drafted this run": ai_drafted, "AI reused from cache": ai_cached,
        "Matters in workbook": len(rows), "Prior snapshot matters": len(old),
        "Changes detected": len(changes), "Token used": bool(getattr(client, "token", None)),
    }
    return {"rows": rows, "sponsors_map": sponsors_map, "histories_map": histories_map,
            "attach_map": attach_map, "text_map": text_map, "ai_map": ai_map,
            "changes": changes, "run_info": run_info, "impact_mode": impact_mode}


def main():
    ap = argparse.ArgumentParser(description="Sync NYC Council legislation 1:1 from Legistar (single profile).")
    ap.add_argument("--file"); ap.add_argument("--since"); ap.add_argument("--until")
    ap.add_argument("--type"); ap.add_argument("--sponsor")
    ap.add_argument("--enrich", action="store_true")
    ap.add_argument("--enrich-status", help="Comma list, e.g. Committee — enrich only these statuses")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--impact", choices=["none", "keyword", "ai"], default="none")
    ap.add_argument("--ai-model", default=os.environ.get("LEGISTAR_AI_MODEL", "claude-haiku-4-5-20251001"))
    ap.add_argument("--limit", type=int); ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--state", default="legistar_state.db"); ap.add_argument("--out", default="legislation.xlsx")
    a = ap.parse_args()

    client = LegistarClient()
    snap = Snapshot(a.state)
    old = snap.load()
    ai = AIImpact(a.ai_model) if a.impact == "ai" else None
    profile = {"name": "cli", "filter": {k: getattr(a, k) for k in ("file", "since", "until", "type", "sponsor") if getattr(a, k)},
               "enrich": a.enrich, "enrich_status": (a.enrich_status.split(",") if a.enrich_status else None),
               "text": a.text, "impact": a.impact, "limit": a.limit, "workers": a.workers}
    print(f"[*] {profile['filter'] or '(no filter)'}  impact={a.impact}")
    bundle = assemble(client, ai, snap, old, profile)
    snap.save(bundle["rows"])
    build_workbook(bundle, a.out)
    ri = bundle["run_info"]
    print(f"[*] {ri['Matters in workbook']} matters, {ri['Changes detected']} changes, "
          f"AI drafted {ri['AI drafted this run']} / reused {ri['AI reused from cache']} -> {a.out}")


# ===========================================================================
# WEBSITE UI (Streamlit) — this is what visitors see and click
# ===========================================================================
import streamlit as st
import pandas as pd

st.set_page_config(page_title="NYC Council Legislation — live", layout="wide")
st.title("🗽 NYC Council Legislation — live")
st.caption("Pulls current data straight from NYC's Legistar API. Always up to date.")

NYC_TOKEN = "Uvxb0j9syjm3aI8h46DhQvnX5skN4aSUL0x_Ee3ty9M.ew0KICAiVmVyc2lvbiI6IDEsDQogICJOYW1lIjogIk5ZQyByZWFkIHRva2VuIDIwMTcxMDI2IiwNCiAgIkRhdGUiOiAiMjAxNy0xMC0yNlQxNjoyNjo1Mi42ODM0MDYtMDU6MDoiLA0KICAiV3JpdGUiOiBmYWxzZQ0KfQ"

with st.form("options"):
    what = st.selectbox(
        "What do you want?",
        ["One specific bill", "Just Council Member Hanks's bills", "Everything introduced this session"])
    col1, col2 = st.columns(2)
    bill_number = col1.text_input("Bill number (for 'One specific bill')", "Int 0225-2026")
    since_date = col2.text_input("Bills introduced on/after (YYYY-MM-DD)", "2024-01-01")
    add_ai = st.checkbox("Add AI impact bullets (needs your Anthropic key)", value=False)
    anthropic_key = st.text_input("Anthropic API key (optional)", "", type="password")
    go = st.form_submit_button("Get legislation ▶")

st.info("Single bill = a few seconds. \"Hanks's bills\" and \"Everything\" scan the whole city and take a few minutes.")

@st.cache_data(ttl=1800, show_spinner=False)
def run_pull(what, bill_number, since_date, add_ai, anthropic_key):
    profile = {"name": "web", "filter": {}, "enrich": True, "text": True, "workers": 3}
    if what == "Just Council Member Hanks's bills":
        profile["filter"] = {"since": since_date, "sponsor": "Hanks"}
    elif what == "Everything introduced this session":
        profile["filter"] = {"since": since_date}; profile["text"] = False
    else:
        profile["filter"] = {"file": bill_number.strip()}
    profile["impact"] = "ai" if (add_ai and anthropic_key.strip()) else "keyword"
    if add_ai and anthropic_key.strip():
        os.environ["ANTHROPIC_API_KEY"] = anthropic_key.strip()
    client = LegistarClient(token=NYC_TOKEN, pause=0.2)
    snap = Snapshot("/tmp/legistar_state.db")
    old = snap.load()
    ai = AIImpact("claude-haiku-4-5-20251001") if profile["impact"] == "ai" else None
    bundle = assemble(client, ai, snap, old, profile)
    snap.save(bundle["rows"])
    build_workbook(bundle, "/tmp/legislation.xlsx")
    return bundle

if go:
    with st.spinner("Working… please wait."):
        bundle = run_pull(what, bill_number, since_date, add_ai, anthropic_key)
    rows = bundle["rows"]
    if not rows:
        st.warning("No bills matched. Try a wider date or check the bill number.")
    else:
        st.success(f"Found {len(rows)} bills.")
        df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
        st.dataframe(df, use_container_width=True, height=500)
        with open("/tmp/legislation.xlsx", "rb") as fh:
            st.download_button("⬇️ Download as Excel", fh.read(), "NYC_Council_legislation.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
