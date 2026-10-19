# SPDX-License-Identifier: Apache-2.0
"""A crewmate on its own file chats on the conductor and hands work to its worker.

The chat half: a binding whose ``kiro_agent`` names a file found ONLY in the
crewmate folder runs its chat on ``kirocrew-mate-conductor``, so the file never
reaches the chat. The hand-off half: that chat's ``session_create`` with no
agent (or the worker named bare) opens ``kirocrew-mate-worker`` on the
crewmate's own workspace, store and member identity. Driven through the real
config, resolver and ``create_session``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from chat_test_helpers import _make_state
from member_memory_helpers import forget_declared_stores, write_member_home

from kiro_crew import mate_worker
from kiro_crew.config import loader
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT
from kiro_crew.members import DM_SLOT_KEY_PREFIX


class TestMemberChatAgent:
    def test_a_crewmate_owned_file_chats_on_the_conductor(self, monkeypatch):
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
        monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"pixel"}))
        assert mate_worker.member_chat_agent("pixel") == MATE_CONDUCTOR_AGENT

    def test_any_other_binding_runs_as_it_is_named(self, monkeypatch):
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
        monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"pixel"}))
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"reviewer"}))
        assert mate_worker.member_chat_agent("reviewer") == "reviewer"
        assert mate_worker.member_chat_agent("") == ""


def test_the_refresh_publishes_only_names_the_user_level_does_not_declare(tmp_path, monkeypatch):
    user, mates = tmp_path / "agents", tmp_path / "mates"
    user.mkdir()
    mates.mkdir()
    for directory, name in ((user, "shared"), (mates, "shared"), (mates, "pixel")):
        (directory / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset())
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset())
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", False)
    stems = dict(loader._MATERIALIZED_STEMS)
    try:
        loader.refresh_materialized_agents(heal_default=False)
        assert loader._MATERIALIZED_MATE_ONLY == frozenset({"pixel"})
        assert mate_worker.is_mate_owned_agent("pixel")
        assert not mate_worker.is_mate_owned_agent("shared")
    finally:
        loader._MATERIALIZED_STEMS.clear()
        loader._MATERIALIZED_STEMS.update(stems)


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture
def crewmate(tmp_path, monkeypatch):
    """``pixel`` bound to its own crewmate file, in its own workspace."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
    from kiro_crew.history import ConversationLog

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = write_member_home(tmp_path, "pixel", "ada")
    config["workspaces"] = {
        "default": {"dir": "workspace"},
        "member-pixel": {"dir": "workspace-member-pixel"},
        "member-ada": {"dir": "workspace-member-ada"},
    }
    config["agents"]["pixel"]["workspace"] = "member-pixel"
    config["agents"]["pixel"]["kiro_agent"] = "pixel"
    config["agents"]["ada"]["workspace"] = "member-ada"
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    for directory in ("workspace", "workspace-member-pixel", "workspace-member-ada"):
        (tmp_path / directory).mkdir(exist_ok=True)
    forget_declared_stores(monkeypatch)
    monkeypatch.setattr(
        loader,
        "_MATERIALIZED_AGENTS",
        frozenset({MATE_WORKER_AGENT, MATE_CONDUCTOR_AGENT, "pixel"}),
    )
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"pixel"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    caller = state.get_or_create_slot(DM_SLOT_KEY_PREFIX + "pixel", mode="member")
    caller.workspace = "member-pixel"
    caller.agent = "pixel"
    execution = resolve_member_execution(KiroCrewConfig.load(), "pixel")
    caller.memory_store = execution.store.legacy_name
    bind_session_execution(slot_history_key(caller), execution, vouch=True)
    return state, caller, execution


def _create(state, caller, **kwargs):
    return asyncio.run(
        sc.create_session(state, caller_session_key=slot_history_key(caller), **kwargs)
    )


def test_the_crewmates_own_chat_runs_on_the_conductor(crewmate):
    _state, _caller, execution = crewmate
    assert execution.template_id == MATE_CONDUCTOR_AGENT


@pytest.mark.parametrize(
    "named, kind",
    [
        ("", ""),
        (MATE_WORKER_AGENT, ""),
        (MATE_WORKER_AGENT, "template"),
        # The stock leaf worker the session_create schema names for every caller.
        ("kirocrew-worker", "template"),
        ("kirocrew-worker", ""),
        # Whatever else the model names: the gateway, not the prompt, decides.
        ("kirocrew", ""),
        ("some-agent-nobody-declared", ""),
        ("reviewer", "template"),
        # A template that shares a peer's name is still a template.
        ("ada", "template"),
        # Its own name is its own worker, not a second conductor chat.
        ("pixel", ""),
    ],
)
def test_the_conductor_hands_off_to_its_worker(crewmate, named, kind):
    from kiro_crew.execution_context import read_session_execution

    state, caller, execution = crewmate
    result = _create(state, caller, agent=named, agent_kind=kind)

    child = state.get_slot(result["target"])
    assert child is not None
    assert child.agent == MATE_WORKER_AGENT
    assert child.workspace == "member-pixel"
    actual = read_session_execution(slot_history_key(child), required=True)
    # The crewmate's own memory and identity, under the worker persona.
    assert actual.store == execution.store
    assert actual.member_id == "pixel"
    assert actual.selection_kind == "template"
    assert actual.template_id == MATE_WORKER_AGENT


def test_the_conductor_naming_another_crewmate_stays_refused(crewmate):
    state, caller, _execution = crewmate
    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller, agent="ada")
    assert refused.value.code == "agent_workspace_mismatch"


def test_the_worker_does_not_hand_off_to_another_worker_by_default(crewmate):
    """A worker is not a conductor: its own unnamed child inherits the worker."""
    from kiro_crew.execution_context import read_session_execution

    state, caller, _execution = crewmate
    worker = state.get_slot(_create(state, caller)["target"])
    assert not sc_conducts(worker)
    _ = read_session_execution(slot_history_key(worker), required=True)


def sc_conducts(slot):
    from kiro_crew.dashboard.session_control_parts.session_create import _conducts_for_a_worker
    from kiro_crew.execution_context import read_session_execution

    return _conducts_for_a_worker(slot, read_session_execution(slot_history_key(slot)))
