#!/usr/bin/env python3
"""
Start the workspace.

One command, or one double-click on Windows. It checks what is loaded, seeds
the office's programs the first time, builds the search index if it is empty,
and opens the browser. Nothing here needs the network: the corpus is on disk
and live sources are refreshed from inside the interface when they are
reachable.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..core.config import DATA_DIR, MEMBER_NAME
from ..core.store import Store
from . import indexer, media, model, schema, seed, server


def prepare(store: Store, quiet: bool = False) -> dict:
    """Make sure the workspace is usable before the browser opens."""
    def say(msg: str) -> None:
        if not quiet:
            print(f"  {msg}")

    schema.apply(store.conn)
    media.init(store)
    out: dict = {}

    if not model.people(store, active_only=False):
        say("Setting up the office roster and programs…")
        out["seed"] = seed.seed(store)
        out["demo"] = seed.demo_tasks(store)

    indexed = store.scalar("SELECT COUNT(*) FROM workspace_records") or 0
    if indexed == 0:
        say("Building the search index (this happens once)…")
        out["index"] = indexer.rebuild(store, progress=lambda m: say("  " + m))
    else:
        out["index"] = {"indexed": indexed, "note": "already built"}

    # The change detector needs a baseline before it can report a change. Set
    # it at first launch rather than on the office's first real scan, so that
    # scan reports what actually moved instead of recording the world silently.
    try:
        from ..live import watch
        watch.init(store)
        if not (store.scalar("SELECT COUNT(*) FROM ws_watch_state") or 0):
            say("Recording a baseline for change detection…")
            out["watch"] = watch.scan(store, baseline=True)["counts"]
    except Exception as exc:                 # never block the workspace opening
        out["watch"] = {"skipped": type(exc).__name__}

    counts = store.counts()
    say(f"{indexed or out['index']['indexed']:,} searchable records · "
        f"{counts['funding']:,} funding lines · {counts['matters']:,} matters")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="universe workspace",
        description=f"The District 49 office workspace for {MEMBER_NAME}.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address. Leave as localhost unless you intend "
                         "to share the workspace on your network.")
    ap.add_argument("--port", type=int, default=8749)
    ap.add_argument("--db", help="Path to the workspace database.")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reindex", action="store_true",
                    help="Rebuild the search index before starting.")
    ap.add_argument("--seed", action="store_true",
                    help="Create the office programs even if people exist.")
    a = ap.parse_args(argv)

    print(f"\n  Preparing the workspace for {MEMBER_NAME}…")
    store = Store(a.db) if a.db else Store()
    try:
        if a.seed:
            seed.seed(store)
            seed.demo_tasks(store)
        prepare(store)
        if a.reindex:
            print("  Rebuilding the search index…")
            indexer.rebuild(store, progress=lambda m: print("   ", m))
    finally:
        store.close()

    if a.host not in ("127.0.0.1", "localhost"):
        print(f"\n  ! Binding to {a.host}. Anyone who can reach this machine and "
              f"has the token can open the workspace.")
    server.serve(host=a.host, port=a.port, db=a.db,
                 open_browser=not a.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
