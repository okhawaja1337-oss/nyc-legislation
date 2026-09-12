# The D49 Universe

A single system for the government work of Council Member Kamillah Hanks,
District 49 — the North Shore of Staten Island. Legislation, budget,
communications and constituent affairs on one desk, with every figure traced
to a source.

It runs offline on a laptop. There is no server to stand up and no account to
create: one SQLite file holds the corpus, one HTML file is the console.

---

## What it is for

The office answers four kinds of question, constantly, under time pressure:

| Question | Where it is answered |
|---|---|
| *What did we fund, what changed, and who is going to call?* | `funding` |
| *What is moving in the Council, and should we be on it?* | `legislation` |
| *How does this official actually perform, on the record?* | `index` |
| *Who does this constituent need to talk to?* | `refer` |

Everything else — briefs, talking points, the console — is those four answers
rendered for whoever is about to walk into a room.

---

## Quick start

```bash
# 1. Load the corpus (each source is optional; load what you have)
python3 -m universe load \
  --council-record  ~/The_Council_Record.html \
  --fiscal          ~/budget/data \
  --mocs            ~/exports/discretionary_tracker.json \
  --si-rollup       ~/exports/si_reconciliation.json \
  --calendar        ~/exports/d49_calendar.json

# 2. See what landed
python3 -m universe status

# 3. Produce a deliverable
python3 -m universe brief fiscal
python3 -m universe brief member Hanks --council

# 4. Build the console and open it
python3 -m universe console
```

`--mocs`, `--si-rollup` and `--calendar` take the JSON that the Google Drive
and Google Calendar connectors return (`{"fileContent": "..."}` and
`{"events": [...]}`). Export once, load once.

---

## The corpus

| Source | What it carries | Provenance |
|---|---|---|
| **The Council Record** | 234 members since 1992, 21,537 matters, 164,236 sponsorships, voting fingerprints, ideal points, election history, career forecasts | derived from Legistar, Wikidata, BOE |
| **Schedule C, FY2022–FY2027** | 50,465 discretionary designations — every organization, EIN, amount, agency and purpose | official primary |
| **Transparency Resolutions** | in-year adds, cuts and reallocations after adoption, line by line with provenance tiers | official primary |
| **SI channel rollup** | every SI pot by channel, capital and expense kept apart | internal |
| **Council tax forecast** | the Finance Division's own revenue projection against OMB | official primary |
| **D49 MOCS tracker** | per-award pipeline status, analyst, change codes | internal |
| **Staten Island reconciliation** | every SI funding line post-TR, by channel and pot | internal |
| **D49 calendar** | hearings, community events, deadlines, staff ownership | internal |
| **The Green Book** | every agency, office and official — the referral directory | official primary |

Live feeds (Legistar, NYC Open Data, the Government Publications Portal, the
Green Book) refresh on top of that with a TTL cache, and fall back to the
cached copy when the network is gone.

---

## The rules the system runs on

These are enforced in code, not asserted in a style guide.

1. **A figure without a citation is not a figure.** Unsourced values render as
   `[verify: source]`. The `verify` stage of every deliverable counts them.
2. **Evidence is assembled before prose.** The lake produces the packet; the
   AI writes over it. With no API key the brief is thinner in prose and
   identical in evidence.
3. **Missing data is missing.** Never imputed, never silently zeroed. It
   lowers a coverage figure that is reported on the face of the output.
4. **Derived is labeled.** Model output — passage odds, ideal points, lean
   estimates, the Accountability Index — is marked as inference, never as fact.
5. **Reconciliation is reported honestly.** When loaded detail does not foot to
   the authoritative total, the system says by how much rather than quietly
   reporting the part it happens to hold.
6. **Capital and expense are never merged.** They are different measures. A
   grand total exists only as two components shown side by side.
7. **A pending-modification line is not money.** It does not take effect until
   a budget modification passes, so it never enters a confirmed total.

### Why the Transparency Resolution figure is not the headline figure

FY2027's two resolutions show a net movement of **$502,851**. Only **$402,851**
of that is confirmed money:

- **$100,000** is a pending-modification designation to the Staten Island
  Institute of Arts and Sciences. It does not exist until a budget
  modification passes.
- **$100,000** was designated to Sundog Theatre in TR#1 and rescinded in TR#2.

That second one is the subtle case. A reversal is a **pair** — the rescinded
designation *and* the line that rescinds it — and both must leave the confirmed
figure, because together they move no money. Filtering on the provenance tier
alone drops only the reversed half and understates confirmed money by the full
$100,000. `universe funding reconcile` pairs them, excludes both, and shows
its work; the arithmetic then foots exactly to the stated net.

---

## Modules

```
universe/
  core/       config (the D49 doctrine), citations, the SQLite lake
  ingest/     Council Record, Schedule C, office spreadsheets, calendar
  live/       Legistar, Open Data, Publications Portal, Green Book, dashboards
  intel/      funding patterns, legislative patterns, the Accountability Index
  ai/         the LLM Council, the briefing engine, the deliverable process
  web/        the single-file console
```

### `intel/funding` — the money
Member portfolio by pot, agency and priority · priority drift across six years ·
organization churn (sustained / new / **lapsed** / growing / cut) · concentration
by Herfindahl · per-resident equity across all 51 districts · MOCS pipeline
exposure · ledger reconciliation.

The lapse list is the one that matters politically: an organization funded four
years running that is not in this year's book *will* call, and the office
should know before they do.

### `intel/legislation` — the record
Sponsorship footprint by priority and committee · peer benchmarks against the
body · coalition by Jaccard overlap (and the **cold list**, where a whip
operation starts) · Staten Island delegation cohesion · sign-on candidates
ranked with their reasons · whip math against 26 and 34.

### `intel/integrity` — the Accountability Index
A transparent score for NYC elected officials. Six pillars, eighteen published
indicators, percentile-normalized **within peer group**. Read
[the methodology](#the-accountability-index) before using a score.

### `ai/` — deliverables
The LLM Council runs five standing perspectives — contrarian, first-principles,
expansionist, outsider, executor — then a blind peer-review round, then a
chairman's synthesis. The process pipeline enforces
`intake → evidence → deliberate → draft → verify → file`, and a deliverable
that skipped verification says so on its face.

Set `ANTHROPIC_API_KEY` for prose synthesis, or point `LLM_COUNCIL_URL` at a
running LLM Council Plus server. Without either, the Council still returns the
structure, the evidence, and the questions each seat presses.

---

## The Accountability Index

Scores every current Council Member — extensible to the Mayor, citywide and
borough offices, and the State and federal delegations — on what the public
record shows.

**Pillars:** Transparency & Disclosure (20%) · Presence & Participation (20%) ·
Legislative Productivity (20%) · Independence of Judgment (15%) · Fiscal
Stewardship (15%) · Accountability & Enforcement (10%).

**Five design commitments:**

- **Peer-group normalization.** A Council Member is scored against Council
  Members. Percentile rank, so the scale is interpretable and outliers do not
  distort it.
- **Coverage gating.** A pillar below 50% indicator coverage is excluded from
  the composite; an official below 50% overall coverage gets no grade at all.
- **Non-monotonic independence.** Near-total alignment with leadership scores
  no better than reflexive opposition. An index that rewards a rubber stamp is
  measuring obedience, not integrity.
- **Current-session presence.** A member who missed votes two terms ago and now
  attends everything is read on what they are doing now. The career figure
  travels alongside as context, never as the score.
- **Thin records ranked separately.** An official below half the peer median
  vote count is scored but listed provisionally. A first-year member's
  attendance over forty votes is not comparable with a veteran's over five
  thousand.

**Grades are relative.** The composite is built from percentile ranks and
clusters near 50 by construction, so a C is a median performer, not a failure.

**Known biases, disclosed on the output:** majority-party advantage in
Productivity; leadership advantage from controlling what moves; tenure effects
in enactment; and the largest one — the index sees only the public record, not
casework, not negotiation, not the majority of what an office actually does.

This methodology was written for this office from standard public-integrity
index practice. It reproduces no other organization's proprietary methodology.

---

## Command reference

```
universe load         --council-record --fiscal --mocs --si-rollup
                      --channel-rollup --tr-ledger --calendar --greenbook
universe status       what is loaded, what works, what the AI layer can do
universe ask          "senior services funding"        full-text across everything
universe brief        fiscal | member NAME | matter ID   [--council] [--ask "..."]
universe funding      portfolio|drift|churn|equity|pipeline|reconcile|citywide|concentration
universe legislation  record|benchmark|coalition|signon|whip|delegation|pending|find
universe index        [--official NAME] [--group council] [--methodology]
universe refer        "no heat and hot water for a week"
universe calendar     [--limit N]
universe feeds        live connectivity and the dashboard register
universe console      [--out PATH]
universe sources      the citation registry
```

Deliverables are written to `out/universe/` as Markdown and JSON, and filed in
the lake with an id so they can be found and reused.

---

## Tests

```bash
python3 -m universe.tests.test_universe
```

The suite encodes the bugs found while building this: sponsorship indices that
are row positions rather than ids, duplicate Schedule C rows collapsing on a
hash, a merged title row hijacking a header parse, an ingest that doubled its
own tables when run twice, and FTS queries that crash on a bare quote.

Network-dependent feeds are asserted on the URLs they build, not on responses,
so the suite passes on a plane.

---

## Known limits

- **Network policy.** Legistar, NYC Open Data, the Publications Portal, the
  Green Book and the Power BI dashboards must be reachable. In a restricted
  environment `universe feeds` reports each one as DOWN rather than failing
  silently.
- **Ledger coverage.** The Staten Island detail currently loads 272 of 712
  lines because the connector truncates large sheet exports. The rollup totals
  are authoritative and foot exactly; `universe funding reconcile` reports the
  gap. Cite the rollup total, not a line count, until a full export is loaded.
- **Initiative names.** Adopted Schedule C records only *Local* or *Citywide*.
  Initiative names come from the office tracker and are backfilled onto adopted
  lines where organization, member and fiscal year match.
- **Power BI.** The Council's dashboards render client-side and publish no
  JSON. Read the figure there and tie it out against Schedule C or Open Data
  before it enters a brief.
