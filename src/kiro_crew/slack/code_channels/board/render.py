"""The work board as Slack sees it: blocks, fallback text, the needs-you reply.

Pure functions of a :class:`~.model.BoardSnapshot`. Every string that reaches
Slack was already reduced to the published set by :mod:`.source` (titles and
summaries redacted and de-pinged); the branch names and URLs added here are
forge- or git-derived and pass the same redaction before they are used.
"""

from __future__ import annotations

from collections.abc import Callable

from kiro_crew.platform.context import redact_via_context
from kiro_crew.slack.code_channels import wrapup as _wrapup
from kiro_crew.slack.code_channels.board import model

#: Builds a dashboard deep link for a worker slot key ("" when none can be built).
LinkFor = Callable[[str], str]

_EMOJI = {
    model.WORKING: ":hourglass_flowing_sand:",
    model.IDLE: ":white_circle:",
    model.WAITING_ON_YOU: ":raising_hand:",
    model.BLOCKED: ":no_entry:",
    model.DONE: ":white_check_mark:",
    model.LOST: ":warning:",
    model.CLOSED: ":heavy_minus_sign:",
}
#: The wrap-up's own words for a PR's state and its CI, so every surface agrees.
PR_WORDS = _wrapup._PR_WORDS
CI_WORDS = _wrapup._CI_WORDS

FALLBACK_PREFIX = "Work board"


def _escape(text: str) -> str:
    """Slack mrkdwn control characters, escaped as Slack documents."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(url: str, text: str) -> str:
    if not url.startswith("https://") and not url.startswith("http://"):
        return _escape(text)
    if redact_via_context(url) != url or any(c in url for c in "<>| \n"):
        return _escape(text)
    return f"<{url}|{_escape(text)}>"


def pr_text(row: model.WorkerRow) -> str:
    """``"PR #12 open · tests passing"`` as plain text (CI only for a live PR), or ``""``.

    The one wording of a worker's PR, used by the board, the context bar and the
    Code tab's worker headers.
    """
    if not row.pr_number:
        return ""
    head = f"PR #{row.pr_number} {PR_WORDS.get(row.pr_state, '')}".strip()
    ci = CI_WORDS.get(row.pr_ci, "")
    return f"{head} · {ci}" if ci and row.pr_state in {"open", "draft"} else head


def pr_words(row: model.WorkerRow) -> str:
    """:func:`pr_text` as mrkdwn, the ``PR #n`` part linked when the forge confirmed it."""
    text = pr_text(row)
    if not text:
        return ""
    label = f"PR #{row.pr_number}"
    head = _link(row.pr_url, label) if row.pr_url else _escape(label)
    return head + _escape(text[len(label) :])


def header_line(snapshot: model.BoardSnapshot) -> str:
    """The header line: the round, the overall word, then the aggregate line.

    The overall word leads only when the aggregate line does not already say it
    (an empty or finished board reads from the aggregate alone).
    """
    parts = [f"Round {snapshot.round}"] if snapshot.round else []
    overall = snapshot.overall
    if overall in {model.OVERALL_EMPTY, model.OVERALL_DONE}:
        parts.append(snapshot.aggregate)
    else:
        parts.append(f"{overall[:1].upper()}{overall[1:]}: {snapshot.aggregate}")
    return " · ".join(parts)


def row_text(row: model.WorkerRow, link_for: LinkFor) -> str:
    """One worker's mrkdwn: title and state, the summary line, then the pointers."""
    label = model.row_label(row)
    lines = [f"{_EMOJI.get(row.state, '')} *{_escape(row.title)}* — _{_escape(label)}_".strip()]
    if row.summary:
        lines.append(_escape(row.summary))
    meta: list[str] = []
    if row.branch:
        meta.append(f"`{_escape(redact_via_context(row.branch))}`")
    pr = pr_words(row)
    if pr:
        meta.append(pr)
    link = link_for(row.session_key) if row.session_key else ""
    if link:
        meta.append(_link(link, "Open in dashboard"))
    if meta:
        lines.append(" · ".join(meta))
    return "\n".join(lines)[:2900]


def board_blocks(
    snapshot: model.BoardSnapshot, link_for: LinkFor, *, conductor_link: str = "", updated: str = ""
) -> list[dict]:
    """The pinned board message."""
    title = "Work board"
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": title}}]
    goal = _escape(snapshot.goal) if snapshot.goal else "_No goal recorded yet._"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Goal:* {goal}"}})
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _escape(header_line(snapshot))}],
        }
    )
    blocks.append({"type": "divider"})
    if not snapshot.rows:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "No work dispatched yet. Each worker appears here once it starts.",
                },
            }
        )
    for row in snapshot.shown:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": row_text(row, link_for)}}
        )
    if snapshot.omitted:
        folded = f"+{snapshot.omitted} more items, counted above and listed in the dashboard"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": folded}]})
    footer = []
    if updated:
        footer.append(f"Updated {_escape(updated)}")
    if conductor_link:
        footer.append(_link(conductor_link, "Open the conductor in the dashboard"))
    if footer:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(footer)}]}
        )
    return blocks


def fallback_text(snapshot: model.BoardSnapshot) -> str:
    """Notification / screen-reader text for the board (no markup)."""
    return f"{FALLBACK_PREFIX}: {header_line(snapshot)}"


def needs_you_text(row: model.WorkerRow, owner_id: str, link_for: LinkFor) -> str:
    """The threaded reply that pages the owner for a worker that waits on them."""
    who = f"<@{owner_id}> " if owner_id and owner_id.isalnum() else ""
    if row.waiting_on == "approval":
        text = f"{who}*{_escape(row.title)}* has a tool approval waiting on you"
        if row.approval_tool:
            text += f": approve {_escape(row.approval_tool)}"
    else:
        text = f"{who}*{_escape(row.title)}* is waiting on you"
        if row.summary:
            text += f": {_escape(row.summary)}"
    link = link_for(row.session_key) if row.session_key else ""
    if link:
        text += f" — {_link(link, 'Open in dashboard')}"
    return text


def outcome_line(row: model.WorkerRow) -> str:
    """One wrap-up line: ``Fix login: PR #12 merged`` / ``rejected, PR #9 closed``."""
    pr = pr_words(row)
    if row.state == model.CLOSED:
        outcome = row.closed_as or "closed"
        if pr:
            outcome += f", {pr}"
    elif pr:
        outcome = pr
    else:
        outcome = f"{_escape(model.row_label(row))}, no PR"
    return f"• {_escape(row.title)}: {outcome}"


_BAR_LABEL = 128


def worker_bar_items(snapshot: model.BoardSnapshot, slots: int) -> list[dict[str, str]]:
    """Context-bar items for the workers, at most *slots* of them.

    Most urgent first (:data:`~.model.STATES` order), one item per worker: its PR
    and CI linked to the PR, else its branch, else its state, with the title cut
    so the detail always fits Slack's label. With room for only ONE item, that
    item is the aggregate line; with more workers than slots, the last slot says
    how many more there are.
    """
    if slots <= 0 or not snapshot.rows:
        return []
    if slots == 1 and len(snapshot.rows) > 1:
        label = f"{len(snapshot.rows)} workers · {snapshot.aggregate}"
        return [{"key": "workers", "label": label[:_BAR_LABEL], "icon": "hierarchy"}]
    rank = {state: i for i, state in enumerate(model.STATES)}
    rows = sorted(snapshot.rows, key=lambda r: rank.get(r.state, len(rank)))
    shown = rows if len(rows) <= slots else rows[: slots - 1]
    items: list[dict[str, str]] = []
    for row in shown:
        detail = pr_text(row) or row.branch or model.row_label(row)
        room = max(8, _BAR_LABEL - len(detail) - 2)
        title = row.title if len(row.title) <= room else row.title[: room - 1].rstrip() + "…"
        item = {
            "key": f"worker-{row.item_id}"[:64],
            "label": f"{title}: {detail}",
            "icon": "hierarchy" if row.pr_number else "branch",
        }
        if row.pr_url:
            item["url"] = row.pr_url
        items.append(item)
    if len(rows) > len(shown):
        items.append(
            {
                "key": "workers-more",
                "label": f"+{len(rows) - len(shown)} more workers",
                "icon": "hierarchy",
            }
        )
    return items
