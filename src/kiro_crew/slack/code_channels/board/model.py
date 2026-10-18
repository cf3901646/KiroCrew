"""The work board's data: what one worker row and the whole board PUBLISH.

Everything here is pure. :mod:`.source` fills it from the channel conductor's own
ledger and its own workers' live sessions; :mod:`.render` turns it into Slack
blocks; the workspace service uses the same snapshot for the context bar and the
combined diff.

THE PUBLISHED SET IS CLOSED. A code channel is a room other people may be in, and
the work ledger is kept out of channel audiences everywhere else
(``handlers/work_ledger.contained_channel_caller``). The conductor's dispatch
exemption admits exactly these fields into the channel, and nothing else may be
added without amending that rule:

* the conductor's ``goal``, first line, at most :data:`GOAL_CHARS` characters,
  through the same passes as a summary (the board header);
* the item's ``title``;
* the DERIVED state (:data:`STATES`), never the raw ``status`` / ``reason`` words;
* the first line of the worker's last report ``summary``, at most
  :data:`SUMMARY_CHARS` characters, through the outbound redaction and with
  channel-wide and user-group pings stripped;
* ``artifacts["branch"]``;
* the pull request number, URL and CI state;
* for a worker with a tool approval pending, WHAT it asks for as one phrase of
  the closed :data:`APPROVAL_WORDS` vocabulary, chosen by the approval's ACP
  tool-call kind -- never the approval's title, its command or its arguments.

Never the acceptance bar, the conductor's decision, the event log, or any
transcript text. :class:`WorkerRow` has no field that could carry one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

#: Longest summary line the board shows.
SUMMARY_CHARS = 140
#: Longest title shown on a row.
TITLE_CHARS = 120
#: Longest goal line in the board header.
GOAL_CHARS = 200

#: What a pending approval asks for, by its ACP tool-call kind (``spawn`` is a
#: coordinator spawn approval). A CLOSED vocabulary: the board shows one of these
#: phrases or "a tool", never the approval's title, which is the transcript's
#: permission row and, for a shell call, its command line.
APPROVAL_WORDS: dict[str, str] = {
    "execute": "a command",
    "edit": "a file edit",
    "delete": "a file delete",
    "move": "a file move",
    "read": "a file read",
    "search": "a search",
    "fetch": "a web fetch",
    "spawn": "a sub-agent",
}


def approval_words(kind: str) -> str:
    """The fixed phrase for an approval of ACP *kind*; ``""`` (shown as "a tool") otherwise."""
    return APPROVAL_WORDS.get(str(kind or "").strip().lower(), "")


WORKING = "working"
IDLE = "idle"
WAITING_ON_YOU = "waiting_on_you"
BLOCKED = "blocked"
DONE = "done"
LOST = "lost"
CLOSED = "closed"

#: Every row state, most urgent first. Rows themselves stay in dispatch order, so
#: a pinned board never reshuffles under the reader's eye.
STATES: tuple[str, ...] = (WAITING_ON_YOU, BLOCKED, LOST, WORKING, IDLE, DONE, CLOSED)

STATE_LABELS = {
    WORKING: "working",
    IDLE: "idle",
    WAITING_ON_YOU: "waiting on you",
    BLOCKED: "blocked",
    DONE: "done",
    LOST: "lost",
    CLOSED: "closed",
}


def row_label(row: "WorkerRow") -> str:
    """The words for a row's state: a pending approval names its tool."""
    if row.waiting_on == "approval":
        return f"waiting on you: approve {row.approval_tool or 'a tool'}"
    return STATE_LABELS.get(row.state, row.state)


#: The board's overall state words.
OVERALL_DONE = "done"
OVERALL_NEEDS_YOU = "needs you"
OVERALL_WORKING = "working"
OVERALL_STUCK = "stuck"
OVERALL_IDLE = "idle"
OVERALL_EMPTY = "no workers yet"

#: Reasons that mean only a PERSON can move the item (``work_vocab``'s words).
_PERSON_REASONS = frozenset({"needs_human", "approval"})
_ASKING_STATUSES = frozenset({"blocked", "question"})

#: Liveness of a worker's session, joined server-side by :mod:`.source`.
LIVE_RUNNING = "running"
LIVE_QUEUED = "queued"
LIVE_IDLE = "idle"
LIVE_APPROVAL = "approval"
LIVE_GONE = "gone"
LIVE_UNBOUND = "unbound"

_USER_PING = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_WS = re.compile(r"\s+")


def _redact(text: str) -> str:
    """The outbound redaction, scanned against what Slack will DISPLAY.

    ``redact_for_display`` also scans the text with emphasis, code and link
    markup rendered away, so a credential split by ``*...*`` cannot reassemble
    on screen; ``redact_via_context`` is the platform's egress redactor.
    """
    from kiro_crew.messaging.display_safety import redact_for_display
    from kiro_crew.platform.context import redact_via_context

    return redact_for_display(text, redact_via_context)[0]


def clean_text(text: str, limit: int) -> str:
    """Agent-authored text made safe for the channel: one line, no pings, redacted.

    The first line only; every Slack mention form defused (the wrap-up's own
    patterns for channel-wide and user-group pings, plus user mentions), so a
    worker's words never page anyone; whitespace folded; the outbound redaction;
    the cut to *limit* characters; and the redaction AGAIN, last, so a cut can
    never leave a credential the first pass would have caught whole.
    """
    from kiro_crew.slack.code_channels.wrapup import _BROADCAST, _GROUP_PING

    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    out = _BROADCAST.sub(r"@\1", first)
    out = _GROUP_PING.sub(lambda m: m.group(1) or "@group", out)
    out = _USER_PING.sub(r"@\1", out)
    out = _WS.sub(" ", out).strip()
    out = _redact(out)
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return _redact(out)


@dataclass(frozen=True)
class WorkerRow:
    """One work item on the board, already reduced to the published set."""

    #: The ledger's server-minted item id (``it_<hex>``): a stable row key.
    item_id: str
    title: str
    state: str
    summary: str = ""
    branch: str = ""
    pr_number: int = 0
    pr_url: str = ""
    pr_state: str = ""
    pr_ci: str = ""
    #: The worker's dashboard slot key, for the deep link (never shown as text).
    session_key: str = ""
    #: ``last_report_at`` of the report that put the row in its state; the
    #: needs-you alert is sent once per value.
    report_stamp: str = ""
    round: int = 0
    #: What a ``waiting_on_you`` row waits on: ``approval`` (a tool approval on
    #: the worker's session) or ``report`` (a report naming a person).
    waiting_on: str = ""
    #: For an ``approval`` wait, what it asks for: one :data:`APPROVAL_WORDS`
    #: phrase (``""`` when the kind is unknown), never the approval's own text.
    approval_tool: str = ""
    #: For a ``closed`` row, how the conductor closed it: ``rejected`` / ``abandoned``.
    closed_as: str = ""

    @property
    def needs_owner(self) -> bool:
        return self.state == WAITING_ON_YOU

    @property
    def settled(self) -> bool:
        return self.state in {DONE, CLOSED}

    def with_pr(self, number: int, url: str, state: str, ci: str) -> WorkerRow:
        return replace(self, pr_number=number, pr_url=url, pr_state=state, pr_ci=ci)


#: Rows the board message, the context bar and the Code tab show at most
#: (Slack caps a message at 50 blocks). Totals always count every item.
MAX_SHOWN = 20


@dataclass(frozen=True)
class BoardSnapshot:
    """The whole board for one channel: EVERY item, in dispatch order."""

    goal: str = ""
    round: int = 0
    rows: tuple[WorkerRow, ...] = field(default_factory=tuple)

    @property
    def shown(self) -> tuple[WorkerRow, ...]:
        """The rows on display: every open row first (dispatch order), then the
        most recently dispatched settled ones, at most :data:`MAX_SHOWN`."""
        open_rows = [r for r in self.rows if not r.settled]
        settled = [r for r in self.rows if r.settled]
        room = max(0, MAX_SHOWN - len(open_rows))
        keep = set(id(r) for r in open_rows[:MAX_SHOWN])
        keep.update(id(r) for r in (settled[-room:] if room else []))
        return tuple(r for r in self.rows if id(r) in keep)

    @property
    def omitted(self) -> int:
        return len(self.rows) - len(self.shown)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def done(self) -> int:
        return sum(1 for r in self.rows if r.state == DONE)

    @property
    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATES, 0)
        for row in self.rows:
            out[row.state] = out.get(row.state, 0) + 1
        return out

    @property
    def overall(self) -> str:
        return overall_state(self.rows)

    @property
    def aggregate(self) -> str:
        return aggregate_line(self.rows)


def row_state(
    *,
    item_state: str,
    status: str | None,
    reason: str | None,
    live: str,
    verdict: str | None = None,
    handed_back: bool = False,
) -> str:
    """The derived row state, from the item and its worker's live session.

    The order is the rule, most decisive first:

    1. a closed item: ``accepted`` is done, ``rejected`` / ``abandoned`` closed;
    2. a tool approval waiting on the worker's session: waiting on you;
    3. a turn in flight or queued: working (a fresh turn outranks an old report);
    4. a ``blocked`` / ``question`` report the conductor already answered (a
       ``decide`` at or after it) or a ``done`` the conductor's evaluator failed:
       the move is the worker's again, so the report's own word does not apply
       (idle, or lost when gone);
    5. a ``blocked`` / ``question`` report a person must move: waiting on you;
    6. any other ``blocked`` / ``question``: blocked (a service, or the conductor);
    7. a session that is gone: done when it last reported done, else lost;
    8. a ``done`` report: done (awaiting the conductor's acceptance);
    9. otherwise idle (open, quiet, or not dispatched yet).
    """
    if item_state == "accepted":
        return DONE
    if item_state in {"rejected", "abandoned"}:
        return CLOSED
    if live == LIVE_APPROVAL:
        return WAITING_ON_YOU
    if live in {LIVE_RUNNING, LIVE_QUEUED}:
        return WORKING
    if handed_back or (status == "done" and verdict == "fail"):
        return LOST if live == LIVE_GONE else IDLE
    if status in _ASKING_STATUSES:
        return WAITING_ON_YOU if reason in _PERSON_REASONS else BLOCKED
    if live == LIVE_GONE:
        return DONE if status == "done" else LOST
    if status == "done":
        return DONE
    return IDLE


def overall_state(rows: tuple[WorkerRow, ...] | list[WorkerRow]) -> str:
    """One word for the whole board."""
    if not rows:
        return OVERALL_EMPTY
    states = {r.state for r in rows}
    if states <= {DONE, CLOSED}:
        return OVERALL_DONE
    if WAITING_ON_YOU in states:
        return OVERALL_NEEDS_YOU
    if WORKING in states:
        return OVERALL_WORKING
    if states & {BLOCKED, LOST}:
        return OVERALL_STUCK
    return OVERALL_IDLE


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def aggregate_line(rows: tuple[WorkerRow, ...] | list[WorkerRow]) -> str:
    """``"3 workers running · 1 waiting on you · 1 of 5 done"``: the one-line status.

    Only non-zero counts are named, in urgency order after "running", and the
    done count always closes the line; an empty board says so.
    """
    if not rows:
        return "No workers yet"
    counts = dict.fromkeys(STATES, 0)
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    parts: list[str] = []
    if counts[WORKING]:
        parts.append(_plural(counts[WORKING], "worker running", "workers running"))
    for state in (WAITING_ON_YOU, BLOCKED, LOST, IDLE):
        if counts[state]:
            parts.append(f"{counts[state]} {STATE_LABELS[state]}")
    if counts[CLOSED]:
        parts.append(f"{counts[CLOSED]} closed")
    parts.append(f"{counts[DONE]} of {len(rows)} done")
    return " · ".join(parts)
