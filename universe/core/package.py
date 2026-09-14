#!/usr/bin/env python3
"""
Building the copy the office downloads.

The package has to hold the whole record and still be small enough that
somebody actually waits for it. Those pull against each other, and the way
they were reconciled is worth writing down, because each decision was made
after measuring rather than guessing.

Three things come out, in order of how much they saved:

  The search index.       81 MB, and rebuilt on first launch anyway. Note that
                          DELETE FROM an FTS5 table does not reclaim its
                          shadow tables -- 81 MB travelled inside the package
                          for an index nobody shipped on purpose. FTS5 has a
                          command for this and it is not DELETE.
  Old bill text.          53 MB. Full text from the 2022 session onward is
                          kept; everything older is dropped and restored on
                          demand. Summaries are never dropped -- they are
                          3.6 MB and they are what makes a bill findable by
                          what it does.
  The change baseline.    Rebuilt at first launch, like the index.

What is never dropped: any figure, any funding line, any reconciliation row,
any citation. A smaller package that cannot answer a question is not smaller,
it is broken.
"""
from __future__ import annotations

import shutil
import sqlite3
import zipfile
from pathlib import Path

from .store import DB_PATH, Store

# The launcher scripts, one pair per platform plus the plain-Python fallback.
LAUNCHERS = (
    "START_WORKSPACE.py", "START_WORKSPACE.cmd", "start-workspace.command",
    "RUN_A_BRIEF.py", "RUN_A_BRIEF.cmd", "run-a-brief.command",
    "SHARE_WITH_STAFF.py", "SHARE_WITH_STAFF.cmd", "share-with-staff.command",
    "READ_ME_FIRST.txt",
)
DERIVED = ("workspace_records", "ws_watch_state")
FTS = ("workspace_fts", "search")


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def slim(db: Path, keep_text_from: int = 2022) -> dict:
    """Strip what the first launch rebuilds, and report what it cost."""
    before = db.stat().st_size
    conn = sqlite3.connect(str(db), isolation_level=None)
    freed = []
    for table in FTS:
        # DELETE FROM an FTS5 table empties the view and leaves every posting
        # list sitting in its shadow tables -- 81 MB of them here, travelling
        # inside a package for an index rebuilt on first launch. The FTS5
        # 'delete-all' command only works on contentless or external-content
        # tables, and these carry their own content, so the way to reclaim the
        # space is to drop the table and recreate it from its own definition.
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
        if not row:
            continue
        try:
            conn.execute(f"DROP TABLE {table}")
            conn.execute(row[0])
            freed.append(table)
        except sqlite3.Error as exc:
            freed.append(f"{table} (failed: {exc})")
    for table in DERIVED:
        try:
            conn.execute(f"DELETE FROM {table}")
            freed.append(table)
        except sqlite3.Error:
            pass
    dropped = 0
    if keep_text_from:
        row = conn.execute(
            "SELECT COUNT(*) FROM matter_text WHERE body != ''").fetchone()
        conn.execute(
            "UPDATE matter_text SET body = '' WHERE CAST(matter_id AS INTEGER) "
            "IN (SELECT matter_id FROM matters WHERE year < ?)",
            (keep_text_from,))
        after_rows = conn.execute(
            "SELECT COUNT(*) FROM matter_text WHERE body != ''").fetchone()
        dropped = (row[0] if row else 0) - (after_rows[0] if after_rows else 0)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value, updated) "
            "VALUES ('legistar.text_trimmed', ?, datetime('now'))",
            (f'{{"keep_from": {keep_text_from}, "dropped": {dropped}, '
             f'"restore": "universe repos sync --repo legistar --force"}}',))
    conn.execute("VACUUM")
    conn.close()
    after = db.stat().st_size
    return {"before_mb": round(before / 1e6, 1),
            "after_mb": round(after / 1e6, 1),
            "saved_mb": round((before - after) / 1e6, 1),
            "cleared": freed, "text_rows_dropped": dropped}


def build(out: Path | str = "D49-Universe.zip", db: Path | None = None,
          keep_text_from: int = 2022, stage: Path | None = None) -> dict:
    """Stage the package, slim the lake and zip it."""
    root = _root()
    stage = Path(stage or (Path.cwd() / ".d49-package"))
    if stage.exists():
        shutil.rmtree(stage)
    folder = stage / "D49"
    (folder / "data" / "universe").mkdir(parents=True)

    shutil.copytree(root / "universe", folder / "universe",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    missing = []
    for name in LAUNCHERS:
        src = root / name
        if src.exists():
            shutil.copy2(src, folder / name)
        else:
            missing.append(name)

    src_db = Path(db or DB_PATH)
    dest_db = folder / "data" / "universe" / "universe.sqlite"
    # A live lake has a write-ahead log; copying the file alone produces a
    # package that opens to a malformed database. backup() takes a consistent
    # snapshot whether or not anything is writing.
    s, d = sqlite3.connect(str(src_db)), sqlite3.connect(str(dest_db))
    s.backup(d)
    s.close()
    d.close()
    slimmed = slim(dest_db, keep_text_from)

    out = Path(out)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(stage))
    size = out.stat().st_size
    shutil.rmtree(stage)
    return {"zip": str(out), "mb": round(size / 1e6, 1),
            "lake_mb": slimmed["after_mb"], "slimmed": slimmed,
            "missing_launchers": missing}


def contents(db: Path | None = None) -> dict:
    """What a package built now would carry."""
    store = Store(db or DB_PATH)
    try:
        out = {}
        for table in ("matters", "matter_text", "funding", "ledger",
                      "sponsorships", "orgs", "contacts", "calendar"):
            try:
                out[table] = store.q(f"SELECT COUNT(*) n FROM {table}")[0]["n"]
            except Exception:                            # noqa: BLE001
                out[table] = 0
        return out
    finally:
        store.close()
