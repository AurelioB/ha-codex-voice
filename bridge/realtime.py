"""Thread-scoped app-server realtime session orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Mapping
from time import monotonic
from typing import Any, Protocol

from .errors import AppServerExited, ProtocolError
from .webrtc import WebRtcPeer

_EVENT_BACKLOG_LIMIT = 64
_STOP_TIMEOUT_SECONDS = 5.0
LOGGER = logging.getLogger(__name__)


class PeerLike(Protocol):
    async def create_offer(self) -> str: ...

    async def set_answer(self, sdp: str) -> None: ...

    async def wait_connected(self, timeout: float | None = None) -> None: ...

    def set_input_buffer_limit(self, maximum_milliseconds: int) -> None: ...

    def feed_audio(self, pcm: bytes) -> None: ...

    async def wait_input_drained(self, timeout: float | None = None) -> None: ...

    def discard_pending_input(self) -> None: ...

    def drain_audio_nowait(self) -> list[bytes]: ...

    def drain_data_events_nowait(self) -> list[str | bytes]: ...

    async def recv_audio(self, timeout: float | None = None) -> bytes: ...

    async def recv_data_event(self, timeout: float | None = None) -> str | bytes: ...

    def send_data_event(self, value: str | bytes) -> None: ...

    async def close(self) -> None: ...


class RealtimeSession:
    """Bind one Codex thread, one app-server subscription, and one WebRTC peer."""

    def __init__(
        self,
        rpc: Any,
        thread_id: str,
        *,
        peer: PeerLike | None = None,
        version: str = "v3",
        timeout: float = 90.0,
    ) -> None:
        if version not in {"v1", "v3"}:
            raise ProtocolError("WebRTC realtime version must be v1 or v3")
        self.rpc = rpc
        self.thread_id = thread_id
        self.peer = peer or WebRtcPeer()
        self.version = version
        self.timeout = timeout
        self.subscription = rpc.subscribe()
        self.realtime_session_id: str | None = None
        self._backlog: deque[dict[str, Any]] = deque(maxlen=_EVENT_BACKLOG_LIMIT)
        self._started = False
        self._closed = False
        self._stop_waiter: asyncio.Future[None] | None = None

    async def start(
        self,
        *,
        prompt: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        include_startup_context: bool = False,
        client_managed_handoffs: bool = True,
        initial_items: list[dict[str, str]] | None = None,
    ) -> None:
        handshake_started = monotonic()
        deadline = monotonic() + self.timeout
        offer_started = monotonic()
        offer = await self.peer.create_offer()
        offer_ice_seconds = monotonic() - offer_started
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "outputModality": "audio",
            "includeStartupContext": include_startup_context,
            "clientManagedHandoffs": client_managed_handoffs,
            "transport": {"type": "webrtc", "sdp": offer},
            "version": self.version,
        }
        if prompt is not None:
            params["prompt"] = prompt
        if model:
            params["model"] = model
        if voice:
            params["voice"] = voice
        if initial_items:
            if self.version != "v3":
                raise ProtocolError("initial realtime items require version v3")
            params["initialItems"] = initial_items
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("realtime handshake timed out")
        realtime_start_started = monotonic()
        await self.rpc.call("thread/realtime/start", params, timeout=remaining)
        realtime_start_rpc_seconds = monotonic() - realtime_start_started

        answer: str | None = None
        started = False
        realtime_start_to_started_seconds = 0.0
        realtime_start_to_sdp_seconds = 0.0
        while answer is None or not started:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("realtime handshake timed out")
            event = await self.subscription.get(timeout=remaining)
            self._raise_if_app_server_exited(event)
            if not self._belongs_to_thread(event):
                continue
            method = event.get("method")
            event_params = event.get("params", {})
            if method == "thread/realtime/error":
                raise ProtocolError(
                    str(event_params.get("message", "realtime session failed"))
                )
            if method == "thread/realtime/started":
                session_id = event_params.get("realtimeSessionId")
                self.realtime_session_id = (
                    session_id if isinstance(session_id, str) else None
                )
                started = True
                realtime_start_to_started_seconds = monotonic() - realtime_start_started
                self._backlog.append(event)
            elif method == "thread/realtime/sdp":
                candidate = event_params.get("sdp")
                if not isinstance(candidate, str) or not candidate:
                    raise ProtocolError("app-server returned an invalid SDP answer")
                answer = candidate
                realtime_start_to_sdp_seconds = monotonic() - realtime_start_started
            else:
                self._backlog.append(event)
        set_answer_started = monotonic()
        await self.peer.set_answer(answer)
        set_answer_seconds = monotonic() - set_answer_started
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("realtime handshake timed out")
        connect_started = monotonic()
        await self.peer.wait_connected(timeout=min(remaining, 15))
        connect_seconds = monotonic() - connect_started
        self._started = True
        LOGGER.info(
            "Realtime handshake timing: offer_ice_seconds=%.3f "
            "realtime_start_rpc_seconds=%.3f "
            "realtime_start_to_started_seconds=%.3f "
            "realtime_start_to_sdp_seconds=%.3f set_answer_seconds=%.3f "
            "connect_seconds=%.3f total_seconds=%.3f",
            offer_ice_seconds,
            realtime_start_rpc_seconds,
            realtime_start_to_started_seconds,
            realtime_start_to_sdp_seconds,
            set_answer_seconds,
            connect_seconds,
            monotonic() - handshake_started,
        )

    def feed_audio(self, pcm: bytes) -> None:
        if not self._started:
            raise ProtocolError("realtime session has not started")
        self.peer.feed_audio(pcm)

    def set_input_buffer_limit(self, maximum_milliseconds: int) -> None:
        """Set a tighter input bound before starting a live audio session."""
        if self._started:
            raise ProtocolError("realtime input bound must be set before start")
        self.peer.set_input_buffer_limit(maximum_milliseconds)

    async def wait_input_drained(
        self,
        timeout: float | None = None,
        *,
        monitor_app_server_exit: bool = True,
    ) -> None:
        if not monitor_app_server_exit:
            await self.peer.wait_input_drained(timeout)
            return
        drain_task = asyncio.create_task(self.peer.wait_input_drained(timeout))
        exit_task = asyncio.create_task(self._watch_for_app_server_exit())
        try:
            done, _ = await asyncio.wait(
                {drain_task, exit_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if exit_task in done:
                await exit_task
            await drain_task
        finally:
            for task in (drain_task, exit_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(drain_task, exit_task, return_exceptions=True)

    async def append_text(self, text: str, role: str = "user") -> None:
        if role not in {"user", "developer", "assistant"}:
            raise ProtocolError(
                "realtime text role must be user, developer, or assistant"
            )
        await self.rpc.call(
            "thread/realtime/appendText",
            {"threadId": self.thread_id, "text": text, "role": role},
            timeout=self.timeout,
        )

    async def append_speech(self, text: str) -> None:
        await self.rpc.call(
            "thread/realtime/appendSpeech",
            {"threadId": self.thread_id, "text": text},
            timeout=self.timeout,
        )

    async def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        while True:
            event = (
                self._backlog.popleft()
                if self._backlog
                else await self.subscription.get(timeout)
            )
            self._raise_if_app_server_exited(event)
            if self._belongs_to_thread(event):
                return event

    async def recv_audio(self, timeout: float | None = None) -> bytes:
        return await self.peer.recv_audio(timeout)

    async def recv_data_event(self, timeout: float | None = None) -> str | bytes:
        return await self.peer.recv_data_event(timeout)

    def request_response_cancel(self) -> None:
        """Request provider response cancellation without claiming it succeeded."""
        if not self._started:
            raise ProtocolError("realtime session has not started")
        self.peer.send_data_event(
            json.dumps({"type": "response.cancel"}, separators=(",", ":"))
        )

    def discard_pending_input(self) -> None:
        """Drop finite STT PCM that has not yet left the paced input track."""
        self.peer.discard_pending_input()

    def drain_audio_nowait(self) -> list[bytes]:
        """Drain already-buffered remote audio without yielding."""
        return self.peer.drain_audio_nowait()

    def drain_data_events_nowait(self) -> list[str | bytes]:
        """Drain already-buffered data-channel events without yielding."""
        return self.peer.drain_data_events_nowait()

    def drain_app_events_nowait(self) -> list[dict[str, Any]]:
        """Drain thread events buffered before a handoff boundary."""
        events = list(self._backlog)
        self._backlog.clear()
        while True:
            try:
                event = self.subscription.get_nowait()
            except asyncio.QueueEmpty:
                return events
            if event.get(
                "method"
            ) == "bridge/appServerExited" or self._belongs_to_thread(event):
                events.append(event)

    async def stop(self) -> None:
        if self._stop_waiter is not None:
            await asyncio.shield(self._stop_waiter)
            return
        self._closed = True
        self._stop_waiter = asyncio.get_running_loop().create_future()
        try:
            await self._stop_once()
        finally:
            if not self._stop_waiter.done():
                self._stop_waiter.set_result(None)

    async def _stop_once(self) -> None:
        cleanup_timeout = max(0.0, min(self.timeout, _STOP_TIMEOUT_SECONDS))
        remote_stop_task: asyncio.Task[None] | None = None
        peer_close_finished = False

        async def stop_remote() -> None:
            try:
                await self.rpc.call(
                    "thread/realtime/stop",
                    {"threadId": self.thread_id},
                    timeout=cleanup_timeout,
                )
            except Exception as err:  # noqa: BLE001 - cleanup is best effort
                LOGGER.warning(
                    "Realtime cleanup step failed (app-server realtime stop): %s",
                    err,
                )

        if self._started:
            remote_stop_task = asyncio.create_task(
                stop_remote(), name=f"codex-realtime-rpc-stop-{self.thread_id}"
            )

        try:
            async with asyncio.timeout(cleanup_timeout):
                if remote_stop_task is not None:
                    # Let a healthy local RPC finish before close exposes the peer as
                    # closed. If it blocks, peer teardown still proceeds concurrently.
                    await asyncio.sleep(0)
                try:
                    await self.peer.close()
                except Exception as err:  # noqa: BLE001 - cleanup is best effort
                    LOGGER.warning(
                        "Realtime cleanup step failed (WebRTC peer close): %s", err
                    )
                peer_close_finished = True
                if remote_stop_task is not None:
                    await remote_stop_task
        except TimeoutError:
            pending_names = []
            if not peer_close_finished:
                pending_names.append("WebRTC peer close")
            if remote_stop_task is not None and not remote_stop_task.done():
                pending_names.append("app-server realtime stop")
            if pending_names:
                LOGGER.warning(
                    "Realtime cleanup timed out after %.1f seconds; cancelling %s",
                    cleanup_timeout,
                    ", ".join(pending_names),
                )
        finally:
            if remote_stop_task is not None and not remote_stop_task.done():
                remote_stop_task.cancel()
            try:
                if remote_stop_task is not None:
                    await asyncio.gather(remote_stop_task, return_exceptions=True)
            finally:
                self.subscription.close()

    def _belongs_to_thread(self, event: Mapping[str, Any]) -> bool:
        params = event.get("params")
        if not isinstance(params, Mapping):
            return False
        return params.get("threadId") == self.thread_id

    async def _watch_for_app_server_exit(self) -> None:
        while True:
            event = await self.subscription.get()
            self._raise_if_app_server_exited(event)
            if self._belongs_to_thread(event):
                self._backlog.append(event)

    @staticmethod
    def _raise_if_app_server_exited(event: Mapping[str, Any]) -> None:
        if event.get("method") != "bridge/appServerExited":
            return
        params = event.get("params")
        returncode = params.get("returncode") if isinstance(params, Mapping) else None
        raise AppServerExited(f"codex app-server exited with status {returncode}")
