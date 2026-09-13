#!/usr/bin/env python3
"""
The workspace server.

A standard-library HTTP server. No framework, no build step, no install: the
office opens a launcher and a browser, and it works on a laptop with nothing
else on it.

Security posture, because this holds constituent names and unreleased drafts:

* A bearer token gates every API route. It is generated on first run and
  written next to the database, not compiled in.
* State-changing requests carry a CSRF token and must come from this origin,
  so a page in another tab cannot drive the workspace.
* A strict Content-Security-Policy: no inline script, no remote origins.
* Every SQL value is bound. CSV exports escape leading =, +, -, @ so a cell
  cannot execute when the file is opened in a spreadsheet.
* The server binds to localhost by default. Sharing it with the office is an
  explicit choice, not an accident of startup.
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import secrets
import threading
import time
import traceback
import urllib.parse
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..core.config import CURRENT_FY, DATA_DIR, DISTRICT, MEMBER_NAME
from ..core.store import Store
from ..live import watch
from . import (assistant, breakdown, events, indexer, media, meeting, model,
               schema, search)


# Query-string keys that are options, not record filters. Everything else a
# caller passes becomes a filter, so a new dimension needs no server change.
_NOT_FILTERS = {"q", "by", "top", "order", "limit", "offset", "sort", "facets",
                "rows", "cols", "measure", "t", "severity", "unacknowledged",
                "days", "hours", "event", "id", "speaker", "kind_of"}


def _run_brief(store: Store, kind: str, subject: str, ask: str,
               council: bool, kw: dict) -> dict:
    """
    Run one brief through the whole contract and report the verdict.

    The job returns the receipt's summary rather than the prose: whether it is
    deliverable is the thing the person who pressed the button needs first, and
    the brief itself is one click away under its id.
    """
    from ..ai import process as proc
    made = proc.run(store, kind, subject, ask=ask or None,
                    with_council=council, write=True, **kw)
    receipt = made.receipt
    return {
        "deliverable_id": made.deliverable_id,
        "kind": kind, "subject": made.subject,
        "status": made.status,
        "verdict": receipt.verdict() if receipt else "unknown",
        "why": receipt.why() if receipt else "",
        "blockers": [{"asks": g.asks, "found": g.found}
                     for g in (receipt.blockers if receipt else [])],
        "warnings": [{"asks": g.asks, "found": g.found}
                     for g in (receipt.warnings if receipt else [])],
    }


def _filters(a: dict) -> dict:
    out = {}
    for key, value in a.items():
        if key in _NOT_FILTERS or value in ("", None):
            continue
        out[key] = int(value) if str(value).lstrip("-").isdigit() else value
    return out

STATIC = Path(__file__).parent / "static"
TOKEN_FILE = DATA_DIR / "workspace-token.txt"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")


def read_token() -> str:
    """The access token, generated once and reused."""
    env = os.environ.get("D49_WORKSPACE_TOKEN")
    if env:
        return env.strip()
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return tok


class Workspace(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, db: str | None = None):
        super().__init__(addr, handler)
        self.db_path = db
        self.token = read_token()
        self.csrf = secrets.token_urlsafe(18)
        self.job: dict[str, Any] = {}
        self._lock = threading.Lock()
        with self.store() as s:
            schema.apply(s.conn)
            media.init(s)

    def store(self) -> Store:
        return Store(self.db_path) if self.db_path else Store()

    # A long refresh runs off the request thread so the UI never blocks.
    def start_job(self, name: str, fn: Callable[[Store], Any]) -> dict:
        with self._lock:
            if self.job.get("status") == "Running":
                return {**self.job, "queued": False,
                        "note": "Another refresh is already running."}
            self.job = {"name": name, "status": "Running", "started": _now(),
                        "detail": ""}

        def run():
            s = self.store()
            try:
                result = fn(s)
                with self._lock:
                    self.job = {"name": name, "status": "Complete",
                                "finished": _now(),
                                "detail": json.dumps(result, default=str)[:2000]}
                events.emit(s, "job.complete", "job", name, f"{name} finished")
            except Exception as exc:
                with self._lock:
                    self.job = {"name": name, "status": "Error",
                                "finished": _now(),
                                "detail": f"{type(exc).__name__}: {exc}"}
                events.emit(s, "job.error", "job", name, f"{name} failed: {exc}")
            finally:
                s.close()

        threading.Thread(target=run, daemon=True).start()
        return {**self.job, "queued": True}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Handler(BaseHTTPRequestHandler):
    server_version = "D49Workspace"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing --
    def log_message(self, fmt, *args):     # quiet; the UI shows what matters
        pass

    def _headers(self, code: int, ctype: str, extra: dict | None = None,
                 length: int | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def send(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self._headers(code, "application/json; charset=utf-8", length=len(body))
        self.wfile.write(body)

    def fail(self, message: str, code: int = 400) -> None:
        self.send({"error": message}, code)

    def authed(self, query_token: str = "") -> bool:
        """Bearer header normally; a query token only for the event stream.

        EventSource cannot set request headers, so the one read-only streaming
        endpoint accepts the token as a parameter. Every other route requires
        the header, and no route that changes state accepts a query token.
        """
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token and query_token:
            token = query_token.strip()
        return secrets.compare_digest(token, self.server.token)

    def same_origin(self) -> bool:
        """A write must come from this page, not another tab."""
        origin = self.headers.get("Origin")
        if not origin:
            return True                       # curl and the launcher
        host = self.headers.get("Host", "")
        return origin.split("://")[-1] == host

    # ---------------------------------------------------------------- GET --
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        args = {k: v[-1] for k, v in
                urllib.parse.parse_qs(parsed.query).items()}

        if not path.startswith("/api/"):
            return self.static(path)
        if path == "/api/stream":
            if not self.authed(args.get("t", "")):
                return self.fail("Enter the workspace access token.", 401)
            return self.stream(args)
        if not self.authed():
            return self.fail("Enter the workspace access token.", 401)

        s = self.server.store()
        try:
            return self.get_api(s, path, args)
        except ValueError as exc:
            return self.fail(str(exc))
        except Exception as exc:
            traceback.print_exc()
            return self.fail(f"{type(exc).__name__}: {exc}", 500)
        finally:
            s.close()

    def get_api(self, s: Store, path: str, a: dict):
        if path == "/api/status":
            return self.send(self.status(s))
        if path == "/api/search":
            return self.send(search.search_nl(
                s, a.get("q", ""),
                limit=min(int(a.get("limit", 60)), 300),
                offset=int(a.get("offset", 0)),
                sort=a.get("sort", "relevance"),
                with_facets=a.get("facets", "1") != "0"))
        if path == "/api/suggest":
            return self.send(search.suggest(s, a.get("q", "")))
        if path == "/api/record":
            r = search.record(s, a.get("key", ""))
            return self.send(r) if r else self.fail("No such record.", 404)
        if path == "/api/programs":
            return self.send(model.programs(s, a.get("archived") == "1"))
        if path == "/api/projects":
            return self.send(model.projects(s, a.get("program_id")))
        if path == "/api/project":
            pid = a.get("id", "")
            proj = [p for p in model.projects(s) if p["id"] == pid]
            return self.send({
                "project": proj[0] if proj else None,
                "sections": model.sections(s, pid),
                "tasks": model.tasks(s, project_id=pid, top_level=True),
                "updates": [dict(r) for r in s.q(
                    "SELECT * FROM ws_status_updates WHERE project_id=? "
                    "ORDER BY id DESC LIMIT 20", [pid])],
                "fields": [dict(r) for r in s.q(
                    "SELECT * FROM ws_fields WHERE project_id=? ORDER BY position",
                    [pid])]})
        if path == "/api/tasks":
            return self.send(model.tasks(
                s, project_id=a.get("project_id"), program_id=a.get("program_id"),
                owner=a.get("owner"), status=a.get("status"),
                top_level=a.get("top_level") == "1"))
        if path == "/api/task":
            t = model.task(s, a.get("id", ""))
            return self.send(t) if t else self.fail("No such task.", 404)
        if path == "/api/mywork":
            return self.send(model.my_work(s, a.get("person", "")))
        if path == "/api/inbox":
            return self.send(model.inbox(s, a.get("person", ""),
                                         a.get("unread") == "1"))
        if path == "/api/people":
            return self.send(model.people(s, a.get("all") != "1"))
        if path == "/api/workload":
            return self.send(model.workload(s, int(a.get("days", 14))))
        if path == "/api/activity":
            return self.send(model.activity(s, int(a.get("limit", 100))))
        if path == "/api/calendar":
            return self.send([dict(r) for r in s.q(
                "SELECT * FROM calendar WHERE substr(start,1,10) >= ? "
                "ORDER BY start LIMIT 300", [date.today().isoformat()])])
        if path == "/api/media":
            return self.send(media.library(s, a.get("kind", ""),
                                           a.get("member") == "1"))
        if path == "/api/media/coverage":
            return self.send(media.coverage(s))
        if path == "/api/assistant/prompts":
            return self.send(assistant.prompts(s))
        if path == "/api/assistant/history":
            return self.send(assistant.history(s))
        if path == "/api/coverage":
            return self.send(indexer.coverage(s))
        if path == "/api/watches":
            return self.send([dict(r) for r in s.q(
                "SELECT * FROM ws_views ORDER BY pinned DESC, created DESC")])
        if path == "/api/export":
            return self.export(s, a)

        # ------------------------------------------------- breakdowns --
        if path == "/api/breakdown":
            return self.send(breakdown.break_by(
                s, a.get("by", "member"), a.get("q", ""), _filters(a),
                top=min(int(a.get("top", 25)), 200),
                order=a.get("order", "amount")))
        if path == "/api/breakdown/profile":
            return self.send(breakdown.profile(s, a.get("q", ""), _filters(a)))
        if path == "/api/breakdown/cross":
            return self.send(breakdown.cross(
                s, a.get("rows", "member"), a.get("cols", "fy"),
                a.get("q", ""), _filters(a), measure=a.get("measure", "amount")))
        if path == "/api/breakdown/dimensions":
            return self.send(breakdown.dimensions())

        # ----------------------------------------------------- changes --
        if path == "/api/changes":
            return self.send(watch.recent(
                s, min(int(a.get("limit", 50)), 300), a.get("severity", ""),
                a.get("kind", ""), a.get("unacknowledged", "") == "1"))
        if path == "/api/changes/digest":
            return self.send(watch.digest(s, int(a.get("hours", 168))))
        if path == "/api/changes/status":
            return self.send(watch.status(s))

        # ----------------------------------------------------- meetings --
        if path == "/api/meetings":
            return self.send(meeting.upcoming(s, int(a.get("days", 21))))
        if path == "/api/meetings/packets":
            return self.send(meeting.saved(s, a.get("event", ""),
                                           min(int(a.get("limit", 25)), 100)))
        if path == "/api/meetings/packet":
            got = meeting.read(s, a.get("id", ""))
            return self.send(got) if got else self.fail("No such packet.", 404)

        # --------------------------------------------------- connectors --
        if path == "/api/connect/status":
            from ..ai import council as ai_council
            from ..connect import captions as cap
            from ..connect import gsheets
            from ..core import keys as K
            return self.send({
                "credentials": K.status(), "ai": ai_council.available(),
                "sheets": gsheets.connections(s, "sheet"),
                "calendar": s.get_meta("calendar.last_sync"),
                "transcripts": cap.coverage(s)})
        if path == "/api/brief/kinds":
            from ..ai import process as proc
            return self.send([
                {"kind": "member",
                 "label": "A Councilmember's record",
                 "subject_label": "Which member?",
                 "placeholder": "Hanks",
                 "does": "Legislative record, funding portfolio, peer benchmark, "
                         "coalition, concentration and equity."},
                {"kind": "fiscal",
                 "label": "The District 49 fiscal position",
                 "subject_label": "Fiscal year (optional)",
                 "placeholder": "2027",
                 "does": "Discretionary totals, channel split, the Transparency "
                         "Resolution ledger, citywide context."},
                {"kind": "matter",
                 "label": "One bill or resolution",
                 "subject_label": "Legistar matter id",
                 "placeholder": "77634",
                 "does": "Sponsors, whip count, committee posture, and what it "
                         "means for the North Shore."},
            ])
        if path == "/api/pipeline":
            from ..core import pipeline as P
            return self.send({"contract": P.contract(),
                              "readiness": P.readiness(s)})
        if path == "/api/pipeline/receipt":
            row = s.one("SELECT deliverable_id, kind, subject, status, meta "
                        "FROM deliverables WHERE deliverable_id=?",
                        (a.get("id", ""),))
            if not row:
                return self.fail("No such deliverable.", 404)
            meta = json.loads(row["meta"] or "{}")
            return self.send({"deliverable_id": row["deliverable_id"],
                              "kind": row["kind"], "subject": row["subject"],
                              "status": row["status"],
                              "receipt": meta.get("receipt")})
        if path == "/api/pipeline/receipts":
            out = []
            for row in s.q("SELECT deliverable_id, kind, subject, status, meta, "
                           "created FROM deliverables ORDER BY created DESC LIMIT ?",
                           (min(int(a.get("limit", 25)), 100),)):
                meta = json.loads(row["meta"] or "{}")
                receipt = meta.get("receipt") or {}
                out.append({"deliverable_id": row["deliverable_id"],
                            "kind": row["kind"], "subject": row["subject"],
                            "status": row["status"], "created": row["created"],
                            "verdict": receipt.get("verdict"),
                            "why": receipt.get("why"),
                            "blockers": len(receipt.get("blockers") or []),
                            "warnings": len(receipt.get("warnings") or [])})
            return self.send(out)
        if path == "/api/quotes":
            from ..connect import captions as cap
            return self.send(cap.quotes(s, a.get("q", ""),
                                        min(int(a.get("limit", 12)), 60),
                                        a.get("speaker", "")))
        return self.fail("Unknown endpoint.", 404)

    def status(self, s: Store) -> dict:
        counts = s.counts()
        return {
            "member": MEMBER_NAME, "district": DISTRICT,
            "csrf": self.server.csrf,
            "counts": counts,
            "indexed": s.scalar("SELECT COUNT(*) FROM workspace_records") or 0,
            "coverage": indexer.coverage(s),
            "media": media.coverage(s),
            "people": model.people(s),
            "ai": assistant.available(),
            "job": self.server.job,
            "cursor": events.latest(s),
            "server_time": _now(),
        }

    def export(self, s: Store, a: dict):
        res = search.search_nl(s, a.get("q", ""), limit=5000, with_facets=False)
        cols = ["kind", "title", "fy", "status", "sponsor", "agency", "org",
                "amount", "pillar", "source_id", "url"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in res["rows"]:
            w.writerow([_csv_safe(r.get(c)) for c in cols])
        body = buf.getvalue().encode()
        self._headers(200, "text/csv; charset=utf-8",
                      {"Content-Disposition":
                       'attachment; filename="d49-export.csv"'}, len(body))
        self.wfile.write(body)

    # ------------------------------------------------------------- stream --
    def stream(self, a: dict):
        """Server-Sent Events. Tails the event log; no polling."""
        cursor = int(a.get("cursor", 0) or 0)
        self._headers(200, "text/event-stream",
                      {"Connection": "keep-alive", "X-Accel-Buffering": "no"})
        s = self.server.store()
        deadline = time.time() + 3600
        try:
            while time.time() < deadline:
                rows = events.since(s, cursor)
                if rows:
                    for e in rows:
                        self.wfile.write(events.format_sse(e).encode())
                        cursor = e["id"]
                    self.wfile.flush()
                else:
                    self.wfile.write(events.keepalive().encode())
                    self.wfile.flush()
                    events.wait(20)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                   # the browser navigated away
        finally:
            s.close()

    # --------------------------------------------------------------- POST --
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self.authed():
            return self.fail("Enter the workspace access token.", 401)
        if not self.same_origin():
            return self.fail("Cross-origin writes are refused.", 403)
        if self.headers.get("X-Workspace-CSRF") != self.server.csrf:
            return self.fail("Stale session. Reload the workspace.", 403)

        length = int(self.headers.get("Content-Length") or 0)
        if length > 4_000_000:
            return self.fail("That payload is too large.", 413)
        try:
            d = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.fail("Malformed request body.")

        s = self.server.store()
        try:
            return self.post_api(s, path, d)
        except ValueError as exc:
            return self.fail(str(exc))
        except Exception as exc:
            traceback.print_exc()
            return self.fail(f"{type(exc).__name__}: {exc}", 500)
        finally:
            s.close()

    def post_api(self, s: Store, path: str, d: dict):
        routes = {
            "/api/programs": lambda: model.save_program(s, d),
            "/api/projects": lambda: model.save_project(s, d),
            "/api/sections": lambda: model.save_section(s, d),
            "/api/project/status": lambda: model.post_status(s, d),
            "/api/tasks": lambda: model.save_task(s, d),
            "/api/task/move": lambda: model.move_task(
                s, d["id"], d.get("section_id", ""), int(d.get("position", 0)),
                d.get("status", ""), d.get("actor", "")),
            "/api/task/complete": lambda: model.complete_task(
                s, d["id"], d.get("actor", "")),
            "/api/task/depend": lambda: model.add_dependency(
                s, d["task_id"], d["blocked_by"], d.get("actor", "")),
            "/api/task/undepend": lambda: model.remove_dependency(
                s, d["task_id"], d["blocked_by"]),
            "/api/task/follow": lambda: model.follow(
                s, d["task_id"], d["person"], bool(d.get("on", True))),
            "/api/task/link": lambda: model.link_record(
                s, d["task_id"], d["record_key"], d.get("note", "")),
            "/api/task/unlink": lambda: model.unlink_record(s, d["id"]),
            "/api/checklist": lambda: model.save_checklist_item(s, d),
            "/api/comments": lambda: model.add_comment(s, d),
            "/api/people": lambda: model.save_person(s, d),
            "/api/fields": lambda: model.save_field(s, d),
            "/api/fields/value": lambda: model.set_field_value(
                s, d["task_id"], d["field_id"], d.get("value", "")),
            "/api/inbox/read": lambda: model.mark_read(
                s, d.get("person", ""), d.get("ids")),
            "/api/assistant": lambda: assistant.ask(
                s, d.get("question", ""), d.get("kind", "answer"),
                d.get("register", "measured"), d.get("audience", ""),
                bool(d.get("council")), d.get("actor", "")),
            "/api/media/clip": lambda: media.add_clip(s, d),
            "/api/media/transcript": lambda: media.add_transcript(
                s, d["id"], d.get("transcript", ""), d.get("speakers", ""),
                d.get("actor", "")),
        }
        if path in routes:
            return self.send(routes[path]())

        # Long-running work goes to the background job runner.
        if path == "/api/refresh":
            return self.send(self.server.start_job(
                "Refreshing sources", lambda st: _refresh(st, d)), 202)
        if path == "/api/reindex":
            return self.send(self.server.start_job(
                "Rebuilding search index",
                lambda st: indexer.rebuild(st, d.get("only"))), 202)
        if path == "/api/media/refresh":
            return self.send(self.server.start_job(
                "Collecting media", lambda st: media.refresh_all(st)), 202)
        if path == "/api/watch/scan":
            return self.send(self.server.start_job(
                "Scanning for changes",
                lambda st: watch.scan(st, d.get("kinds"),
                                      baseline=bool(d.get("baseline")))), 202)
        if path == "/api/changes/ack":
            ok = watch.acknowledge(s, d.get("id", ""), d.get("who") or "workspace")
            return self.send({"acknowledged": ok})
        if path == "/api/meetings/build":
            row = s.one("SELECT * FROM calendar WHERE event_id=?",
                        (d.get("event_id", ""),))
            if not row:
                return self.fail("No such calendar item.", 404)
            event = dict(row)
            event["packet_kind"] = meeting.classify(event)
            event["committee"] = meeting.committee_of(event.get("summary") or "")
            return self.send(self.server.start_job(
                f"Preparing {event['summary'][:40]}",
                lambda st: {"id": meeting.build(
                    st, event, int(d.get("fy", CURRENT_FY)),
                    use_ai=d.get("ai", True), actor=d.get("who", ""))["id"]}), 202)
        if path == "/api/meetings/week":
            return self.send(self.server.start_job(
                "Preparing the week",
                lambda st: meeting.week(st, int(d.get("days", 7)),
                                        int(d.get("fy", CURRENT_FY)),
                                        use_ai=bool(d.get("ai", True)))), 202)
        if path == "/api/brief/run":
            from ..ai import process as proc
            kind = (d.get("kind") or "").strip()
            if kind not in proc.BUILDERS:
                return self.fail(f"Unknown brief kind {kind!r}.", 400)
            subject = (d.get("subject") or "").strip()
            ask = (d.get("ask") or "").strip()
            council = bool(d.get("council"))
            if kind == "matter" and not subject.isdigit():
                return self.fail("A bill brief needs the numeric Legistar "
                                 "matter id, for example 77634.", 400)
            kw = {}
            if kind == "fiscal" and subject.isdigit():
                kw["fy"] = int(subject)
                subject = ""
            return self.send(self.server.start_job(
                f"Writing the {kind} brief",
                lambda st: _run_brief(st, kind, subject, ask, council, kw)), 202)
        if path == "/api/connect/sync":
            what = d.get("what", "")
            if what == "calendar":
                from ..connect import gcal
                return self.send(self.server.start_job(
                    "Syncing the calendar", lambda st: gcal.sync(st)), 202)
            if what == "sheets":
                from ..connect import gsheets
                return self.send(self.server.start_job(
                    "Syncing the sheets", lambda st: gsheets.sync_all(st)), 202)
            if what == "captions":
                from ..connect import captions as cap
                return self.send(self.server.start_job(
                    "Fetching transcripts",
                    lambda st: cap.pull_missing(st, int(d.get("limit", 25)))), 202)
            return self.fail("Unknown connector.", 400)
        return self.fail("Unknown endpoint.", 404)

    # --------------------------------------------------------------- files --
    def static(self, path: str):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            target = STATIC / "index.html"
            if not target.is_file():
                return self.fail("The workspace interface is not installed.", 404)
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._headers(200, ctype, length=len(body))
        self.wfile.write(body)


def _csv_safe(v: Any) -> Any:
    """A cell starting with =, +, - or @ executes in a spreadsheet. Defuse it."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def _refresh(store: Store, d: dict) -> dict:
    """Pull live sources, then reindex what changed."""
    from .sync import refresh_sources
    out = refresh_sources(store, full=bool(d.get("full")))
    out["index"] = indexer.rebuild(store, ["matters", "calendar", "media"])
    return out


def serve(host: str = "127.0.0.1", port: int = 8749, db: str | None = None,
          open_browser: bool = True) -> None:
    srv = Workspace((host, port), Handler, db=db)
    url = f"http://{host}:{port}/"
    print(f"\n  D49 workspace — {MEMBER_NAME}, District {DISTRICT}")
    print(f"  {url}")
    print(f"  Access token: {srv.token}")
    print("  Keep this window open. Press Ctrl+C to stop.\n")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Workspace stopped.")
    finally:
        srv.server_close()
