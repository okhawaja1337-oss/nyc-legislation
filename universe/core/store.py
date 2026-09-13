#!/usr/bin/env python3
"""
The data lake.

One SQLite file holds every normalized entity the Universe knows about --
members, bills, votes, funding lines, organizations, calendar items, notes --
plus a full-text index across all of them so a single query can reach
legislation, money and constituent work at once.

SQLite is the right call here: the whole corpus is tens of megabytes, the
office needs it to work offline on a laptop, and there is no server to run.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import DB_PATH, JOURNAL_DIR

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- ------------------------------------------------------------- people ----
CREATE TABLE IF NOT EXISTS members (
  person_id     INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  last          TEXT,
  party         TEXT,
  borough       TEXT,
  district      INTEGER,
  current       INTEGER DEFAULT 0,
  first_seen    TEXT,
  last_seen     TEXT,
  sessions      TEXT,
  leadership    TEXT,
  wiki          TEXT,
  qid           TEXT,
  dob           TEXT,
  gender        TEXT,
  extra         TEXT
);
CREATE INDEX IF NOT EXISTS ix_members_district ON members(district);
CREATE INDEX IF NOT EXISTS ix_members_current  ON members(current);

CREATE TABLE IF NOT EXISTS member_metrics (
  person_id     INTEGER,
  metric        TEXT,
  value         REAL,
  text_value    TEXT,
  session       TEXT,
  source_id     TEXT,
  PRIMARY KEY (person_id, metric, session)
);
CREATE INDEX IF NOT EXISTS ix_metrics_metric ON member_metrics(metric);

-- -------------------------------------------------------- legislation ----
CREATE TABLE IF NOT EXISTS matters (
  matter_id     INTEGER PRIMARY KEY,
  file          TEXT,
  name          TEXT,
  type          TEXT,
  status        TEXT,
  committee     TEXT,
  year          INTEGER,
  session       TEXT,
  enacted       INTEGER DEFAULT 0,
  local_law     TEXT,
  prime_id      INTEGER,
  n_sponsors    INTEGER,
  topics        TEXT,
  pending       INTEGER DEFAULT 0,
  pass_prob     REAL,
  pillars       TEXT,
  d49_score     REAL,
  source_id     TEXT,
  updated       TEXT
);
CREATE INDEX IF NOT EXISTS ix_matters_year   ON matters(year);
CREATE INDEX IF NOT EXISTS ix_matters_status ON matters(status);
CREATE INDEX IF NOT EXISTS ix_matters_prime  ON matters(prime_id);

CREATE TABLE IF NOT EXISTS sponsorships (
  matter_id     INTEGER,
  person_id     INTEGER,
  role          TEXT,          -- 'prime' | 'cosponsor'
  PRIMARY KEY (matter_id, person_id)
);
CREATE INDEX IF NOT EXISTS ix_spon_person ON sponsorships(person_id);

CREATE TABLE IF NOT EXISTS votes (
  matter_ref    TEXT,
  person_id     INTEGER,
  vote          TEXT,
  vote_date     TEXT,
  aye           INTEGER,
  nay           INTEGER,
  topic         TEXT,
  title         TEXT
);
CREATE INDEX IF NOT EXISTS ix_votes_person ON votes(person_id);

-- ------------------------------------------------------------- money ----
CREATE TABLE IF NOT EXISTS funding (
  line_id       TEXT PRIMARY KEY,
  fy            INTEGER,
  channel       TEXT,          -- expense | capital | initiative
  pot           TEXT,          -- initiative or capital section
  member        TEXT,
  person_id     INTEGER,
  district      INTEGER,
  borough       TEXT,
  org           TEXT,
  org_key       TEXT,
  ein           TEXT,
  program       TEXT,
  agency        TEXT,
  amount        REAL,
  section       TEXT,
  purpose       TEXT,
  status        TEXT,          -- adopted | TR-add | TR-cut | pending MOCS | cleared
  tier          TEXT,          -- ADOPTED-IMPLEMENTATION | ADOPTED-PENDING-MOD
                               -- | REVERSED | CONTEXT
  reso          TEXT,          -- which Transparency Resolution moved it
  mocs_id       TEXT,
  analyst       TEXT,
  in_d49        INTEGER DEFAULT 0,
  pillar        TEXT,
  source_id     TEXT,
  locator       TEXT,
  updated       TEXT
);
CREATE INDEX IF NOT EXISTS ix_funding_fy      ON funding(fy);
CREATE INDEX IF NOT EXISTS ix_funding_member  ON funding(member);
CREATE INDEX IF NOT EXISTS ix_funding_orgkey  ON funding(org_key);
CREATE INDEX IF NOT EXISTS ix_funding_ein     ON funding(ein);
CREATE INDEX IF NOT EXISTS ix_funding_pot     ON funding(pot);
CREATE INDEX IF NOT EXISTS ix_funding_d49     ON funding(in_d49);
CREATE INDEX IF NOT EXISTS ix_funding_tier    ON funding(tier);
CREATE INDEX IF NOT EXISTS ix_funding_channel ON funding(channel);

CREATE TABLE IF NOT EXISTS orgs (
  org_key       TEXT PRIMARY KEY,
  name          TEXT,
  ein           TEXT,
  borough       TEXT,
  zip           TEXT,
  in_d49        INTEGER DEFAULT 0,
  first_fy      INTEGER,
  last_fy       INTEGER,
  n_awards      INTEGER,
  total_awarded REAL,
  pillars       TEXT,
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS fiscal_series (
  series        TEXT,
  label         TEXT,
  fy            INTEGER,
  value         REAL,
  unit          TEXT,
  source_id     TEXT,
  PRIMARY KEY (series, fy)
);

-- ------------------------------------------------------- office ops ----
CREATE TABLE IF NOT EXISTS calendar (
  event_id      TEXT PRIMARY KEY,
  start         TEXT,
  end           TEXT,
  summary       TEXT,
  location      TEXT,
  kind          TEXT,          -- hearing | community | staff | ceremonial | deadline
  owners        TEXT,          -- staff initials
  pillar        TEXT,
  link          TEXT,
  updated       TEXT
);
CREATE INDEX IF NOT EXISTS ix_cal_start ON calendar(start);
CREATE INDEX IF NOT EXISTS ix_cal_kind  ON calendar(kind);

CREATE TABLE IF NOT EXISTS contacts (
  contact_id    TEXT PRIMARY KEY,
  level         TEXT,          -- city | borough | council | state | federal | community
  agency        TEXT,
  office        TEXT,
  title         TEXT,
  person        TEXT,
  email         TEXT,
  phone         TEXT,
  address       TEXT,
  borough       TEXT,
  district      INTEGER,
  zip           TEXT,
  service_area  TEXT,          -- what constituents call this office about
  parent        TEXT,
  url           TEXT,
  source_id     TEXT,
  updated       TEXT
);
CREATE INDEX IF NOT EXISTS ix_contacts_level    ON contacts(level);
CREATE INDEX IF NOT EXISTS ix_contacts_district ON contacts(district);
CREATE INDEX IF NOT EXISTS ix_contacts_agency   ON contacts(agency);

CREATE TABLE IF NOT EXISTS notes (
  note_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type   TEXT,
  entity_id     TEXT,
  author        TEXT,
  body          TEXT,
  tags          TEXT,
  created       TEXT
);
CREATE INDEX IF NOT EXISTS ix_notes_entity ON notes(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS deliverables (
  deliverable_id TEXT PRIMARY KEY,
  kind           TEXT,
  subject        TEXT,
  status         TEXT,
  body           TEXT,
  meta           TEXT,
  created        TEXT,
  updated        TEXT
);

CREATE TABLE IF NOT EXISTS sources (
  source_id     TEXT PRIMARY KEY,
  name          TEXT,
  publisher     TEXT,
  url           TEXT,
  accessed      TEXT,
  coverage      TEXT,
  tier          TEXT,
  locator       TEXT,
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS meta (
  key           TEXT PRIMARY KEY,
  value         TEXT,
  updated       TEXT
);

-- --------------------------------------------------------- full text ----
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
  entity_type, entity_id, title, body, tags,
  tokenize='porter unicode61'
);
"""


class Store:
    """Thin, explicit wrapper over the SQLite lake."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        # Migrate before the schema script: it creates indexes over the new
        # columns, and an index over a column that does not exist yet fails
        # the whole script. On a fresh lake the migration is a no-op.
        self._migrate()
        self.conn.executescript(SCHEMA)

    # --------------------------------------------------------- migration --
    # CREATE TABLE IF NOT EXISTS never adds a column to a table that already
    # exists, so a lake built by an earlier version needs the new columns
    # applied explicitly. Additive only -- nothing here drops or rewrites data.
    MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        ("funding", "tier", "TEXT"),
        ("funding", "reso", "TEXT"),
    )

    def _migrate(self) -> None:
        for table, column, decl in self.MIGRATIONS:
            try:
                cols = {r["name"] for r in
                        self.conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.OperationalError:
                continue
            if cols and column not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.conn.commit()

    # ------------------------------------------------------------ basics --
    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def q(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row else None

    def upsert(self, table: str, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        placeholders = ",".join("?" * len(cols))
        sql = (f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
               f"VALUES ({placeholders})")
        with self.tx() as c:
            c.executemany(sql, [[_enc(r.get(k)) for k in cols] for r in rows])
        return len(rows)

    # ------------------------------------------------------------ search --
    def index(self, entity_type: str, entity_id: str, title: str,
              body: str = "", tags: str = "") -> None:
        self.conn.execute(
            "DELETE FROM search WHERE entity_type=? AND entity_id=?",
            (entity_type, str(entity_id)))
        self.conn.execute(
            "INSERT INTO search (entity_type, entity_id, title, body, tags) "
            "VALUES (?,?,?,?,?)",
            (entity_type, str(entity_id), title or "", body or "", tags or ""))

    def index_many(self, rows: Iterable[tuple]) -> int:
        rows = list(rows)
        with self.tx() as c:
            c.executemany(
                "INSERT INTO search (entity_type, entity_id, title, body, tags) "
                "VALUES (?,?,?,?,?)", rows)
        return len(rows)

    def search(self, query: str, entity_type: str | None = None,
               limit: int = 40) -> list[sqlite3.Row]:
        """Full-text search across everything, ranked by bm25."""
        q = _fts_escape(query)
        sql = ("SELECT entity_type, entity_id, title, "
               "snippet(search, 3, '<<', '>>', ' … ', 18) AS snip, "
               "bm25(search) AS score FROM search WHERE search MATCH ?")
        params: list[Any] = [q]
        if entity_type:
            sql += " AND entity_type = ?"
            params.append(entity_type)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        try:
            return self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

    # ------------------------------------------------------------- meta --
    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value, updated) VALUES (?,?,?)",
            (key, _enc(value), _now()))
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return row[0]

    # ---------------------------------------------------------- journal --
    def journal(self, event: str, detail: dict) -> None:
        """Append-only change log. This is how 'what changed' stays honest."""
        path = JOURNAL_DIR / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
        rec = {"ts": _now(), "event": event, **detail}
        with path.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def read_journal(self, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        for path in sorted(JOURNAL_DIR.glob("*.jsonl"), reverse=True):
            for line in reversed(path.read_text().splitlines()):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
                if len(out) >= limit:
                    return out
        return out

    def counts(self) -> dict[str, int]:
        tables = ("members", "matters", "sponsorships", "votes", "funding",
                  "orgs", "calendar", "contacts", "notes", "deliverables",
                  "sources", "member_metrics", "fiscal_series")
        return {t: (self.scalar(f"SELECT COUNT(*) FROM {t}") or 0) for t in tables}

    def close(self) -> None:
        self.conn.close()

    # Usable with `with`, so a short-lived connection always gets closed.
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ------------------------------------------------------------- helpers ----
def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    if isinstance(v, bool):
        return int(v)
    return v


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fts_escape(q: str) -> str:
    """Make arbitrary user text safe for an FTS5 MATCH."""
    q = (q or "").strip()
    if not q:
        return '""'
    # Quote each bare term; keep explicit operators the user typed.
    ops = {"AND", "OR", "NOT", "NEAR"}
    out = []
    for tok in q.replace('"', " ").split():
        out.append(tok if tok.upper() in ops else f'"{tok}"')
    return " ".join(out)
