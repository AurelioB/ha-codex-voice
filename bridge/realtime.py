"""Thread-scoped app-server realtime session orchestration."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Mapping
from time import monotonic
from typing import Any, Protocol

from .errors import AppServerExited, ProtocolError
from .webrtc import WebRtcPeer

_EVENT_BACKLOG_LIMIT = 64


class PeerLike(Protocol):
    async def create_offer(self) -> str: ...

    async def set_answer(self, sdp: str) -> None: ...

    async def wait_connected(self, timeout: float | None = None) -> None: ...

    def feed_audio(self, pcm: bytes) -> None: ...

    async def wait_input_drained(self, timeout: float | None = None) -> None: ...

    async def recv_audio(self, timeout: float | None = None) -> bytes: ...

    async def recv_data_event(self, timeout: float | None = None) -> str | bytes: ...

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
        deadline = monotonic() + self.timeout
        offer = await self.peer.create_offer()
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
        await self.rpc.call("thread/realtime/start", params, timeout=remaining)

        answer: str | None = None
        started = False
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
                self._backlog.append(event)
            elif method == "thread/realtime/sdp":
                candidate = event_params.get("sdp")
                if not isinstance(candidate, str) or not candidate:
                    raise ProtocolError("app-server returned an invalid SDP answer")
                answer = candidate
            else:
                self._backlog.append(event)
        await self.peer.set_answer(answer)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("realtime handshake timed out")
        await self.peer.wait_connected(timeout=min(remaining, 15))
        self._started = True

    def feed_audio(self, pcm: bytes) -> None:
        if not self._started:
            raise ProtocolError("realtime session has not started")
        self.peer.feed_audio(pcm)

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

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            with contextlib.suppress(Exception):
                await self.rpc.call(
                    "thread/realtime/stop",
                    {"threadId": self.thread_id},
                    timeout=min(self.timeout, 10),
                )
        await self.peer.close()
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
