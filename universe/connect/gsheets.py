#!/usr/bin/env python3
"""
The Google Sheets connector.

Three of the office's books live in Sheets and change during budget season:
the MOCS discretionary tracker, the Staten Island rollup, and the Transparency
Resolution ledger. Re-exporting them by hand is how a briefing ends up quoting
last week's number, so this pulls them on demand and on a timer.

Transports, cheapest first:

  1. ``csv``     -- the /export?format=csv endpoint. Works on any sheet shared
                    "anyone with the link can view". No credential at all.
  2. ``gviz``    -- the visualisation endpoint, which accepts a sheet *name*
                    where the CSV export wants a numeric gid.
  3. ``api_key`` -- Sheets API v4. Public sheets only.
  4. ``oauth``   -- Sheets API v4 with a bearer token. Private sheets.

Everything converges on the same pipe-table text the ingesters already read
and already have tests for. The connector's job is transport, not parsing: it
does not get to invent a second way of reading a budget line.
"""
from __future__ import annotations

import csv as csvmod
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..core import keys
from ..core.store import Store
from ..ingest import sheets as sheet_ingest

UA = "D49-Universe/1.0 (+council.nyc.gov District 49)"
EXPORT = "https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
GVIZ = "https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&sheet={name}"
API = "https://sheets.googleapis.com/v4/spreadsheets"
TIMEOUT = 45

CONNECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS ws_connections (
  id TEXT PRIMARY KEY,
  kind TEXT,              -- sheet | calendar | channel | feed
  label TEXT,
  role TEXT,              -- which ingester consumes it
  target TEXT,            -- spreadsheet id, calendar id, channel id
  detail TEXT,            -- JSON: gid, sheet name, options
  enabled INTEGER DEFAULT 1,
  last_sync TEXT,
  last_status TEXT,
  last_rows INTEGER,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_ws_conn_kind ON ws_connections(kind, enabled);
"""

# The books the office already keeps. `role` names the ingester that owns each.
D49_SHEETS: tuple[dict, ...] = (
    {"id": "sheet:mocs", "label": "D49 MOCS discretionary tracker",
     "role": "mocs_tracker", "target": "1F_JSeKkjRTDyCRut8KhzZwfQOvG2yHzdWb5K8iJwKjw",
     "detail": {"gid": "0"}},
    {"id": "sheet:si_rollup", "label": "Staten Island funding rollup",
     "role": "si_rollup", "target": "1LDg7pweIf8j62Ma1RZTNu6Blnc9HMtyyrivVUq2Ljbw",
     "detail": {"gid": "0", "fy": 2027}},
    {"id": "sheet:tr_ledger", "label": "Transparency Resolution ledger",
     "role": "tr_ledger", "target": "1hIzjtujWqhfOr1WZ0fbytLcKNn_oPQuzcGFwHLqm0xU",
     "detail": {"gid": "2144202155", "fy": 2027}},
)

# role -> the ingester it feeds. Adding a book means adding a row above and a
# line here; it must never mean a new parser.
INGESTERS: dict[str, Callable[..., dict]] = {
    "mocs_tracker": sheet_ingest.ingest_mocs_tracker,
    "si_rollup": sheet_ingest.ingest_si_rollup,
    "tr_ledger": sheet_ingest.ingest_tr_ledger,
    "channel_rollup": sheet_ingest.ingest_channel_rollup,
}


# ------------------------------------------------------------------ store ----
def init(store: Store) -> None:
    store.conn.executescript(CONNECTION_SCHEMA)
    store.conn.commit()


def register(store: Store, rows: list[dict] | None = None) -> int:
    """Record the office's sheets. Idempotent; never clobbers a last_sync."""
    init(store)
    out = 0
    for row in (rows or list(D49_SHEETS)):
        existing = store.one("SELECT id FROM ws_connections WHERE id=?", (row["id"],))
        payload = {"id": row["id"], "kind": row.get("kind", "sheet"),
                   "label": row["label"], "role": row["role"],
                   "target": row["target"],
                   "detail": json.dumps(row.get("detail") or {}),
                   "enabled": int(row.get("enabled", 1))}
        if existing:
            store.conn.execute(
                "UPDATE ws_connections SET kind=?,label=?,role=?,target=?,detail=?,"
                "enabled=? WHERE id=?",
                (payload["kind"], payload["label"], payload["role"], payload["target"],
                 payload["detail"], payload["enabled"], payload["id"]))
        else:
            store.upsert("ws_connections", [payload])
        out += 1
    store.conn.commit()
    return out


def connections(store: Store, kind: str = "") -> list[dict]:
    init(store)
    sql = "SELECT * FROM ws_connections"
    params: list = []
    if kind:
        sql += " WHERE kind=?"
        params.append(kind)
    return [dict(r) for r in store.q(sql + " ORDER BY kind, label", params)]


# ------------------------------------------------------------- transports ----
def _fetch(url: str, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:600]
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def to_markdown(rows: list[list[str]]) -> str:
    """
    CSV rows to the pipe table the ingesters read.

    Cell text is escaped the way the Drive connector escapes it, because the
    ingesters already unescape exactly that. A literal pipe inside a cell would
    otherwise split one budget line into two.
    """
    def cell(v: Any) -> str:
        return str(v if v is not None else "").replace("|", r"\|").replace("\n", " ").strip()
    return "\n".join("| " + " | ".join(cell(c) for c in row) + " |" for row in rows)


def _csv_to_rows(text: str) -> list[list[str]]:
    return [r for r in csvmod.reader(io.StringIO(text))]


def _looks_like_html(body: str) -> bool:
    head = body.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def fetch_sheet(spreadsheet_id: str, gid: str = "0", sheet_name: str = "",
                transport: str = "auto") -> dict:
    """Pull one tab. Returns rows, the transport that answered, and attempts."""
    attempts: list[dict] = []

    def try_csv(url: str, name: str) -> list[list[str]] | None:
        status, body = _fetch(url)
        # A sign-in redirect answers 200 with an HTML login page. Treating that
        # as data would ingest a web page as a budget book.
        bad_html = status == 200 and _looks_like_html(body)
        ok = status == 200 and not bad_html and body.strip()
        attempts.append({"transport": name, "status": status, "ok": bool(ok),
                         "detail": ("sign-in page returned, not data" if bad_html
                                    else None if ok else body[:200])})
        return _csv_to_rows(body) if ok else None

    order = ([transport] if transport != "auto"
             else ["csv", "gviz", "oauth", "api_key"])

    for name in order:
        rows = None
        if name == "csv":
            rows = try_csv(EXPORT.format(sid=spreadsheet_id, gid=gid or "0"), "csv")
        elif name == "gviz":
            if not sheet_name:
                attempts.append({"transport": "gviz", "ok": False,
                                 "detail": "no sheet name given"})
            else:
                rows = try_csv(GVIZ.format(sid=spreadsheet_id,
                                           name=urllib.parse.quote(sheet_name)), "gviz")
        elif name in ("oauth", "api_key"):
            token = keys.get("google_oauth_token") if name == "oauth" else None
            key = keys.get("google_api_key") if name == "api_key" else None
            if not token and not key:
                attempts.append({"transport": name, "ok": False,
                                 "detail": f"no google_{'oauth_token' if name == 'oauth' else 'api_key'} configured"})
                continue
            rng = urllib.parse.quote(sheet_name or "A1:ZZ20000")
            url = f"{API}/{spreadsheet_id}/values/{rng}"
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            else:
                url += f"?key={urllib.parse.quote(key)}"
            status, body = _fetch(url, headers)
            ok = status == 200
            attempts.append({"transport": name, "status": status, "ok": ok,
                             "detail": None if ok else body[:200]})
            if ok:
                try:
                    rows = json.loads(body).get("values", [])
                except ValueError:
                    rows = None
        if rows:
            return {"ok": True, "transport": name, "rows": rows,
                    "row_count": len(rows), "attempts": attempts}

    return {"ok": False, "rows": [], "row_count": 0, "attempts": attempts,
            "how_to_fix": HOW_TO_FIX}


HOW_TO_FIX = (
    "No sheet transport answered. Either share the sheet as 'Anyone with the "
    "link can view' -- which makes the credential-free CSV export work -- or "
    "run `universe connect key set google_oauth_token` with a token that has "
    "the spreadsheets.readonly scope. Credentials are stored in "
    "~/.d49/config.json, never in the repository."
)


# ----------------------------------------------------------------- syncing ----
def sync_one(store: Store, conn_row: dict, transport: str = "auto") -> dict:
    init(store)
    detail = json.loads(conn_row.get("detail") or "{}")
    got = fetch_sheet(conn_row["target"], str(detail.get("gid", "0")),
                      detail.get("sheet_name", ""), transport)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not got["ok"]:
        store.conn.execute(
            "UPDATE ws_connections SET last_sync=?, last_status=?, last_error=? WHERE id=?",
            (stamp, "failed", json.dumps(got["attempts"])[:1500], conn_row["id"]))
        store.conn.commit()
        return {**got, "connection": conn_row["id"], "label": conn_row["label"]}

    ingester = INGESTERS.get(conn_row["role"])
    if ingester is None:
        return {"ok": False, "connection": conn_row["id"],
                "error": f"no ingester for role {conn_row['role']!r}"}

    text = to_markdown(got["rows"])
    kwargs = {k: v for k, v in detail.items() if k in ("fy",)}
    loaded = ingester(store, text, **kwargs)
    store.conn.execute(
        "UPDATE ws_connections SET last_sync=?, last_status=?, last_rows=?, "
        "last_error=NULL WHERE id=?",
        (stamp, "ok", got["row_count"], conn_row["id"]))
    store.conn.commit()
    return {"ok": True, "connection": conn_row["id"], "label": conn_row["label"],
            "transport": got["transport"], "rows": got["row_count"],
            "loaded": loaded}


def sync_all(store: Store, transport: str = "auto") -> dict:
    register(store)
    results = [sync_one(store, row, transport)
               for row in connections(store, "sheet") if row.get("enabled", 1)]
    ok = [r for r in results if r.get("ok")]
    return {"synced": len(ok), "failed": len(results) - len(ok), "results": results}


def sync_file(store: Store, role: str, path: Path | str, **kwargs) -> dict:
    """Load an exported sheet: .csv, a Drive-connector JSON, or a markdown table."""
    ingester = INGESTERS.get(role)
    if ingester is None:
        return {"ok": False, "error": f"unknown role {role!r}; "
                                      f"expected one of {sorted(INGESTERS)}"}
    p = Path(path)
    raw = p.read_text()
    if p.suffix.lower() == ".csv":
        text = to_markdown(_csv_to_rows(raw))
    elif p.suffix.lower() == ".json":
        text = sheet_ingest.load_connector_json(p)
    else:
        text = raw
    return {"ok": True, "role": role, "source": str(p), "loaded": ingester(store, text, **kwargs)}
