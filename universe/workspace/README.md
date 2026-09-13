# The D49 Workspace

An Asana-shaped workspace for the office, sitting directly on top of the
District 49 record: 75,000+ searchable records, the programs the office
actually runs, and an assistant that answers from the evidence rather than
from memory.

It runs on one computer. No host, no account, no build step — a launcher and a
browser.

```
python3 -m universe workspace          # or double-click START_WORKSPACE.cmd
```

---

## The shape of the work

Work nests the way the budget cycle nests, so the tool matches the job:

```
Program        Budget & Discretionary Funding        (a season)
 └ Project     Schedule C designations               (a deliverable)
    └ Section  Intake · In progress · Review · Done  (a stage)
       └ Task  Reconcile the SI ledger               (one person, one date)
          └ Subtask, checklist, dependencies, custom fields
```

Six programs are created on first run, from the office's real portfolio:
**Budget & Discretionary Funding**, **Legislative**, **Land Use &
Development**, **Constituent Services**, **Communications & Press** and
**District Operations**. The budget program carries the fiscal calendar as
dated milestones, generated from the Charter's sequence rather than typed, so
next year's cycle is one command.

**Views**, per project: Board (drag between sections), List, Timeline (bars,
milestones, a line on today) and Updates (Asana-style status posts — a colour
and a paragraph).

**Across the office**: Home, My work (overdue / today / this week / blocked),
Inbox, Calendar, Workload.

---

## Live, not polled

Every write appends to an event log. Browsers hold an `EventSource` on
`/api/stream` and receive events as they land — an assignment appears on a
colleague's board immediately, not up to fifteen seconds later.

The log is the transport *and* the audit trail. A browser that was closed
reconnects with the last event id it saw and receives exactly what it missed:
no lost updates, no full reload, and unsent edits are never clobbered.

---

## Search that answers while you type

One box over bills, funding lines, organizations, officials, hearings, media,
notes and tasks.

| | |
|---|---|
| `kind:funding fy:2027 org:Snug` | field filters, bound as parameters |
| `amount:>50000` · `fy:2022..2027` | comparisons and ranges |
| `-org:"Project Hospitality"` | exclusion |
| `ferry OR toll` · `"Snug Harbor"` | boolean and phrases |
| `committee:Transport*` | prefixes |

Typical queries answer in **1–45 ms** over 75,000 records. Typeahead returns in
under 3 ms. Two things make that true: facet counts come from a **single pass**
over the matching set rather than one GROUP BY per facet (which was 75–90% of
the response time), and every filter the interface offers has an index behind
it.

**Natural language degrades in three steps** and says which one answered:
the query as typed → content words, all required → content words, any of them.
A precise query is never loosened; a conversational one still finds the record.

---

## The assistant

Ask a question, or ask for a deliverable: talking points, a quote, a press
statement or release, hearing questions, a constituent reply, a newsletter
item, a memo, social posts.

The order is the design: **retrieve, then write.** The assistant searches the
index, pulls the matching funding lines, bills, hearings and clips, and hands
that packet to the model. The model writes over evidence it was given; it never
supplies the facts. With no API key the assistant still answers — with the
evidence, the figures and the structure, minus the prose.

Two rules are enforced in code, not asked for in a prompt:

- **A quote must come from a recording.** The quote deliverable returns only
  verbatim excerpts from a stored transcript, with the clip's URL. It cannot
  manufacture something the Member never said, because it has no path to.
- **A loose match never produces a total.** If retrieval had to fall back to
  matching *any* search word, the funding total is suppressed and the reason is
  printed. Summing loosely-matched lines yields a figure that looks
  authoritative and is not — the one mistake this system exists to prevent.

Set `ANTHROPIC_API_KEY` before starting to enable drafting. `--council` routes
a request through the five-seat LLM Council.

---

## The public record

Council hearing video and agendas (Legistar), the Council's YouTube channel,
news coverage and Council press releases — all through **key-free public
feeds** (RSS and the Legistar API), so there is no quota to manage.

Items naming the Member are flagged. A transcript is stored **only when a real
one is supplied** — pasted by staff or returned by a caption endpoint — and
only clips with a transcript can be quoted.

---

## What it refuses to do

- Lose an edit: every task write is guarded by a revision check, and a stale
  save is refused with an explanation rather than silently overwriting.
- Delete good data on a failed refresh: if Legistar is unreachable, the last
  successful pull stays and the failure is recorded against the source.
- Create a dependency cycle, or let a task block itself.
- Notify anyone for a write with no human actor, so seeding or importing does
  not fill every inbox.
- Invent a mention: an `@handle` matching nobody on the roster stays plain text.

---

## Security

Constituent names and unreleased drafts live here.

- A bearer token gates every API route, generated on first run and written
  beside the database.
- Writes require a CSRF token and a same-origin request.
- A strict Content-Security-Policy: no inline script, no inline style, no
  remote origins. (The interface carries positional values as `data-*` and
  applies them through the CSSOM, which the policy allows.)
- Every SQL value is bound. CSV exports escape leading `=`, `+`, `-`, `@` so a
  cell cannot execute when opened in a spreadsheet.
- Binds to localhost. Sharing it is an explicit `--host` choice, and the
  launcher warns when you make it.

---

## Command line

```
universe workspace [--port 8749] [--host 127.0.0.1] [--seed] [--reindex]
universe reindex   [--only matters funding orgs members calendar media tasks]
universe assistant "what did we fund for seniors" [--kind talking_points] [--council]
universe media     [--collect]
```

---

## Tests

```bash
python3 -m universe.tests.test_workspace     # 30 tests
python3 -m universe.tests.test_universe      # 29 tests
```

The workspace suite encodes the rules above and the bugs found building it: a
v1 database migrating without losing a row, a refused dependency cycle, a
suppressed total on a loose match, a refresh failure that preserves data, a
quote blocked without a transcript, and an actorless import that notifies
nobody.

The interface is verified in a real browser (Chromium via Playwright): zero
console errors, zero failed requests, no horizontal overflow at 390px, and
every view — board, timeline, search, palette, task drawer, assistant —
rendering against the live server.

---

## Known limits

- **Live sources need network access.** Legistar, NYC Open Data, YouTube, the
  news feeds and the Council press feed were unreachable from the build
  environment, so those paths are verified on the requests they build and on
  their failure handling, not against live responses.
- **No language model is bundled.** Without `ANTHROPIC_API_KEY` the assistant
  returns evidence and structure rather than prose, and says so on its face.
- **One computer, one copy.** Two machines do not synchronize. Sharing the
  workspace means running it on one machine others can reach, with the
  `--host` flag and the token.
- **Estimates drive the workload view.** Tasks without an estimate count toward
  the task total but not the hours, so a low bar with many tasks means
  estimates are missing, not that someone is free.
