"""Slack Code channels: a worker's tool approval is asked in its channel too.

A worker the channel's own session created (``_created_by``) runs with at most
the channel's mode, so on Ask its prompt would show only in the dashboard. It is
relayed top-level into the channel, headed with the worker's title; Approve /
Reject resolve the worker's own future through the dashboard's slot-scoped
resolver; only the owner may answer; a prompt decided elsewhere updates the
card in place; and a session that is not this channel's worker is never relayed.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER

# The trust-card harness: a bound channel whose session is a real-shaped slot.
from test_slack_code_channel_agent_surface import CH, SLOT, env  # noqa: F401
from test_slack_code_channel_trust_from_slack import _slot, chan  # noqa: F401

from kiro_crew.slack.code_channels import approval_picker
from kiro_crew.slack.code_channels import worker_approvals as wa

WORKER = "chat-5-1"
WORKER_SESSION = f"dashboard:{WORKER}"
REQUEST = "req-w1"


@pytest.fixture
def relay(chan: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    monkeypatch.setattr(wa, "live_config", chan.cfg)
    worker = _slot(key=WORKER, channel="")
    worker._slack_linked = False
    worker._slack_channel = None
    worker._slack_thread_ts = None
    worker._created_by = SLOT
    worker.title = "Fix #21964"
    chan.worker = worker
    chan.state._slots[WORKER] = worker
    chan.state.get_slot = lambda key: chan.state._slots.get(key)
    wa.reset()
    yield chan
    wa.reset()
    from kiro_crew.slack.code_channels import surface

    surface._archived_here.discard(CH)


async def _run_relay(e: Any, slot: Any | None = None, tool_input: str = "npm test") -> Any:
    """Raise one prompt on *slot* (the worker by default) and hand it to the relay."""
    slot = slot or e.worker
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures[REQUEST] = fut
    card = await wa.relay(
        e.state, slot, f"dashboard:{slot.key}", REQUEST, "Run shell", tool_input, fut
    )
    return card, fut


def _click(user: str, card: Any, action_id: str) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user},
        "channel": {"id": card.channel},
        "message": {"ts": card.ts},
        "actions": [{"action_id": action_id, "value": card.request_id}],
    }


def _rows(e: Any) -> list[dict[str, Any]]:
    return e.sel.ops(wa.OPERATION)


# ── Posting ───────────────────────────────────────────────────────────────


def test_a_worker_approval_posts_top_level_in_the_channel(relay: Any) -> None:
    card, fut = asyncio.run(_run_relay(relay))
    assert card is not None and not fut.done()
    channel, blocks, text, thread_ts = relay.slack.blocks_posts[-1]
    assert channel == CH and thread_ts is None
    assert blocks[0]["text"]["text"] == "🔐 *Worker: Fix #21964* wants to run:"
    assert "npm test" in blocks[1]["text"]["text"]
    buttons = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert [b["action_id"] for b in buttons] == [
        wa.APPROVE_ACTION_ID,
        wa.TRUST_ACTION_ID,
        wa.REJECT_ACTION_ID,
    ]
    assert [b.get("style") for b in buttons].count("primary") == 1
    assert "Fix #21964" in text
    assert any("result=posted" in r["resources"] for r in _rows(relay))


def test_the_worker_title_cannot_ping_the_channel(relay: Any) -> None:
    relay.worker.title = "Fix it <!channel> *now*"
    asyncio.run(_run_relay(relay))
    head = relay.slack.blocks_posts[-1][1][0]["text"]["text"]
    assert "<!channel>" not in head and "&lt;!channel&gt;" in head


@pytest.mark.parametrize(
    "case", ["no_creator", "other_creator", "replaced_session", "linked_worker", "off", "archived"]
)
def test_a_session_that_is_not_this_channels_worker_is_never_relayed(relay: Any, case: str) -> None:
    worker = relay.worker
    if case == "no_creator":
        worker._created_by = ""
    elif case == "other_creator":
        other = _slot(key="chat-8-1", channel="")
        relay.state._slots[other.key] = other
        worker._created_by = other.key
    elif case == "replaced_session":
        # The channel's session was closed and replaced: the old slot's children
        # are not the channel's workers any more.
        relay.state.linked["100.000001"] = _slot(key="chat-7-1")
    elif case == "linked_worker":
        worker._slack_linked = True
    elif case == "off":
        relay.on = False
    elif case == "archived":
        from kiro_crew.slack.code_channels import surface

        surface.fence_archived(CH)
    card, fut = asyncio.run(_run_relay(relay))
    assert card is None and not fut.done()
    assert relay.slack.blocks_posts == []


# ── Answering from the channel ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action_id", "result", "line"),
    [
        (wa.APPROVE_ACTION_ID, "approved", "Approved by"),
        (wa.REJECT_ACTION_ID, "rejected", "Rejected by"),
    ],
)
def test_the_owner_answers_a_worker_approval(
    relay: Any, action_id: str, result: str, line: str
) -> None:
    async def go() -> tuple[Any, asyncio.Future]:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, action_id))
        await wa.finish(card, fut.result(), timed_out=False)
        return card, fut

    card, fut = asyncio.run(go())
    assert fut.result() == result
    assert relay.resolved == [(WORKER, REQUEST, result == "approved")]
    updates = [u for u in relay.slack.updates if u[1] == card.ts]
    assert len(updates) == 1 and line in updates[0][2]  # finish did not overwrite it
    assert all(b.get("type") != "actions" for b in updates[0][3])
    assert _rows(relay)[-1]["outcome"] == "allowed"


def test_a_non_owner_is_refused_and_nothing_is_answered(relay: Any) -> None:
    async def go() -> tuple[Any, asyncio.Future]:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click("USTRANGER", card, wa.APPROVE_ACTION_ID))
        return card, fut

    card, fut = asyncio.run(go())
    assert not fut.done() and relay.resolved == []
    assert relay.slack.ephemerals[-1][1] == "USTRANGER"
    assert not relay.slack.updates


def test_a_stale_card_is_refused(relay: Any) -> None:
    async def go() -> tuple[Any, asyncio.Future]:
        card, fut = await _run_relay(relay)
        fut.set_result("approved")  # answered in the dashboard meanwhile
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.REJECT_ACTION_ID))
        return card, fut

    _card, fut = asyncio.run(go())
    assert fut.result() == "approved" and relay.resolved == []
    assert relay.slack.ephemerals[-1][2] == wa.NOT_LIVE_TEXT


def test_trust_this_channel_on_a_worker_card(relay: Any) -> None:
    async def go() -> tuple[Any, asyncio.Future]:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return card, fut

    _card, fut = asyncio.run(go())
    assert fut.result() == "approved"
    assert relay.slot._trust is True  # the channel's session
    assert relay.worker._trust is False  # a running worker keeps its mode
    assert [r for r in relay.mode_rows if r["operation"] == "mode_change:trust"]
    assert [p for p in relay.slack.posts if "*Trust*" in p[1]]


# ── Answered elsewhere ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("outcome", "timed_out", "key"),
    [
        ("approved", False, "approved"),
        ("rejected", False, "rejected"),
        ("rejected", True, "timeout"),
    ],
)
def test_a_prompt_decided_elsewhere_updates_the_card_in_place(
    relay: Any, outcome: str, timed_out: bool, key: str
) -> None:
    async def go() -> Any:
        card, fut = await _run_relay(relay)
        fut.set_result(outcome)  # the dashboard (or the window) decided it
        await wa.finish(card, outcome, timed_out=timed_out)
        return card

    card = asyncio.run(go())
    channel, ts, text, blocks = relay.slack.updates[-1]
    assert (channel, ts) == (CH, card.ts)
    assert text == wa.ANSWERED_ELSEWHERE[key]
    assert all(b.get("type") != "actions" for b in blocks)
    assert f"{CH}:{card.ts}" not in wa._cards


def test_the_session_runner_relays_and_retires_worker_cards() -> None:
    """_run_chat hands an unlinked prompt to the relay and retires it in the finally."""
    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner._run_chat)
    assert "_worker_card = await relay_worker_approval(" in src
    assert "await finish_worker_approval(" in src
    assert chat_runner.relay_worker_approval is wa.relay
    assert chat_runner.finish_worker_approval is wa.finish


def test_the_dispatcher_routes_worker_card_controls(relay: Any, monkeypatch: Any) -> None:
    seen: list[str] = []

    async def click(_orch: Any, p: dict[str, Any]) -> bool:
        seen.append(p["actions"][0]["action_id"])
        return True

    monkeypatch.setattr(wa, "handle_click", click)
    card = SimpleNamespace(channel=CH, ts="300.000009", request_id=REQUEST)
    for action_id in sorted(wa.ACTION_IDS):
        assert asyncio.run(
            wa.dispatch_card_click(relay.orch, _click(OWNER, card, action_id), action_id)
        )
    assert sorted(seen) == sorted(wa.ACTION_IDS)
    assert not asyncio.run(wa.dispatch_card_click(relay.orch, {}, "approve_tool"))
    assert approval_picker.TRUST_CALL_ACTION_ID not in wa.ACTION_IDS


def test_a_code_channel_card_footer_cannot_ping_the_channel() -> None:
    from kiro_crew.slack.handler import _build_approval_blocks, _LinkedApprovalEvent

    event = _LinkedApprovalEvent(REQUEST, "Run <!here> now", "ls")
    code = _build_approval_blocks(event, is_dm=False, channel_trust=True)  # type: ignore[arg-type]
    plain = _build_approval_blocks(event, is_dm=False)  # type: ignore[arg-type]
    assert "<!here>" not in code[-1]["elements"][0]["text"]
    assert "<!here>" in plain[-1]["elements"][0]["text"]  # every other card is unchanged
