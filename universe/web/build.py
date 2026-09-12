#!/usr/bin/env python3
"""
Build the console.

One self-contained HTML file with the office's whole picture embedded: the
fiscal position, the legislative record, the Accountability Index, the
calendar, the referral routes, and the citation registry. No server, no
install, no network -- it opens on a laptop on the Staten Island Ferry.

The payload is deliberately an *aggregate* extract rather than the whole lake.
Fifty thousand funding rows in a browser is a slow page nobody opens; the
figures that drive decisions, with their sources attached, is a page the
office actually uses.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ..core.citations import CitationRegistry, DEFAULT_SOURCES, Source
from ..core.config import (CURRENT_FY, DISTRICT, DISTRICT_LABEL, MEMBER_LAST,
                           MEMBER_NAME, OUT_DIR, PILLARS)
from ..core.store import Store
from ..intel import funding as FI
from ..intel import integrity as II
from ..intel import legislation as LI
from ..live.dashboards import all_dashboards


def payload(store: Store, fy: int = CURRENT_FY) -> dict:
    reg = CitationRegistry(DEFAULT_SOURCES)
    for r in store.q("SELECT * FROM sources"):
        d = dict(r)
        if d.get("source_id") and d["source_id"] not in reg:
            reg.add(Source(**{k: d.get(k) for k in
                              ("source_id", "name", "publisher", "url", "accessed",
                               "coverage", "tier", "locator", "notes")}))

    idx = II.build_index(store, fy=fy)
    slim = [{k: o[k] for k in ("person_id", "name", "party", "district",
                               "composite", "grade", "coverage", "peer_percentile",
                               "thin_record", "rank")
             if k in o} | {"pillars": {pk: pv["score"] for pk, pv in o["pillars"].items()}}
            for o in idx["officials"]]

    return {
        "built": date.today().isoformat(),
        "member": MEMBER_NAME, "district": DISTRICT, "district_label": DISTRICT_LABEL,
        "fy": fy,
        "counts": store.counts(),
        "headlines": (store.get_meta("council_record.loaded") or {}).get("headlines", {}),
        "pillars": {k: v["label"] for k, v in PILLARS.items()},
        "fiscal": {
            "citywide": FI.citywide_context(store),
            "portfolio": FI.member_portfolio(store, MEMBER_LAST, fy),
            "drift": FI.pillar_drift(store, MEMBER_LAST),
            "concentration": FI.concentration(store, MEMBER_LAST, fy),
            "equity": FI.district_equity(store, fy),
            "churn": FI.org_trajectories(store, MEMBER_LAST),
            "pipeline": FI.pipeline_risk(store, fy),
            "reconcile": FI.reconcile_si(store, fy),
        },
        "legislation": {
            "record": LI.legislative_record(store, MEMBER_LAST),
            "benchmark": LI.peer_benchmark(store, MEMBER_LAST),
            "coalition": LI.coalition(store, MEMBER_LAST, limit=15),
            "delegation": LI.si_delegation(store),
            "signon": LI.signon_candidates(store, MEMBER_LAST, limit=15),
            "pending": LI.pending_for_pillars(store, limit=40),
        },
        "index": {"summary": {k: idx[k] for k in
                              ("as_of", "session_scored", "n_scored",
                               "n_provisional", "median_observations",
                               "thin_record_cutoff", "coverage_floor")},
                  "ranking": idx["ranking"], "provisional": idx["provisional"],
                  "officials": slim, "methodology": idx["methodology"]},
        "calendar": [dict(r) for r in store.q(
            "SELECT start, end, summary, location, kind, owners, pillar "
            "FROM calendar ORDER BY start LIMIT 400")],
        "dashboards": all_dashboards(),
        "sources": [s.to_dict() for s in reg.all()],
        "deliverables": [dict(r) for r in store.q(
            "SELECT deliverable_id, kind, subject, status, created "
            "FROM deliverables ORDER BY created DESC LIMIT 50")],
        "journal": store.read_journal(60),
    }


HTML = r"""<title>D49 Universe — Government Intelligence Console</title>
<style>
:root{
  --paper:#FAF9F6;--surface:#fff;--surface2:#F2F0EA;--line:#DEDACF;--line2:#CBC6B8;
  --ink:#1C1B17;--ink2:#57544B;--muted:#8A8678;
  --navy:#16306B;--navy-ink:#16306B;--gold:#BF9000;--gold-soft:#F4EBD3;--gold-ink:#7A5D06;
  --dem:#3B66A3;--rep:#A3403B;
  --good:#2E6B34;--good-bg:#E3EEE0;--warn:#8A6D00;--warn-bg:#F6EFD2;--crit:#8E2F2A;--crit-bg:#F6DFDC;
  --serif:"Iowan Old Style",Georgia,"Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#101623;--surface:#18202F;--surface2:#1E2738;--line:#2C3549;--line2:#3A445A;
  --ink:#E9E7DF;--ink2:#B4B1A5;--muted:#847F6F;--navy-ink:#9FB4E4;--gold-soft:#2E2A1A;--gold-ink:#D9B54A;
  --dem:#4C7FD0;--rep:#C7574A;
  --good:#7FBF87;--good-bg:#1C2A1E;--warn:#D9B54A;--warn-bg:#2C2717;--crit:#E08A83;--crit-bg:#2E1B19;}}
:root[data-theme="dark"]{
  --paper:#101623;--surface:#18202F;--surface2:#1E2738;--line:#2C3549;--line2:#3A445A;
  --ink:#E9E7DF;--ink2:#B4B1A5;--muted:#847F6F;--navy-ink:#9FB4E4;--gold-soft:#2E2A1A;--gold-ink:#D9B54A;
  --dem:#4C7FD0;--rep:#C7574A;
  --good:#7FBF87;--good-bg:#1C2A1E;--warn:#D9B54A;--warn-bg:#2C2717;--crit:#E08A83;--crit-bg:#2E1B19;}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:14.5px;line-height:1.55;margin:0}
.wrap{max-width:1180px;margin:0 auto;padding-inline:20px}
header{border-bottom:3px double var(--gold);background:var(--surface)}
header .wrap{padding-block:22px 0}
.eyebrow{font-size:10.5px;letter-spacing:.22em;color:var(--navy-ink);font-weight:700;text-transform:uppercase}
h1{font-family:var(--serif);font-size:clamp(26px,4vw,38px);margin:.15em 0 .1em;letter-spacing:-.01em}
.sub{color:var(--ink2);max-width:72ch;margin:0 0 14px}
nav{display:flex;flex-wrap:wrap;gap:2px;margin-top:14px}
nav button{background:none;border:none;border-bottom:3px solid transparent;padding:9px 13px;
  font:inherit;font-weight:600;color:var(--ink2);cursor:pointer;border-radius:4px 4px 0 0}
nav button:hover{background:var(--surface2)}
nav button[aria-selected="true"]{color:var(--navy-ink);border-bottom-color:var(--gold);background:var(--surface2)}
main{padding-block:24px 60px}
section[hidden]{display:none!important}
h2{font-family:var(--serif);font-size:22px;margin:26px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
h3{font-size:14px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:20px 0 8px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px}
.kpi{font-family:var(--serif);font-size:27px;font-weight:700;letter-spacing:-.02em;line-height:1.1}
.kpi.sm{font-size:20px}
.klabel{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:5px}
.knote{font-size:12px;color:var(--ink2);margin-top:5px}
table{width:100%;border-collapse:collapse;font-size:13px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--surface2);font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);position:sticky;top:0}
tbody tr:hover{background:var(--surface2)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;font-weight:700;border:1px solid var(--line2)}
.pill.dem{color:var(--dem);border-color:var(--dem)}.pill.rep{color:var(--rep);border-color:var(--rep)}
.pill.good{background:var(--good-bg);color:var(--good);border-color:var(--good)}
.pill.warn{background:var(--warn-bg);color:var(--warn);border-color:var(--warn)}
.pill.crit{background:var(--crit-bg);color:var(--crit);border-color:var(--crit)}
.note{background:var(--gold-soft);border:1px solid var(--gold);color:var(--gold-ink);
  padding:10px 13px;border-radius:7px;font-size:13px;margin:12px 0}
.muted{color:var(--muted);font-size:12.5px}
a{color:var(--navy-ink)}
input[type=search],select{font:inherit;padding:7px 10px;border:1px solid var(--line2);
  border-radius:6px;background:var(--surface);color:var(--ink);min-width:220px}
.bar{height:7px;background:var(--surface2);border-radius:99px;overflow:hidden;min-width:70px}
.bar>i{display:block;height:100%;background:var(--navy-ink)}
.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:10px 0}
details{border:1px solid var(--line);border-radius:7px;padding:9px 12px;margin:8px 0;background:var(--surface)}
summary{cursor:pointer;font-weight:600}
footer{border-top:1px solid var(--line);color:var(--muted);font-size:12px;padding-block:16px}
@media(max-width:620px){.kpi{font-size:22px}nav button{padding:8px 10px;font-size:13px}}
</style>

<header><div class="wrap">
  <div class="eyebrow">District 49 · North Shore · Staten Island</div>
  <h1>The D49 Universe</h1>
  <p class="sub">Legislation, budget, communications and constituent affairs for
  Council Member Kamillah Hanks — one desk, every source cited.</p>
  <nav id="tabs" role="tablist"></nav>
</div></header>

<main class="wrap" id="main"></main>
<footer class="wrap">
  <span id="foot"></span> ·
  <button onclick="document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark'"
    style="background:none;border:1px solid var(--line2);border-radius:5px;padding:3px 9px;cursor:pointer;font:inherit;color:inherit">theme</button>
</footer>

<script id="universe-data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('universe-data').textContent);
const $ = (h) => { const t=document.createElement('template'); t.innerHTML=h.trim(); return t.content; };
const usd = n => n==null ? '—' : '$'+Math.round(n).toLocaleString();
const num = n => n==null ? '—' : Number(n).toLocaleString(undefined,{maximumFractionDigits:2});
const pct = n => n==null ? '—' : Number(n).toFixed(1)+'%';
const esc = s => String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const kpi=(label,val,note)=>`<div class="card"><div class="klabel">${esc(label)}</div>
  <div class="kpi">${val}</div>${note?`<div class="knote">${note}</div>`:''}</div>`;
const table=(cols,rows)=>`<div class="scroll"><table><thead><tr>${
  cols.map(c=>`<th class="${c.num?'num':''}">${esc(c.label)}</th>`).join('')}</tr></thead><tbody>${
  rows.map(r=>`<tr>${cols.map(c=>`<td class="${c.num?'num':''}">${c.get(r)??'—'}</td>`).join('')}</tr>`).join('')
  }</tbody></table></div>`;
const party=p=>`<span class="pill ${p==='DEM'?'dem':p==='REP'?'rep':''}">${esc(p||'—')}</span>`;

const TABS = [
 ['brief','Command'],['fiscal','Budget'],['patterns','Patterns'],
 ['legis','Legislation'],['index','Accountability Index'],
 ['calendar','Calendar'],['refer','Constituent Routing'],['sources','Sources']
];

// ---------------------------------------------------------------- views --
const V = {};

V.brief = () => {
  const f=D.fiscal, l=D.legislation, e=f.equity.focus||{}, rec=f.reconcile;
  const t=l.record.totals||{};
  return `<h2>Where things stand</h2>
  <div class="grid">
   ${kpi('FY'+D.fy+' D49 designations', usd(f.portfolio.total), f.portfolio.lines+' lines')}
   ${kpi('Per resident', '$'+num(e.per_resident), 'rank '+e.rank+' of '+(f.equity.all||[]).length+' districts')}
   ${kpi('SI capital §254', usd(rec.capital.adopted), num(rec.capital.lines)+' lines — never merged with expense')}
   ${kpi('SI expense', usd(rec.expense.adopted), num(rec.expense.lines)+' lines')}
   ${kpi('TR confirmed', usd(rec.tr_movement.confirmed), 'vs '+usd(rec.tr_movement.stated_net)+' headline')}
   ${kpi('In MOCS pipeline', usd(f.pipeline.pending_total), pct(f.pipeline.pending_share_pct)+' of tracked')}
   ${kpi('Prime sponsored', num(t.prime), num(t.prime_enacted)+' enacted ('+pct((l.record.enactment_rate||0)*100)+')')}
   ${kpi('Accountability Index', (D.index.officials.find(o=>o.district===D.district)||{}).grade||'—',
        'peer percentile '+num((D.index.officials.find(o=>o.district===D.district)||{}).peer_percentile))}
  </div>
  <div class="note"><b>${esc(rec.rule)}</b><br>
   Capital ${usd(rec.capital.adopted)} over ${num(rec.capital.lines)} lines ·
   Expense ${usd(rec.expense.adopted)} over ${num(rec.expense.lines)} lines.</div>
  <div class="note"><b>Transparency Resolution movement.</b> The headline net is
   ${usd(rec.tr_movement.stated_net)}, but confirmed money is
   <b>${usd(rec.tr_movement.confirmed)}</b> —
   ${usd(rec.tr_movement.pending_mod)} awaits a budget modification and
   ${usd(rec.tr_movement.reversed_and_excluded)} was designated then rescinded
   ${(rec.tr_movement.reversal_pairs||[]).map(p=>`(${esc(p.org)}, ${esc(p.designated_in)} → ${esc(p.reversed_in)})`).join(', ')}.
   ${esc(rec.tr_movement.reading)}</div>

  <h2>What the delegation looks like</h2>
  <p class="muted">${esc(l.delegation.reading)}</p>
  ${table([{label:'Pair',get:r=>esc(r.a)+' ↔ '+esc(r.b)},
           {label:'Agreement',num:1,get:r=>num(r.agreement)},
           {label:'Contested votes',num:1,get:r=>num(r.n)}], l.delegation.pairs)}
  <p class="muted">${esc(l.delegation.implication)}</p>

  <h2>Lapsed grantees most likely to call</h2>
  ${table([{label:'Organization',get:r=>esc(r.org)},
           {label:'Years funded',num:1,get:r=>r.years_funded},
           {label:'Last FY',num:1,get:r=>r.last_fy},
           {label:'Lifetime',num:1,get:r=>usd(r.total)},
           {label:'Risk',get:r=>`<span class="pill ${r.risk==='call expected'?'warn':''}">${esc(r.risk||'low')}</span>`}],
          f.churn.lapsed.slice(0,12))}`;
};

V.fiscal = () => {
  const f=D.fiscal, c=f.citywide, g=c.growth;
  return `<h2>The citywide trend</h2>
  <div class="grid">
   ${kpi('Schedule C growth', pct(g.total_pct), g.span)}
   ${kpi('Member designations', pct(g.member_pct), 'growth since '+(g.span||'').split('-')[0])}
   ${kpi('Citywide initiatives', pct(g.citywide_pct), 'the pots members do not control directly')}
  </div>
  ${table([{label:'FY',get:r=>r.fy},{label:'Designations',num:1,get:r=>num(r.n)},
           {label:'Total',num:1,get:r=>usd(r.total)},
           {label:'Member',num:1,get:r=>usd(r.member_total)},
           {label:'Citywide',num:1,get:r=>usd(r.citywide_total)}], c.by_fy)}
  ${(c.forecast&&c.forecast.highlights||[]).length?`<h3>Revenue outlook — ${esc(c.forecast.label||'')}</h3>
    <ul>${c.forecast.highlights.map(h=>`<li>${esc(h)}</li>`).join('')}</ul>`:''}

  <h2>District ${D.district} portfolio, FY${D.fy}</h2>
  ${table([{label:'Priority',get:r=>esc(D.pillars[r.pillar]||r.pillar)},
           {label:'Lines',num:1,get:r=>num(r.lines)},
           {label:'Total',num:1,get:r=>usd(r.total)},
           {label:'Share',num:1,get:r=>`<div class="bar" title="${pct(r.share_pct)}"><i style="width:${Math.min(100,r.share_pct||0)}%"></i></div>`},
           {label:'%',num:1,get:r=>pct(r.share_pct)}], f.portfolio.by_pillar)}

  <h3>Largest grantees</h3>
  ${table([{label:'Organization',get:r=>esc((r.org||'').split(' - ')[0])},
           {label:'EIN',get:r=>esc(r.ein)},{label:'Lines',num:1,get:r=>r.lines},
           {label:'Total',num:1,get:r=>usd(r.total)},
           {label:'Share',num:1,get:r=>pct(r.share_pct)}], f.portfolio.top_orgs.slice(0,15))}

  <h2>Per-resident delivery across the 51 districts</h2>
  <p class="muted">${esc(f.equity.caveat)}</p>
  ${table([{label:'Rank',num:1,get:r=>r.rank},{label:'District',num:1,get:r=>'D'+r.district},
           {label:'Total',num:1,get:r=>usd(r.total)},
           {label:'Population',num:1,get:r=>num(r.population)},
           {label:'Per resident',num:1,get:r=>'$'+num(r.per_resident)}],
          (f.equity.all||[]).filter(r=>r.rank<=10||[49,50,51].includes(r.district)))}

  <h2>MOCS pipeline exposure</h2>
  <p class="muted">${esc(f.pipeline.caveat)}</p>
  ${table([{label:'Status',get:r=>esc(r.status)},{label:'Lines',num:1,get:r=>r.lines},
           {label:'Total',num:1,get:r=>usd(r.total)}], f.pipeline.by_status)}
  ${f.pipeline.pending.length?`<h3>Pending awards</h3>${table([
    {label:'Organization',get:r=>esc(r.org)},{label:'Initiative',get:r=>esc(r.pot)},
    {label:'Amount',num:1,get:r=>usd(r.amount)},{label:'Analyst',get:r=>esc(r.analyst)}],
    f.pipeline.pending.slice(0,25))}`:''}`;
};

V.patterns = () => {
  const f=D.fiscal, ch=f.churn, co=f.concentration;
  return `<h2>Priority drift</h2>
  <p class="muted">How the district's funding mix moved across fiscal years — the
   one-sentence story a budget director has to be able to tell.</p>
  ${table([{label:'Priority',get:r=>esc(r.label)},
           {label:'First FY',num:1,get:r=>r.first_fy},{label:'Last FY',num:1,get:r=>r.last_fy},
           {label:'Share change',num:1,get:r=>{const v=r.share_change_pts;
             return v==null?'—':`<span class="pill ${v>0?'good':v<0?'crit':''}">${v>0?'+':''}${num(v)} pts</span>`}},
           {label:'Dollar change',num:1,get:r=>usd(r.dollar_change)}], f.drift.drift)}

  <h2>Concentration</h2>
  <div class="grid">
   ${kpi('Organizations funded', num(co.orgs), 'FY'+co.fy)}
   ${kpi('Effective organizations', num(co.effective_orgs), co.reading)}
   ${kpi('Top 5 share', pct(co.top5_share_pct), 'top 10: '+pct(co.top10_share_pct))}
   ${kpi('HHI', num(co.hhi), 'above 2,500 is highly concentrated')}
  </div>

  <h2>Organization churn</h2>
  <div class="grid">
   ${kpi('Sustained', num(ch.counts.sustained),'funded again this year')}
   ${kpi('New', num(ch.counts.new),'first time in the book')}
   ${kpi('Lapsed', num(ch.counts.lapsed),'funded before, not this year')}
   ${kpi('Grew / cut', num(ch.counts.growing)+' / '+num(ch.counts.cut),'against prior year')}
  </div>
  <h3>Largest increases</h3>
  ${table([{label:'Organization',get:r=>esc(r.org)},{label:'Prior',num:1,get:r=>usd(r.prior)},
           {label:'Latest',num:1,get:r=>usd(r.latest)},
           {label:'Change',num:1,get:r=>`<span class="pill good">+${num(r.change)}</span>`}],
          ch.growing.slice(0,10))}
  <h3>Largest reductions</h3>
  ${table([{label:'Organization',get:r=>esc(r.org)},{label:'Prior',num:1,get:r=>usd(r.prior)},
           {label:'Latest',num:1,get:r=>usd(r.latest)},
           {label:'Change',num:1,get:r=>`<span class="pill crit">${num(r.change)}</span>`}],
          ch.cut.slice(0,10))}`;
};

V.legis = () => {
  const l=D.legislation, r=l.record, b=l.benchmark, beh=r.behavior;
  const rk=(x)=>x?`${x.rank} of ${x.of} (median ${num(x.median)})`:'—';
  return `<h2>The record</h2>
  <div class="grid">
   ${kpi('Prime sponsored', num(r.totals.prime), rk(b.prime_sponsored))}
   ${kpi('Co-sponsored', num(r.totals.cosponsor), rk(b.cosponsored))}
   ${kpi('Enacted', num(r.totals.prime_enacted), rk(b.enacted))}
   ${kpi('Enactment rate', pct((r.enactment_rate||0)*100),'of prime-sponsored introductions')}
  </div>
  <div class="note"><b>Voting behaviour</b> (model-derived from the roll-call record,
   labeled inference): dissent ${pct((beh.dissent_rate||0)*100)} ·
   alignment with the Speaker ${pct((beh.alignment_with_speaker||0)*100)} ·
   profile “${esc(beh.profile_type||'—')}”.</div>

  <h3>Where the record sits, by committee</h3>
  ${table([{label:'Committee',get:r=>esc(r.committee)},{label:'Prime items',num:1,get:r=>r.n},
           {label:'Enacted',num:1,get:r=>num(r.enacted)}], r.by_committee)}

  <h2>Sign-on candidates</h2>
  <p class="muted">${esc(l.signon.label)}</p>
  ${table([{label:'File',get:r=>esc(r.file)},{label:'Bill',get:r=>esc(r.name)},
           {label:'Status',get:r=>esc(r.status)},{label:'Sponsors',num:1,get:r=>r.n_sponsors},
           {label:'Odds',num:1,get:r=>pct((r.pass_prob||0)*100)},
           {label:'Why',get:r=>esc((r.reasons||[]).join('; '))}], l.signon.candidates)}

  <h2>Closest legislative partners</h2>
  ${table([{label:'Member',get:r=>esc(r.name)},{label:'District',num:1,get:r=>'D'+r.district},
           {label:'Party',get:r=>party(r.party)},
           {label:'Shared items',num:1,get:r=>num(r.shared)},
           {label:'Overlap',num:1,get:r=>num(r.jaccard)}], l.coalition.partners)}`;
};

V.index = () => {
  const i=D.index, m=i.methodology, s=i.summary;
  return `<h2>NYC Accountability Index</h2>
  <p class="muted">Session ${esc(s.session_scored)} · ${s.n_scored} ranked ·
   ${s.n_provisional} provisional · as of ${esc(s.as_of)}</p>
  <div class="note"><b>Read the pillars, not only the composite.</b>
   ${esc(m.grade_scale)}</div>
  ${table([{label:'#',num:1,get:r=>r.rank},{label:'Official',get:r=>esc(r.name)},
           {label:'D',num:1,get:r=>r.district},{label:'Party',get:r=>party(r.party)},
           {label:'Score',num:1,get:r=>num(r.composite)},
           {label:'Grade',get:r=>`<span class="pill ${r.composite>=60?'good':r.composite>=40?'warn':'crit'}">${esc(r.grade)}</span>`},
           {label:'Coverage',num:1,get:r=>pct((r.coverage||0)*100)},
           {label:'Votes',num:1,get:r=>num(r.n_observations)}], i.ranking)}
  ${i.provisional.length?`<h3>Provisional — thin record, not comparable</h3>
   ${table([{label:'Official',get:r=>esc(r.name)},{label:'D',num:1,get:r=>r.district},
            {label:'Score',num:1,get:r=>num(r.composite)},
            {label:'Votes',num:1,get:r=>num(r.n_observations)}], i.provisional)}`:''}

  <h2>Methodology</h2>
  <p>${esc(m.normalization)}</p><p>${esc(m.aggregation)}</p>
  <p><b>Missing data.</b> ${esc(m.missing_data)}</p>
  <p><b>Thin records.</b> ${esc(m.thin_record_rule)}</p>
  <p><b>Presence.</b> ${esc(m.session_rule)}</p>
  <details open><summary>Known biases — read before using a score</summary>
   <ul>${(m.known_biases||[]).map(b=>`<li>${esc(b)}</li>`).join('')}</ul></details>
  <h3>Pillars</h3>
  ${table([{label:'Pillar',get:r=>esc(r.label)},{label:'Weight',num:1,get:r=>pct(r.weight*100)},
           {label:'Asks',get:r=>esc(r.asks)}], m.pillars)}
  <h3>Indicators</h3>
  <p class="muted">${esc(m.coverage_note)}</p>
  ${table([{label:'Indicator',get:r=>esc(r.label)},{label:'Pillar',get:r=>esc(r.pillar)},
           {label:'Direction',get:r=>esc(r.direction.replace(/_/g,' '))},
           {label:'Weight',num:1,get:r=>num(r.weight)},
           {label:'Source',get:r=>esc(r.source_id)},
           {label:'Live',get:r=>r.computed?'<span class="pill good">computed</span>':'<span class="pill warn">declared</span>'},
           {label:'Definition',get:r=>esc(r.definition)}], m.indicators)}`;
};

V.calendar = () => {
  if(!D.calendar.length) return `<h2>Calendar</h2><div class="note">No calendar loaded.
   Run <code>python3 -m universe load --calendar FILE</code>.</div>`;
  const kinds=[...new Set(D.calendar.map(e=>e.kind))];
  return `<h2>The office calendar</h2>
  <div class="row"><input type="search" id="calq" placeholder="filter events…">
   <select id="calk"><option value="">every kind</option>
   ${kinds.map(k=>`<option>${esc(k)}</option>`).join('')}</select></div>
  <div id="calout"></div>`;
};

V.refer = () => `<h2>Constituent routing</h2>
  <p class="muted">Type what the constituent actually said. Routing comes from the
   office's service-area table; contact details come from the Green Book once loaded.</p>
  <div class="row"><input type="search" id="refq" style="min-width:min(100%,460px)"
   placeholder="no heat and hot water for a week and the landlord is ignoring us"></div>
  <div id="refout"></div>
  <div class="note">If nothing routes, 311 is the system of record —
   <a href="https://portal.311.nyc.gov/" target="_blank" rel="noopener">open a service
   request</a> and keep the SR number so the office can escalate it.</div>`;

V.sources = () => `<h2>Dashboards</h2>
  <p class="muted">Interactive reports that publish no API — read the figure there,
   then tie it out against Schedule C or Open Data before it enters a brief.</p>
  ${table([{label:'Dashboard',get:r=>`<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a>`},
           {label:'Publisher',get:r=>esc(r.publisher)},
           {label:'Answers',get:r=>esc(r.answers)}], D.dashboards)}
  <h2>Citation registry</h2>
  <p class="muted">Every source the system will cite, with its provenance tier.</p>
  ${table([{label:'Tier',get:r=>`<span class="pill ${r.tier.startsWith('OFFICIAL')?'good':r.tier==='INTERNAL'?'warn':''}">${esc(r.tier)}</span>`},
           {label:'ID',get:r=>esc(r.source_id)},
           {label:'Source',get:r=>r.url?`<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a>`:esc(r.name)},
           {label:'Publisher',get:r=>esc(r.publisher)},
           {label:'Coverage',get:r=>esc(r.coverage)}], D.sources)}
  <h2>Load journal</h2>
  ${table([{label:'When',get:r=>esc((r.ts||'').slice(0,19))},{label:'Event',get:r=>esc(r.event)},
           {label:'Detail',get:r=>esc(JSON.stringify(Object.fromEntries(
             Object.entries(r).filter(([k])=>!['ts','event'].includes(k)))).slice(0,140))}],
          D.journal.slice(0,25))}`;

// --------------------------------------------------------------- wiring --
const SERVICE = {
 HPD:['housing code violations','heat and hot water','Section 8','affordable housing lotteries','landlord harassment'],
 DOB:['construction permits','illegal conversions','stop work orders','facade and scaffolding','elevator complaints'],
 DOT:['potholes','street resurfacing','traffic signals','bus stops','sidewalk repair','street lights','parking regulations'],
 DSNY:['missed collection','illegal dumping','street cleaning','containerization','snow removal'],
 DPR:['park maintenance','street trees','playgrounds','tree pruning'],
 NYPD:['precinct concerns','quality of life enforcement','traffic safety','community affairs'],
 DOE:['school placement','IEP and special education','busing','school capital projects'],
 DFTA:['older adult centers','home-delivered meals','caregiver support','benefits screening'],
 HRA:['SNAP','cash assistance','Medicaid','rental arrears','one-shot deals'],
 DHS:['shelter placement','street homelessness outreach'],
 DOHMH:['restaurant grades','rodent complaints','lead paint','mental health services'],
 ACS:['child welfare','child care vouchers','foster care'],
 SBS:['small business support','commercial leases','MWBE certification'],
 DEP:['water bills','sewer backups','catch basins','noise complaints'],
 DCWP:['consumer complaints','paid sick leave','licensing'],
 MOIA:['immigration legal services','IDNYC'],
 DORIS:['vital records','municipal archives']
};
function route(q){
  const low=q.toLowerCase(); const out=[];
  for(const [code,areas] of Object.entries(SERVICE)){
    let s=0; for(const a of areas){ if(low.includes(a)) s+=2;
      for(const w of a.split(' ')) if(w.length>4&&low.includes(w)) s+=1; }
    if(s) out.push({code,areas,score:s});
  }
  return out.sort((a,b)=>b.score-a.score);
}
function bindRefer(){
  const q=document.getElementById('refq'), out=document.getElementById('refout');
  if(!q) return;
  const run=()=>{ const r=route(q.value);
    out.innerHTML = !q.value.trim() ? '' : r.length
      ? table([{label:'Send to',get:x=>`<b>${esc(x.code)}</b>`},
               {label:'Because they own',get:x=>esc(x.areas.join('; '))},
               {label:'Match',num:1,get:x=>x.score}], r)
      : `<div class="note">No agency route matched. Use 311 and log the SR number.</div>`;
  };
  q.addEventListener('input',run);
}
function bindCal(){
  const q=document.getElementById('calq'), k=document.getElementById('calk'),
        out=document.getElementById('calout');
  if(!out) return;
  const run=()=>{ const term=(q.value||'').toLowerCase(), kind=k.value;
    const rows=D.calendar.filter(e=>(!kind||e.kind===kind) &&
      (!term || (e.summary+' '+(e.location||'')).toLowerCase().includes(term)));
    out.innerHTML = table([
      {label:'When',get:e=>esc((e.start||'').slice(0,16).replace('T',' '))},
      {label:'Kind',get:e=>`<span class="pill">${esc(e.kind)}</span>`},
      {label:'Event',get:e=>esc(e.summary)},
      {label:'Where',get:e=>esc(e.location)},
      {label:'Owners',get:e=>esc((JSON.parse(e.owners||'[]')).join(', '))}], rows);
  };
  q.addEventListener('input',run); k.addEventListener('change',run); run();
}

const nav=document.getElementById('tabs'), main=document.getElementById('main');
TABS.forEach(([id,label],i)=>{
  const b=document.createElement('button');
  b.textContent=label; b.setAttribute('role','tab'); b.dataset.view=id;
  b.onclick=()=>show(id); nav.appendChild(b);
});
function show(id){
  [...nav.children].forEach(b=>b.setAttribute('aria-selected', String(b.dataset.view===id)));
  main.innerHTML = V[id] ? V[id]() : '';
  if(id==='refer') bindRefer();
  if(id==='calendar') bindCal();
  location.hash = id;
}
document.getElementById('foot').textContent =
  `Built ${D.built} · ${num(D.counts.matters)} matters · ${num(D.counts.funding)} funding lines · ` +
  `${num(D.counts.members)} members · ${num(D.counts.sources)} sources`;
show((location.hash||'').replace('#','') in V ? location.hash.replace('#','') : 'brief');
</script>
"""


def build(store: Store, out: Path | None = None, fy: int = CURRENT_FY) -> Path:
    data = payload(store, fy)
    html = HTML.replace("__DATA__", json.dumps(data, default=str)
                        .replace("</script>", "<\\/script>"))
    out = out or (OUT_DIR / "d49-universe-console.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    store.journal("console.built", {"path": str(out), "bytes": len(html)})
    return out
