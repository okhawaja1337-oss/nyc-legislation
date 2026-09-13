"""
Connectors: the seams where this system touches somebody else's server.

Each module here does one thing -- pull a source the office does not own and
hand back rows the lake already understands. They share three habits:

  * **Several transports, cheapest first.** A calendar can be read from a
    secret ICS link with no credential at all, or from the API with a token.
    The office should not have to do an OAuth dance to get the week's hearings.
  * **A failure is data, not an exception.** Every fetch returns a result that
    says what it tried, what answered, and what it got. Nothing here raises
    into a briefing.
  * **Provenance travels with the row.** What came from the calendar is marked
    as coming from the calendar, so a citation can be written later.
"""
