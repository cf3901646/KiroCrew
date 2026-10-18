"""The conductor's work board in a code channel.

A conductor-led code channel splits its work and hands each piece to a worker
session. This package shows that work IN the channel, published by the
workspace's own per-channel refresh (no second publisher):

* **The board** (:mod:`.publisher`): one pinned message, edited in place, with the
  goal, round, done/total, the overall state and the one-line aggregate
  ("3 workers running · 1 waiting on you") in its header, then one row per worker
  -- title, state, last report line, branch, PR + CI, "Open in dashboard". A
  worker that waits on a person gets one threaded reply mentioning the owner.
* **The context bar** (``workspace.context_bar``): one item per worker.
* **The Code tab** (``workspace.service._worker_groups``): the combined change,
  the channel's own diff then each worker's commits under a per-worker header.
* **The wrap-up** (:func:`outcome_lines`): one outcome line per worker.

Slack's ``agents.sessions.setStatus`` takes no text in a code channel (it has no
``loading_messages``), so the aggregate lives in the board header; the native
status keeps its plain working / ready signal (the conductor's turns, and its
workers' through the workspace's folder watcher).

WHAT IT READS: only the channel conductor's own ledger and its own workers
(:mod:`.source`, behind one scope check). WHAT IT PUBLISHES: only the fields
:mod:`.model` lists.

WHEN IT REFRESHES. The conductor's own turn ends (the core's hooks); a session in
the channel's folder -- a worker -- starts or ends a turn (the workspace's own
folder watcher, ``WorkspaceService.watch_tick``); a tool approval is raised on or
answered for one of ITS workers (:func:`note_session_turn`, called by the
dashboard's approval registry); and its ledger moves (the crew log's ``work`` fold
advancing for its board, :func:`_on_work_fold`, so the ledger routes are
untouched). A turn end -- the conductor's or a folder session's -- refreshes with
no debounce of its own; a turn start, an approval and a ledger move arm the refresh through
:data:`~.workspace.service.BOARD_DEBOUNCE_SECS`, so a burst costs one publish. A
worker is matched to its channel through the board this process last published,
so no store is read for a session that is nobody's worker; a conductor not yet on
a board is matched through the code-channel store's in-memory bindings
(:func:`note_ledger_write`), so its first dispatch posts and pins the board.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: ``worker slot key -> channel id`` for every worker on a board this process
#: published; ``conductor ledger key -> channel id`` likewise.
_worker_channels: dict[str, str] = {}
_conductor_channels: dict[str, str] = {}
_MAX_TRACKED = 4096


def remember(channel_id: str, conductor_key: str, worker_keys: list[str]) -> None:
    """Record which channel a conductor and its workers publish to."""
    from kiro_crew import session_ledger

    if len(_worker_channels) > _MAX_TRACKED:
        _worker_channels.clear()
    if len(_conductor_channels) > _MAX_TRACKED:
        _conductor_channels.clear()
    for key in worker_keys:
        if key:
            _worker_channels[key] = channel_id
    if conductor_key:
        # The fold names a board by the conductor's slot; both spellings are kept.
        _conductor_channels[conductor_key] = channel_id
        _conductor_channels[session_ledger.ledger_key(conductor_key)] = channel_id


def forget_channel(channel_id: str) -> None:
    for table in (_worker_channels, _conductor_channels):
        for key in [k for k, v in table.items() if v == channel_id]:
            table.pop(key, None)


def _schedule(channel_id: str) -> bool:
    from kiro_crew.slack.code_channels.binding import active_binding
    from kiro_crew.slack.code_channels.workspace import core as workspace
    from kiro_crew.slack.code_channels.workspace.service import BOARD_DEBOUNCE_SECS

    service = workspace.service()
    binding = active_binding(channel_id)
    if service is None or binding is None or binding.archived_at:
        return False
    service.schedule_refresh(binding, delay=BOARD_DEBOUNCE_SECS)
    return True


def note_session_turn(session_key: str) -> None:
    """A tool approval was raised on or answered for a session: refresh its conductor's board.

    Cheap and synchronous for every other session (one dict lookup); never raises.
    Must be called on the event loop.
    """
    try:
        channel = _worker_channels.get(session_key) or _worker_channels.get(
            session_key.removeprefix("dashboard_")
        )
        if channel:
            _schedule(channel)
    except Exception:
        logger.debug("code channel board: turn trigger failed", exc_info=True)


def note_ledger_write(conductor_key: str) -> None:
    """A conductor's ledger moved (a report, a create, a bind): refresh its board.

    A conductor whose board this process has not published yet -- its first
    ``goal``, ``create`` or ``bind`` -- is looked up among the code channels'
    bindings (the store's in-memory snapshot, no disk read), so the board posts
    and pins as the first worker is dispatched rather than when the conductor's
    turn ends. A key no channel is bound to is remembered as such for
    :data:`_UNBOUND_TTL_SECS`, so a busy ledger elsewhere costs one dict lookup.

    Must be called on the event loop; :func:`_on_work_fold` hops there first.
    """
    try:
        channel = _conductor_channels.get(conductor_key) or _bound_channel_of(conductor_key)
        if channel:
            _schedule(channel)
    except Exception:
        logger.debug("code channel board: ledger trigger failed", exc_info=True)


#: How long a ledger key no code channel is bound to is not looked up again.
_UNBOUND_TTL_SECS = 30.0
#: ``ledger key -> monotonic time`` it was last found bound to no channel.
_unbound: dict[str, float] = {}


def _recently_unbound(key: str) -> bool:
    seen = _unbound.get(key)
    return seen is not None and time.monotonic() - seen < _UNBOUND_TTL_SECS


def _bound_channel_of(conductor_key: str) -> str:
    """The channel whose live binding names *conductor_key*'s session, or ``""``."""
    from kiro_crew import session_ledger
    from kiro_crew.slack.code_channels.store import store

    key = session_ledger.ledger_key(conductor_key)
    if not key or _recently_unbound(key):
        return ""
    bindings = store().loaded_bindings()
    if bindings is None:
        return ""  # not loaded yet: unknown, so nothing is remembered as unbound
    for binding in bindings:
        if binding.archived_at or not binding.session_key:
            continue
        if session_ledger.ledger_key(binding.session_key) == key:
            _unbound.pop(key, None)
            # Read the board through the usual path, which re-judges scope.
            remember(binding.channel_id, binding.session_key, [])
            return str(binding.channel_id)
    if len(_unbound) > _MAX_TRACKED:
        _unbound.clear()
    _unbound[key] = time.monotonic()
    return ""


_bus: dict[str, Any] = {"dispose": None, "loop": None}


def _on_work_fold(event: Any) -> None:
    """ON THE BUS'S THREAD: dict lookups, then hop to the gateway loop. Never raises.

    A board this process has not published is hopped too (unless its key was
    just found bound to no channel), so a code channel's FIRST ledger write
    reaches :func:`note_ledger_write`, which looks its channel up.
    """
    try:
        board = str(getattr(event, "key", "") or "")
        loop = _bus["loop"]
        if not board or loop is None:
            return
        if board in _conductor_channels or not _recently_unbound(_fold_key(board)):
            loop.call_soon_threadsafe(note_ledger_write, board)
    except Exception:
        pass


def _fold_key(board: str) -> str:
    from kiro_crew import session_ledger

    return session_ledger.ledger_key(board)


def subscribe() -> None:
    """Subscribe once to every ``work`` fold advance (filtered here by board).

    Called on the event loop when the workspace's watcher starts (a channel's
    first turn or bind) and again by every board read; idempotent.
    """
    if _bus["dispose"] is not None:
        return
    try:
        from kiro_crew.crew_log import bus
        from kiro_crew.work_vocab import WORK_FOLD_NAME

        _bus["loop"] = asyncio.get_running_loop()
        _bus["dispose"] = bus.subscribe(
            bus.FOLD_ADVANCED, _on_work_fold, scope=bus.SCOPE_SLOT, fold=WORK_FOLD_NAME
        )
    except Exception:
        logger.debug("code channel board: no crew-log subscription", exc_info=True)


def reset_for_tests() -> None:
    dispose = _bus["dispose"]
    if callable(dispose):
        dispose()
    _bus["dispose"] = _bus["loop"] = None
    _worker_channels.clear()
    _conductor_channels.clear()
    _unbound.clear()
    _audience_retried.clear()


async def read(binding: Any) -> Any:
    """The production board reader the workspace service is built with."""
    from kiro_crew.slack.code_channels import surface
    from kiro_crew.slack.code_channels.board import source

    subscribe()
    snapshot = await source.read_board(surface.dashboard_state(), binding)
    if snapshot is not None:
        _audience_retried.pop(binding.channel_id, None)
        remember(
            binding.channel_id,
            binding.session_key,
            [row.session_key for row in snapshot.rows if row.session_key],
        )
        await _file_workers(binding, snapshot)
    else:
        _retry_once_audience_is_read(binding)
    return snapshot


#: How long after an "is this channel shared?" check started the board retries.
_AUDIENCE_RETRY_SECS = 10.0
#: At most one such retry per channel in this window, so a channel whose sharing
#: cannot be read (no Slack client) is not re-read on a loop.
_AUDIENCE_RETRY_WINDOW_SECS = 300.0
#: ``channel id -> monotonic time`` of its last audience retry.
_audience_retried: dict[str, float] = {}


def _retry_once_audience_is_read(binding: Any) -> None:
    """The scope refused only because the channel's sharing was not freshly on record.

    The exemption starts that read itself (``audience.refresh_soon``) but nothing
    re-arms the board, so a first dispatch landing then would post no board until
    the next trigger. One retry per channel per :data:`_AUDIENCE_RETRY_WINDOW_SECS`
    is scheduled; it re-judges everything.
    """
    from kiro_crew.slack.code_channels import audience

    channel = str(getattr(binding, "channel_id", "") or "")
    if not channel or getattr(binding, "archived_at", None):
        return
    last = _audience_retried.get(channel)
    if last is not None and time.monotonic() - last < _AUDIENCE_RETRY_WINDOW_SECS:
        return
    if audience.channel_shared(channel) is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if len(_audience_retried) > _MAX_TRACKED:
        _audience_retried.clear()
    _audience_retried[channel] = time.monotonic()
    loop.call_later(_AUDIENCE_RETRY_SECS, _schedule, channel)


def forget_unbound() -> None:
    """A channel was bound: no ledger key is known to be bound to no channel any more."""
    _unbound.clear()


async def publish(binding: Any, snapshot: Any) -> str:
    """The production board publisher the workspace service is built with."""
    from kiro_crew.slack.code_channels import surface
    from kiro_crew.slack.code_channels.board import links, publisher

    link_for = await asyncio.to_thread(links.link_builder)
    state = surface.dashboard_state()
    owner = str(getattr(state, "owner_id", "") or "")
    return await publisher.publish(binding, snapshot, link_for=link_for, owner_id=owner)


async def _file_workers(binding: Any, snapshot: Any) -> None:
    from kiro_crew.slack.code_channels.board import filing

    try:
        await filing.file_unfiled_workers(binding, snapshot)
    except Exception:
        logger.debug("code channel board: worker filing failed", exc_info=True)


async def outcome_lines(binding: Any) -> list[str]:
    """The wrap-up's per-worker outcomes (``• Fix login: PR #12 merged``)."""
    from kiro_crew.slack.code_channels.board import render
    from kiro_crew.slack.code_channels.workspace import core as workspace

    service = workspace.service()
    snapshot = service.board(binding.team_id, binding.channel_id) if service else None
    if snapshot is None:
        try:
            snapshot = await read(binding)
        except Exception:
            snapshot = None
    if snapshot is None or not snapshot.rows:
        return []
    return [render.outcome_line(row) for row in snapshot.rows]
