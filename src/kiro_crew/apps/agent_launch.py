"""The Crew-owned launch copy a palette-opened app agent runs under on kiro-cli.

kiro-cli takes its agent as a NAME (``--agent <name>``, ``session/set_mode``) and
resolves that name against the session's checkout before ``~/.kiro/agents``, by
declared ``name`` or by filename stem. The app's own spec is registered as
``~/.kiro/agents/<app>--<agent>.json`` but declares the bare ``name: <agent>``, so
handing kiro-cli the bare name lets a checkout's same-named spec win, including one
written after every check ran.

So a session a palette command opened for its app's own agent does not launch the
bare name on kiro-cli. Before each turn the app's registered file -- that one file,
read by its path, never looked up by name -- is copied to
``~/.kiro/agents/kirocrew-app-launch-<app tag>-<keyed tag>.json`` with ``name`` set
to the same reserved name, and kiro-cli is asked for that name -- on ``--agent``
and in ``session/set_mode`` only (the Kiro runtime's ``launch_agent``). The session's
agent stays the bare name every gate, profile, audit row and hook reads. The
ownership proof (the registered file declares the agent) runs on the read that
produces the copy, so kiro-cli is only ever asked for a name Crew writes.

The keyed tag is an HMAC of ``<app>/<agent>`` under a secret this home creates
owner-only (``<data home>/app-agent-launch/app-agent-launch.key``), so a checkout
cannot be prepared to claim the name: it is not derivable from public names. Only
the gateway reads the secret, so its directory is masked in every agent sandbox
(``sandbox._CREW_HIDDEN_LEAVES``), precreated before every spawn so the mask has a
target, and fenced from the file tools, like the ``tag-grants`` store key. The prefix is
also reserved (``acp.managed_agent_shadow``): a checkout claiming a
``kirocrew-app-launch-*`` name refuses the kiro-cli spawn, as a claim on a
skill-view alias does. What remains is the window every reserved name has between
that spawn-time scan and kiro-cli's own read, open only to a writer that already
reads ``~/.kiro/agents`` and writes into the checkout in that window. A file
already at the copy's path that does not carry :data:`OWNER_MARKER` is somebody
else's and is never overwritten, and a second user-level spec declaring the
reserved name refuses the turn.

The copy is the registered spec's native skill view, as every Kiro launch runs
(``acp.skill_projection.project_agent_view``, reached through
``agent_sdk.drivers.acp.native_skill_view``): ``skill://`` resources are dropped
and the agent's skills are served through ``kirocrew-core``'s ``skill_search``,
scoped to the registered file (:func:`registered_spec_path`). Past that only
``name`` and ``description`` change: every grant, tool, server, hook and prompt is
the app's own, so permission and trust behave exactly as they do for the
registered spec. Discovery leaves the copies out of every roster
(``agent_discovery.list_agents``). The app's deregistration, and an update that
drops an agent from its manifest, remove them (:func:`remove_app_agent_launches`).
The name is fixed per home, app and agent, and the file is rewritten only when its
content changed, so concurrent sessions of one app agent share one copy.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import stat
import threading
from collections.abc import Collection
from pathlib import Path
from typing import Any

from kiro_crew.agent_spec_format import APP_AGENT_LAUNCH_PREFIX

logger = logging.getLogger(__name__)

#: Opening of every launch copy's ``description``: how this module tells its own
#: file from a user's. ``description`` is a first-class kiro-cli spec field, so it
#: survives ``deny_unknown_fields`` where a custom key would not.
OWNER_MARKER = "Kiro Crew app agent launch copy"

#: Hex digits of the app tag, the prefix :func:`remove_app_agent_launches`
#: matches; it must not collide across the apps installed on one host.
_TAG_HEX = 8

#: Hex digits of the keyed agent tag. Keyed with a secret only this Kiro Crew home
#: holds, so a checkout cannot be written ahead of time to claim the name.
_KEYED_TAG_HEX = 24

#: The directory under the data home that holds the per-home secret behind the keyed
#: tag. A whole DIRECTORY rather than a file in the data-home root: a mask covers a
#: path, so the first-use temp staged beside a root-level key would sit at a name every
#: sandbox can see, and a key created after a sandbox spawned would appear unmasked in
#: it. The directory is masked and precreated, so both are covered. Spelled here and in
#: ``sandbox_leaves``; ``test_app_agent_launch_copy`` pins the two equal.
_KEY_DIR_LEAF = "app-agent-launch"
#: The per-home secret's file name inside :data:`_KEY_DIR_LEAF`, owner-only.
_KEY_FILENAME = "app-agent-launch.key"

#: Serializes a publication against a removal in this process.
_LAUNCH_FILE_LOCK = threading.Lock()

#: Launch copies whose published view is a skill-search agent, by name: the
#: projection's own verdict (``project_agent_view``), kept so the spawn holds the
#: copy exactly as the projection would have. Read under :data:`_LAUNCH_FILE_LOCK`.
_SEARCH_COPIES: set[str] = set()


class AppAgentLaunchError(RuntimeError):
    """The launch copy could not be produced; the app-owned turn must not run.

    ``code`` is stable and logged; ``detail`` is the sentence the chat shows.

    * ``not_installed`` -- the app's registered spec is absent or does not declare
      the agent.
    * ``unverifiable`` -- the registered entry, or the file at the copy's path,
      could not be read safely (a link, a protected target, an unreadable file).
    * ``foreign`` -- a file at the copy's path is not this module's.
    * ``shadowed`` -- another user-level spec declares the copy's name.
    * ``no_skill_view`` -- the registered spec cannot get a native skill view (its
      ``kirocrew-core`` or ``skill_search`` is restricted), as any Kiro spawn refuses.
    * ``write_failed`` -- the copy could not be written.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{detail} [{code}]")
        self.code = code
        self.detail = detail


def _tag(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_TAG_HEX]


def _app_prefix(app_name: str) -> str:
    return f"{APP_AGENT_LAUNCH_PREFIX}{_tag(app_name)}-"


def _host_key() -> bytes:
    """This home's launch-name secret, created owner-only on first use.

    Raises :class:`AppAgentLaunchError` when it can be neither read nor created,
    so no copy is published under a name a checkout could predict.
    """
    from kiro_crew import platform_compat
    from kiro_crew.config.paths import data_home

    key_dir = data_home() / _KEY_DIR_LEAF
    path = key_dir / _KEY_FILENAME
    try:
        platform_compat.make_owner_only_dir(key_dir)
        if not stat.S_ISDIR(os.lstat(key_dir).st_mode):
            raise OSError("not a directory")
    except OSError as exc:
        # A link or a file at the name is not the masked directory: a key read or
        # written through it would not be the one only this gateway holds.
        raise AppAgentLaunchError("unverifiable", f"could not use {key_dir}: {exc}") from exc
    if not os.path.lexists(path):
        # Written whole to a private temporary, then linked into place, so the key
        # file is never seen empty or half-written; a concurrent first use that
        # links first wins and both read its key.
        tmp = path.with_name(f".{_KEY_FILENAME}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(secrets.token_hex(32))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        except OSError as exc:
            raise AppAgentLaunchError("unverifiable", f"could not create {path}: {exc}") from exc
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError("not a regular file")
        key = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise AppAgentLaunchError("unverifiable", f"could not read {path}: {exc}") from exc
    if len(key) < 32:
        raise AppAgentLaunchError("unverifiable", f"{path} does not hold a launch key.")
    return key.encode("ascii")


def app_agent_launch_name(app_name: str, agent: str) -> str:
    """The reserved name *app_name*'s agent *agent* launches under on kiro-cli.

    Fixed for this home; raises :class:`AppAgentLaunchError` when its key is
    unreadable.
    """
    keyed = hmac.new(_host_key(), f"{app_name}/{agent}".encode("utf-8"), hashlib.sha256)
    return f"{_app_prefix(app_name)}{keyed.hexdigest()[:_KEYED_TAG_HEX]}"


def is_owned_launch_spec(data: object) -> bool:
    """Whether a parsed spec carries this module's owner marker."""
    if not isinstance(data, dict):
        return False
    description = data.get("description")
    return isinstance(description, str) and description.startswith(OWNER_MARKER)


def registered_spec_path(app_name: str, agent: str, agents_dir: Path | None = None) -> Path:
    """Where *app_name*'s agent *agent* is registered: ``<agents dir>/<app>--<agent>.json``.

    The one file the launch copy and the copy's skill scope are read from, by path,
    never by a name lookup that could reach a checkout's same-named spec.
    """
    from kiro_crew.apps import bridges

    directory = agents_dir if agents_dir is not None else bridges._kiro_agents_dir()
    return directory / (bridges._safe_link_name(bridges._namespace(app_name, agent)) + ".json")


def _read_registered_spec(app_name: str, agent: str, agents_dir: Path) -> dict[str, Any]:
    """The app's registered ``<app>--<agent>.json``, proven to declare *agent*."""
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.apps.manager import _screened_spec_path

    path = registered_spec_path(app_name, agent, agents_dir)
    # Screened before the reader resolves it, so a link to a protected location or
    # an untrusted share is refused without being followed.
    screened = _screened_spec_path(path)
    if screened is None:
        raise AppAgentLaunchError(
            "unverifiable",
            f"the agent entry at {path} could not be verified as the app's own agent. "
            "Fix or remove that entry, then run the command again from the command palette.",
        )
    data = _read_agent_spec(screened, operation="app_agent_launch", source="unknown")
    if not (isinstance(data, dict) and data.get("name", path.stem) == agent):
        raise AppAgentLaunchError(
            "not_installed", f"the app's agent {agent!r} is not registered at {path}."
        )
    return data


def _read_current(target: Path) -> dict[str, Any] | None:
    """The copy now at *target*, ``None`` when absent; raises when it cannot be judged."""
    from kiro_crew.agent_discovery import _read_agent_spec

    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AppAgentLaunchError("unverifiable", f"could not inspect {target}: {exc}") from exc
    # This module only ever writes regular files; a link or anything else is not ours.
    if not stat.S_ISREG(info.st_mode):
        raise AppAgentLaunchError(
            "foreign", f"{target} is not a regular file; refusing to write through it."
        )
    data = _read_agent_spec(target, operation="app_agent_launch", source="unknown")
    if not isinstance(data, dict) or not is_owned_launch_spec(data):
        raise AppAgentLaunchError(
            "foreign",
            f"{target} exists and was not written by Kiro Crew; it is not overwritten. "
            "Remove it, then run the command again from the command palette.",
        )
    return data


def is_search_launch_copy(name: str) -> bool:
    """Whether the launch copy *name* was published as a skill-search agent's view."""
    with _LAUNCH_FILE_LOCK:
        return name in _SEARCH_COPIES


def _skill_view(
    app_name: str, agent: str, spec: dict[str, Any], source: Path
) -> tuple[dict[str, Any], bool]:
    """*spec* as the native skill view every Kiro launch runs (``acp.skill_projection``).

    ``skill://`` resources are dropped and the agent's skills are served through
    ``kirocrew-core``'s ``skill_search``, scoped to this registered file; every other
    field is the app's own. One copy serves every workspace, so the view is built as
    if the workspace lends inherited skill trees: the search tool is then present
    for an unmapped agent too, and a workspace that lends none leaves it with nothing
    to find. A spec the projection refuses a view for refuses the turn, with the
    projection's own sentence.
    """
    from kiro_crew.agent_sdk.drivers.acp import native_skill_view
    from kiro_crew.config.paths import data_home

    view, search, refusal = native_skill_view(
        spec, source, agent, crew_home_id=data_home().absolute().as_posix()
    )
    if view is None:
        raise AppAgentLaunchError(
            "no_skill_view",
            f"the app's agent {app_name}/{agent} cannot get a skill view: {refusal}",
        )
    return view, search


def publish_app_agent_launch(app_name: str, agent: str) -> str:
    """Copy *app_name*'s registered spec for *agent* to its launch name; return the name.

    Raises :class:`AppAgentLaunchError` on every failure and never returns a name
    whose file is not on disk. Writes only when the content changed. Blocking file
    I/O: call it off the event loop.
    """
    from kiro_crew.agent import _atomic_json_write
    from kiro_crew.agent_sdk import agents_dir_competing_claim
    from kiro_crew.apps import bridges

    name = app_agent_launch_name(app_name, agent)
    agents_dir = bridges._kiro_agents_dir()
    target = agents_dir / f"{name}.json"
    with _LAUNCH_FILE_LOCK:
        source = _read_registered_spec(app_name, agent, agents_dir)
        launch, search = _skill_view(
            app_name, agent, source, registered_spec_path(app_name, agent, agents_dir)
        )
        launch["name"] = name
        launch["description"] = (
            f"{OWNER_MARKER} of {app_name}/{agent}; written before each turn, do not edit."
        )
        current = _read_current(target)
        claim = agents_dir_competing_claim(name, agents_dir, target)
        if claim is not None:
            if claim.kind == "claimed":
                raise AppAgentLaunchError(
                    "shadowed",
                    f"{Path(claim.detail).name} in {agents_dir} also declares the name "
                    f"{name!r}, which Kiro Crew reserves for this app's agent. Remove it, "
                    "then run the command again from the command palette.",
                )
            raise AppAgentLaunchError("unverifiable", claim.detail)
        if current != launch:
            try:
                _atomic_json_write(target, launch)
            except Exception as exc:  # noqa: BLE001 -- every write failure refuses the turn
                raise AppAgentLaunchError(
                    "write_failed", f"could not write {target}: {exc}"
                ) from exc
            logger.info("App agent launch copy written: %s", target)
        if search:
            _SEARCH_COPIES.add(name)
        else:
            _SEARCH_COPIES.discard(name)
    return name


def remove_app_agent_launches(app_name: str, *, keep_agents: Collection[str] = ()) -> int:
    """Remove the launch copies *app_name*'s agents left in the agents directory.

    The copies of the agents named in *keep_agents* stay. Only regular files
    carrying :data:`OWNER_MARKER` are removed. Best-effort: an unreadable
    directory or file is skipped.
    """
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.apps import bridges

    agents_dir = bridges._kiro_agents_dir()
    prefix = _app_prefix(app_name)
    try:
        kept = {f"{app_agent_launch_name(app_name, agent)}.json" for agent in keep_agents}
    except AppAgentLaunchError:
        # Without the key the kept names are unknown, and removing blind could
        # take a still-declared agent's copy; leave them all.
        logger.warning("App %s: launch key unreadable, launch copies left in place", app_name)
        return 0
    removed = 0
    with _LAUNCH_FILE_LOCK:
        try:
            entries = [p for p in agents_dir.iterdir() if p.name.startswith(prefix)]
        except OSError:
            return 0
        for path in entries:
            if path.suffix != ".json" or path.name in kept:
                continue
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                data = _read_agent_spec(path, operation="app_agent_launch", source="unknown")
                if not is_owned_launch_spec(data):
                    continue
                path.unlink()
                removed += 1
            except OSError:
                continue
    if removed:
        logger.info("Removed %d app agent launch copy(ies) for app %s", removed, app_name)
    return removed
