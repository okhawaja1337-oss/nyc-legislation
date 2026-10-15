#!/usr/bin/env python3
"""
The workspace schema.

The office does not run on a flat task list. Work nests the way the budget
cycle nests: a **program** runs for a season (the FY2028 budget, the
legislative session, land use), a **project** is a deliverable inside it (the
Preliminary Budget Response, a single ULURP), a **section** is a stage, and a
**task** is what one person does by one date -- with subtasks under it.

Everything additive: an existing workspace database keeps its rows. The
original tables (workspace_tasks, comments, checklist, watches, changes, sync,
details, records, fts) are declared here unchanged so a v1 database opens
without migration, and the new columns are applied by ``migrate()``.
"""
from __future__ import annotations

import sqlite3

# --------------------------------------------------------------- v1 tables --
# Declared exactly as the first workspace shipped them, so an existing file
# opens untouched. New columns arrive through MIGRATIONS below.
BASE = """
CREATE TABLE IF NOT EXISTS workspace_records (
  key TEXT PRIMARY KEY, kind TEXT, entity_id TEXT, title TEXT, body TEXT,
  year INTEGER, status TEXT, committee TEXT, sponsor TEXT, agency TEXT,
  channel TEXT, district INTEGER, source_id TEXT, url TEXT, updated TEXT,
  detail TEXT, amount REAL, org TEXT, pillar TEXT, ein TEXT, fy INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS workspace_fts USING fts5(
  key UNINDEXED, title, body, tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS workspace_tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, owner TEXT, backup TEXT, due TEXT,
  priority TEXT, status TEXT, reviewer TEXT, link TEXT, blocker TEXT,
  followup TEXT, outcome TEXT, created TEXT, updated TEXT,
  project TEXT, description TEXT, revision INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS workspace_task_log (
  id INTEGER PRIMARY KEY, task_id TEXT, at TEXT, actor TEXT,
  before_json TEXT, after_json TEXT
);
CREATE TABLE IF NOT EXISTS workspace_checklist (
  id INTEGER PRIMARY KEY, task_id TEXT, title TEXT, done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS workspace_comments (
  id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created TEXT
);
CREATE TABLE IF NOT EXISTS workspace_watches (
  id TEXT PRIMARY KEY, name TEXT, filters TEXT, created TEXT
);
CREATE TABLE IF NOT EXISTS workspace_changes (
  id INTEGER PRIMARY KEY, record_key TEXT, kind TEXT, title TEXT, at TEXT,
  fields TEXT, before_json TEXT, after_json TEXT
);
CREATE TABLE IF NOT EXISTS workspace_sync (
  source TEXT PRIMARY KEY, attempted TEXT, succeeded TEXT, status TEXT,
  rows INTEGER, error TEXT, cursor TEXT
);
CREATE TABLE IF NOT EXISTS workspace_details (
  matter_id INTEGER PRIMARY KEY, payload TEXT, fetched TEXT
);
"""

# ------------------------------------------------------------- v2 structure --
STRUCTURE = """
-- A program runs for a season and holds projects. The budget cycle is one.
CREATE TABLE IF NOT EXISTS ws_programs (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT, pillar TEXT,
  lead TEXT, color TEXT, icon TEXT, fy INTEGER,
  starts TEXT, ends TEXT, archived INTEGER DEFAULT 0,
  position INTEGER DEFAULT 0, created TEXT, updated TEXT
);

CREATE TABLE IF NOT EXISTS ws_projects (
  id TEXT PRIMARY KEY, program_id TEXT, name TEXT NOT NULL, purpose TEXT,
  lead TEXT, health TEXT DEFAULT 'On track', color TEXT,
  starts TEXT, due TEXT, archived INTEGER DEFAULT 0,
  default_view TEXT DEFAULT 'board', position INTEGER DEFAULT 0,
  created TEXT, updated TEXT
);
CREATE INDEX IF NOT EXISTS ix_ws_projects_program ON ws_projects(program_id);

CREATE TABLE IF NOT EXISTS ws_sections (
  id TEXT PRIMARY KEY, project_id TEXT, name TEXT NOT NULL,
  position INTEGER DEFAULT 0, collapsed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ws_sections_project ON ws_sections(project_id);

-- People. Capacity is what makes a workload view mean anything.
CREATE TABLE IF NOT EXISTS ws_people (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, initials TEXT, role TEXT,
  email TEXT, color TEXT, weekly_hours REAL DEFAULT 35,
  active INTEGER DEFAULT 1, created TEXT
);

-- Dependencies: this task cannot start until that one finishes.
CREATE TABLE IF NOT EXISTS ws_task_deps (
  task_id TEXT, blocked_by TEXT, created TEXT,
  PRIMARY KEY (task_id, blocked_by)
);

-- Collaborators who follow a task without owning it.
CREATE TABLE IF NOT EXISTS ws_followers (
  task_id TEXT, person TEXT, added TEXT, PRIMARY KEY (task_id, person)
);

-- Custom fields, defined per project.
CREATE TABLE IF NOT EXISTS ws_fields (
  id TEXT PRIMARY KEY, project_id TEXT, name TEXT NOT NULL,
  type TEXT DEFAULT 'text', options TEXT, position INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ws_field_values (
  task_id TEXT, field_id TEXT, value TEXT,
  PRIMARY KEY (task_id, field_id)
);

-- Attach a record from the lake -- a bill, a funding line, an org, a contact.
CREATE TABLE IF NOT EXISTS ws_links (
  id INTEGER PRIMARY KEY, task_id TEXT, record_key TEXT, note TEXT, created TEXT
);
CREATE INDEX IF NOT EXISTS ix_ws_links_task ON ws_links(task_id);

-- Asana-style project status updates: a colour and a paragraph, weekly.
CREATE TABLE IF NOT EXISTS ws_status_updates (
  id INTEGER PRIMARY KEY, project_id TEXT, author TEXT, health TEXT,
  body TEXT, created TEXT
);

-- @mentions and the inbox they feed.
CREATE TABLE IF NOT EXISTS ws_notifications (
  id INTEGER PRIMARY KEY, person TEXT, kind TEXT, task_id TEXT,
  project_id TEXT, actor TEXT, body TEXT, created TEXT, read INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ws_notif_person ON ws_notifications(person, read);

-- Saved views: a named route plus its filters.
CREATE TABLE IF NOT EXISTS ws_views (
  id TEXT PRIMARY KEY, person TEXT, name TEXT, route TEXT, filters TEXT,
  pinned INTEGER DEFAULT 0, created TEXT
);

-- The event log. Every mutation appends here; the SSE stream tails it, so a
-- second browser sees a change without polling the whole board.
CREATE TABLE IF NOT EXISTS ws_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT, kind TEXT,
  entity TEXT, entity_id TEXT, summary TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS ix_ws_events_at ON ws_events(id);
"""

# Indexes live apart from the tables because several of them cover columns
# that MIGRATIONS adds. An index over a column that does not exist yet fails
# the whole script, so these run last.
INDEXES = """
CREATE INDEX IF NOT EXISTS workspace_filters ON workspace_records(kind, year, status);
CREATE INDEX IF NOT EXISTS workspace_org     ON workspace_records(org);
CREATE INDEX IF NOT EXISTS workspace_pillar  ON workspace_records(pillar);
CREATE INDEX IF NOT EXISTS workspace_fy      ON workspace_records(kind, fy);
CREATE INDEX IF NOT EXISTS workspace_amount  ON workspace_records(kind, amount);
CREATE INDEX IF NOT EXISTS workspace_sponsor ON workspace_records(sponsor);
CREATE INDEX IF NOT EXISTS workspace_kindyr  ON workspace_records(kind, year, fy, status);
CREATE INDEX IF NOT EXISTS workspace_init    ON workspace_records(initiative);
CREATE INDEX IF NOT EXISTS workspace_tier    ON workspace_records(kind, tier);
CREATE INDEX IF NOT EXISTS workspace_spkey   ON workspace_records(sponsor_key);
CREATE INDEX IF NOT EXISTS workspace_cmte    ON workspace_records(committee);
CREATE INDEX IF NOT EXISTS workspace_agency  ON workspace_records(agency);
CREATE INDEX IF NOT EXISTS ix_ws_tasks_project ON workspace_tasks(project_id);
CREATE INDEX IF NOT EXISTS ix_ws_tasks_parent  ON workspace_tasks(parent_id);
CREATE INDEX IF NOT EXISTS ix_ws_tasks_owner   ON workspace_tasks(owner, status);
CREATE INDEX IF NOT EXISTS ix_ws_tasks_due     ON workspace_tasks(due);
CREATE INDEX IF NOT EXISTS ix_ws_comments_task ON workspace_comments(task_id);
"""

# Columns added to tables that already exist in a v1 database.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("workspace_tasks", "project_id", "TEXT"),
    ("workspace_tasks", "section_id", "TEXT"),
    ("workspace_tasks", "parent_id", "TEXT"),
    ("workspace_tasks", "position", "INTEGER DEFAULT 0"),
    ("workspace_tasks", "milestone", "INTEGER DEFAULT 0"),
    ("workspace_tasks", "starts", "TEXT"),
    ("workspace_tasks", "estimate_hours", "REAL"),
    ("workspace_tasks", "completed_at", "TEXT"),
    ("workspace_tasks", "tags", "TEXT"),
    ("workspace_tasks", "recurrence", "TEXT"),
    ("workspace_comments", "parent_id", "INTEGER"),
    ("workspace_comments", "pinned", "INTEGER DEFAULT 0"),
    ("workspace_comments", "edited", "TEXT"),
    ("workspace_checklist", "position", "INTEGER DEFAULT 0"),
    ("workspace_checklist", "owner", "TEXT"),
    ("workspace_records", "amount", "REAL"),
    ("workspace_records", "org", "TEXT"),
    ("workspace_records", "pillar", "TEXT"),
    ("workspace_records", "ein", "TEXT"),
    ("workspace_records", "fy", "INTEGER"),
    # Breakdown dimensions. `pot` and `tier` used to live inside the detail
    # JSON, which meant "break the budget down by initiative" could not be a
    # query -- and that is the cut a budget director asks for most.
    ("workspace_records", "initiative", "TEXT"),
    ("workspace_records", "tier", "TEXT"),
    # A case- and format-stable key for a person, so "HANKS" and "Hanks" are
    # one bucket rather than two half-totals.
    ("workspace_records", "sponsor_key", "TEXT"),
    ("workspace_watches", "person", "TEXT"),
    ("workspace_watches", "last_seen", "TEXT"),
)

# Work stages. The first is where new work lands; the last means done.
STAGES = ("Intake", "Research", "Drafting", "Messaging review",
          "COS review", "Ready", "Completed")
PRIORITIES = ("Low", "Normal", "High", "Urgent")
HEALTH = ("On track", "At risk", "Off track", "On hold", "Complete")
FIELD_TYPES = ("text", "number", "currency", "date", "select", "person", "checkbox")

# Record kinds the search index carries.
KINDS = ("matter", "funding", "member", "contact", "calendar", "note",
         "deliverable", "task", "org")


def apply(conn: sqlite3.Connection) -> None:
    """Create everything and bring an older database forward. Idempotent."""
    conn.executescript(BASE)
    conn.executescript(STRUCTURE)
    for table, column, decl in MIGRATIONS:
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.OperationalError:
            continue
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.executescript(INDEXES)
    conn.commit()
