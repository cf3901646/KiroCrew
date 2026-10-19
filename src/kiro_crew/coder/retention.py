"""What happens to a session's Coder workspace when the session is archived.

Archive stops it and stamps ``deletion_due_at``; a reopen clears the stamp, and
:func:`sweep_due` deletes due workspaces once proven. Nothing raises into close.
An AWS Fargate task is the workspace itself, so archiving stops it at once
(:func:`_archive_fargate`), with no retention window.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional
from weakref import WeakValueDictionary

from kiro_crew.coder.client import CoderClientError
from kiro_crew.coder.registry import DELETING, WorkspaceBinding, WorkspaceRegistryCorrupt

logger = logging.getLogger(__name__)

_RUNNING = "running"
#: Strong references: asyncio holds only weak ones.
_TASKS: "set[asyncio.Task[None]]" = set()
SWEEP_INTERVAL_SECS = 60.0
#: Keeps boot work off the startup path.
SWEEP_INITIAL_DELAY_SECS = 30.0


@dataclass
class SessionLifecycle:
    """Serialize one owner's workspace operations and supersede queued archives."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0


# Retained only by active preparations and queued archives.
_LIFECYCLES: WeakValueDictionary[tuple[asyncio.AbstractEventLoop, str], SessionLifecycle] = (
    WeakValueDictionary()
)


def session_lifecycle(session_key: str) -> SessionLifecycle:
    """Shared across provider instances on the gateway loop, keyed by workspace owner."""
    key = (asyncio.get_running_loop(), session_key)
    lifecycle = _LIFECYCLES.get(key)
    if lifecycle is None:
        lifecycle = SessionLifecycle()
        _LIFECYCLES[key] = lifecycle
    return lifecycle


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def _environments() -> Optional[Any]:
    """The live Coder environments, or None when Coder is not configured."""
    from kiro_crew.coder.session_launch import CoderSessionEnvironments
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
    if not getattr(section, "coder_url", ""):
        return None
    return CoderSessionEnvironments(section, await asyncio.to_thread(data_home))


def compute_survives_stop(environments: Any, binding: WorkspaceBinding) -> bool:
    """Whether stopping keeps the files (blocking): the bound profile's flag while it
    names the template, else every profile on it must agree; none is False."""
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    profiles = [
        p
        for p in getattr(environments._config, "profiles", []) or []
        if getattr(p, "template", None) == binding.template
    ]
    try:
        chosen = environments.choices.get(binding.session_key)
    except SessionEnvironmentUnavailable:
        chosen = None
    matched = [p for p in profiles if p.id == chosen] or profiles
    return bool(matched) and all(getattr(p, "compute_survives_stop", True) is True for p in matched)


async def _binding_survives_stop(environments: Any, session_key: str) -> bool:
    """:func:`compute_survives_stop` for a session's binding; True when it has none."""
    binding = await asyncio.to_thread(environments.registry.get_by_session, session_key)
    if binding is None:
        return True
    return await asyncio.to_thread(compute_survives_stop, environments, binding)


async def _fargate_environments() -> Any:
    """The live Fargate environments. Constructing them makes no AWS call."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    return FargateSessionEnvironments(
        config.session_environments, await asyncio.to_thread(data_home)
    )


#: Bound on the pre-stop unsaved-work check; a slow machine just skips it.
UNSAVED_CHECK_SECS = 20.0
Notify = Callable[[str, str], None]


def schedule_archived(session_keys: Iterable[str], notify: Optional[Notify] = None) -> None:
    """Fire-and-forget from the archive path: the close never waits on Coder."""
    keys = tuple(dict.fromkeys(k for k in session_keys if k))
    if not keys:
        return
    try:
        # Capture now: prepare can run before the task's first turn.
        requests = tuple((key, session_lifecycle(key)) for key in keys)
        task = asyncio.ensure_future(
            _archive(tuple((key, state, state.generation) for key, state in requests), notify)
        )
    except RuntimeError:
        return  # no running loop (a synchronous caller in a test)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def feed_notifier(state: Any) -> Optional[Notify]:
    """A :data:`Notify` that publishes to the dashboard's notification feed."""
    bus = getattr(state, "notification_bus", None)
    if bus is None:
        return None

    def notify(title: str, body: str) -> None:
        from kiro_crew.notifications.bus import NotificationPayload

        bus.push(
            NotificationPayload(
                source="system",
                channel="system.agent",
                kind="agent",
                title=title,
                body=body,
                group_key="session-environment-unsaved",
            )
        )

    return notify


async def warn_unsaved_work(
    session_keys: tuple[str, ...],
    notify: Optional[Notify],
    *,
    survives_stop: bool = True,
    ephemeral: bool = False,
) -> None:
    """Notify about unsaved work in running workspaces, before any stop; bounded, never raises.

    For an ``ephemeral`` (AWS Fargate) workspace the files go with the stop itself.
    """
    if notify is None:
        return
    from kiro_crew.coder.session_browse import unsaved_work
    from kiro_crew.session_environment import session_runs_remotely

    for key in session_keys:
        try:
            if not await asyncio.to_thread(session_runs_remotely, key):
                continue
            summary = await asyncio.wait_for(unsaved_work(key), UNSAVED_CHECK_SECS)
        except Exception:  # noqa: BLE001 -- the archive proceeds regardless
            logger.debug("Unsaved-work check failed for %s", key, exc_info=True)
            continue
        if summary:
            if ephemeral:
                body = (
                    f"Its AWS Fargate task held {summary}, which existed only in that task. "
                    "Archiving stops the task, and its files go with it."
                )
            else:
                if survives_stop:
                    advice = "Reopen the session and commit or push before its retention runs out."
                else:
                    advice = (
                        "Its files are lost when that machine's compute stops (its template's "
                        "own stop deadline or the retention deletion), so reopen the session "
                        "and commit or push now."
                    )
                body = f"Its workspace holds {summary}, which exists only on that machine. {advice}"
            try:
                notify("Archived session has unsaved remote work", body)
            except Exception:  # noqa: BLE001 -- a failed notice never fails the archive
                logger.warning("Could not publish the unsaved-work notice", exc_info=True)


async def _archive(
    requests: tuple[tuple[str, SessionLifecycle, int], ...], notify: Optional[Notify] = None
) -> None:
    try:
        environments = await _environments()
        fargate = await _fargate_environments()
        for key, lifecycle, generation in requests:
            async with lifecycle.lock:
                if generation != lifecycle.generation:
                    continue
                has_task = await _fargate_task_recorded(fargate, key)
                if environments is None and not has_task:
                    continue
                survives = (
                    await _binding_survives_stop(environments, key)
                    if environments is not None
                    else True
                )
                await warn_unsaved_work((key,), notify, survives_stop=survives, ephemeral=has_task)
                # A resume requested during the remote probe supersedes this close.
                if generation != lifecycle.generation:
                    continue
                if has_task:
                    await _archive_fargate(fargate, key)
                if environments is not None:
                    await _archive_session(environments, key)
    except Exception:  # noqa: BLE001 -- never surfaces into a close that already succeeded
        logger.warning("Archived session workspace: could not schedule its release", exc_info=True)


async def _fargate_task_recorded(fargate: Any, session_key: str) -> bool:
    """Whether ``session_key`` has a recorded Fargate task; False when unreadable.

    The Fargate registry fails closed, but that must not stall a Coder archive:
    a task it cannot list gets no keep-alive either, so its bootstrap's idle exit
    still stops it.
    """
    try:
        return bool(await fargate.has_task(session_key))
    except Exception:  # noqa: BLE001 -- one provider's failure never blocks another's
        logger.warning("Archived session: could not read its Fargate task", exc_info=True)
        return False


async def _archive_fargate(fargate: Any, session_key: str) -> None:
    """Stop an archived session's Fargate task now. The caller holds its lifecycle lock.

    A stopped task is gone with its files, so there is no retention window to
    honour; keeping it running would only bill for nothing. Never raises.
    """
    try:
        await fargate.archive_session(session_key)
    except Exception:  # noqa: BLE001 -- the bootstrap's idle exit still stops the task
        logger.warning("Archived session task: could not stop it", exc_info=True)


#: Ceiling on the shutdown stop, so it never holds the exit; the guest's own
#: timeouts (and the Fargate bootstrap's idle exit) stop what it misses.
SHUTDOWN_STOP_SECS = 20.0


async def stop_on_shutdown_enabled() -> bool:
    """``session_environments.stop_on_shutdown`` (default True); True when unreadable."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        config = live.snapshot()
        if config is None:
            config = await asyncio.to_thread(KiroCrewConfig.load)
        return bool(getattr(config.session_environments, "stop_on_shutdown", True))
    except Exception:  # noqa: BLE001 -- an unreadable config keeps the safe default
        return True


async def close_fargate_forwards() -> None:
    """End this gateway's own Fargate SSM forward children; never touches a task.

    Runs on every shutdown, whatever ``stop_on_shutdown`` says: the switch is
    about the tasks, and these are the gateway's processes, which would
    otherwise outlive it holding their loopback ports.
    """
    from kiro_crew.fargate_session.launch import close_forwards

    await close_forwards()


async def stop_for_shutdown() -> dict[str, int]:
    """Stop every owned remote environment, stamping nothing so a reopen resumes;
    stop-destroys-files ones and every Fargate task (its row and secret survive
    for reconnect) count as ``skipped``. Never raises."""
    counts = {"coder": 0, "skipped": 0}

    async def coder() -> None:
        environments = await _environments()
        if environments is None:
            return
        rows = await asyncio.to_thread(environments.registry.list_bindings)
        if not rows:
            return
        client = environments._client()
        for row in rows:
            if row.state == DELETING or row.deployment_id != environments.url:
                continue
            if not await asyncio.to_thread(compute_survives_stop, environments, row):
                counts["skipped"] += 1
                continue
            try:
                ws = await client.get_workspace(row.workspace_name)
                if (
                    ws is not None
                    and ws.status == _RUNNING
                    and (not row.workspace_uuid or ws.uuid == row.workspace_uuid)
                ):
                    await client.stop_workspace(row.workspace_name)
                    counts["coder"] += 1
            except CoderClientError as exc:
                logger.info("Shutdown: Coder workspace stop failed (%s); auto-stop applies", exc)

    async def fargate() -> None:
        environments = await _fargate_environments()
        # The task IS the workspace: shutdown must not destroy its files.
        live_tasks = len(await asyncio.to_thread(environments.registry.all))
        # Read before adding: "+=" would load the count before the await and
        # lose a concurrent provider's increment.
        counts["skipped"] += live_tasks
        # Its SSM forwards are this gateway's own processes, though: end them.
        await close_fargate_forwards()

    async def guarded(name: str, step: Callable[[], Awaitable[None]]) -> None:
        try:
            await step()
        except Exception:  # noqa: BLE001 -- shutdown must not fail on one provider
            logger.warning("Shutdown: could not stop %s session environments", name, exc_info=True)

    await asyncio.gather(guarded("Coder", coder), guarded("Fargate", fargate))
    if counts["skipped"]:
        logger.info(
            "Shutdown: left %d session environment(s) running: stopping their compute "
            "would destroy their files",
            counts["skipped"],
        )
    if counts["coder"]:
        logger.info("Shutdown: stopped session environments %s", counts)
    return counts


async def archive_session(environments: Any, session_key: str) -> Optional[WorkspaceBinding]:
    """Stamp ``session_key``'s workspace for deletion and stop it. None if it has none."""
    lifecycle = session_lifecycle(session_key)
    async with lifecycle.lock:
        return await _archive_session(environments, session_key)


async def _archive_session(environments: Any, session_key: str) -> Optional[WorkspaceBinding]:
    from kiro_crew.config.session_environments import ARCHIVED_RETENTION_DAYS

    existing = await asyncio.to_thread(environments.registry.get_by_session, session_key)
    if existing is None or existing.deployment_id != environments.url:
        return None

    due = _now() + timedelta(days=ARCHIVED_RETENTION_DAYS)
    binding = await asyncio.to_thread(
        environments.registry.schedule_deletion, session_key, _stamp(due)
    )
    if binding is None or binding.deployment_id != environments.url:
        return None
    if not await asyncio.to_thread(compute_survives_stop, environments, binding):
        # A stop would destroy the files retention exists to keep.
        logger.info("Archived session workspace: not stopped; its stop destroys its files")
        return binding
    try:
        workspace = await environments._client().get_workspace(binding.workspace_name)
        if (
            workspace is not None
            and workspace.status == _RUNNING
            and (not binding.workspace_uuid or workspace.uuid == binding.workspace_uuid)
        ):
            await environments._client().stop_workspace(binding.workspace_name)
    except CoderClientError as exc:
        # Auto-stop still applies; the stamp stands.
        logger.info("Archived session workspace: stop failed (%s); auto-stop applies", exc)
    return binding


async def sweep_due(environments: Any, *, now: Optional[datetime] = None) -> int:
    """Delete every workspace whose retention ran out. Returns how many went."""
    moment = now or _now()
    rows = await asyncio.to_thread(environments.registry.due_for_deletion, moment)
    rows = [row for row in rows if row.deployment_id == environments.url]
    if not rows:
        return 0
    client = environments._client()
    owner, owner_id = await client.current_user()
    deleted = 0
    for row in rows:
        claimed = await asyncio.to_thread(
            environments.registry.begin_deletion, row.binding_id, moment
        )
        if claimed is None or claimed.deployment_id != environments.url:
            continue  # reopened in the meantime
        try:
            workspace = await client.get_workspace(claimed.workspace_name)
        except CoderClientError as exc:
            logger.warning("Archived session workspace: lookup failed (%s); retrying later", exc)
            continue
        if workspace is not None:
            if (
                workspace.owner not in (owner, owner_id)
                or workspace.template != claimed.template
                or (claimed.workspace_uuid and workspace.uuid != claimed.workspace_uuid)
            ):
                # Not the workspace this row minted: forget the row, never act on it.
                logger.warning("Archived session workspace changed identity; forgetting it")
            else:
                try:
                    await client.delete_workspace(claimed.workspace_name)
                except CoderClientError as exc:
                    # Stays ``deleting``: retried next pass, reopen refused.
                    logger.warning("Archived session workspace: delete failed (%s)", exc)
                    continue
        await asyncio.to_thread(environments.registry.remove_deleted, claimed.binding_id)
        deleted += 1
    return deleted


async def keep_alive_once() -> int:
    """Touch every live Fargate task once; a failure is logged, never raised."""
    try:
        fargate = await _fargate_environments()
        return int(await fargate.keep_alive())
    except Exception:  # noqa: BLE001 -- the heartbeat outlives any one pass
        logger.warning("Retention sweep: Fargate keep-alive failed", exc_info=True)
        return 0


async def sweep_once(*, keep_alive: bool = True) -> int:
    """Keep live Fargate tasks alive, retry their stops and sweep expired workspaces.

    A Fargate failure (its registry fails closed) never stops the Coder sweep.
    ``keep_alive=False`` leaves the heartbeat to :func:`run_sweeper`'s own loop.
    """
    if keep_alive:
        await keep_alive_once()
    stopped = 0
    try:
        fargate = await _fargate_environments()
        stopped = await fargate.retry_stops()
    except Exception:  # noqa: BLE001 -- one provider's failure never blocks another's
        logger.warning("Retention sweep: Fargate session tasks failed", exc_info=True)
    environments = await _environments()
    return stopped + (await sweep_due(environments) if environments is not None else 0)


async def _keep_alive_loop(interval: float) -> None:
    """The Fargate heartbeat, on its own clock.

    Slow stop retries or a slow Coder sweep must never delay it: a live task
    that goes 900 s without a touch idle-exits and deletes its files.
    """
    while True:
        await keep_alive_once()
        await asyncio.sleep(interval)


async def run_sweeper(
    *,
    interval: float = SWEEP_INTERVAL_SECS,
    initial_delay: float = SWEEP_INITIAL_DELAY_SECS,
) -> None:
    """Run retention sweeps until cancelled. Failures are logged; the loop never dies."""
    await asyncio.sleep(initial_delay)
    heartbeat = asyncio.create_task(_keep_alive_loop(interval))
    try:
        await _sweep_forever(interval)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _sweep_forever(interval: float) -> None:
    last_error = ""
    while True:
        try:
            deleted = await sweep_once(keep_alive=False)
            if deleted:
                logger.info("Archived session workspaces deleted: %d", deleted)
            last_error = ""
        except asyncio.CancelledError:
            raise
        except (CoderClientError, WorkspaceRegistryCorrupt) as exc:
            # The same failure every minute is one warning, not sixty.
            message = str(exc)
            if message != last_error:
                logger.warning("Archived session sweep failed: %s", message)
            last_error = message
        except Exception:  # noqa: BLE001 -- the loop outlives any one pass
            logger.warning("Archived session sweep failed", exc_info=True)
        await asyncio.sleep(interval)


def resume_check(registry: Any, session_key: str) -> None:
    """Cancel a pending deletion for a reopened session, or refuse mid-delete."""
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    binding = registry.get_by_session(session_key)
    if binding is None:
        return
    if binding.state == DELETING or (
        binding.deletion_due_at and registry.cancel_deletion(binding.binding_id) is None
    ):
        raise SessionEnvironmentUnavailable(
            "this archived session's workspace is being deleted; try again in a minute "
            "and it will start on a fresh one"
        )
