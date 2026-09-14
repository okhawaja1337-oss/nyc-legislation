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


def cmd_reindex(a, store: Store) -> None:
    from .workspace import indexer
    _p(indexer.rebuild(store, a.only, progress=lambda m: print("  ", m)), raw=True)


def cmd_assistant(a, store: Store) -> None:
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




# ------------------------------------------------------- connect & watch ----
def cmd_connect(a, store: Store) -> None:
    """Credentials, connectors and syncs. Secrets never touch the repository."""
    from .core import keys as K
    what = a.what

    if what == "key":
        if a.action == "status" or not a.action:
            rows = K.status()
            print("Credential                Configured  Source")
            for name, info in rows.items():
                mark = f"yes ({info['count']})" if info["configured"] else "no"
                print(f"  {name:<24}{mark:<12}{info['source']}")
            print("\nSecrets are read from the environment or ~/.d49/config.json "
                  "(owner-only).\nThey are never written into this repository.")
            return
        if a.action == "set":
            if not a.name:
                print("usage: universe connect key set <name> [value]"); return
            value = a.value
            if value is None:
                import getpass
                value = getpass.getpass(f"{a.name} (input hidden): ").strip()
            if not value:
                print("nothing entered; nothing written"); return
            current = K.get_all(a.name) if a.name == "anthropic_api_key" else []
            payload = ({a.name: sorted(set(current + [value]), key=lambda v: v != value)}
                       if a.name == "anthropic_api_key" and a.append
                       else {a.name: value})
            path = K.save(payload)
            print(f"stored {a.name} ({K.redact(value)}) in {path} with owner-only permissions")
            return
        if a.action == "clear":
            print("cleared" if K.clear(a.name) else "nothing stored under that name")
            return

    if what == "calendar":
        from .connect import gcal
        if a.ics_url:
            K.save({"calendar_ics_url": a.ics_url})
            print("stored the calendar address in ~/.d49/config.json")
        if a.file:
            _p(gcal.sync_file(store, a.file), raw=True); return
        _p(gcal.sync(store, a.calendar_id, a.days_back, a.days_ahead,
                     a.transport), raw=True)
        return

    if what == "sheets":
        from .connect import gsheets
        if a.file and a.role:
            _p(gsheets.sync_file(store, a.role, a.file), raw=True); return
        if a.list:
            for row in gsheets.connections(store, "sheet"):
                print(f"  {row['id']:<20}{row['role']:<16}{row['label']}")
                print(f"  {'':<20}last sync: {row['last_sync'] or 'never'} "
                      f"({row['last_status'] or '-'})")
            return
        _p(gsheets.sync_all(store, a.transport), raw=True)
        return

    if what == "captions":
        from .connect import captions
        if a.file and a.media:
            cues = captions.parse_any(Path(a.file).read_text())
            _p(captions.save(store, a.media, cues, source="file"), raw=True); return
        if a.video:
            _p(captions.pull(store, a.media or f"video:{a.video}", a.video,
                             a.transport), raw=True); return
        if a.coverage:
            _p(captions.coverage(store), raw=True); return
        _p(captions.pull_missing(store, a.limit, not a.all), raw=True)
        return

    if what == "status":
        from .connect import gsheets
        from .connect import captions
        from .ai import council as AI
        _p({"credentials": K.status(), "ai": AI.available(),
            "sheets": gsheets.connections(store, "sheet"),
            "calendar": store.get_meta("calendar.last_sync"),
            "transcripts": captions.coverage(store)}, raw=True)


def cmd_pipeline(a, store: Store) -> None:
    """The deliverable contract: inputs, outputs, and the gate on each stage."""
    from .core import pipeline as P
    if a.action == "readiness":
        got = P.readiness(store)
        print("READY" if got["ready"] else "NOT READY")
        for table, n in got["counts"].items():
            mark = "  " if n else "!!"
            print(f"  {mark} {table:14s}{n if n is not None else 'missing':>10}")
        print(f"\n{got['note']}")
        return
    if a.action == "gates":
        for stage in P.contract():
            print(f"\n=== {stage['name'].upper()}  —  {stage['does']}")
            for g in stage["gates"]:
                print(f"  [{g['severity']:8s}] {g['asks']}")
                print(f"             {g['because']}")
        return
    if a.action == "check":
        got = _p(None, raw=False) if False else None
        row = store.one("SELECT * FROM deliverables WHERE deliverable_id=?", (a.id,))
        if not row:
            print("no such deliverable"); return
        meta = json.loads(row["meta"] or "{}")
        receipt = meta.get("receipt")
        if not receipt:
            print(f"{row['deliverable_id']} was filed before the contract "
                  f"existed; re-run it to get a receipt."); return
        print(f"{row['kind']}: {row['subject']}")
        print(f"VERDICT: {receipt['verdict'].upper()}")
        print(receipt["why"])
        for st in receipt["stages"]:
            print(f"\n{'OK ' if st['passed'] else '!! '}{st['name']}")
            for g in st["gates"]:
                print(f"     [{'PASS' if g['passed'] else 'FAIL'}] {g['asks']}")
                print(f"            {g['found']}")
        return
    # default: show the declared contract, inputs and outputs
    for stage in P.contract():
        blocking = sum(1 for g in stage["gates"] if g["severity"] == "blocking")
        print(f"\n=== {stage['name'].upper()}")
        print(f"    {stage['does']}")
        print(f"    IN   {' · '.join(stage['consumes'])}")
        print(f"    OUT  {' · '.join(stage['produces'])}")
        print(f"    GATE {len(stage['gates'])} checks, {blocking} blocking")
        for g in stage["gates"]:
            flag = "!" if g["severity"] == "blocking" else " "
            print(f"      {flag} {g['asks']}")
    print("\nA blocking gate that fails stops the deliverable from being filed.")
    print("Run `universe pipeline readiness` to see whether the inputs exist.")


def cmd_directory(a, store: Store) -> None:
    """The Staten Island contact directory, across every level of government."""
    from .live import si_directory as SD
    if a.action == "load":
        _p(SD.load(store), raw=True); return
    if a.action == "route":
        got = SD.route(store, " ".join(a.query or []))
        print(f"Who to call about: {got['problem']}\n")
        for m in got["matches"]:
            reach = m["phone"] or m["email"] or m["url"] or "no contact on file"
            print(f"  {m['agency']} — {m['office']}")
            print(f"    {reach}")
            if m.get("person"):
                print(f"    {m['title'] or 'contact'}: {m['person']}")
            print(f"    confidence: {m['confidence']}  ·  matched on "
                  f"{', '.join(m['matched_on'])}")
            if m.get("note"):
                print(f"    {m['note']}")
            print()
        if got.get("note"):
            print(f"  ⚠ {got['note']}")
        return
    if a.action == "gaps":
        got = SD.gaps(store)
        print(f"{got['total']} contacts on file; "
              f"{len(got['no_phone_or_email'])} with no phone or email, "
              f"{len(got['unverified_people'])} people unconfirmed.\n")
        for row in got["no_phone_or_email"]:
            print(f"  no contact  {row['agency']} — {row['office']}")
        for row in got["unverified_people"]:
            print(f"  unconfirmed {row['person'] or '—'} ({row['office']})")
        print(f"\n{got['how_to_close']}")
        return
    rows = SD.directory(store, a.level or "", " ".join(a.query or []),
                        a.confidence or "")
    level = None
    for r in rows:
        if r["level"] != level:
            level = r["level"]
            print(f"\n=== {level.replace('_', ' ').upper()}")
        who = f"{r['person']} — " if r["person"] else ""
        print(f"  {who}{r['title'] or r['office']}")
        print(f"    {r['agency']} · {r['office']}")
        if r["phone"]:
            print(f"    tel {r['phone']}")
        if r["email"]:
            print(f"    {r['email']}")
        if r["address"]:
            print(f"    {r['address']} {r['zip'] or ''}".rstrip())
        print(f"    [{r['confidence']}] {r['url'] or ''}")


def cmd_watch(a, store: Store) -> None:
    """Change detection over the budget and the legislative record."""
    from .live import watch
    if a.action == "scan":
        _p(watch.scan(store, a.kinds or None, baseline=a.baseline), raw=True)
    elif a.action == "digest":
        d = watch.digest(store, a.hours)
        print(d["headline"]); print()
        for item in d["items"]:
            print(f"  [{item['severity']}] {item['label']}")
            print(f"      {item['field'] or item['change']}: "
                  f"{item['before']} -> {item['after']}")
            print(f"      {item['why']}")
        if d["sources"]:
            print(f"\nSources: {', '.join(d['sources'])}")
    elif a.action == "list":
        for item in watch.recent(store, a.limit, a.severity, a.kind or "",
                                 a.unacknowledged):
            print(f"  {item['at'][:16]}  [{item['severity']:<6}] {item['label']}")
            print(f"      {item['field'] or item['change']}: "
                  f"{item['before']} -> {item['after']}  ({item['why']})")
    elif a.action == "ack":
        print("acknowledged" if watch.acknowledge(store, a.id, a.who or "OK")
              else "no such change")
    else:
        _p(watch.status(store), raw=True)


def cmd_repos(a, store: Store) -> None:
    """The source repositories on GitHub: what is here, and how old it is."""
    from .live import repos as R
    if a.action == "sync":
        out = R.sync(store, a.repo or None, pull=not a.no_pull, force=a.force)
        for key, rec in out["repos"].items():
            if "error" in rec:
                print(f"  {key}: {rec['error']}")
                continue
            print(f"  {key:<12} {rec['state']:<9} {rec.get('says','')}")
            counts = rec.get("manifest", {}).get("counts", {})
            if counts:
                print("               " + "  ".join(
                    f"{k}={v}" for k, v in sorted(counts.items()) if v))
        print()
        print("Anything marked new or stale is loaded on this run. "
              "Run `universe watch scan` to see what moved.")
    elif a.action == "adopt":
        _p(R.adopt(store, a.repo[0] if a.repo else "", a.path), raw=True)
    elif a.action == "stale":
        rows = R.stale(store)
        if not rows:
            print("Every handled file is loaded from its current bytes.")
        for r in rows:
            print(f"  [{r['status']:<9}] {r['path']}  ({r['handler'] or 'no handler'})")
    else:
        st = R.status(store)
        print(f"Clones live under {st['root']}")
        print(f"Last sync: {st['last_sync'] or 'never'}\n")
        for key, rec in st["repos"].items():
            mark = "" if rec.get("cloned") else "  [NOT CLONED]"
            print(f"  {key}{mark}")
            print(f"    {rec['url']}")
            print(f"    {rec['purpose']}")
            print(f"    {rec['files']} files, {rec['bytes'] / 1e6:.1f} MB, "
                  f"last loaded {rec['last_ingest'] or 'never'}")
            if rec["by_status"]:
                print("    " + "  ".join(f"{k}={v}" for k, v in
                                         sorted(rec["by_status"].items())))
            print()


def cmd_position(a, store: Store) -> None:
    """Every defensible answer to 'what did District 49 get'."""
    from .intel import ledger as L
    if a.what == "channels":
        for row in L.channels(store):
            print(f"  {str(row['channel']):<14} {row['lines']:>6} lines  "
                  f"${row['total'] or 0:>14,.0f}")
        return
    if a.what == "categories":
        for row in L.categories(store):
            print(f"  {row['category']:<26} ${row['amount'] or 0:>12,.0f}  "
                  f"{row['share']:>5.1f}%")
        return
    if a.what == "items":
        for row in L.items(store, a.category, limit=a.limit):
            print(f"  ${row['amount'] or 0:>12,.0f}  {row['label'][:86]}")
        return
    if a.what == "find":
        for row in L.find(store, " ".join(a.keyword), limit=a.limit):
            amt = f"${row['amount']:,.0f}" if row["amount"] else "—"
            print(f"  {amt:>14}  [{row['kind']:<9}] {row['label'][:60]}")
            print(f"                  {row['locator']}")
        return
    if a.what == "ties":
        for c in L.tie_checks(store):
            print(f"  [{'PASS' if c['passes'] else 'CHECK'}] {c['sheet'][:32]:<34} "
                  f"{c['check'][:52]}")
        return
    if a.what == "books":
        for book, rec in L.loaded(store).items():
            print(f"  {book}")
            print(f"    {rec['rows']} rows, {rec['sheets']} sheets, "
                  f"{rec['lines']} lines, {rec['ties']} tie-checks")
        return

    pos = L.position(store, a.fy)
    print(f"DISTRICT 49 POSITION — FY{pos['fy']}")
    print("Five true answers to five different questions. "
          "Quoting one without its question is how an office "
          "contradicts itself in public.\n")
    for b in pos["bases"]:
        if b.get("amount") is None:
            print(f"  {b['key']:<15} NOT LOADED — {b['question']}")
            continue
        tie = {True: "ties ✓", False: "DOES NOT TIE", None: "—"}[b.get("ties")]
        print(f"  ${b['amount']:>13,.0f}   {b['question']}")
        extra = f"{b['lines']} lines · " if b.get("lines") else ""
        print(f"  {'':>14}   {extra}{b.get('source') or ''}  [{tie}]")
        print(f"  {'':>14}   {b['caution']}")
        print()
    tc = pos["tie_checks"]
    print(f"Reconciliation checks: {tc['passing']} of {tc['total']} passing.")
    for note in pos["notes"]:
        print(f"  ! {note}")


def cmd_breakdown(a, store: Store) -> None:
    """Cut any legislation or budget search by any dimension."""
    from .workspace import breakdown as BD
    filters = {}
    for pair in (a.filter or []):
        if "=" in pair:
            key, _, value = pair.partition("=")
            filters[key.strip()] = int(value) if value.strip().lstrip("-").isdigit() \
                else value.strip()
    query = " ".join(a.query or [])
    if a.dimensions:
        for dim in BD.dimensions():
            print(f"  {dim['name']:<14}{dim['means']}")
        return
    if a.cross:
        _p(BD.cross(store, a.by, a.cross, query, filters, measure=a.measure), raw=True)
    elif a.profile:
        got = BD.profile(store, query, filters)
        print(f"{got['matched']:,} records matched.\n")
        for name, cut in got["cuts"].items():
            print(BD.to_markdown(cut)); print()
    else:
        print(BD.to_markdown(BD.break_by(store, a.by, query, filters, top=a.top,
                                         order=a.order)))


def cmd_meeting(a, store: Store) -> None:
    """Calendar-driven packets: briefs, talking points, questions."""
    from .workspace import meeting as MT
    if a.action == "list":
        for row in MT.upcoming(store, a.days):
            flag = "*" if row["has_packet"] else " "
            print(f" {flag}{(row['start'] or '')[:16]:18s}{row['packet_kind']:<10}"
                  f"{str(row['committee'] or '-')[:26]:<28}{(row['summary'] or '')[:50]}")
        return
    if a.action == "week":
        got = MT.week(store, a.days, a.fy, use_ai=not a.no_ai, limit=a.limit)
        print(f"{got['meetings']} meetings in the next {got['window_days']} days; "
              f"{got['prepared']} packets built, {got['oversight']} oversight.\n")
        for row in got["packets"]:
            print(f"  {(row['when'] or '')[:16]}  [{row['kind']}] {row['title'][:60]}")
            print(f"      {row['agenda_items']} agenda items, "
                  f"{row['questions']} questions")
            print(f"      {row['bottom_line'][:150]}")
        if got.get("deferred"):
            print(f"\n  {len(got['deferred'])} deferred or cancelled item(s) skipped.")
        return
    if a.action == "packet":
        event = None
        if a.event:
            row = store.one("SELECT * FROM calendar WHERE event_id=?", (a.event,))
            event = dict(row) if row else None
        else:
            wanted = (a.match or "").lower()
            for row in MT.upcoming(store, a.days):
                if not wanted or wanted in (row["summary"] or "").lower():
                    event = row
                    break
        if not event:
            print("no matching calendar item"); return
        event.setdefault("packet_kind", MT.classify(event))
        packet = MT.build(store, event, a.fy, use_ai=not a.no_ai, actor="OK")
        print(MT.to_markdown(packet))
        return
    if a.action == "saved":
        for row in MT.saved(store, a.event or "", a.limit):
            print(f"  {row['id']}\n      {row['title'][:70]} "
                  f"({row['kind']}, {row['mode']}, {row['created'][:16]})")
        return
    if a.action == "read":
        got = MT.read(store, a.id)
        print(got["body"] if got else "no such packet")


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
    ix2.set_defaults(fn=cmd_reindex)

    ask = sub.add_parser("assistant", help="ask the assistant; drafts talking points, quotes, press")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--kind", default="answer")
    ask.add_argument("--register", default="measured")
    ask.add_argument("--council", action="store_true")
    ask.set_defaults(fn=cmd_assistant)

    md = sub.add_parser("media", help="the public record: hearings, video, press")
    md.add_argument("--collect", action="store_true", help="pull every feed now")
    md.set_defaults(fn=cmd_media)

    cn = sub.add_parser("connect", help="credentials, connectors and live syncs")
    cn_sub = cn.add_subparsers(dest="what", required=True)
    ck = cn_sub.add_parser("key", help="store or inspect a credential")
    ck.add_argument("action", nargs="?", choices=["status", "set", "clear"],
                    default="status")
    ck.add_argument("name", nargs="?"); ck.add_argument("value", nargs="?")
    ck.add_argument("--append", action="store_true",
                    help="keep existing keys and add this one (rotation)")
    cc = cn_sub.add_parser("calendar", help="sync the office calendar")
    cc.add_argument("--ics-url", help="the calendar's secret iCal address")
    cc.add_argument("--calendar-id"); cc.add_argument("--file")
    cc.add_argument("--days-back", type=int, default=60)
    cc.add_argument("--days-ahead", type=int, default=180)
    cc.add_argument("--transport", default="auto",
                    choices=["auto", "ics", "public", "api_key", "oauth"])
    cs = cn_sub.add_parser("sheets", help="sync the office's Google Sheets")
    cs.add_argument("--list", action="store_true")
    cs.add_argument("--file"); cs.add_argument("--role")
    cs.add_argument("--transport", default="auto",
                    choices=["auto", "csv", "gviz", "api_key", "oauth"])
    cp = cn_sub.add_parser("captions", help="fetch hearing and video transcripts")
    cp.add_argument("--video", help="a YouTube url or id")
    cp.add_argument("--media", help="the media row to attach the transcript to")
    cp.add_argument("--file", help="a .vtt or .srt caption file")
    cp.add_argument("--limit", type=int, default=25)
    cp.add_argument("--all", action="store_true", help="not just items naming the Member")
    cp.add_argument("--coverage", action="store_true")
    cp.add_argument("--transport", default="auto",
                    choices=["auto", "timedtext", "innertube", "provider", "sidecar"])
    cn_sub.add_parser("status", help="what is connected and what is not")
    cn.set_defaults(fn=cmd_connect)

    pl = sub.add_parser("pipeline",
                        help="the deliverable contract: inputs, outputs, gates")
    pl.add_argument("action", nargs="?",
                    choices=["contract", "gates", "readiness", "check"],
                    default="contract")
    pl.add_argument("--id", help="a deliverable id, for `check`")
    pl.set_defaults(fn=cmd_pipeline)

    dr = sub.add_parser("directory",
                        help="Staten Island contacts at every level of government")
    dr.add_argument("action", nargs="?",
                    choices=["list", "load", "route", "gaps"], default="list")
    dr.add_argument("query", nargs="*", help="a name, agency, or a problem to route")
    dr.add_argument("--level",
                    choices=["federal", "state", "city", "community_board",
                             "nonprofit", "institution"])
    dr.add_argument("--confidence", choices=["published", "listed", "unverified"])
    dr.set_defaults(fn=cmd_directory)

    wt = sub.add_parser("watch", help="detect changes in the budget and legislation")
    wt.add_argument("action", nargs="?",
                    choices=["scan", "digest", "list", "ack", "status"],
                    default="status")
    wt.add_argument("--kinds", nargs="*",
                help="matter funding calendar ledger")
    wt.add_argument("--baseline", action="store_true",
                    help="record the world as it is without reporting changes")
    wt.add_argument("--hours", type=int, default=168)
    wt.add_argument("--limit", type=int, default=50)
    wt.add_argument("--severity", default="", choices=["", "low", "medium", "high"])
    wt.add_argument("--kind", default="")
    wt.add_argument("--unacknowledged", action="store_true")
    wt.add_argument("--id"); wt.add_argument("--who")
    wt.set_defaults(fn=cmd_watch)

    bk = sub.add_parser("breakdown",
                        help="cut any search by member, initiative, committee, agency")
    bk.add_argument("query", nargs="*")
    bk.add_argument("--by", default="member")
    bk.add_argument("--cross", help="second dimension, for a grid")
    bk.add_argument("--measure", default="amount", choices=["amount", "records"])
    bk.add_argument("--filter", nargs="*", help="kind=funding fy=2027 …")
    bk.add_argument("--top", type=int, default=25)
    bk.add_argument("--order", default="amount", choices=["amount", "records", "name"])
    bk.add_argument("--profile", action="store_true", help="every standard cut at once")
    bk.add_argument("--dimensions", action="store_true", help="list what can be cut by")
    bk.set_defaults(fn=cmd_breakdown)

    mt = sub.add_parser("meeting",
                        help="packets for calendar items: briefs, points, questions")
    mt.add_argument("action", nargs="?",
                    choices=["list", "week", "packet", "saved", "read"],
                    default="list")
    mt.add_argument("--days", type=int, default=7)
    mt.add_argument("--fy", type=int, default=CURRENT_FY)
    mt.add_argument("--event", help="a calendar event id")
    mt.add_argument("--match", help="match a calendar item by title")
    mt.add_argument("--id", help="a saved packet id")
    mt.add_argument("--limit", type=int, default=12)
    mt.add_argument("--no-ai", action="store_true", help="evidence only, no prose")
    mt.set_defaults(fn=cmd_meeting)

    rp = sub.add_parser("repos",
                        help="the source repositories on GitHub, kept live")
    rp.add_argument("action", nargs="?",
                    choices=["status", "sync", "stale", "adopt"],
                    default="status")
    rp.add_argument("--repo", nargs="*", help="budget legislation legistar")
    rp.add_argument("--path", help="an existing checkout to adopt")
    rp.add_argument("--no-pull", action="store_true",
                    help="use the clone on disk, do not reach GitHub")
    rp.add_argument("--force", action="store_true",
                    help="re-ingest every handled file, changed or not")
    rp.set_defaults(fn=cmd_repos)

    po = sub.add_parser("position",
                        help="what District 49 actually got, on every basis")
    po.add_argument("what", nargs="?",
                    choices=["summary", "channels", "categories", "items",
                             "find", "ties", "books"],
                    default="summary")
    po.add_argument("keyword", nargs="*", help="for `find`")
    po.add_argument("--fy", type=int, default=CURRENT_FY)
    po.add_argument("--category", help="for `items`")
    po.add_argument("--limit", type=int, default=40)
    po.set_defaults(fn=cmd_position)

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
