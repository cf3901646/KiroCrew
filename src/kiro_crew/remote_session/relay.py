"""Gateway-side relay: the sandboxed bridge cannot use ``coder login``, so the
gateway dials. :class:`WorkspaceRelay` starts a guest worker per
token-authenticated loopback connection and pipes its :class:`RemoteLink` to
the bridge, with bounded connections and an idle close.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from kiro_crew.remote_session import wire

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_MAX_LIVE_RELAYS = 64
_TOKEN_LINE_MAX = 256
_TOKEN_READ_TIMEOUT = 10.0
#: Strong references: asyncio keeps only weak ones to server tasks.
_LIVE: "set[Any]" = set()


class _Relay:
    def __init__(self, *, name: str, idle_timeout: float) -> None:
        self.name = name
        self.idle_timeout = idle_timeout
        self.port = 0
        self._server: Optional[asyncio.base_events.Server] = None
        self._active = 0
        self._last_activity = time.monotonic()
        self._idle_task: Optional[asyncio.Task] = None
        #: Awaited once when the relay closes: what lives as long as it does.
        self.on_close: Optional[Callable[[], Any]] = None

    async def start(self) -> int:
        if len(_LIVE) >= _MAX_LIVE_RELAYS:
            raise RuntimeError("too many remote workspace relays are open")
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        _LIVE.add(self)
        self._idle_task = asyncio.ensure_future(self._reap_when_idle())
        return self.port

    async def close(self) -> None:
        _LIVE.discard(self)
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        self._closed()
        on_close, self.on_close = self.on_close, None
        if on_close is not None:
            with contextlib.suppress(Exception):
                await on_close()

    def _closed(self) -> None:
        """Subclass hook: release what the relay owns."""

    async def _reap_when_idle(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, self.idle_timeout))
            if not self._active and time.monotonic() - self._last_activity >= self.idle_timeout:
                await self.close()
                return

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raise NotImplementedError


class WorkspaceRelay(_Relay):
    """Per-connection fresh guest worker + gateway-side link, behind a token."""

    def __init__(
        self,
        launch: Any,
        remote_argv: Sequence[str],
        token_path: Path,
        token: bytes,
        *,
        link_factory: Optional[Callable[..., Any]] = None,
        name: str = "",
        idle_timeout: float = 900.0,
        max_connections: int = 4,
        prestart: bool = True,
        spare_max_age: float = wire.SPARE_MAX_AGE_SECONDS,
    ) -> None:
        super().__init__(name=name, idle_timeout=idle_timeout)
        self.launch = launch
        self.remote_argv = list(remote_argv)
        self.token_path = Path(token_path)
        self._token = token
        self._link_factory = link_factory
        self.max_connections = max_connections
        self.prestart = prestart
        # A pre-started spare takes ~5 s of worker start off the launch path.
        self._spare: Optional[asyncio.Task] = None
        self._spare_started = 0.0
        self.spare_max_age = spare_max_age

    async def start(self) -> int:
        port = await super().start()
        self._refill()
        return port

    def _closed(self) -> None:
        with contextlib.suppress(OSError):
            self.token_path.unlink()

    async def close(self) -> None:
        # Stop the spare here: a detached stop can be cancelled at loop shutdown.
        spare, self._spare = self._spare, None
        await super().close()
        if spare is not None:
            await self._discard(spare)

    def _refill(self) -> None:
        if not self.prestart or self._server is None or self._spare is not None:
            return
        self._spare_started = time.monotonic()
        self._spare = asyncio.ensure_future(self._start_worker())

    async def _start_worker(self) -> tuple[str, bytes, int]:
        from kiro_crew.remote_session import wire

        worker_id = self.launch.new_worker_id()
        secret = wire.new_secret()
        port = await self.launch.start_worker(worker_id, self.remote_argv, secret)
        return worker_id, secret, port

    async def _discard(self, spare: asyncio.Task) -> None:
        """Stop a spare nobody will use (relay closing, or it went stale)."""
        # Never cancel a start in flight: the setsid'd guest worker would leak.
        try:
            worker_id, _secret, _port = await spare
        except BaseException:
            return
        with contextlib.suppress(Exception):
            await self.launch.stop_worker(worker_id)

    async def _take_worker(self) -> tuple[str, bytes, int]:
        """The spare if it is usable, else a worker started now; then refill."""
        spare, self._spare = self._spare, None
        worker = None
        if spare is not None:
            if time.monotonic() - self._spare_started > self.spare_max_age:
                # It may have hit the guest's orphan timeout: start fresh.
                await self._discard(spare)
            else:
                try:
                    worker = await spare
                except Exception as exc:
                    logger.warning("remote workspace %s: spare worker failed: %s", self.name, exc)
        if worker is None:
            worker = await self._start_worker()
        self._refill()
        return worker

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._active >= self.max_connections:
            writer.close()
            return
        self._active += 1
        worker_id = ""
        link = None
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), _TOKEN_READ_TIMEOUT)
            except (asyncio.TimeoutError, ValueError):
                return
            presented = line.strip()[:_TOKEN_LINE_MAX]
            if not hmac.compare_digest(presented, self._token):
                logger.warning("remote workspace %s: refused a relay connection", self.name)
                return
            link, worker_id = await self._open_link()
            await self._pump(reader, writer, link)
        except Exception as exc:  # the bridge sees the stream end; record why
            logger.warning("remote workspace %s: connection failed: %s", self.name, exc)
        finally:
            writer.close()
            if link is not None:
                with contextlib.suppress(Exception):
                    await link.aclose()
            if worker_id:
                with contextlib.suppress(Exception):
                    await self.launch.stop_worker(worker_id)
            self._active -= 1
            self._last_activity = time.monotonic()

    async def _open_link(self) -> tuple[Any, str]:
        from kiro_crew.remote_session.client import command_dial
        from kiro_crew.remote_session.link import RemoteLink

        worker_id, secret, port = await self._take_worker()
        own_dial = getattr(self.launch, "dial", None)
        dial = (
            own_dial(port)
            if callable(own_dial)
            else command_dial(self.launch.dial_argv(port), self.launch.dial_env())
        )
        factory = self._link_factory or RemoteLink
        link = factory(worker_id, secret, dial, **self.launch.link_kwargs())
        link.start()
        return link, worker_id

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, link: Any
    ) -> None:
        from kiro_crew.remote_session.link import LinkUnavailable

        async def up() -> None:
            while chunk := await reader.read(_CHUNK):
                await link.write(chunk)
                self._last_activity = time.monotonic()
            link.close_input()

        async def down() -> None:
            while True:
                try:
                    chunk = await link.stdout.read(_CHUNK)
                except LinkUnavailable:
                    return
                if not chunk:
                    return
                writer.write(chunk)
                await writer.drain()
                self._last_activity = time.monotonic()

        tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
        try:
            # Down ends the connection (the guest finished or the link was lost);
            # up ending only closes the guest's stdin, whose output still drains.
            await tasks[1]
        finally:
            tasks[0].cancel()
        if getattr(link, "state", "") not in ("finished", "closed"):
            logger.warning(
                "remote workspace %s: link ended %s: %s",
                self.name,
                getattr(link, "state", "?"),
                getattr(link, "error", ""),
            )


def write_token_file(directory: Path, name: str) -> tuple[Path, bytes]:
    """A fresh relay token in a private file the sandboxed bridge can read."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = os.urandom(24).hex().encode("ascii")
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, token)
    finally:
        os.close(fd)
    return path, token
