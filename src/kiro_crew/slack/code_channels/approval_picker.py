"""A code channel's approval mode, set from Slack.

A code channel's session starts on ``slack.code_channel_defaults.approval_mode``
(:mod:`.binding`). From the channel, the owner can put it on Trust with
**Trust this channel** on a tool-approval card the channel's session posted,
which approves that call too (the Trust change answers every call the session
is waiting on).

**One path.** Every change runs the dashboard picker's own mutation,
:func:`kiro_crew.dashboard.chat_handlers.apply_approval_mode`, on the channel's
slot -- the call ``POST /api/chat/mode`` makes after its caller checks, and the
one a crewmate profile write already makes. No approval store of its own: the
slot flags, the session policy a worker inherits, the pending-card sweep and the
``mode_change:<mode>`` SEL row are that function's. The row's caller names the
Slack user (``slack-code-channels:<user>``), and this module writes one more row
per decision (operation :data:`OPERATION`), refusals included.

**Who may change it.** The owner (``is_allowed_user``), in a channel and origin
that still admit the bot, in the bound workspace, in a channel not archived --
the gates every code-channel command applies. Anyone else is told so
ephemerally, and nothing changes.

**Trust is said out loud.** A change to Trust posts one line in the channel:
tools run without prompts, and workers the session starts inherit it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import bound_slot, code_channels_on, live_config
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)

#: The approval card's "Trust this channel" button (routed by
#: ``slack/interactions.py``).
TRUST_CALL_ACTION_ID = "kc_code_channel_trust_call"

AUDIT_CALLER = "slack-code-channels"
OPERATION = "slack.code_channel.approval_mode"

#: The kickoff select's action id (routed by ``slack/interactions.py``).
APPROVAL_SELECT_ACTION_ID = "kc_code_channel_approval"
#: Prefix of the select block's id; a fresh suffix per render makes Slack re-draw
#: the select from ``initial_option`` instead of keeping a refused pick.
APPROVAL_BLOCK_PREFIX = "kc_code_channel_approval."

#: The modes in picker order: (mode, label, ``/kirocrew trust`` word).
MODES: tuple[tuple[str, str, str], ...] = (
    ("normal", "Ask", "ask"),
    ("trust_reads", "Trust reads", "reads"),
    ("trust", "Trust", "all"),
)
_LABELS = {mode: text for mode, text, _ in MODES}
_BY_WORD = {word: mode for mode, _, word in MODES}

PICKER_LABEL = "*Approval mode*"
PICKER_PLACEHOLDER = "Choose a mode"
SET_NOTE = "Approval mode set to *{label}* by <@{user}>."
UNKNOWN_TEXT = "❌ `{word}` isn't a mode. Use `/{command} trust ask`, `reads` or `all`."
CURRENT_TEXT = (
    "This channel is on *{label}*. Change it with `/{command} trust ask`, `reads` or `all`."
)

TRUST_CALL_LABEL = "Trust this channel"
TRUST_WARNING = (
    "⚠️ <@{user}> put this channel on *Trust*: the agent's tools run without asking, "
    "and workers it starts inherit that. `/{command} trust ask` turns it off."
)
FAILED_TEXT = "⚠️ Could not change the approval mode: {reason}"
NO_SESSION_TEXT = "This channel's session isn't open right now. Try again in a moment."
NOT_ALLOWED_TEXT = "⛔ You can't change this channel's approval mode."
NOT_THIS_CARD_TEXT = "⛔ That approval isn't this channel's to trust."
NOT_LIVE_TEXT = "⛔ That approval isn't waiting any more, so nothing changed."
#: The same words ``apply_approval_mode`` answers a spent prompt with (its 409).
STALE_REASON = "the approval is no longer waiting"
TRUSTED_CARD_TEXT = "🤝 Approved, and this channel is now on Trust (<@{user}>)."


def _audit(outcome: str, resources: str, error: str = "") -> None:
    surface.audit(AUDIT_CALLER, OPERATION, outcome, resources, error)


# ── The mode a slot holds ─────────────────────────────────────────────────


def current_mode(slot: Any) -> str:
    """``normal``, ``trust_reads`` or ``trust`` -- what the dashboard header shows.

    A live trust scope reads as Trust, as it does in the header.
    """
    if getattr(slot, "_trust", False) or getattr(slot, "_trust_scope", ""):
        return "trust"
    if getattr(slot, "_trust_reads", False):
        return "trust_reads"
    return "normal"


def label(mode: str) -> str:
    return _LABELS.get(mode, _LABELS["normal"])


def _command() -> str:
    """The app's slash command (``slack.command``), as the person types it."""
    try:
        name = str(getattr(getattr(live_config(), "slack", None), "command", "") or "")
    except Exception:
        name = ""
    return name.strip().lstrip("/") or "kirocrew"


# ── The kickoff select ────────────────────────────────────────────────────


def _option(mode: str) -> dict[str, Any]:
    return {"text": {"type": "plain_text", "text": label(mode)}, "value": mode}


def approval_block(selected: str) -> dict[str, Any]:
    """The kickoff's Approval mode select, on *selected* (drawn by
    :func:`.agent_picker.kickoff_blocks`, below the agent picker)."""
    return {
        "type": "section",
        "block_id": f"{APPROVAL_BLOCK_PREFIX}{time.time_ns()}",
        "text": {"type": "mrkdwn", "text": PICKER_LABEL},
        "accessory": {
            "type": "static_select",
            "action_id": APPROVAL_SELECT_ACTION_ID,
            "placeholder": {"type": "plain_text", "text": PICKER_PLACEHOLDER},
            "options": [_option(mode) for mode, _, _ in MODES],
            "initial_option": _option(selected if selected in _LABELS else "normal"),
        },
    }


async def _refresh(binding: Binding, note: str = "") -> None:
    """Re-draw the kickoff from the session's real agent and mode (never raises)."""
    from kiro_crew.slack.code_channels.agent_picker import refresh_kickoff

    await refresh_kickoff(binding, note=note)


# ── The change ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModeResult:
    """What :func:`set_channel_mode` did, and the reply for the person.

    ``result`` is ``set``, ``unchanged``, ``unavailable`` or ``refused``.
    """

    result: str
    text: str


_locks: dict[str, asyncio.Lock] = {}


def _lock(channel_id: str) -> asyncio.Lock:
    lock = _locks.get(channel_id)
    if lock is None:
        lock = _locks[channel_id] = asyncio.Lock()
    return lock


def reset() -> None:
    """Forget per-channel locks (tests)."""
    _locks.clear()


async def _apply(
    state: Any, slot: Any, mode: str, user: str, still_live: Callable[[], bool] | None = None
) -> tuple[bool, str]:
    """The dashboard picker's mutation on *slot*; ``(ok, error)``.

    *still_live*, when given, is asked once more after the last await and right
    before the mutation: a change asked for on behalf of one approval card must
    not land once that card's prompt has been answered or has expired.
    """
    from kiro_crew.dashboard.chat_handlers import apply_approval_mode
    from kiro_crew.safety_override import approval_mode_permitted

    # The governance check ``POST /api/chat/mode`` makes before it mutates.
    if not await asyncio.to_thread(approval_mode_permitted, mode):
        return False, "the approval-modes policy does not allow it"
    if still_live is not None and not still_live():
        return False, STALE_REASON
    resp = await apply_approval_mode(
        state,
        mode=mode,
        slot=slot,
        slot_key=slot.key,
        request_app="",
        cron_creator="",
        audit_caller=lambda _label: f"{AUDIT_CALLER}:{user}",
        # Asked again inside, after its own await and right before the grant.
        still_live=still_live,
    )
    try:
        body = json.loads(getattr(resp, "text", "") or "{}")
    except ValueError:
        body = {}
    if getattr(resp, "status", 200) >= 400 or not body.get("ok", False):
        return False, str(body.get("error") or "the change was refused")
    return True, ""


async def set_channel_mode(
    binding: Binding,
    mode: str,
    user: str,
    *,
    via: str,
    still_live: Callable[[], bool] | None = None,
) -> ModeResult:
    """Put *binding*'s session on *mode* for *user*, who is already authorized.

    *still_live* ties the change to one approval card: it is checked under the
    channel lock and again right before the mutation, and a card whose prompt
    is no longer waiting changes no mode. On a change to Trust, posts the
    channel's warning line.
    """
    channel = binding.channel_id
    resources = f"channel={channel} user={user} mode={mode} via={via}"
    if mode not in _LABELS:
        _audit("denied", resources, "unknown mode")
        return ModeResult("refused", FAILED_TEXT.format(reason="unknown mode"))
    state = surface.dashboard_state()
    slot = bound_slot(state, binding)
    if slot is None:
        _audit("error", resources, "session unavailable")
        return ModeResult("unavailable", NO_SESSION_TEXT)
    async with _lock(channel):
        if still_live is not None and not still_live():
            _audit("denied", resources, STALE_REASON)
            return ModeResult("refused", NOT_LIVE_TEXT)
        if current_mode(slot) == mode:
            _audit("allowed", f"{resources} result=unchanged")
            return ModeResult("unchanged", f"ℹ️ This channel is already on *{label(mode)}*.")
        try:
            ok, error = await _apply(state, slot, mode, user, still_live)
        except Exception:
            logger.warning("code channel %s: approval mode change failed", channel, exc_info=True)
            ok, error = False, "the change failed"
        if not ok and error == STALE_REASON:
            _audit("denied", resources, STALE_REASON)
            return ModeResult("refused", NOT_LIVE_TEXT)
        if not ok:
            _audit("error", f"{resources} result=refused", error)
            return ModeResult("refused", FAILED_TEXT.format(reason=error))
        _audit("allowed", f"{resources} result=set")
    if mode == "trust":
        await _post_channel(channel, TRUST_WARNING.format(user=user, command=_command()))
    await _refresh(binding, note=SET_NOTE.format(label=label(mode), user=user))
    return ModeResult("set", f"✅ This channel is now on *{label(mode)}*.")


async def _post_channel(channel: str, text: str) -> None:
    """A top-level line in the channel, where the session's replies go."""
    slack = surface.slack()
    if slack is None or not await surface.outbound_permitted():
        return
    try:
        await slack.post_message(channel, text)
    except Exception:
        logger.info("code channel %s: approval notice not posted", channel, exc_info=True)


# ── Authorization ─────────────────────────────────────────────────────────


async def authorize(
    orch: Any, binding: Binding, user: str, team: str, resources: str
) -> str | None:
    """None when *user* may change *binding*'s mode; else the refusal to show.

    The code channel's one owner gate (:func:`.agent_picker.authorize`: bound
    workspace, owner, channel and origin admit the bot, not archived), with this
    module's audit rows. ``""`` means refuse silently (a foreign workspace).
    """
    from kiro_crew.slack.code_channels import agent_picker

    return await agent_picker.authorize(
        orch, binding, user, team, resources, audit=_audit, not_allowed=NOT_ALLOWED_TEXT
    )


async def _ephemeral(channel: str, user: str, text: str) -> None:
    slack = surface.slack()
    if slack is None or not (channel and user and text):
        return
    try:
        await slack.post_ephemeral(channel, user, text)
    except Exception:
        logger.debug("code channel ephemeral failed", exc_info=True)


def _ids(payload: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any]]:
    """``(channel, user, message ts, team, action)`` of a ``block_actions``."""
    channel = str((payload.get("channel") or {}).get("id") or "")
    user = str((payload.get("user") or {}).get("id") or "")
    msg_ts = str((payload.get("message") or {}).get("ts") or "")
    team = str(
        (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""
    )
    action = (payload.get("actions") or [{}])[0] or {}
    return channel, user, msg_ts, team, action


# ── "Trust this channel" on an approval card ──────────────────────────────


def _card_entry(channel: str, msg_ts: str) -> Any | None:
    from kiro_crew.slack.handler import _linked_approvals

    return _linked_approvals.get(f"{channel}:{msg_ts}")


async def handle_trust_call(orch: Any, payload: dict[str, Any]) -> bool:
    """A click on an approval card's "Trust this channel"; False when the gate is shut.

    Only a card the channel's OWN session posted counts: the card's registry
    entry must name the session the channel's binding resolves to. The session
    goes to Trust through :func:`set_channel_mode`, whose sweep approves every
    call the session is waiting on, this one included; a Trust that does not
    take approves nothing, so a failed escalation is never reported as an
    approval.
    """
    if not code_channels_on(live_config(orch)):
        return False
    channel, user, msg_ts, team, _action = _ids(payload)
    resources = f"channel={channel} user={user} via=approval_card message={msg_ts}"
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    entry = _card_entry(channel, msg_ts) if binding is not None else None
    state = surface.dashboard_state()
    slot = bound_slot(state, binding) if binding is not None else None
    from kiro_crew.dashboard.chat_utils import effective_session_key

    if (
        binding is None
        or entry is None
        or slot is None
        or str(getattr(entry, "session_key", "")) != effective_session_key(slot)
    ):
        _audit("denied", resources, "not this channel's approval card")
        await _ephemeral(channel, user, NOT_THIS_CARD_TEXT)
        return True
    refusal = await authorize(orch, binding, user, team, resources)
    if refusal is not None:
        await _ephemeral(channel, user, refusal)
        return True
    # Pin the ONE call this card asks about: the channel slot's own future for
    # the card's request id, captured before anything changes. Request ids are
    # only unique per session, so nothing here ever looks one up state-wide.
    request_id = str(getattr(entry, "request_id", ""))
    futures = getattr(slot, "_approval_futures", None) or {}
    expected = futures.get(request_id)

    def _live() -> bool:
        # The card's own prompt, still waiting on the channel's slot.
        return (
            expected is not None
            and not expected.done()
            and getattr(slot, "_approval_futures", {}).get(request_id) is expected
        )

    if not _live():
        _audit("denied", resources, STALE_REASON)
        await _ephemeral(channel, user, NOT_LIVE_TEXT)
        return True
    outcome = await set_channel_mode(binding, "trust", user, via="approval_card", still_live=_live)
    if outcome.result not in ("set", "unchanged"):
        await _ephemeral(channel, user, outcome.text)
        return True
    # A change to Trust answered the call through its slot-scoped sweep. An
    # already-trusted session's card (a race) is answered on that slot alone,
    # and only while the slot still holds the very future the card was for.
    resolved = expected is not None and expected.done()
    if not resolved and expected is not None:
        resolve = getattr(state, "resolve_slot_approval", None)
        if callable(resolve):
            try:
                resolved = bool(resolve(slot, request_id, True, expected_future=expected))
            except Exception:
                logger.warning("code channel %s: trusted card not resolved", channel, exc_info=True)
    _audit(
        "allowed",
        f"{resources} session={entry.session_key} request={request_id} result=approved",
        "" if resolved else "future_not_found",
    )
    slack = surface.slack()
    if slack is not None:
        try:
            await slack.update_message(channel, msg_ts, text=TRUSTED_CARD_TEXT.format(user=user))
        except Exception:
            logger.debug("trusted approval card not updated", exc_info=True)
    return True


# ── The kickoff select and /kirocrew trust ───────────────────────────────


async def handle_select(orch: Any, payload: dict[str, Any]) -> bool:
    """A pick in the kickoff's Approval mode select; False when the gate is shut."""
    if not code_channels_on(live_config(orch)):
        return False
    channel, user, msg_ts, team, action = _ids(payload)
    mode = str(((action.get("selected_option") or {}).get("value")) or "").strip()
    resources = f"channel={channel} user={user} mode={mode} via=select"
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None or not msg_ts or msg_ts != binding.anchor_ts:
        _audit("denied", f"{resources} message={msg_ts}", "not a code channel kickoff")
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    refusal = await authorize(orch, binding, user, team, resources)
    if refusal is not None:
        await _ephemeral(channel, user, refusal)
        if refusal:
            await _refresh(binding)  # put the select back on the real mode
        return True
    outcome = await set_channel_mode(binding, mode, user, via="select")
    if outcome.result != "set":
        await _ephemeral(channel, user, outcome.text)
        await _refresh(binding)
    return True


async def handle_command(
    orch: Any,
    binding: Binding,
    user: str,
    arg: str,
    respond: Callable[[str], Awaitable[None]],
) -> None:
    """``/kirocrew trust [<ask|reads|all>]`` in an authorized code channel."""
    command = _command()
    word = arg.strip().lower()
    if not word:
        slot = bound_slot(surface.dashboard_state(), binding)
        _audit("allowed", f"channel={binding.channel_id} user={user} via=command result=shown")
        if slot is None:
            await respond(NO_SESSION_TEXT)
            return
        await respond(CURRENT_TEXT.format(label=label(current_mode(slot)), command=command))
        return
    mode = _BY_WORD.get(word)
    if mode is None:
        _audit("denied", f"channel={binding.channel_id} user={user} via=command", "unknown mode")
        from kiro_crew.slack.format import escape_mrkdwn

        await respond(UNKNOWN_TEXT.format(word=escape_mrkdwn(word[:40]), command=command))
        return
    await respond((await set_channel_mode(binding, mode, user, via="command")).text)
