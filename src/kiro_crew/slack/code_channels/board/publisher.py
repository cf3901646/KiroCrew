"""Publish the work board: one pinned message per channel, edited in place.

Called by the workspace refresh (:meth:`~..workspace.service.WorkspaceService.refresh`)
on its snapshot, outside the channel's lock, so the board, the context bar and the
Code-tab diff are published from the same snapshot by the same per-channel worker.
Publishes of one channel are kept apart by that worker (``schedule_refresh``), not
by the lock -- which matters, because posting a missing board and paging the owner
are not idempotent. That worker is
already coalescing (one run plus at most one queued rerun per channel) and the
board's triggers arm it through a short debounce window, so a burst of worker
reports costs one ``chat.update``.

* **First publish** posts the board and pins it (``pins.add``; ``already_pinned``
  counts as pinned). The message ts, a digest of what it shows, and the pin
  are kept on the channel's surface record.
* **Later publishes** edit that message with ``chat.update`` only when the digest
  moved. A message Slack does not have any more (``message_not_found``,
  ``cant_update_message``) is reposted and pinned again.
* **Waiting on you.** A worker that newly waits on a person (a report with
  ``reason`` ``needs_human`` / ``approval``, or a tool approval pending on its
  session) gets ONE threaded reply under the board that mentions the owner. Sent
  once per report (``<item_id>:<last_report_at>``) and once per spell of a
  pending approval, so the board's own edits never page anyone twice.

Every write first checks the ``channels`` policy and the archive / Stop fences the
rest of the surface honours; nothing here raises into a turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.board import model, render
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import get_async, update_async

logger = logging.getLogger(__name__)

#: Needs-you alert tokens remembered per channel.
MAX_ALERTS = 100
#: Slack answers meaning the board message is gone: post a new one.
_GONE = frozenset({"message_not_found", "cant_update_message", "edit_window_closed"})


def _code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    try:
        return str((response or {}).get("error", "") or "")
    except Exception:
        return ""


def digest(snapshot: model.BoardSnapshot, links: dict[str, str]) -> str:
    """What the board SHOWS, hashed: the snapshot plus the links it renders."""
    payload = {
        "goal": snapshot.goal,
        "round": snapshot.round,
        "rows": [row.__dict__ for row in snapshot.rows],
        "links": links,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def alert_token(row: model.WorkerRow) -> str:
    """Once per report that waits on a person; once per spell of a pending approval."""
    if row.waiting_on == "approval":
        return f"{row.item_id}:approval:{row.report_stamp}"
    return f"{row.item_id}:{row.report_stamp or 'live'}"


def _is_approval_alert(key: str) -> bool:
    return ":approval:" in key


def _stamp() -> str:
    return time.strftime("%H:%M UTC", time.gmtime())


async def publish(
    binding: Binding,
    snapshot: model.BoardSnapshot | None,
    *,
    link_for: Callable[[str], str],
    owner_id: str = "",
) -> str:
    """Bring the channel's board up to *snapshot*; returns what it did (for tests/logs).

    ``""`` nothing to do, ``posted`` / ``updated`` / ``reposted``, or a refusal
    word (``no_slack``, ``denied``, ``archived``, ``stopped``, ``failed``).
    """
    if snapshot is None:
        return ""
    slack = surface.slack()
    if slack is None:
        return "no_slack"
    channel = binding.channel_id
    if await surface.is_archived(binding):
        return "archived"
    if surface.channel_stopped(channel):
        return "stopped"
    if not await surface.outbound_permitted():
        surface.audit(
            "slack", "slack.code_channel.board", "denied", f"channel={channel}", "channels policy"
        )
        return "denied"
    links = {r.session_key: link_for(r.session_key) for r in snapshot.rows if r.session_key}
    conductor_link = link_for(binding.session_key) if binding.session_key else ""
    links["@conductor"] = conductor_link

    def _link(key: str) -> str:
        return links.get(key, "")

    record = await get_async(channel)
    ts = record.board_ts if record else ""
    pinned = bool(record.board_pinned) if record else False
    done = ""
    new_digest = digest(snapshot, links)
    if not ts or record is None or record.board_digest != new_digest:
        blocks = render.board_blocks(
            snapshot, _link, conductor_link=conductor_link, updated=_stamp()
        )
        text = render.fallback_text(snapshot)
        if ts:
            try:
                await slack.update_message(channel, ts, text, blocks)
                done = "updated"
            except Exception as exc:
                if _code(exc) in _GONE:
                    ts, pinned = "", False
                else:
                    logger.info(
                        "code channel %s: board update failed: %s", channel, _code(exc) or exc
                    )
                    return "failed"
        if not ts:
            try:
                ts = str(await slack.post_blocks(channel, blocks, text) or "")
            except Exception as exc:
                logger.info("code channel %s: board post failed: %s", channel, _code(exc) or exc)
                return "failed"
            if not ts:
                return "failed"
            done = "reposted" if record and record.board_ts else "posted"
            surface.audit(
                "slack", "slack.code_channel.board", "allowed", f"channel={channel} ts={ts}"
            )
        try:
            await update_async(channel, board_ts=ts, board_digest=new_digest, board_pinned=pinned)
        except Exception:
            logger.warning("code channel %s: board pointer not stored", channel, exc_info=True)
    if ts and not pinned:
        pinned = await _pin(slack, channel, ts)
        if pinned:
            try:
                await update_async(channel, board_pinned=True)
            except Exception:
                logger.debug("code channel %s: pin flag not stored", channel, exc_info=True)
    if ts:
        await _record_summary(binding, ts)
        await _alert(slack, channel, ts, snapshot, _link, owner_id)
    return done


async def _record_summary(binding: Binding, board_ts: str) -> None:
    """Make the board the channel's ``summary_message`` while the session runs.

    The board IS the conductor's running summary, so Slack can show it live
    (`code channel properties`_), not only after the archive. A current wrap-up
    (posted for this same session) is the summary instead and is never
    displaced; it is recorded by :func:`..wrapup.post_wrap_up`, and stays the
    summary for the rest of that session even as the board keeps changing. Sent
    once per board message: an edit keeps the ts, so it stays the summary.

    .. _code channel properties: https://docs.slack.dev/ai/slack-code/code-channel-properties#summary-message
    """
    channel = binding.channel_id
    record = await get_async(channel)
    if record is not None and record.summary_message_ts == board_ts:
        return
    if (
        record is not None
        and record.summary_ts
        and record.summary_session_key == binding.session_key
    ):
        return
    client = surface.client()
    if client is None:
        return
    from kiro_crew.slack.code_channels.models import CodeChannelApiError

    try:
        await client.set_summary_message(channel, board_ts)
    except CodeChannelApiError as exc:
        logger.info("code channel %s: live summary not recorded: %s", channel, exc.code)
        from kiro_crew.slack.code_channels.notices import NOTIFIED, notify_refusal

        if exc.code in NOTIFIED:
            # Slack will refuse it again: not asked once more for this message.
            await notify_refusal(channel, exc.code, session_key=binding.session_key)
            await update_async(channel, summary_message_ts=board_ts)
        return
    try:
        await update_async(channel, summary_message_ts=board_ts)
    except Exception:
        logger.debug("code channel %s: summary pointer not stored", channel, exc_info=True)


async def _pin(slack: Any, channel: str, ts: str) -> bool:
    try:
        await slack.add_pin(channel, ts)
        return True
    except Exception as exc:
        if _code(exc) == "already_pinned":
            return True
        logger.info("code channel %s: board not pinned: %s", channel, _code(exc) or exc)
        return False


async def _alert(
    slack: Any,
    channel: str,
    board_ts: str,
    snapshot: model.BoardSnapshot,
    link_for: Callable[[str], str],
    owner_id: str,
) -> None:
    """Thread one owner mention per worker report that waits on a person."""
    waiting = [row for row in snapshot.rows if row.needs_owner]
    record = await get_async(channel)
    sent = list(record.board_alerts) if record else []
    # An approval alert lives only while that approval is pending: once it is
    # answered it is forgotten, so the NEXT approval pages again.
    live = {alert_token(row) for row in waiting}
    kept = [t for t in sent if not _is_approval_alert(t) or t in live]
    cleared = len(kept) != len(sent)
    sent = kept
    fresh = [row for row in waiting if alert_token(row) not in sent]
    for row in fresh:
        try:
            await slack.post_message(
                channel, render.needs_you_text(row, owner_id, link_for), thread_ts=board_ts
            )
        except Exception as exc:
            logger.info("code channel %s: needs-you reply failed: %s", channel, _code(exc) or exc)
            continue
        sent.append(alert_token(row))
        surface.audit(
            "slack",
            "slack.code_channel.board_alert",
            "allowed",
            f"channel={channel} item={row.item_id}",
        )
    if fresh or cleared:
        try:
            await update_async(channel, board_alerts=sent[-MAX_ALERTS:])
        except Exception:
            logger.debug("code channel %s: alert tokens not stored", channel, exc_info=True)
