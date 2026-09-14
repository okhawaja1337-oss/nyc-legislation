D49 UNIVERSE — District 49 government intelligence system
=========================================================
Councilmember Kamillah Hanks · North Shore, Staten Island


START IT
--------
  Mac      double-click  start-workspace.command
  Windows  double-click  START_WORKSPACE.cmd

Your browser opens by itself. That is the whole install.

A black-and-white text window opens first. Ignore it — it looks alarming and
is not. It is the engine running. Close it when you are finished.

The first launch takes about a minute: it builds the search index over the
whole record and takes a baseline for change detection. Every launch after
that is instant. Let it finish.

If nothing happens when you double-click, you probably do not have Python.
Get it from python.org/downloads. On Windows, tick "Add Python to PATH" on
the very first screen — nothing works without that box ticked.


SEARCH — one box over everything
--------------------------------
Type a bill number and you get the bill:

      365-2026            both matters carrying that number
      Int 365-2026        exactly one
      LL 42 of 2025       Local Law 42 of 2025 (which is Int 0977-2024)
      77661               a Legistar matter id
      13-3481845          every funding line for that EIN
      FY27                the shape of a fiscal year

Type words and you search the whole record — including the text of the bills
themselves, so "Legionnaires" finds the bill that establishes the hotline
whether or not the word survived into its title.

Narrow it with fields:  kind:funding  fy:2027  org:"Snug Harbor"
                        sponsor:Hanks  committee:Housing  amount:>50000
Ranges: fy:2022..2027    Exclude: -kind:matter


EVERYTHING IS CITEABLE
----------------------
Every search result carries its own citation — the source, the exact locator,
and the page a reader opens to check it. Click a citation to copy it.
Underneath the results is a numbered bibliography for the whole set.

Nothing here needs to be retyped into a memo, which is where the errors come
from.


BRIEF ANYTHING
--------------
Press "Brief this" on any search result. You get a brief in the house shape —
a short bottom line, the details as bullets, what it means for District 49,
and three questions — built from the record, with the figures traced.

It works on a bill, a funding line, an organisation, a reconciliation row or
a whole fiscal year. Or use "Run a brief" in the left menu.

Every brief passes through 17 checks before it is filed. A brief that fails
one is still written, but lands marked "blocked" with the reason attached —
so a brief that reaches the Councilmember has been checked.


THE DISTRICT POSITION
---------------------
"The district position" in the left menu shows every defensible answer to
"what did District 49 get", side by side, each with the question it answers:

  $3,008,000    what the Councilmember personally designated
  $1,042,000    what Schedule C codes to District 49
  $35,903,925   what the office secured for the North Shore
  $39,627,090   the full district breakdown — 245 items, 17 categories
  $95,282,347   Staten Island combined

They differ by a factor of twelve. Quoting one without its question is how an
office contradicts itself in public.


KEEPING IT CURRENT
------------------
In the black text window, or a terminal in this folder:

  python3 -m universe repos sync      pull both GitHubs and load what moved
  python3 -m universe watch scan      what changed in the budget or the docket
  python3 -m universe position        the district position
  python3 -m universe ask "…"         search from the command line

"Source repositories" in the left menu shows what is loaded and how old it is.

This package ships the full text of bills from the 2022 session onward, and
the official summary of every bill back to 1996. To pull the older text too:

  python3 -m universe repos sync --repo legistar --force


THE WHOLE OFFICE
----------------
  Mac      double-click  share-with-staff.command
  Windows  double-click  SHARE_WITH_STAFF.cmd

It prints one link. Send it to your staff. They click it once and they are in;
their browser stores the token and strips it from the address bar, so it will
not sit in their history or get pasted into an email by mistake.


NOTHING HERE CAN BREAK ANYTHING
-------------------------------
This is a copy, running on your own machine. Nothing you click sends anything
anywhere. If it ever gets into a state you do not like, delete this folder,
unzip the download again, and you are back to a clean start.
