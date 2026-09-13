#!/usr/bin/env python3
"""
The Universe command line.

    python3 -m universe load        --council-record FILE --fiscal DIR
    python3 -m universe status
    python3 -m universe ask         "what did we fund for seniors last year"
    python3 -m universe brief       fiscal|member|matter [subject] [--council]
    python3 -m universe funding     portfolio|drift|churn|equity|pipeline|reconcile
    python3 -m universe legislation record|coalition|signon|whip|delegation
    python3 -m universe index       [--official NAME] [--group council]
    python3 -m universe refer       "no heat and hot water for a week"
    python3 -m universe calendar    [--days 14]
    python3 -m universe console     [--out PATH]
    python3 -m universe feeds
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core.citations import CitationRegistry, DEFAULT_SOURCES
from .core.config import CURRENT_FY, DISTRICT, MEMBER_LAST, OUT_DIR
from .core.store import Store


def _p(obj, raw: bool = False) -> None:
    print(json.dumps(obj, indent=2, default=str) if raw else obj)


def _registry(store: Store) -> CitationRegistry:
    reg = CitationRegistry(DEFAULT_SOURCES)
    for r in store.q("SELECT * FROM sources"):
        d = dict(r)
        if d.get("source_id") and d["source_id"] not in reg:
            from .core.citations import Source
            reg.add(Source(**{k: d.get(k) for k in
                              ("source_id", "name", "publisher", "url", "accessed",
                               "coverage", "tier", "locator", "notes")}))
    return reg


# ------------------------------------------------------------- commands ----
def cmd_load(a, store: Store) -> None:
    out = {}
    if a.council_record:
        from .ingest.council_record import ingest as ing_cr
        out["council_record"] = ing_cr(store, a.council_record)
    if a.fiscal:
        from .ingest.fiscal import ingest_all
        out["fiscal"] = ingest_all(store, a.fiscal)
    if a.mocs:
        from .ingest.sheets import ingest_mocs_tracker, load_connector_json, enrich_pots_from_tracker
        out["mocs"] = ingest_mocs_tracker(store, load_connector_json(a.mocs))
        out["pots_enriched"] = enrich_pots_from_tracker(store)
    if a.si_rollup:
        from .ingest.sheets import ingest_si_rollup, load_connector_json
        out["si_rollup"] = ingest_si_rollup(store, load_connector_json(a.si_rollup))
    if a.channel_rollup:
        from .ingest.sheets import ingest_channel_rollup, load_connector_json
        out["channel_rollup"] = ingest_channel_rollup(
            store, load_connector_json(a.channel_rollup))
    if a.tr_ledger:
        from .ingest.sheets import ingest_tr_ledger, load_connector_json
        out["tr_ledger"] = ingest_tr_ledger(store, load_connector_json(a.tr_ledger))
    if a.calendar:
        from .ingest.calendar import ingest_connector_file
        out["calendar"] = ingest_connector_file(store, a.calendar)
    if a.greenbook:
        from .live.greenbook import ingest as ing_gb
        out["greenbook"] = ing_gb(store)
    if not out:
        print("nothing to load — pass --council-record / --fiscal / --mocs / "
              "--si-rollup / --channel-rollup / --tr-ledger / --calendar / --greenbook")
        return
    _p(out, raw=True)


def cmd_status(a, store: Store) -> None:
    from .ai.council import available
    counts = store.counts()
    meta = store.get_meta("council_record.loaded") or {}
    _p({
        "database": str(store.path),
        "counts": counts,
        "council_record": meta.get("headlines", {}),
        "schedule_c": store.get_meta("schedule_c.summary") or {},
        "ai": available(),
        "deliverables": store.scalar("SELECT COUNT(*) FROM deliverables"),
        "journal_events": len(store.read_journal(500)),
    }, raw=True)


def cmd_ask(a, store: Store) -> None:
    q = " ".join(a.question)
    hits = store.search(q, a.type, limit=a.limit)
    if not hits:
        print(f"no matches for {q!r}")
        return
    for h in hits:
        print(f"[{h['entity_type']}:{h['entity_id']}] {h['title'][:90]}")
        if h["snip"]:
            print(f"    {h['snip'][:160]}")


def cmd_brief(a, store: Store) -> None:
    from .ai.process import run
    d = run(store, a.kind, a.subject or "", with_council=a.council,
            ask=a.ask, registry=_registry(store))
    if a.json:
        _p(d.to_dict(), raw=True)
    else:
        print(d.to_markdown(_registry(store)))


def cmd_funding(a, store: Store) -> None:
    from .intel import funding as F
    who = a.member or MEMBER_LAST
    fn = {
        "portfolio": lambda: F.member_portfolio(store, who, a.fy),
        "drift": lambda: F.pillar_drift(store, who),
        "churn": lambda: F.org_trajectories(store, who),
        "equity": lambda: F.district_equity(store, a.fy),
        "pipeline": lambda: F.pipeline_risk(store, a.fy),
        "reconcile": lambda: F.reconcile_si(store, a.fy),
        "citywide": lambda: F.citywide_context(store),
        "concentration": lambda: F.concentration(store, who, a.fy),
    }[a.what]
    _p(fn(), raw=True)


def cmd_legislation(a, store: Store) -> None:
    from .intel import legislation as L
    who = a.member or MEMBER_LAST
    fn = {
        "record": lambda: L.legislative_record(store, who),
        "benchmark": lambda: L.peer_benchmark(store, who),
        "coalition": lambda: L.coalition(store, who),
        "signon": lambda: L.signon_candidates(store, who, limit=a.limit),
        "whip": lambda: L.whip_count(store, int(a.matter)) if a.matter
                         else {"error": "pass --matter MATTER_ID"},
        "delegation": lambda: L.si_delegation(store),
        "pending": lambda: L.pending_for_pillars(store, limit=a.limit),
        "find": lambda: L.find_matters(store, a.member or "", limit=a.limit),
    }[a.what]
    _p(fn(), raw=True)


def cmd_index(a, store: Store) -> None:
    from .intel import integrity as I
    if a.official:
        _p(I.official_scorecard(store, a.official, a.fy), raw=True)
    elif a.methodology:
        _p(I.methodology(a.group), raw=True)
    else:
        idx = I.build_index(store, fy=a.fy, group=a.group)
        if a.json:
            _p(idx, raw=True)
            return
        print(f"NYC Accountability Index — {a.group}, session "
              f"{idx['session_scored']}, as of {idx['as_of']}")
        print(f"{idx['n_scored']} ranked, {idx['n_provisional']} provisional "
              f"(thin record under {idx['thin_record_cutoff']:.0f} votes)\n")
        for r in idx["ranking"]:
            print(f"  {r['rank']:2}. {r['name'][:28]:30} D{str(r['district'] or ''):3} "
                  f"{r['party'] or '':4} {r['composite']:5.1f}  {r['grade']:22} "
                  f"cov={r['coverage']:.0%}")
        if idx["provisional"]:
            print("\n  Provisional (thin record — not comparable):")
            for r in idx["provisional"]:
                print(f"   p{r['provisional_rank']:2}. {r['name'][:28]:30} "
                      f"{r['composite']:5.1f}  n={r['n_observations']:.0f}")


def cmd_refer(a, store: Store) -> None:
    from .live.greenbook import refer
    _p(refer(store, " ".join(a.problem)), raw=True)


def cmd_calendar(a, store: Store) -> None:
    rows = store.q("""SELECT start, summary, location, kind, owners, pillar
                      FROM calendar ORDER BY start LIMIT ?""", [a.limit])
    if not rows:
        print("no calendar loaded — run: python3 -m universe load --calendar FILE")
        return
    for r in rows:
        owners = ", ".join(json.loads(r["owners"] or "[]")) or "—"
        print(f"{(r['start'] or '')[:16]}  [{r['kind']:9}] {r['summary'][:62]:64} "
              f"{owners}")


def cmd_feeds(a, store: Store) -> None:
    from .live.dashboards import all_dashboards
    from .live.feeds import status
    print("Live feeds:")
    for s in status():
        mark = "ok " if s["ok"] else "DOWN"
        print(f"  [{mark}] {s['feed']:26} rows={s['rows']:<4} "
              f"{'(cached)' if s['cached'] else ''} {s['error'] or ''}")
    print("\nDashboards (interactive, read in browser):")
    for d in all_dashboards():
        print(f"  {d['name'][:34]:36} {d['url'][:70]}")


def cmd_workspace(a, store: Store) -> None:
    from .workspace.launch import main as launch
    store.close()          # the server opens its own connections per request
    argv = ["--host", a.host, "--port", str(a.port)]
    if a.db:
        argv += ["--db", a.db]
    if a.no_browser:
        argv.append("--no-browser")
    if a.reindex:
        argv.append("--reindex")
    if a.seed:
        argv.append("--seed")
    launch(argv)


def cmd_index(a, store: Store) -> None:
    from .workspace import indexer
    _p(indexer.rebuild(store, a.only, progress=lambda m: print("  ", m)), raw=True)


def cmd_ask(a, store: Store) -> None:
    from .workspace import assistant
    out = assistant.ask(store, " ".join(a.question), kind=a.kind,
                        register=a.register, council=a.council)
    print(out["body"] or out.get("why", ""))
    if out.get("quotes"):
        for q in out["quotes"]:
            print(f'\n  "{q["text"]}"\n    — {q["source"]} {q.get("url") or ""}')


def cmd_media(a, store: Store) -> None:
    from .workspace import media
    if a.collect:
        _p(media.refresh_all(store), raw=True)
    else:
        _p(media.coverage(store), raw=True)


def cmd_console(a, store: Store) -> None:
    from .web.build import build
    path = build(store, Path(a.out) if a.out else None)
    print(f"console written: {path}")


def cmd_sources(a, store: Store) -> None:
    reg = _registry(store)
    for s in reg.all():
        print(f"  [{s.tier:17}] {s.source_id:22} {s.name[:56]}")
        if s.url:
            print(f"{'':44}{s.url}")


# ----------------------------------------------------------------- main ----
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="universe",
                                description="District 49 government intelligence system")
    p.add_argument("--db", help="path to the SQLite lake")
    sub = p.add_subparsers(dest="cmd", required=True)

    lo = sub.add_parser("load", help="ingest sources into the lake")
    lo.add_argument("--council-record"); lo.add_argument("--fiscal")
    lo.add_argument("--mocs"); lo.add_argument("--si-rollup")
    lo.add_argument("--channel-rollup", help="SI funding by channel and pot")
    lo.add_argument("--tr-ledger", help="line-by-line Transparency Reso ledger")
    lo.add_argument("--calendar"); lo.add_argument("--greenbook", action="store_true")
    lo.set_defaults(fn=cmd_load)

    st = sub.add_parser("status", help="what is loaded and what works")
    st.set_defaults(fn=cmd_status)

    as_ = sub.add_parser("ask", help="full-text search across everything")
    as_.add_argument("question", nargs="+")
    as_.add_argument("--type", help="members|matters|funding|calendar|contact|deliverable")
    as_.add_argument("--limit", type=int, default=20)
    as_.set_defaults(fn=cmd_ask)

    br = sub.add_parser("brief", help="produce a deliverable")
    br.add_argument("kind", choices=["fiscal", "member", "matter"])
    br.add_argument("subject", nargs="?")
    br.add_argument("--council", action="store_true", help="run the LLM Council")
    br.add_argument("--ask", help="the specific question to deliberate")
    br.add_argument("--json", action="store_true")
    br.set_defaults(fn=cmd_brief)

    fu = sub.add_parser("funding", help="funding pattern intelligence")
    fu.add_argument("what", choices=["portfolio", "drift", "churn", "equity",
                                     "pipeline", "reconcile", "citywide",
                                     "concentration"])
    fu.add_argument("--member"); fu.add_argument("--fy", type=int, default=CURRENT_FY)
    fu.set_defaults(fn=cmd_funding)

    le = sub.add_parser("legislation", help="legislative pattern intelligence")
    le.add_argument("what", choices=["record", "benchmark", "coalition", "signon",
                                     "whip", "delegation", "pending", "find"])
    le.add_argument("--member"); le.add_argument("--matter")
    le.add_argument("--limit", type=int, default=25)
    le.set_defaults(fn=cmd_legislation)

    ix = sub.add_parser("index", help="the NYC Accountability Index")
    ix.add_argument("--official"); ix.add_argument("--group", default="council")
    ix.add_argument("--fy", type=int, default=CURRENT_FY)
    ix.add_argument("--methodology", action="store_true")
    ix.add_argument("--json", action="store_true")
    ix.set_defaults(fn=cmd_index)

    rf = sub.add_parser("refer", help="route a constituent problem to an office")
    rf.add_argument("problem", nargs="+")
    rf.set_defaults(fn=cmd_refer)

    ca = sub.add_parser("calendar", help="the office calendar")
    ca.add_argument("--limit", type=int, default=40)
    ca.set_defaults(fn=cmd_calendar)

    fe = sub.add_parser("feeds", help="live feed connectivity")
    fe.set_defaults(fn=cmd_feeds)

    co = sub.add_parser("console", help="build the single-file HTML console")
    co.add_argument("--out")
    co.set_defaults(fn=cmd_console)

    so = sub.add_parser("sources", help="the citation registry")
    so.set_defaults(fn=cmd_sources)

    ws = sub.add_parser("workspace", help="start the office workspace (web app)")
    ws.add_argument("--host", default="127.0.0.1")
    ws.add_argument("--port", type=int, default=8749)
    ws.add_argument("--no-browser", action="store_true")
    ws.add_argument("--reindex", action="store_true")
    ws.add_argument("--seed", action="store_true")
    ws.set_defaults(fn=cmd_workspace)

    ix2 = sub.add_parser("reindex", help="rebuild the workspace search index")
    ix2.add_argument("--only", nargs="*", help="matters funding orgs members …")
    ix2.set_defaults(fn=cmd_index)

    ask = sub.add_parser("assistant", help="ask the assistant; drafts talking points, quotes, press")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--kind", default="answer")
    ask.add_argument("--register", default="measured")
    ask.add_argument("--council", action="store_true")
    ask.set_defaults(fn=cmd_ask)

    md = sub.add_parser("media", help="the public record: hearings, video, press")
    md.add_argument("--collect", action="store_true", help="pull every feed now")
    md.set_defaults(fn=cmd_media)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.db) if args.db else Store()
    try:
        args.fn(args, store)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
