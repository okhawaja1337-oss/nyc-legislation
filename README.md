# 🗽 NYC Legislative Intelligence

A single desk for the legislation, people, elections, and votes that shape New
York City — across **three levels of government**: City Hall, Albany, and
Washington. It turns official data into tight, plain-English briefings built for
a busy elected official and their staff — the house style is **"Bulletpoints for
Bureaucrats."**

Built as one [Streamlit](https://streamlit.io) app on top of the NYC Council's
official **Legistar** system, extended with the NY State and U.S. Congress data
sources and an AI briefing/ideation layer.

---

## What it does

### 📜 Legislation (City Hall — live from NYC Legistar)
- **Legislation list** — every bill for a chosen year, all types, searchable by number or word.
- **Bill detail** — sponsors, committee, status, action history, roll-call votes, attachments, full text, and an optional AI policy analysis.
- **Hearings** — committee meeting schedule, locations, agendas, and outcomes.
- **What changed** — new co-sponsors, amendments, and status moves since the last load.
- **Overview** — counts by type, status, and policy topic, with an Excel export.

### 🌐 All Levels (Albany + Washington)
- **🏠 Find my reps** — type any NYC address and get the officials who represent it: City Council, State Senate, State Assembly, and U.S. House districts, each matched to its officeholder. (Council + House resolve with **no key**.)
- **🏙️ State & Federal** — search **NY State** bills and track what **NYC's U.S. congressional delegation** is sponsoring/cosponsoring in Congress.
- **🗳️ Votes & decisions** — NYC Council roll-calls, NY State floor/committee votes, and **U.S. House roll-calls** (from the House Clerk) filtered to how NYC's delegation voted — all with per-member tallies.
- **🔔 Activity (all levels)** — a durable watchlist across NYC/NYS/Federal bills with **Refresh & diff** to catch what moved.
- **👤 Who governs NYC** — a unified directory across city, state, and federal.
- **🗳️ Elections & terms** — a deterministic ballot calendar computed from each office's fixed cycle.

### 👥 People & Coalitions
- **📖 CM Wiki** — a personality-driven page per Council Member: an AI *legislative persona* (style only, grounded in the record), their prime-sponsored decisions, policy focus and coalition, and a tool that **estimates their lean on any issue** from their sponsorship record (a labeled inference, not a vote prediction).
- **Members, Dossier, Compare** — a member's record by year, prime vs. co-sponsor, outcomes, and head-to-head comparison.
- **🪪 Deep profile** — one polished page per official at any level (facts, committees, record, AI "record at a glance").
- **🤝 Coalitions** — the co-sponsorship network and strongest partnerships.
- **🗺️ District map** — all 51 Council districts.

### 📰 Briefings & Ideas
- **📰 Briefing Studio** — turn any bill, member, or topic into a plain-English briefing with tone presets (**Staff · Press-ready · Constituent · One-pager**). Copy-ready; exports to Markdown, print/PDF-HTML, and Excel.
- **📦 District Packet** — a one-click printable bundle: a member's profile + their bills + upcoming hearings.
- **💡 Policy Lab** — brainstorm new laws: structured concepts (mechanism, lead agency, supporters/opponents, fiscal & legal flags, precedent, PR angle, draft intro summary), expandable to a one-page memo.

### 📣 Politics & Messaging
The member's own communications & influence layer — **advocacy in their voice**, kept separate from the neutral-analysis tools.
- **🎯 Issue War Room** — one topic in, a full kit out: a briefing + grounded figures + computed **swing members** + an influence memo + a draft statement, assembled in one pass and exportable together.
- **📝 Statement Studio** — press statements, reporter quotes, floor remarks, newsletter blurbs, social posts, or talking points, in a measured/firm/urgent register, with a built-in style anchor.
- **⚡ Rapid Response** — a grounded, on-message reply to a statement you supply (the tool never manufactures anyone's quote).
- **🧭 Influence Map** — where a majority comes from on an issue across the progressive wing, moderates, and Republican minority, grounded in the co-sponsorship coalitions.
- **📊 Grounded figures** — pull live NYPD complaint counts by category (NYC Open Data) for a date window, each returned with its source citation, so statements cite real, verifiable numbers instead of guesses.

> Integrity guardrails: never invents statistics or another official's words, tags unknown figures `[verify: source]`, criticizes record not persons, and hardcodes no caucus rosters. Drafts are decision-support — confirm any figure before release.

### 💬 Ask
An AI chat over the loaded bills **and** NYC government generally — the City Charter, Administrative Code, ULURP, the budget process — with optional web search for citations.

---

## Quick start

Requires Python 3.9+.

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open the URL Streamlit prints (usually http://localhost:8501). In the app,
open the **⚙️ Data controls** panel, pick **All legislation + 2026**, and press
**Load data** — every tab comes alive from there.

### Deploy (Streamlit Community Cloud)
1. Push this repo to GitHub.
2. On [share.streamlit.io](https://share.streamlit.io), point a new app at `app.py`.
3. (Optional) add API keys as app **Secrets** or enter them in-app.

### Scheduled packets (headless)
`scheduled_packet.py` generates a District Packet unattended — Markdown + printable HTML — so a member's packet can land in your inbox every Monday:

```bash
python3 scheduled_packet.py --level nyc --member Hanks --year 2026 --out out/hanks
# weekly cron (Mondays 08:07):
# 7 8 * * 1  cd /path/to/repo && python3 scheduled_packet.py --level nyc --member Hanks --out out/hanks_$(date +\%Y\%m\%d)
```

Set `ANTHROPIC_API_KEY` for the AI "record at a glance" and `CONGRESS_API_KEY` for a federal member's sponsored bills.

---

## API keys (all optional, all free)

The core NYC legislation features work out of the box. The AI and
state/federal features light up when you add keys — entered in-app, used only
for the live session, and never stored by this app.

| Feature | Key | Get it (free) |
|---|---|---|
| AI briefings, Policy Lab, Ask, analyses | **Anthropic API key** — ⚙️ controls panel | https://console.anthropic.com |
| NY State bill search, members, votes | **NY State Open Legislation** — *State & Federal* tab | https://legislation.nysenate.gov/public/subscribe |
| Congress delegation bill tracking | **Congress.gov API** — *State & Federal* tab | https://api.congress.gov/sign-up |

The NYC Legistar data and the federal **delegation roster** (via the public
[`congress-legislators`](https://github.com/unitedstates/congress-legislators)
dataset) need no key.

---

## Data vs. analysis — the ground rule

- **Facts** (bills, sponsors, votes, members, districts) come from official APIs and are presented as-is.
- **Anything AI-written** is clearly labeled as *analysis/inference*, is grounded strictly in the provided data, and never invents figures — it names the source to check instead.
- **No volatile officeholder names are hardcoded.** Current names come from live sources, so the roster stays correct through elections; only stable structure (offices, term lengths, election cycles) is baked in.

---

## Architecture

One Streamlit UI (`app.py`) over small, self-contained, defensively-written
modules. Each degrades gracefully when a key or the network is missing — the app
stays up and tells you what it couldn't reach.

| Module | Responsibility |
|---|---|
| `app.py` | The Streamlit UI, data-load orchestration, and all tabs. Also holds the NYC Legistar client and pure transforms. |
| `llm.py` | Shared Anthropic client (JSON mode, prose, web search). |
| `briefing.py` | "Bulletpoints for Bureaucrats" briefing builders + Markdown→HTML + print export. |
| `policylab.py` | Structured legislative ideation. |
| `packet.py` | The printable District Packet assembler. |
| `profiles.py` | Cross-level member deep-profile shaping + AI "record at a glance". |
| `people.py` | Offices, election-cycle logic, unified directory shaping, address→rep matching. |
| `store.py` | Tiny JSON-file persistence (the durable watchlist). |
| `sources/nystate.py` | NY State Open Legislation client (bills, members, votes). |
| `sources/congress.py` | Congress.gov client + `congress-legislators` loader (NYC delegation, committees). |
| `sources/districts.py` | Address geocoding (NYC GeoSearch) + point-in-polygon district lookup. |

### Data sources
- **NYC Legistar Web API** — `webapi.legistar.com` (bills, sponsors, histories, votes, events, members).
- **NY State Open Legislation** — `legislation.nysenate.gov`.
- **Congress.gov API v3** + **congress-legislators** dataset.
- **NYC GeoSearch** (NYC Planning Labs) + **Census TIGERweb** / **NYC DCP** district layers.
- **NYC Open Data** (311 and agency datasets) for real-data context in analyses.

---

## Testing

The modules are pure and unit-testable, and the UI is verified with Streamlit's
headless [`AppTest`](https://docs.streamlit.io/develop/api-reference/app-testing)
harness. A quick offline check:

```bash
python3 -m py_compile app.py *.py sources/*.py
```

> Note: outbound calls to the live data hosts require normal internet egress. In
> restricted CI/sandbox environments those hosts may be blocked; the app is
> written to degrade gracefully there, and runs fully against live data in a
> normal deployment.

---

## Notes

- The in-app watchlist persists within a running deployment but not across a
  fresh redeploy — for permanent, restart-proof day-to-day tracking, use the
  scheduled backend handed to Council IT.
- This tool surfaces and summarizes public records. AI output is decision
  *support*, not an official position — verify figures against OMB / IBO /
  agency sources before anything goes out the door.
