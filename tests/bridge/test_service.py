from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import wave
from array import array
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, WSServerHandshakeError, web

from bridge import service as bridge_service
from bridge.config import BridgeConfig
from bridge.errors import AppServerExited, BridgeBusyError, ProtocolError
from bridge.runtime import IsolatedCodexRuntime
from bridge.service import BridgeState, _codex_child_environment, create_app

AUTH = {"Authorization": "Bearer test-token"}


def _transcription_payload() -> dict[str, Any]:
    return {
        "audio": base64.b64encode(b"\x01\x00" * 160).decode(),
        "format": "pcm",
        "sample_rate": 24_000,
        "channels": 1,
    }


def _transcription_stream_start(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "start",
        "protocol_version": 1,
        "format": "pcm",
        "codec": "pcm",
        "sample_rate": 16_000,
        "bit_rate": 16,
        "channels": 1,
    }
    payload.update(overrides)
    return payload


async def _request_speech_session_handoff(
    client: Any, *, voice: str = "cove", language: str = "en-US"
) -> dict[str, Any]:
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    start = _transcription_stream_start(
        language=language,
        speech_session_handoff={
            "version": 1,
            "voice": voice,
            "language": language,
        },
    )
    await websocket.send_json(start)
    assert await websocket.receive_json() == {
        "type": "started",
        "protocol_version": 1,
    }
    await websocket.send_bytes(b"\x00\x20" * 160)
    await websocket.send_json({"type": "end"})
    result = await websocket.receive_json(timeout=1)
    close = await websocket.receive(timeout=1)
    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    return result


def _synthesis_payload() -> dict[str, Any]:
    return {
        "text": "Welcome home",
        "voice": "cove",
        "language": "en-US",
        "format": "wav",
    }


async def _assert_busy(response: Any) -> None:
    assert response.status == 409
    assert await response.json() == {
        "error": "another speech session is already active",
        "code": "busy",
    }


class FakeSubscription:
    def __init__(self, rpc: FakeRpc) -> None:
        self.rpc = rpc
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self.queue.get()
        return await asyncio.wait_for(self.queue.get(), timeout)

    def get_nowait(self) -> dict[str, Any]:
        return self.queue.get_nowait()

    def close(self) -> None:
        self.rpc.subscriptions.discard(self)


class FakePeer:
    def __init__(self, rpc: FakeRpc) -> None:
        self.rpc = rpc
        self.thread_id: str | None = None
        self.answer: str | None = None
        self.fed = bytearray()
        self.audio: asyncio.Queue[bytes] = asyncio.Queue()
        self.data: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.closed = False
        self.pending_input_discarded = False
        self.tasks: set[asyncio.Task[None]] = set()

    async def create_offer(self) -> str:
        return "v=0\r\nfake-offer\r\n"

    async def set_answer(self, sdp: str) -> None:
        self.answer = sdp

    async def wait_connected(self, timeout: float | None = None) -> None:
        return None

    def feed_audio(self, pcm: bytes) -> None:
        self.fed.extend(pcm)
        if self.thread_id is not None:
            task = asyncio.create_task(self._broadcast_transcript())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _broadcast_transcript(self) -> None:
        self.rpc.transcript_started.set()
        if self.rpc.transcript_gate is not None:
            await self.rpc.transcript_gate.wait()
        await self.rpc.broadcast(
            {
                "method": "thread/realtime/transcript/done",
                "params": {
                    "threadId": self.thread_id,
                    "role": "user",
                    "text": "Turn on the kitchen",
                },
            }
        )

    async def wait_input_drained(self, timeout: float | None = None) -> None:
        self.rpc.input_drain_started.set()
        if self.rpc.input_drain_gate is not None:
            await self.rpc.input_drain_gate.wait()

    def discard_pending_input(self) -> None:
        self.pending_input_discarded = True

    def drain_audio_nowait(self) -> list[bytes]:
        chunks: list[bytes] = []
        while True:
            try:
                chunks.append(self.audio.get_nowait())
            except asyncio.QueueEmpty:
                return chunks

    def drain_data_events_nowait(self) -> list[str | bytes]:
        events: list[str | bytes] = []
        while True:
            try:
                events.append(self.data.get_nowait())
            except asyncio.QueueEmpty:
                return events

    async def recv_audio(self, timeout: float | None = None) -> bytes:
        if timeout is None:
            return await self.audio.get()
        return await asyncio.wait_for(self.audio.get(), timeout)

    async def recv_data_event(self, timeout: float | None = None) -> str | bytes:
        if timeout is None:
            return await self.data.get()
        return await asyncio.wait_for(self.data.get(), timeout)

    async def close(self) -> None:
        self.closed = True
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class FakeCollectorSession:
    """Queue-backed realtime surface for synthesis collector regressions."""

    def __init__(self) -> None:
        self.audio: asyncio.Queue[bytes] = asyncio.Queue()
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.data: asyncio.Queue[str | bytes] = asyncio.Queue()

    async def recv_audio(self) -> bytes:
        return await self.audio.get()

    async def next_event(self) -> dict[str, Any]:
        return await self.events.get()

    async def recv_data_event(self) -> str | bytes:
        return await self.data.get()


class FakeRpc:
    def __init__(self) -> None:
        self.running = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[int | str, dict[str, Any]]] = []
        self.subscriptions: set[FakeSubscription] = set()
        self.thread_count = 0
        self.turn_count = 0
        self.peers: list[FakePeer] = []
        self.emit_tool_once = True
        self.emit_turn_completion = True
        self.tool_result_received = asyncio.Event()
        self.active_profile_id = "ha-voice-minimal"
        self.permission_profile_allowed = True
        self.turn_start_error: Exception | None = None
        self.turn_start_gate: asyncio.Event | None = None
        self.turn_interrupt_error: Exception | None = None
        self.thread_delete_error: Exception | None = None
        self.turn_gate: asyncio.Event | None = None
        self.input_drain_gate: asyncio.Event | None = None
        self.input_drain_started = asyncio.Event()
        self.transcript_gate: asyncio.Event | None = None
        self.transcript_started = asyncio.Event()
        self.realtime_start_gate: asyncio.Event | None = None
        self.realtime_start_started = asyncio.Event()
        self.realtime_stop_gate: asyncio.Event | None = None
        self.realtime_stop_started = asyncio.Event()
        self.synthesis_append_gate: asyncio.Event | None = None
        self.synthesis_append_started = asyncio.Event()
        self.handoff_append_error: Exception | None = None
        self.tasks: set[asyncio.Task[None]] = set()

    def peer_factory(self) -> FakePeer:
        peer = FakePeer(self)
        self.peers.append(peer)
        return peer

    async def start(self) -> None:
        self.running = True

    async def close(self) -> None:
        self.running = False
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def health(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "initialized": self.running,
            "pid": 123,
            "auth_mode": "chatgpt",
            "plan_type": "plus",
            "stderr": ["must never be public"],
        }

    def subscribe(self, **_: Any) -> FakeSubscription:
        subscription = FakeSubscription(self)
        self.subscriptions.add(subscription)
        return subscription

    async def broadcast(self, event: dict[str, Any]) -> None:
        for subscription in tuple(self.subscriptions):
            await subscription.queue.put(event)

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del timeout
        values = dict(params or {})
        self.calls.append((method, values))
        if method == "thread/delete" and self.thread_delete_error is not None:
            raise self.thread_delete_error
        if method == "permissionProfile/list":
            return {
                "data": [
                    {
                        "id": "ha-voice-minimal",
                        "description": "Test least-privilege profile",
                        "allowed": self.permission_profile_allowed,
                    }
                ]
            }
        if method == "thread/start":
            self.thread_count += 1
            return {
                "activePermissionProfile": {
                    "id": self.active_profile_id,
                    "extends": ":read-only",
                },
                "runtimeWorkspaceRoots": [],
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "thread": {"id": f"thread-{self.thread_count}"},
            }
        if method == "turn/start":
            if self.turn_start_gate is not None:
                await self.turn_start_gate.wait()
            if self.turn_start_error is not None:
                raise self.turn_start_error
            self.turn_count += 1
            turn_id = f"turn-{self.turn_count}"
            turn_text = str(values["input"][0]["text"])
            task = asyncio.create_task(
                self._emit_turn(values["threadId"], turn_id, turn_text)
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return {"turn": {"id": turn_id, "status": "inProgress"}}
        if method == "turn/interrupt" and self.turn_interrupt_error is not None:
            raise self.turn_interrupt_error
        if method == "thread/realtime/start":
            self.realtime_start_started.set()
            if self.realtime_start_gate is not None:
                await self.realtime_start_gate.wait()
            thread_id = values["threadId"]
            peer = self.peers[-1]
            peer.thread_id = thread_id
            await self.broadcast(
                {
                    "method": "thread/realtime/started",
                    "params": {
                        "threadId": thread_id,
                        "realtimeSessionId": "realtime-1",
                        "version": values["version"],
                    },
                }
            )
            await self.broadcast(
                {
                    "method": "thread/realtime/sdp",
                    "params": {"threadId": thread_id, "sdp": "v=0\r\nfake-answer\r\n"},
                }
            )
            return {}
        if method == "thread/realtime/stop":
            self.realtime_stop_started.set()
            if self.realtime_stop_gate is not None:
                await self.realtime_stop_gate.wait()
            return {}
        is_synthesis_append = method == "thread/realtime/appendText" and str(
            values.get("text", "")
        ).startswith("Vocalize only")
        if method == "thread/realtime/appendSpeech" or is_synthesis_append:
            if method == "thread/realtime/appendSpeech":
                handoff_error = self.handoff_append_error
                self.handoff_append_error = None
                if handoff_error is not None:
                    raise handoff_error
            if method == "thread/realtime/appendSpeech" or is_synthesis_append:
                self.synthesis_append_started.set()
                if self.synthesis_append_gate is not None:
                    await self.synthesis_append_gate.wait()
            peer = self.peers[-1]
            if method == "thread/realtime/appendSpeech":
                task = asyncio.create_task(
                    self._emit_synthesis_result(peer, values["threadId"])
                )
                peer.tasks.add(task)
                task.add_done_callback(peer.tasks.discard)
            else:
                await self._emit_synthesis_result(peer, values["threadId"])
            return {}
        return {}

    async def _emit_synthesis_result(self, peer: FakePeer, thread_id: str) -> None:
        await asyncio.sleep(0)
        if peer.closed:
            return
        peer.audio.put_nowait(b"\x01\x00" * 480)
        peer.data.put_nowait(json.dumps({"type": "turn.done"}))
        await self.broadcast(
            {
                "method": "thread/realtime/transcript/done",
                "params": {
                    "threadId": thread_id,
                    "role": "assistant",
                    "text": "Conversational rendering",
                },
            }
        )

    async def respond_result(
        self, request_id: int | str, result: Mapping[str, Any]
    ) -> None:
        self.responses.append((request_id, dict(result)))
        self.tool_result_received.set()

    async def _emit_turn(self, thread_id: str, turn_id: str, text: str) -> None:
        await asyncio.sleep(0)
        if self.turn_gate is not None:
            await self.turn_gate.wait()
        await self.broadcast(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "delta": f"Done:{text}",
                },
            }
        )
        if self.emit_tool_once:
            self.emit_tool_once = False
            await self.broadcast(
                {
                    "id": "rpc-call-1",
                    "method": "item/tool/call",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "callId": "call-1",
                        "namespace": None,
                        "tool": "HassTurnOn",
                        "arguments": {"name": "Kitchen"},
                    },
                }
            )
            await self.tool_result_received.wait()
        if not self.emit_turn_completion:
            return
        await self.broadcast(
            {
                "method": "item/started",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {"type": "agentMessage"},
                },
            }
        )
        await self.broadcast(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "status": "completed"},
                },
            }
        )


@pytest.fixture
def fake_rpc() -> FakeRpc:
    return FakeRpc()


@pytest.fixture
def bridge_app(
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> web.Application:
    monkeypatch.setattr(bridge_service, "SPEECH_SESSION_HANDOFF_ENABLED", True)
    return create_app(
        BridgeConfig(bearer_token="test-token"),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )


def test_transcription_silence_defaults_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HA_CODEX_BRIDGE_TOKEN", "test-token")
    monkeypatch.delenv("HA_CODEX_TRANSCRIBE_SILENCE_MS", raising=False)

    assert BridgeConfig(bearer_token="test-token").silence_ms == 0
    assert BridgeConfig.from_env().silence_ms == 0


def test_transcription_silence_supports_explicit_nonzero_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HA_CODEX_BRIDGE_TOKEN", "test-token")
    monkeypatch.setenv("HA_CODEX_TRANSCRIBE_SILENCE_MS", "750")

    assert BridgeConfig(bearer_token="test-token", silence_ms=500).silence_ms == 500
    assert BridgeConfig.from_env().silence_ms == 750


def test_codex_child_environment_excludes_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app-server child never inherits bridge or developer credentials."""
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("HA_CODEX_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("HASS_TOKEN", "home-assistant-secret")
    monkeypatch.setenv("GH_TOKEN", "github-secret")

    environment = _codex_child_environment()

    assert "HOME" not in environment
    assert "HA_CODEX_BRIDGE_TOKEN" not in environment
    assert "HASS_TOKEN" not in environment
    assert "GH_TOKEN" not in environment


def test_adaptive_transcription_gain_lifts_quiet_audio_without_clipping() -> None:
    samples = array("h", [26] * 999 + [1_000])
    source = samples.tobytes()

    amplified, gain = bridge_service._apply_transcription_gain(source)
    peak, rms = bridge_service._normalized_pcm16_levels(amplified)

    assert gain > 20
    assert peak <= bridge_service.TRANSCRIPTION_TARGET_PEAK + 0.001
    assert rms > bridge_service._normalized_pcm16_levels(source)[1]


def test_adaptive_transcription_gain_leaves_healthy_audio_unchanged() -> None:
    source = array("h", [16_384, -16_384] * 20).tobytes()

    amplified, gain = bridge_service._apply_transcription_gain(source)

    assert amplified == source
    assert gain == 1.0


def test_adaptive_transcription_gain_leaves_silence_unchanged() -> None:
    source = b"\x00\x00" * 20

    amplified, gain = bridge_service._apply_transcription_gain(source)

    assert amplified == source
    assert gain == 1.0


def test_transcription_silence_trim_keeps_preroll_before_synthetic_speech() -> None:
    silence = array("h", [0]) * (10 * bridge_service.REALTIME_SAMPLE_RATE)
    speech = array("h", [12_000, -12_000]) * (bridge_service.REALTIME_SAMPLE_RATE // 2)
    source = (silence + speech).tobytes()

    trimmed = bridge_service._trim_transcription_silence(source)
    trimmed_samples = array("h")
    trimmed_samples.frombytes(trimmed)
    first_speech_sample = next(
        index for index, sample in enumerate(trimmed_samples) if sample
    )

    assert 0.25 <= first_speech_sample / bridge_service.REALTIME_SAMPLE_RATE <= 0.4
    assert trimmed.endswith(speech.tobytes())
    assert len(trimmed) < len(source) / 4


def test_transcription_silence_trim_leaves_immediate_speech_unchanged() -> None:
    source = (
        array("h", [10_000, -10_000]) * bridge_service.REALTIME_SAMPLE_RATE
    ).tobytes()

    assert bridge_service._trim_transcription_silence(source) == source


def test_transcription_silence_trim_leaves_quiet_early_speech_unchanged() -> None:
    quiet_speech = array("h", [150, -150]) * bridge_service.REALTIME_SAMPLE_RATE
    loud_speech = array("h", [10_000, -10_000]) * (
        bridge_service.REALTIME_SAMPLE_RATE // 4
    )
    source = (quiet_speech + loud_speech).tobytes()

    assert bridge_service._trim_transcription_silence(source) == source


def test_transcription_silence_trim_preserves_earlier_high_energy_audio() -> None:
    initial_silence = array("h", [0]) * (3 * bridge_service.REALTIME_SAMPLE_RATE)
    high_energy_frame = array("h", [16_000, -16_000]) * (
        bridge_service.REALTIME_SAMPLE_RATE
        * bridge_service.TRANSCRIPTION_TRIM_FRAME_MS
        // 2_000
    )
    middle_silence = array("h", [0]) * (4 * bridge_service.REALTIME_SAMPLE_RATE)
    speech = array("h", [12_000, -12_000]) * (bridge_service.REALTIME_SAMPLE_RATE // 4)
    source = (initial_silence + high_energy_frame + middle_silence + speech).tobytes()

    trimmed = bridge_service._trim_transcription_silence(source)
    trimmed_samples = array("h")
    trimmed_samples.frombytes(trimmed)
    first_active_sample = next(
        index for index, sample in enumerate(trimmed_samples) if sample
    )

    assert first_active_sample / bridge_service.REALTIME_SAMPLE_RATE == pytest.approx(
        bridge_service.TRANSCRIPTION_TRIM_PREROLL_MS / 1_000
    )
    assert high_energy_frame.tobytes() in trimmed


@pytest.mark.parametrize("padding_level", [0, 300], ids=["digital", "room-noise"])
def test_transcription_silence_trim_preserves_padded_quiet_speech(
    padding_level: int,
) -> None:
    padding = array("h", [padding_level, -padding_level]) * (
        5 * bridge_service.REALTIME_SAMPLE_RATE // 2
    )
    quiet_level = 150 if padding_level == 0 else 500
    quiet_speech = array("h", [quiet_level, -quiet_level]) * (
        bridge_service.REALTIME_SAMPLE_RATE // 2
    )
    loud_speech = array("h", [12_000, -12_000]) * (
        bridge_service.REALTIME_SAMPLE_RATE // 4
    )
    speech = (quiet_speech + loud_speech).tobytes()
    source = (padding + quiet_speech + loud_speech).tobytes()

    trimmed = bridge_service._trim_transcription_silence(source)

    assert trimmed.endswith(speech)
    assert len(trimmed) < len(source)
    assert len(trimmed) / 2 / bridge_service.REALTIME_SAMPLE_RATE == pytest.approx(1.82)


@pytest.mark.parametrize(
    "source",
    [
        b"\x00\x00" * bridge_service.REALTIME_SAMPLE_RATE,
        array("h", [1_000, -1_000] * bridge_service.REALTIME_SAMPLE_RATE).tobytes(),
        (
            array("h", [1_000, -1_000]) * (bridge_service.REALTIME_SAMPLE_RATE // 2)
            + array("h", [1_300, -1_300]) * (bridge_service.REALTIME_SAMPLE_RATE // 2)
        ).tobytes(),
    ],
    ids=["silence", "uniform-noise", "low-contrast"],
)
def test_transcription_silence_trim_requires_clear_contrast(source: bytes) -> None:
    assert bridge_service._trim_transcription_silence(source) == source


def test_transcription_silence_trim_preserves_pcm16_byte_alignment() -> None:
    silence = array("h", [0]) * (3 * bridge_service.REALTIME_SAMPLE_RATE)
    speech = array("h", [12_345, -12_345]) * (bridge_service.REALTIME_SAMPLE_RATE // 4)

    trimmed = bridge_service._trim_transcription_silence((silence + speech).tobytes())
    trimmed_samples = array("h")
    trimmed_samples.frombytes(trimmed)

    assert len(trimmed) % 2 == 0
    assert len(trimmed) < len((silence + speech).tobytes())
    assert next(sample for sample in trimmed_samples if sample) == 12_345


def test_isolated_codex_runtime_links_only_secure_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """App Server gets private homes while Codex retains managed token refresh."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    auth_file.chmod(0o600)
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("CODEX_HOME", "/safe/codex")
    monkeypatch.setenv("HA_CODEX_BRIDGE_TOKEN", "bridge-secret")

    runtime = IsolatedCodexRuntime(str(auth_file))
    root = runtime.root
    try:
        private_codex_home = Path(runtime.environment["CODEX_HOME"])
        linked_auth = private_codex_home / "auth.json"
        assert linked_auth.is_symlink()
        assert linked_auth.samefile(auth_file)
        assert Path(runtime.environment["HOME"]).parent == root
        assert private_codex_home.parent == root
        assert root.stat().st_mode & 0o777 == 0o700
        assert "HA_CODEX_BRIDGE_TOKEN" not in runtime.environment
        assert "/safe/home" not in runtime.environment.values()
        assert "/safe/codex" not in runtime.environment.values()
    finally:
        runtime.cleanup()
    assert not root.exists()


def test_isolated_codex_runtime_rejects_exposed_auth(tmp_path: Path) -> None:
    """An auth file readable by another local user fails closed."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    auth_file.chmod(0o644)

    with pytest.raises(ValueError, match="group or others"):
        IsolatedCodexRuntime(str(auth_file))


def test_isolated_codex_runtime_requires_refreshable_auth(tmp_path: Path) -> None:
    """A read-only auth file cannot safely retain rotating refresh tokens."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    auth_file.chmod(0o400)

    with pytest.raises(ValueError, match="writable"):
        IsolatedCodexRuntime(str(auth_file))


@pytest.mark.asyncio
async def test_thread_fails_closed_when_permission_profile_is_not_active(
    fake_rpc: FakeRpc,
) -> None:
    """A legacy or broadened active profile cannot run a voice thread."""
    fake_rpc.active_profile_id = ":read-only"
    await fake_rpc.start()
    state = BridgeState(BridgeConfig(bearer_token="test-token"), rpc=fake_rpc)
    try:
        with pytest.raises(ProtocolError, match="required permission profile"):
            await state.start_thread({})
    finally:
        await state.close()
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls


@pytest.mark.asyncio
async def test_speech_session_lease_releases_when_owner_is_cancelled(
    fake_rpc: FakeRpc,
) -> None:
    """Cancellation cannot permanently strand the exclusive speech channel."""
    state = BridgeState(BridgeConfig(bearer_token="test-token"), rpc=fake_rpc)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def hold_lease() -> None:
        async with state.speech_session_lease():
            entered.set()
            await blocked.wait()

    owner = asyncio.create_task(hold_lease())
    await entered.wait()
    with pytest.raises(BridgeBusyError, match="already active"):
        async with state.speech_session_lease():
            pass

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    async with state.speech_session_lease():
        pass
    await state.close()


@pytest.mark.asyncio
async def test_multiple_retained_cleanups_keep_speech_lane_busy(
    fake_rpc: FakeRpc,
) -> None:
    """One completed cleanup cannot release the lane while another is pending."""

    class GatedSession:
        def __init__(self, started: asyncio.Event, gate: asyncio.Event) -> None:
            self.rpc = fake_rpc
            self.started = started
            self.gate = gate

        async def stop(self) -> None:
            self.started.set()
            await self.gate.wait()

    state = BridgeState(BridgeConfig(bearer_token="test-token"), rpc=fake_rpc)
    first_started = asyncio.Event()
    first_gate = asyncio.Event()
    second_started = asyncio.Event()
    second_gate = asyncio.Event()
    first = bridge_service._RetainedSpeechSession(
        session=GatedSession(first_started, first_gate),  # type: ignore[arg-type]
        thread_id="first-thread",
        voice="cove",
    )
    second = bridge_service._RetainedSpeechSession(
        session=GatedSession(second_started, second_gate),  # type: ignore[arg-type]
        thread_id="second-thread",
        voice="cove",
    )
    first_waiter = asyncio.create_task(state.close_speech_session_resource(first))
    second_waiter = asyncio.create_task(state.close_speech_session_resource(second))
    await asyncio.gather(first_started.wait(), second_started.wait())

    first_gate.set()
    await first_waiter

    assert len(state._speech_cleanup_tasks) == 1
    admission_entered = asyncio.Event()

    async def wait_for_cleanup() -> None:
        async with state.speech_session_lease():
            admission_entered.set()

    admission = asyncio.create_task(wait_for_cleanup())
    await asyncio.sleep(0)
    assert not admission.done()

    second_gate.set()
    await second_waiter
    await admission

    assert not state._speech_cleanup_tasks
    assert admission_entered.is_set()
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 2
    await state.close()


@pytest.mark.asyncio
async def test_thread_delete_falls_back_to_unsubscribe(fake_rpc: FakeRpc) -> None:
    """Older app-server failures still release the event subscription."""
    fake_rpc.thread_delete_error = RuntimeError("delete unavailable")

    await bridge_service._dispose_thread(fake_rpc, "thread-1")

    assert fake_rpc.calls[-2:] == [
        ("thread/delete", {"threadId": "thread-1"}),
        ("thread/unsubscribe", {"threadId": "thread-1"}),
    ]


@pytest.mark.asyncio
async def test_health_requires_bearer_and_reports_ready(
    aiohttp_client: Any, bridge_app: web.Application
) -> None:
    client = await aiohttp_client(bridge_app)
    unauthorized = await client.get("/health")
    assert unauthorized.status == 401
    response = await client.get("/health", headers=AUTH)
    assert response.status == 200
    assert await response.json() == {
        "status": "ok",
        "app_server": {
            "running": True,
            "initialized": True,
            "auth_mode": "chatgpt",
            "plan_type": "plus",
        },
    }


@pytest.mark.asyncio
async def test_health_rejects_non_subscription_auth(aiohttp_client: Any) -> None:
    rpc = FakeRpc()
    rpc.health = lambda: {
        "running": rpc.running,
        "initialized": rpc.running,
        "auth_mode": "apikey",
        "stderr": ["secret material"],
    }
    app = create_app(
        BridgeConfig(bearer_token="test-token"),
        rpc=rpc,
        peer_factory=rpc.peer_factory,
    )
    client = await aiohttp_client(app)
    response = await client.get("/health", headers=AUTH)
    assert response.status == 503
    payload = await response.json()
    assert payload["status"] == "unavailable"
    assert payload["app_server"]["auth_mode"] == "apikey"
    assert "stderr" not in payload["app_server"]


@pytest.mark.asyncio
async def test_conversation_contract_tools_and_thread_reuse(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    client = await aiohttp_client(bridge_app)
    start = {
        "type": "start",
        "conversation_id": "conversation-1",
        "effort": "low",
        "instructions": "Current Home Assistant context: morning",
        "messages": [
            {"role": "user", "content": "Remember the kitchen"},
            {"role": "assistant", "content": "I will remember it."},
            {"role": "user", "content": "Turn on the kitchen"},
        ],
        "tools": [
            {
                "name": "HassTurnOn",
                "description": "Turn on a device",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                },
            }
        ],
    }
    websocket = await client.ws_connect("/v1/conversation", headers=AUTH)
    await websocket.send_json(start)
    assert (await websocket.receive_json())["type"] == "started"
    assert await websocket.receive_json() == {
        "type": "delta",
        "delta": "Done:Turn on the kitchen",
    }
    tool_call = await websocket.receive_json()
    assert tool_call["type"] == "tool_call"
    assert tool_call["call_id"] == "call-1"
    await websocket.send_json(
        {"type": "tool_result", "id": "call-1", "result": {"success": True}}
    )
    done = await websocket.receive_json()
    assert done["type"] == "done"
    await websocket.close()
    await asyncio.sleep(0)
    assert not any(method == "thread/delete" for method, _ in fake_rpc.calls)

    second = await client.ws_connect("/v1/conversation", headers=AUTH)
    await second.send_json(
        {
            **start,
            "instructions": "Current Home Assistant context: evening",
            "messages": [{"role": "user", "content": "And the dining room"}],
        }
    )
    assert (await second.receive_json())["type"] == "started"
    assert (await second.receive_json())["type"] == "delta"
    assert (await second.receive_json())["type"] == "done"
    await second.close()

    starts = [params for method, params in fake_rpc.calls if method == "thread/start"]
    assert len(starts) == 1
    assert starts[0]["permissions"] == "ha-voice-minimal"
    assert "sandbox" not in starts[0]
    assert starts[0]["runtimeWorkspaceRoots"] == []
    assert starts[0]["config"]["features"]["shell_tool"] is False
    assert (
        starts[0]["config"]["permissions"]["ha-voice-minimal"]["filesystem"][":root"]
        == "deny"
    )
    assert starts[0]["approvalPolicy"] == "never"
    assert "developerInstructions" not in starts[0]
    assert Path(starts[0]["cwd"]).name.startswith("ha-codex-voice-")
    assert starts[0]["dynamicTools"][0]["inputSchema"]["type"] == "object"
    turns = [params for method, params in fake_rpc.calls if method == "turn/start"]
    assert [turn["input"][0]["text"] for turn in turns] == [
        "Turn on the kitchen",
        "And the dining room",
    ]
    assert [turn["effort"] for turn in turns] == ["low", "low"]
    assert (
        turns[0]["additionalContext"]["home_assistant_instructions"]["value"]
        == "Current Home Assistant context: morning"
    )
    assert (
        "Remember the kitchen"
        in turns[0]["additionalContext"]["home_assistant_history"]["value"]
    )
    assert (
        turns[1]["additionalContext"]["home_assistant_instructions"]["value"]
        == "Current Home Assistant context: evening"
    )
    assert "home_assistant_history" not in turns[1]["additionalContext"]
    assert fake_rpc.responses == [
        (
            "rpc-call-1",
            {
                "contentItems": [{"type": "inputText", "text": '{"success":true}'}],
                "success": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_one_shot_conversation_deletes_private_thread(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    """A conversation without a stable ID cannot remain loaded after close."""
    fake_rpc.emit_tool_once = False
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/conversation", headers=AUTH)
    await websocket.send_json(
        {
            "type": "start",
            "messages": [{"role": "user", "content": "one shot"}],
            "tools": [],
        }
    )
    assert (await websocket.receive_json())["type"] == "started"
    assert (await websocket.receive_json())["type"] == "delta"
    assert (await websocket.receive_json())["type"] == "done"
    await websocket.send_json({"type": "stop"})
    await websocket.close()
    for _ in range(20):
        if any(method == "thread/delete" for method, _ in fake_rpc.calls):
            break
        await asyncio.sleep(0)

    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls
    turn = next(params for method, params in fake_rpc.calls if method == "turn/start")
    assert turn["effort"] == bridge_service.DEFAULT_CONVERSATION_EFFORT


@pytest.mark.asyncio
async def test_overlapping_turns_are_rejected_without_queueing(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    """One thread admits one turn and rejects authenticated message floods."""
    fake_rpc.emit_tool_once = False
    fake_rpc.turn_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    start = {
        "type": "start",
        "conversation_id": "shared-conversation",
        "messages": [],
        "tools": [],
    }
    first = await client.ws_connect("/v1/conversation", headers=AUTH)
    await first.send_json(start)
    assert (await first.receive_json())["type"] == "started"
    second = await client.ws_connect("/v1/conversation", headers=AUTH)
    await second.send_json(start)
    assert (await second.receive_json())["type"] == "started"

    await first.send_json({"type": "message", "text": "alpha"})
    for _ in range(20):
        if fake_rpc.turn_count == 1:
            break
        await asyncio.sleep(0)
    assert fake_rpc.turn_count == 1

    for index in range(20):
        await first.send_json({"type": "message", "text": f"extra-{index}"})
    await second.send_json({"type": "message", "text": "beta"})

    for _ in range(20):
        error = await first.receive_json(timeout=0.2)
        assert error["type"] == "error"
        assert error["code"] == "busy"
    second_error = await second.receive_json(timeout=0.2)
    assert second_error["type"] == "error"
    assert second_error["code"] == "busy"
    assert fake_rpc.turn_count == 1

    fake_rpc.turn_gate.set()
    assert await first.receive_json() == {"type": "delta", "delta": "Done:alpha"}
    assert (await first.receive_json())["type"] == "done"

    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_tool_change_does_not_delete_busy_conversation(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    """A changed tool schema cannot tear down another socket's active turn."""
    fake_rpc.emit_tool_once = False
    fake_rpc.turn_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    first = await client.ws_connect("/v1/conversation", headers=AUTH)
    await first.send_json(
        {
            "type": "start",
            "conversation_id": "busy-tool-change",
            "messages": [],
            "tools": [],
        }
    )
    assert (await first.receive_json())["type"] == "started"
    await first.send_json({"type": "message", "text": "keep working"})
    for _ in range(20):
        if fake_rpc.turn_count == 1:
            break
        await asyncio.sleep(0)

    replacement = await client.ws_connect("/v1/conversation", headers=AUTH)
    await replacement.send_json(
        {
            "type": "start",
            "conversation_id": "busy-tool-change",
            "messages": [],
            "tools": [
                {
                    "name": "HassTurnOn",
                    "description": "Turn on a device",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
    )
    error = await replacement.receive_json(timeout=0.2)
    assert error["type"] == "error"
    assert "tools cannot change" in error["error"]
    assert not any(method == "thread/delete" for method, _ in fake_rpc.calls)

    fake_rpc.turn_gate.set()
    assert (await first.receive_json())["type"] == "delta"
    assert (await first.receive_json())["type"] == "done"
    await first.close()
    await replacement.close()


@pytest.mark.asyncio
async def test_retired_thread_is_rechecked_after_turn_lock_acquisition(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    """A scheduled task cannot start after its cached thread is replaced."""
    fake_rpc.emit_tool_once = False
    client = await aiohttp_client(bridge_app)
    first = await client.ws_connect("/v1/conversation", headers=AUTH)
    await first.send_json(
        {
            "type": "start",
            "conversation_id": "retire-race",
            "messages": [],
            "tools": [],
        }
    )
    assert (await first.receive_json())["thread_id"] == "thread-1"
    state = bridge_app[bridge_service.STATE_KEY]
    old_turn_state = state._conversations["retire-race"].turn_state
    await old_turn_state.turn_lock.acquire()
    try:
        await first.send_json({"type": "message", "text": "stale"})
        for _ in range(20):
            if old_turn_state.pending_owner is not None:
                break
            await asyncio.sleep(0)
        await state.retire_conversation("retire-race", "thread-1", old_turn_state)
        assert (
            "thread/delete",
            {"threadId": "thread-1"},
        ) in fake_rpc.calls
        replacement = await client.ws_connect("/v1/conversation", headers=AUTH)
        await replacement.send_json(
            {
                "type": "start",
                "conversation_id": "retire-race",
                "messages": [],
                "tools": [
                    {
                        "name": "HassTurnOn",
                        "description": "Turn on a device",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        )
        assert (await replacement.receive_json())["thread_id"] == "thread-2"
    finally:
        old_turn_state.turn_lock.release()

    error = await first.receive_json(timeout=0.2)
    assert error["type"] == "error"
    assert "retired" in error["error"]
    assert not any(method == "turn/start" for method, _ in fake_rpc.calls)

    await replacement.send_json({"type": "message", "text": "fresh"})
    assert (await replacement.receive_json())["type"] == "delta"
    assert (await replacement.receive_json())["type"] == "done"
    replacement_turn = next(
        params for method, params in fake_rpc.calls if method == "turn/start"
    )
    assert replacement_turn["threadId"] == "thread-2"
    await first.close()
    await replacement.close()


@pytest.mark.asyncio
async def test_disconnect_during_turn_start_retires_cached_thread(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    fake_rpc.turn_start_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/conversation", headers=AUTH)
    await websocket.send_json(
        {
            "type": "start",
            "conversation_id": "disconnect-start",
            "messages": [{"role": "user", "content": "waiting"}],
            "tools": [],
        }
    )
    assert (await websocket.receive_json())["thread_id"] == "thread-1"
    await websocket.close()
    for _ in range(20):
        if any(method == "thread/delete" for method, _ in fake_rpc.calls):
            break
        await asyncio.sleep(0)
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls

    fake_rpc.turn_start_gate.set()
    replacement = await client.ws_connect("/v1/conversation", headers=AUTH)
    await replacement.send_json(
        {
            "type": "start",
            "conversation_id": "disconnect-start",
            "messages": [],
            "tools": [],
        }
    )
    assert (await replacement.receive_json())["thread_id"] == "thread-2"
    await replacement.close()


@pytest.mark.asyncio
async def test_failed_interrupt_on_disconnect_retires_cached_thread(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    fake_rpc.emit_tool_once = False
    fake_rpc.turn_gate = asyncio.Event()
    fake_rpc.turn_interrupt_error = RuntimeError("interrupt failed")
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/conversation", headers=AUTH)
    await websocket.send_json(
        {
            "type": "start",
            "conversation_id": "failed-interrupt",
            "messages": [{"role": "user", "content": "waiting"}],
            "tools": [],
        }
    )
    assert (await websocket.receive_json())["thread_id"] == "thread-1"
    for _ in range(20):
        if fake_rpc.turn_count == 1:
            break
        await asyncio.sleep(0)
    await websocket.close()
    for _ in range(20):
        if any(method == "thread/delete" for method, _ in fake_rpc.calls):
            break
        await asyncio.sleep(0)

    assert (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ) in fake_rpc.calls
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls


@pytest.mark.asyncio
async def test_reused_thread_drops_late_events_from_prior_turn(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    """A stale turn cannot leak text or tool calls into the next socket."""
    fake_rpc.emit_tool_once = False
    client = await aiohttp_client(bridge_app)
    start = {
        "type": "start",
        "conversation_id": "stale-event-conversation",
        "messages": [{"role": "user", "content": "first"}],
        "tools": [],
    }
    first = await client.ws_connect("/v1/conversation", headers=AUTH)
    await first.send_json(start)
    assert (await first.receive_json())["type"] == "started"
    assert (await first.receive_json())["type"] == "delta"
    assert (await first.receive_json())["type"] == "done"
    await first.close()

    fake_rpc.turn_gate = asyncio.Event()
    second = await client.ws_connect("/v1/conversation", headers=AUTH)
    await second.send_json({**start, "messages": []})
    assert (await second.receive_json())["type"] == "started"
    await second.send_json({"type": "message", "text": "second"})
    for _ in range(20):
        if fake_rpc.turn_count >= 2:
            break
        await asyncio.sleep(0)
    assert fake_rpc.turn_count == 2
    await fake_rpc.broadcast(
        {
            "id": "stale-rpc-request",
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "stale-call",
                "tool": "HassTurnOn",
                "arguments": {"name": "Unsafe stale target"},
            },
        }
    )
    with pytest.raises(TimeoutError):
        await second.receive_json(timeout=0.03)

    fake_rpc.turn_gate.set()
    assert await second.receive_json() == {"type": "delta", "delta": "Done:second"}
    assert (await second.receive_json())["type"] == "done"
    await second.close()


@pytest.mark.asyncio
async def test_app_server_exit_fails_active_conversation_immediately(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    fake_rpc.turn_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/conversation", headers=AUTH)
    await websocket.send_json(
        {
            "type": "start",
            "conversation_id": "app-server-exit",
            "messages": [{"role": "user", "content": "waiting"}],
            "tools": [],
        }
    )
    assert (await websocket.receive_json())["type"] == "started"
    for _ in range(20):
        if fake_rpc.turn_count:
            break
        await asyncio.sleep(0)
    await fake_rpc.broadcast(
        {"method": "bridge/appServerExited", "params": {"returncode": 1}}
    )

    error = await websocket.receive_json(timeout=0.2)
    assert error["type"] == "error"
    assert "exited with status 1" in error["error"]
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls
    close_message = await websocket.receive(timeout=0.2)
    assert close_message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}


@pytest.mark.asyncio
async def test_turn_start_timeout_fails_and_closes_conversation(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    fake_rpc.turn_start_error = TimeoutError()
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/conversation", headers=AUTH)
    await websocket.send_json(
        {
            "type": "start",
            "conversation_id": "turn-timeout",
            "messages": [{"role": "user", "content": "waiting"}],
            "tools": [],
        }
    )

    assert (await websocket.receive_json())["type"] == "started"
    assert await websocket.receive_json(timeout=0.2) == {
        "type": "error",
        "error": "conversation timed out",
    }
    close_message = await websocket.receive(timeout=0.2)
    assert close_message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls

    fake_rpc.turn_start_error = None
    replacement = await client.ws_connect("/v1/conversation", headers=AUTH)
    await replacement.send_json(
        {
            "type": "start",
            "conversation_id": "turn-timeout",
            "messages": [],
            "tools": [],
        }
    )
    assert (await replacement.receive_json())["thread_id"] == "thread-2"
    await replacement.close()


@pytest.mark.asyncio
async def test_missing_turn_completion_interrupts_and_retires_thread(
    aiohttp_client: Any, fake_rpc: FakeRpc
) -> None:
    fake_rpc.emit_tool_once = False
    fake_rpc.emit_turn_completion = False
    app = create_app(
        BridgeConfig(bearer_token="test-token", request_timeout=0.03),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )
    client = await aiohttp_client(app)
    websocket = await client.ws_connect("/v1/conversation", headers=AUTH)
    await websocket.send_json(
        {
            "type": "start",
            "conversation_id": "missing-completion",
            "messages": [{"role": "user", "content": "waiting"}],
            "tools": [],
        }
    )

    assert (await websocket.receive_json())["thread_id"] == "thread-1"
    assert (await websocket.receive_json())["type"] == "delta"
    assert await websocket.receive_json(timeout=0.2) == {
        "type": "error",
        "error": "conversation timed out",
    }
    assert (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ) in fake_rpc.calls
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls


@pytest.mark.asyncio
async def test_transcription_stream_overlaps_handshake_and_assembles_result(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The disposable Codex session starts while the microphone is still open."""
    fake_rpc.realtime_start_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    private_prompt = "private-stream-vocabulary"

    with caplog.at_level(logging.INFO, logger="bridge.service"):
        await websocket.send_json(
            _transcription_stream_start(
                language="en-US",
                prompt=private_prompt,
            )
        )
        assert await websocket.receive_json() == {
            "type": "started",
            "protocol_version": 1,
        }
        await asyncio.wait_for(fake_rpc.realtime_start_started.wait(), timeout=1)
        assert any(method == "thread/realtime/start" for method, _ in fake_rpc.calls)
        assert fake_rpc.peers[-1].fed == b""

        await websocket.send_bytes(b"\x00\x20" * 320)
        fake_rpc.realtime_start_gate.set()
        for _ in range(100):
            if fake_rpc.peers[-1].answer is not None:
                break
            await asyncio.sleep(0)
        assert fake_rpc.peers[-1].answer == "v=0\r\nfake-answer\r\n"
        assert fake_rpc.peers[-1].fed == b""

        await websocket.send_json({"type": "end"})
        assert await websocket.receive_json(timeout=1) == {
            "type": "result",
            "text": "Turn on the kitchen",
            "language": "en-US",
        }
        close = await websocket.receive(timeout=1)
        assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

        for _ in range(100):
            if any(
                record.getMessage().startswith("Realtime transcription stream timing:")
                for record in caplog.records
            ):
                break
            await asyncio.sleep(0)

    assert fake_rpc.peers[-1].fed
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls
    stream_timing = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Realtime transcription stream timing:")
    )
    match = re.fullmatch(
        r"Realtime transcription stream timing: capture_seconds=(\d+\.\d{3}) "
        r"handshake_capture_overlap_seconds=(\d+\.\d{3}) "
        r"post_capture_seconds=(\d+\.\d{3}) total_seconds=(\d+\.\d{3})",
        stream_timing,
    )
    assert match is not None
    assert all(float(value) >= 0 for value in match.groups())
    assert private_prompt not in caplog.text


@pytest.mark.asyncio
async def test_transcription_stream_feeds_confident_speech_before_eof(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)

    with caplog.at_level(logging.INFO, logger="bridge.service"):
        await websocket.send_json(_transcription_stream_start())
        assert (await websocket.receive_json())["type"] == "started"
        await asyncio.wait_for(fake_rpc.realtime_start_started.wait(), timeout=1)
        await websocket.send_bytes(b"\x00\x20" * 6_400)
        for _ in range(100):
            if fake_rpc.peers[-1].fed:
                break
            await asyncio.sleep(0)

        assert fake_rpc.peers[-1].fed
        assert not websocket.closed
        await websocket.send_json({"type": "end"})
        assert await websocket.receive_json(timeout=1) == {
            "type": "result",
            "text": "Turn on the kitchen",
        }
        await websocket.receive(timeout=1)

    assert "Realtime live transcription timing: live_feed=True" in caplog.text


@pytest.mark.asyncio
async def test_transcription_stream_quiet_audio_keeps_normalized_eof_fallback(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)

    with caplog.at_level(logging.INFO, logger="bridge.service"):
        await websocket.send_json(_transcription_stream_start())
        assert (await websocket.receive_json())["type"] == "started"
        await asyncio.wait_for(fake_rpc.realtime_start_started.wait(), timeout=1)
        await websocket.send_bytes(b"\x00\x01" * 6_400)
        await asyncio.sleep(0)
        assert fake_rpc.peers[-1].fed == b""

        await websocket.send_json({"type": "end"})
        assert (await websocket.receive_json(timeout=1))["type"] == "result"
        await websocket.receive(timeout=1)

    assert fake_rpc.peers[-1].fed
    assert "Realtime live transcription timing: live_feed=False" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"protocol_version": 2}, "protocol_version must be 1"),
        ({"format": "wav"}, "format must be 'pcm'"),
        ({"codec": "opus"}, "codec must be 'pcm'"),
        ({"sample_rate": 24_000}, "sample_rate must be 16000 or 48000"),
        ({"bit_rate": 24}, "bit_rate must be 16"),
        ({"channels": 2}, "channels must be 1"),
        ({"prompt": None}, "prompt must be a string"),
        (
            {"speech_session_handoff": "enabled"},
            "speech_session_handoff must be an object",
        ),
        (
            {"speech_session_handoff": {"version": 2, "voice": "cove"}},
            "version must be 1",
        ),
        (
            {"speech_session_handoff": {"version": 1, "voice": ""}},
            "speech_session_handoff voice must be a non-empty string",
        ),
        (
            {"speech_session_handoff": {"version": 1, "voice": "cove"}},
            "speech_session_handoff language must be a non-empty language tag",
        ),
        (
            {
                "language": "en-US",
                "speech_session_handoff": {
                    "version": 1,
                    "voice": "cove",
                    "language": "es-MX",
                },
            },
            "speech_session_handoff language must match transcription language",
        ),
    ],
)
async def test_transcription_stream_rejects_malformed_start(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    overrides: dict[str, Any],
    expected_error: str,
) -> None:
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)

    await websocket.send_json(_transcription_stream_start(**overrides))

    assert await websocket.receive_json() == {
        "type": "error",
        "error": expected_error,
    }
    assert not any(method == "thread/start" for method, _ in fake_rpc.calls)


@pytest.mark.asyncio
async def test_transcription_stream_rejects_binary_first_message(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)

    await websocket.send_bytes(b"\x00\x00")

    assert await websocket.receive_json() == {
        "type": "error",
        "error": "binary WebSocket messages are not supported",
    }
    assert not any(method == "thread/start" for method, _ in fake_rpc.calls)


@pytest.mark.asyncio
async def test_transcription_stream_rejects_empty_audio(
    aiohttp_client: Any, bridge_app: web.Application
) -> None:
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())
    assert (await websocket.receive_json())["type"] == "started"

    await websocket.send_json({"type": "end"})

    assert await websocket.receive_json() == {
        "type": "error",
        "error": "audio payload contains no samples",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_name", "limit_value", "expected_error"),
    [
        (
            "TRANSCRIPTION_STREAM_MAX_FRAME_BYTES",
            2,
            "audio frame exceeds 256 KiB",
        ),
        (
            "TRANSCRIPTION_STREAM_MAX_RAW_BYTES",
            2,
            "audio capture exceeds the size limit",
        ),
    ],
)
async def test_transcription_stream_enforces_binary_size_limits(
    aiohttp_client: Any,
    bridge_app: web.Application,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
    expected_error: str,
) -> None:
    monkeypatch.setattr(bridge_service, limit_name, limit_value)
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())
    assert (await websocket.receive_json())["type"] == "started"

    await websocket.send_bytes(b"\x01\x00\x02\x00")

    assert await websocket.receive_json() == {
        "type": "error",
        "error": expected_error,
    }


@pytest.mark.asyncio
async def test_transcription_stream_enforces_duration_while_capturing(
    aiohttp_client: Any,
    bridge_app: web.Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "MAX_TRANSCRIPTION_DURATION_SECONDS", 0.0)
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())
    assert (await websocket.receive_json())["type"] == "started"

    await websocket.send_bytes(b"\x01\x00")

    assert await websocket.receive_json() == {
        "type": "error",
        "error": "audio must not exceed 0 seconds for transcription",
    }


@pytest.mark.asyncio
async def test_transcription_stream_capture_timeout_is_safe(
    aiohttp_client: Any,
    bridge_app: web.Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge_service, "TRANSCRIPTION_STREAM_CAPTURE_TIMEOUT_SECONDS", 0.01
    )
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())
    assert (await websocket.receive_json())["type"] == "started"

    assert await websocket.receive_json(timeout=1) == {
        "type": "error",
        "error": "audio capture timed out",
    }


@pytest.mark.asyncio
async def test_transcription_stream_total_timeout_starts_after_capture(
    aiohttp_client: Any,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller may capture longer than the legacy post-body processing budget."""

    async def immediate_result(*_: Any, **__: Any) -> str:
        return "Captured after setup"

    monkeypatch.setattr(bridge_service, "_wait_for_user_transcript", immediate_result)
    app = create_app(
        BridgeConfig(bearer_token="test-token", transcript_timeout=0.02),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )
    client = await aiohttp_client(app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())
    assert (await websocket.receive_json())["type"] == "started"
    await asyncio.wait_for(fake_rpc.realtime_start_started.wait(), timeout=1)

    await asyncio.sleep(0.04)
    await websocket.send_bytes(b"\x00\x20" * 80)
    await websocket.send_json({"type": "end"})

    assert await websocket.receive_json(timeout=1) == {
        "type": "result",
        "text": "Captured after setup",
    }


@pytest.mark.asyncio
async def test_transcription_stream_enforces_post_capture_total_timeout(
    aiohttp_client: Any,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = asyncio.Event()

    async def never_returns(*_: Any, **__: Any) -> str:
        await blocked.wait()
        return "unreachable"

    monkeypatch.setattr(bridge_service, "_wait_for_user_transcript", never_returns)
    app = create_app(
        BridgeConfig(bearer_token="test-token", transcript_timeout=0.02),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )
    client = await aiohttp_client(app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())
    assert (await websocket.receive_json())["type"] == "started"
    await websocket.send_bytes(b"\x00\x20" * 80)
    await websocket.send_json({"type": "end"})

    assert await websocket.receive_json(timeout=1) == {
        "type": "error",
        "error": "transcription timed out",
    }
    assert fake_rpc.peers[-1].closed
    assert any(method == "thread/delete" for method, _ in fake_rpc.calls)


@pytest.mark.asyncio
async def test_transcription_stream_auth_and_busy_fail_before_upgrade(
    aiohttp_client: Any, bridge_app: web.Application
) -> None:
    client = await aiohttp_client(bridge_app)
    with pytest.raises(WSServerHandshakeError) as unauthorized:
        await client.ws_connect("/v1/transcribe/stream")
    assert unauthorized.value.status == 401

    state = bridge_app[bridge_service.STATE_KEY]
    async with state.speech_session_lease():
        with pytest.raises(WSServerHandshakeError) as busy:
            await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    assert busy.value.status == 409


@pytest.mark.asyncio
async def test_transcription_stream_subscription_auth_fails_before_upgrade(
    aiohttp_client: Any,
) -> None:
    rpc = FakeRpc()
    rpc.health = lambda: {
        "running": rpc.running,
        "initialized": rpc.running,
        "auth_mode": "apikey",
    }
    app = create_app(BridgeConfig(bearer_token="test-token"), rpc=rpc)
    client = await aiohttp_client(app)

    with pytest.raises(WSServerHandshakeError) as auth_failure:
        await client.ws_connect("/v1/transcribe/stream", headers=AUTH)

    assert auth_failure.value.status == 503


@pytest.mark.asyncio
async def test_transcription_stream_races_capture_against_early_attempt_failure(
    aiohttp_client: Any,
    bridge_app: web.Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_error = "private-thread-start-details"
    state = bridge_app[bridge_service.STATE_KEY]

    async def fail_start(*_: Any, **__: Any) -> str:
        raise ProtocolError(private_error)

    monkeypatch.setattr(state, "start_thread", fail_start)
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())

    assert (await websocket.receive_json())["type"] == "started"
    assert await websocket.receive_json(timeout=1) == {
        "type": "error",
        "error": "transcription failed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_message", [True, False], ids=["cancel", "close"])
async def test_transcription_stream_cancel_and_disconnect_clean_up(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    cancel_message: bool,
) -> None:
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())
    assert (await websocket.receive_json())["type"] == "started"
    await asyncio.wait_for(fake_rpc.realtime_start_started.wait(), timeout=1)

    if cancel_message:
        await websocket.send_json({"type": "cancel"})
        close = await websocket.receive(timeout=1)
        assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    else:
        await websocket.close()

    for _ in range(100):
        if (
            any(method == "thread/delete" for method, _ in fake_rpc.calls)
            and not bridge_app[bridge_service.STATE_KEY]._speech_session_active
        ):
            break
        await asyncio.sleep(0)
    assert fake_rpc.peers[-1].closed
    assert any(method == "thread/delete" for method, _ in fake_rpc.calls)
    assert not bridge_app[bridge_service.STATE_KEY]._speech_session_active


@pytest.mark.asyncio
async def test_transcription_stream_cancel_after_end_cleans_up(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    fake_rpc.transcript_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())
    assert (await websocket.receive_json())["type"] == "started"
    await websocket.send_bytes(b"\x00\x20" * 80)
    await websocket.send_json({"type": "end"})
    await asyncio.wait_for(fake_rpc.transcript_started.wait(), timeout=1)

    await websocket.send_json({"type": "cancel"})
    close = await websocket.receive(timeout=1)
    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

    for _ in range(100):
        if (
            any(method == "thread/delete" for method, _ in fake_rpc.calls)
            and not bridge_app[bridge_service.STATE_KEY]._speech_session_active
        ):
            break
        await asyncio.sleep(0)
    assert fake_rpc.peers[-1].closed
    assert any(method == "thread/delete" for method, _ in fake_rpc.calls)
    assert not bridge_app[bridge_service.STATE_KEY]._speech_session_active


@pytest.mark.asyncio
async def test_transcription_stream_retries_with_fresh_threads(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def flaky_transcript(*_: Any, **__: Any) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError
        return "Recovered stream transcript"

    monkeypatch.setattr(bridge_service, "_wait_for_user_transcript", flaky_transcript)
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(_transcription_stream_start())
    assert (await websocket.receive_json())["type"] == "started"
    await websocket.send_bytes(b"\x00\x30" * 160)
    await websocket.send_json({"type": "end"})

    assert await websocket.receive_json(timeout=1) == {
        "type": "result",
        "text": "Recovered stream transcript",
    }
    assert attempts == 2
    assert [
        method
        for method, _ in fake_rpc.calls
        if method in {"thread/start", "thread/delete"}
    ] == ["thread/start", "thread/delete", "thread/start", "thread/delete"]


@pytest.mark.asyncio
async def test_transcription_stream_logs_no_private_material(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_transcript = "Private streamed transcript"
    private_prompt = "private-stream-prompt"

    async def private_result(*_: Any, **__: Any) -> str:
        return private_transcript

    monkeypatch.setattr(bridge_service, "_wait_for_user_transcript", private_result)
    client = await aiohttp_client(bridge_app)
    with caplog.at_level(logging.INFO, logger="bridge.service"):
        websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
        await websocket.send_json(
            _transcription_stream_start(prompt=private_prompt, language="es-MX")
        )
        assert (await websocket.receive_json())["type"] == "started"
        private_audio = b"\x34\x12" * 80
        await websocket.send_bytes(private_audio)
        await websocket.send_json({"type": "end"})
        assert (await websocket.receive_json())["text"] == private_transcript
        close = await websocket.receive(timeout=1)
        assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
        for _ in range(100):
            if any(
                record.getMessage().startswith("Realtime transcription stream timing:")
                for record in caplog.records
            ):
                break
            await asyncio.sleep(0)

    service_log = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "bridge.service"
    )
    for private_value in (
        private_transcript,
        private_prompt,
        base64.b64encode(private_audio).decode(),
        "fake-offer",
        "fake-answer",
        "thread-1",
        "test-token",
    ):
        assert private_value not in service_log
    assert service_log.count("Realtime transcription attempt timing:") == 1
    assert service_log.count("Realtime transcription stream timing:") == 1


@pytest.mark.asyncio
async def test_speech_session_handoff_reuses_exact_realtime_session(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = await aiohttp_client(bridge_app)
    with caplog.at_level(logging.INFO):
        transcription = await _request_speech_session_handoff(client)

        handoff = transcription["speech_session_handoff"]
        assert handoff["version"] == 1
        assert handoff["expires_in_ms"] == 30_000
        assert handoff["voice"] == "cove"
        token = handoff["token"]
        padded_token = token + "=" * ((4 - len(token) % 4) % 4)
        assert len(base64.urlsafe_b64decode(padded_token)) == 32
        state = bridge_app[bridge_service.STATE_KEY]
        offer = state._speech_session_offer
        assert offer is not None
        assert token not in repr(offer)
        assert not fake_rpc.peers[0].closed
        assert fake_rpc.peers[0].pending_input_discarded
        assert not any(method == "thread/delete" for method, _ in fake_rpc.calls)

        payload = _synthesis_payload()
        payload["speech_session_handoff_token"] = token
        response = await client.post("/v1/synthesize", headers=AUTH, json=payload)

    assert response.status == 200
    with wave.open(BytesIO(await response.read()), "rb") as audio:
        assert audio.readframes(audio.getnframes())
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 1
    assert sum(method == "thread/realtime/start" for method, _ in fake_rpc.calls) == 1
    assert any(method == "thread/realtime/appendSpeech" for method, _ in fake_rpc.calls)
    assert not any(
        method == "thread/realtime/appendText"
        and str(params.get("text", "")).startswith("Vocalize only")
        for method, params in fake_rpc.calls
    )
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1
    assert bridge_app[bridge_service.STATE_KEY]._speech_session_offer is None
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_speech_session_handoff_is_disabled_by_default(
    aiohttp_client: Any,
    fake_rpc: FakeRpc,
) -> None:
    """Released bridge requests always close STT and omit reuse tickets."""
    app = create_app(
        BridgeConfig(bearer_token="test-token"),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )
    client = await aiohttp_client(app)

    transcription = await _request_speech_session_handoff(client)

    assert transcription["text"] == "Turn on the kitchen"
    assert "speech_session_handoff" not in transcription
    assert app[bridge_service.STATE_KEY]._speech_session_offer is None
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_post_speech_session_handoff_is_disabled_by_default(
    aiohttp_client: Any,
    fake_rpc: FakeRpc,
) -> None:
    """The finite transcription route cannot issue a reuse ticket either."""
    app = create_app(
        BridgeConfig(bearer_token="test-token"),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )
    client = await aiohttp_client(app)
    payload = _transcription_payload()
    payload["language"] = "en-US"
    payload["speech_session_handoff"] = {
        "version": 1,
        "voice": "cove",
        "language": "en-US",
    }

    response = await client.post("/v1/transcribe", headers=AUTH, json=payload)

    assert response.status == 200
    transcription = await response.json()
    assert transcription["text"] == "Turn on the kitchen"
    assert "speech_session_handoff" not in transcription
    assert app[bridge_service.STATE_KEY]._speech_session_offer is None
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_post_transcription_handoff_reuses_exact_realtime_session(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    payload = _transcription_payload()
    payload["speech_session_handoff"] = {
        "version": 1,
        "voice": " COVE ",
        "language": "en-US",
    }
    payload["language"] = "EN_us"

    transcription_response = await client.post(
        "/v1/transcribe", headers=AUTH, json=payload
    )

    assert transcription_response.status == 200
    transcription = await transcription_response.json()
    assert transcription["text"] == "Turn on the kitchen"
    handoff = transcription["speech_session_handoff"]
    assert handoff["version"] == 1
    assert handoff["voice"] == "cove"
    assert handoff["language"] == "en-US"
    assert handoff["expires_in_ms"] == 30_000
    token = handoff["token"]
    state = bridge_app[bridge_service.STATE_KEY]
    assert state._speech_session_offer is not None
    assert token not in repr(state._speech_session_offer)
    assert not fake_rpc.peers[0].closed

    synthesis_payload = _synthesis_payload()
    synthesis_payload["speech_session_handoff_token"] = token
    synthesis_payload["language"] = "en-US"
    synthesis_response = await client.post(
        "/v1/synthesize", headers=AUTH, json=synthesis_payload
    )

    assert synthesis_response.status == 200
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 1
    assert sum(method == "thread/realtime/start" for method, _ in fake_rpc.calls) == 1
    assert (
        sum(method == "thread/realtime/appendSpeech" for method, _ in fake_rpc.calls)
        == 1
    )
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_speech_session_handoff_language_mismatch_uses_cold_path(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client, language="EN_us")
    handoff = transcription["speech_session_handoff"]
    assert handoff["language"] == "en-US"
    payload = _synthesis_payload()
    payload["language"] = "es-MX"
    payload["speech_session_handoff_token"] = handoff["token"]

    response = await client.post("/v1/synthesize", headers=AUTH, json=payload)

    assert response.status == 200
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 2
    assert not any(
        method == "thread/realtime/appendSpeech" for method, _ in fake_rpc.calls
    )
    assert all(peer.closed for peer in fake_rpc.peers)
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handoff", "expected_error"),
    [
        ("enabled", "speech_session_handoff must be an object"),
        ({"version": 2, "voice": "cove"}, "version must be 1"),
        (
            {"version": 1, "voice": ""},
            "speech_session_handoff voice must be a non-empty string",
        ),
        (
            {"version": 1, "voice": "cove"},
            "speech_session_handoff language must be a non-empty language tag",
        ),
    ],
)
async def test_post_transcription_rejects_invalid_handoff_request(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    handoff: object,
    expected_error: str,
) -> None:
    client = await aiohttp_client(bridge_app)
    payload = _transcription_payload()
    payload["speech_session_handoff"] = handoff

    response = await client.post("/v1/transcribe", headers=AUTH, json=payload)

    assert response.status == 400
    assert await response.json() == {"error": expected_error}
    assert not any(method == "thread/start" for method, _ in fake_rpc.calls)
    assert bridge_app[bridge_service.STATE_KEY]._speech_session_offer is None


@pytest.mark.asyncio
async def test_post_transcription_rejects_contradictory_handoff_language(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    payload = _transcription_payload()
    payload["language"] = "en-US"
    payload["speech_session_handoff"] = {
        "version": 1,
        "voice": "cove",
        "language": "es-MX",
    }

    response = await client.post("/v1/transcribe", headers=AUTH, json=payload)

    assert response.status == 400
    assert await response.json() == {
        "error": "speech_session_handoff language must match transcription language"
    }
    assert not any(method == "thread/start" for method, _ in fake_rpc.calls)
    assert bridge_app[bridge_service.STATE_KEY]._speech_session_offer is None


@pytest.mark.asyncio
async def test_post_transcription_handoff_timeout_cleans_every_attempt(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def timeout_transcript(*_: Any, **__: Any) -> str:
        raise TimeoutError

    monkeypatch.setattr(bridge_service, "_wait_for_user_transcript", timeout_transcript)
    client = await aiohttp_client(bridge_app)
    payload = _transcription_payload()
    payload["speech_session_handoff"] = {
        "version": 1,
        "voice": "cove",
        "language": "en-US",
    }
    payload["language"] = "en-US"

    response = await client.post("/v1/transcribe", headers=AUTH, json=payload)

    assert response.status == 504
    assert bridge_app[bridge_service.STATE_KEY]._speech_session_offer is None
    assert len(fake_rpc.peers) == bridge_service.TRANSCRIPTION_MAX_ATTEMPTS
    assert all(peer.closed for peer in fake_rpc.peers)
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == (
        bridge_service.TRANSCRIPTION_MAX_ATTEMPTS
    )


@pytest.mark.asyncio
async def test_post_transcription_v1_cleans_up_without_handoff_offer(
    aiohttp_client: Any,
    fake_rpc: FakeRpc,
) -> None:
    app = create_app(
        BridgeConfig(bearer_token="test-token", realtime_version="v1"),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )
    client = await aiohttp_client(app)
    payload = _transcription_payload()
    payload["speech_session_handoff"] = {
        "version": 1,
        "voice": "cove",
        "language": "en-US",
    }
    payload["language"] = "en-US"

    response = await client.post("/v1/transcribe", headers=AUTH, json=payload)

    assert response.status == 200
    result = await response.json()
    assert result["text"] == "Turn on the kitchen"
    assert "speech_session_handoff" not in result
    assert app[bridge_service.STATE_KEY]._speech_session_offer is None
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_second_stt_preempts_unused_offer_before_starting(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    await _request_speech_session_handoff(client)
    state = bridge_app[bridge_service.STATE_KEY]
    for _ in range(100):
        if state._speech_owner is None:
            break
        await asyncio.sleep(0)
    payload = _transcription_payload()
    payload["speech_session_handoff"] = {
        "version": 1,
        "voice": "cove",
        "language": "en-US",
    }
    payload["language"] = "en-US"

    response = await client.post("/v1/transcribe", headers=AUTH, json=payload)

    assert response.status == 200
    result = await response.json()
    final_token = result["speech_session_handoff"]["token"]
    assert fake_rpc.peers[0].closed
    assert not fake_rpc.peers[1].closed
    assert [
        method
        for method, _ in fake_rpc.calls
        if method in {"thread/start", "thread/delete"}
    ] == ["thread/start", "thread/delete", "thread/start"]

    await state.release_speech_session_offer(final_token)

    assert state._speech_session_offer is None
    assert not state._speech_cleanup_tasks
    assert all(peer.closed for peer in fake_rpc.peers)
    assert [
        method
        for method, _ in fake_rpc.calls
        if method in {"thread/start", "thread/delete"}
    ] == ["thread/start", "thread/delete", "thread/start", "thread/delete"]


@pytest.mark.asyncio
async def test_release_race_delays_new_stt_instead_of_dropping_it(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]
    state = bridge_app[bridge_service.STATE_KEY]
    fake_rpc.realtime_stop_gate = asyncio.Event()
    release = asyncio.create_task(state.release_speech_session_offer(token))
    await asyncio.wait_for(fake_rpc.realtime_stop_started.wait(), timeout=1)

    next_stt = asyncio.create_task(
        client.post("/v1/transcribe", headers=AUTH, json=_transcription_payload())
    )
    await asyncio.sleep(0)
    assert not next_stt.done()

    fake_rpc.realtime_stop_gate.set()
    await release
    response = await asyncio.wait_for(next_stt, timeout=2)

    assert response.status == 200
    assert [
        method
        for method, _ in fake_rpc.calls
        if method in {"thread/start", "thread/delete"}
    ] == ["thread/start", "thread/delete", "thread/start", "thread/delete"]
    assert state._speech_session_offer is None
    assert not state._speech_cleanup_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "incompatibility",
    ["missing", "token", "voice", "instructions"],
)
async def test_incompatible_synthesis_preempts_offer_and_uses_cold_path(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    incompatibility: str,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]
    payload = _synthesis_payload()
    payload["speech_session_handoff_token"] = token
    if incompatibility == "missing":
        payload.pop("speech_session_handoff_token")
    elif incompatibility == "token":
        payload["speech_session_handoff_token"] = "unrelated-token"
    elif incompatibility == "voice":
        payload["voice"] = "alloy"
    else:
        payload["instructions"] = "Speak quietly"

    response = await client.post("/v1/synthesize", headers=AUTH, json=payload)

    assert response.status == 200
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 2
    assert sum(method == "thread/realtime/start" for method, _ in fake_rpc.calls) == 2
    assert not any(
        method == "thread/realtime/appendSpeech" for method, _ in fake_rpc.calls
    )
    assert any(
        method == "thread/realtime/appendText"
        and str(params.get("text", "")).startswith("Vocalize only")
        for method, params in fake_rpc.calls
    )
    assert [
        method
        for method, _ in fake_rpc.calls
        if method in {"thread/start", "thread/delete"}
    ] == ["thread/start", "thread/delete", "thread/start", "thread/delete"]
    assert all(peer.closed for peer in fake_rpc.peers)


@pytest.mark.asyncio
async def test_incompatible_offer_cleanup_survives_pre_yield_cancellation(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    await _request_speech_session_handoff(client)
    state = bridge_app[bridge_service.STATE_KEY]
    for _ in range(100):
        if state._speech_owner is None:
            break
        await asyncio.sleep(0)
    assert state._speech_owner is None
    fake_rpc.realtime_stop_gate = asyncio.Event()
    lease_entered = asyncio.Event()

    async def preempt_offer() -> None:
        async with state.speech_session_lease(
            handoff_token="unrelated-token",
            voice="cove",
        ):
            lease_entered.set()

    preemption = asyncio.create_task(preempt_offer())
    await asyncio.wait_for(fake_rpc.realtime_stop_started.wait(), timeout=1)
    preemption.cancel()
    with pytest.raises(asyncio.CancelledError):
        await preemption

    assert not lease_entered.is_set()
    assert state._speech_session_offer is None
    assert len(state._speech_cleanup_tasks) == 1

    retry_entered = asyncio.Event()

    async def wait_for_cleanup() -> None:
        async with state.speech_session_lease():
            retry_entered.set()

    retry = asyncio.create_task(wait_for_cleanup())
    await asyncio.sleep(0)
    assert not retry.done()

    fake_rpc.realtime_stop_gate.set()
    for _ in range(100):
        if not state._speech_cleanup_tasks:
            break
        await asyncio.sleep(0)

    assert not state._speech_cleanup_tasks
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1
    await retry
    assert retry_entered.is_set()


@pytest.mark.asyncio
async def test_speech_session_handoff_release_is_idempotent(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]

    unknown = await client.post(
        "/v1/speech-session/release",
        headers=AUTH,
        json={"speech_session_handoff_token": "unrelated-token"},
    )
    assert unknown.status == 204
    assert bridge_app[bridge_service.STATE_KEY]._speech_session_offer is not None
    first = await client.post(
        "/v1/speech-session/release",
        headers=AUTH,
        json={"speech_session_handoff_token": token},
    )
    second = await client.post(
        "/v1/speech-session/release",
        headers=AUTH,
        json={"speech_session_handoff_token": token},
    )

    assert first.status == 204
    assert second.status == 204
    assert bridge_app[bridge_service.STATE_KEY]._speech_session_offer is None
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_speech_session_release_keeps_cleanup_tracked(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]
    state = bridge_app[bridge_service.STATE_KEY]
    fake_rpc.realtime_stop_gate = asyncio.Event()

    release = asyncio.create_task(state.release_speech_session_offer(token))
    await asyncio.wait_for(fake_rpc.realtime_stop_started.wait(), timeout=1)
    release.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release

    assert state._speech_session_offer is None
    assert len(state._speech_cleanup_tasks) == 1
    contender = asyncio.create_task(
        client.post("/v1/synthesize", headers=AUTH, json=_synthesis_payload())
    )
    await asyncio.sleep(0)
    assert not contender.done()

    fake_rpc.realtime_stop_gate.set()
    for _ in range(100):
        if not state._speech_cleanup_tasks:
            break
        await asyncio.sleep(0)

    assert not state._speech_cleanup_tasks
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1
    response = await asyncio.wait_for(contender, timeout=2)
    assert response.status == 200


@pytest.mark.asyncio
async def test_speech_session_handoff_expires_and_cleans_up(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "SPEECH_SESSION_HANDOFF_TTL_SECONDS", 0.01)
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    expired_token = transcription["speech_session_handoff"]["token"]
    assert transcription["speech_session_handoff"]["expires_in_ms"] == 10

    for _ in range(100):
        if bridge_app[bridge_service.STATE_KEY]._speech_session_offer is None:
            break
        await asyncio.sleep(0.002)

    assert bridge_app[bridge_service.STATE_KEY]._speech_session_offer is None
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1

    payload = _synthesis_payload()
    payload["speech_session_handoff_token"] = expired_token
    response = await client.post("/v1/synthesize", headers=AUTH, json=payload)
    assert response.status == 200
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 2


@pytest.mark.asyncio
async def test_speech_session_handoff_watchdog_invalidates_assistant_audio(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    await _request_speech_session_handoff(client)
    assert bridge_app[bridge_service.STATE_KEY]._speech_session_offer is not None

    fake_rpc.peers[0].audio.put_nowait(b"\x01\x00")
    for _ in range(100):
        if (
            bridge_app[bridge_service.STATE_KEY]._speech_session_offer is None
            and fake_rpc.peers[0].closed
        ):
            break
        await asyncio.sleep(0)

    assert bridge_app[bridge_service.STATE_KEY]._speech_session_offer is None
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "result"),
    [
        ("audio", b"\x01\x00"),
        (
            "app",
            {
                "method": "thread/realtime/transcript/done",
                "params": {
                    "threadId": "race-thread",
                    "role": "assistant",
                    "text": "assistant-output",
                },
            },
        ),
    ],
)
async def test_speech_session_handoff_claim_validates_ready_watchdog_child(
    fake_rpc: FakeRpc,
    source: str,
    result: object,
) -> None:
    """A claim cannot hide an unsafe result already dequeued by the watchdog."""

    class RacingSession:
        def __init__(self) -> None:
            self.rpc = fake_rpc
            self.receiver_started = asyncio.Event()
            self.release_result = asyncio.Event()
            self.claim_ready = asyncio.Event()
            self.receiver_task: asyncio.Task[Any] | None = None
            self.closed = False

        async def _receive(self, receiver: str) -> object:
            if source != receiver:
                await asyncio.Future()
                raise AssertionError("unreachable")
            self.receiver_started.set()
            await self.release_result.wait()
            self.receiver_task = asyncio.current_task()
            self.claim_ready.set()
            return result

        async def recv_audio(self) -> Any:
            return await self._receive("audio")

        async def next_event(self) -> Any:
            return await self._receive("app")

        async def recv_data_event(self) -> Any:
            return await self._receive("data")

        def discard_pending_input(self) -> None:
            return None

        def drain_audio_nowait(self) -> list[bytes]:
            return []

        def drain_app_events_nowait(self) -> list[dict[str, Any]]:
            return []

        def drain_data_events_nowait(self) -> list[str | bytes]:
            return []

        async def stop(self) -> None:
            self.closed = True

    state = BridgeState(BridgeConfig(bearer_token="test-token"), rpc=fake_rpc)
    session = RacingSession()
    resource = bridge_service._RetainedSpeechSession(
        session=session,  # type: ignore[arg-type]
        thread_id="race-thread",
        voice="cove",
        language="en-US",
    )
    handoff = await state.offer_speech_session(resource)
    claimed: list[bridge_service._RetainedSpeechSession | None] = []

    async def claim_offer() -> None:
        await session.claim_ready.wait()
        assert session.receiver_task is not None
        assert session.receiver_task.done()
        async with state.speech_session_lease(
            handoff_token=handoff["token"],
            voice="cove",
            language="en-US",
        ) as retained:
            claimed.append(retained)

    claim = asyncio.create_task(claim_offer())
    await session.receiver_started.wait()
    session.release_result.set()
    await asyncio.wait_for(claim, timeout=1)

    assert claimed == [None]
    assert resource.invalidated
    assert session.closed
    assert state._speech_session_offer is None
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1
    await state.close()


@pytest.mark.asyncio
async def test_speech_handoff_watchdog_rejects_mixed_active_transcript(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    await _request_speech_session_handoff(client)
    state = bridge_app[bridge_service.STATE_KEY]

    await fake_rpc.broadcast(
        {
            "method": "thread/realtime/itemAdded",
            "params": {
                "threadId": "thread-1",
                "item": {
                    "type": "handoff_request",
                    "input_transcript": "valid-input",
                    "active_transcript": [
                        {"role": "user", "text": "valid-input"},
                        {"role": "assistant", "text": "assistant-output"},
                    ],
                },
            },
        }
    )
    for _ in range(100):
        if state._speech_session_offer is None and fake_rpc.peers[0].closed:
            break
        await asyncio.sleep(0)

    assert state._speech_session_offer is None
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.parametrize(
    "event",
    [
        {"type": "turn.created", "turn": {"role": "assistant"}},
        {"type": "turn.done", "turn": {"role": "assistant"}},
        {"type": "output_transcript.added", "text": "private-output"},
    ],
)
def test_speech_handoff_data_validator_rejects_output_shapes(
    event: dict[str, Any],
) -> None:
    with pytest.raises(ProtocolError):
        bridge_service._validate_speech_handoff_data_event(json.dumps(event))


@pytest.mark.parametrize(
    "event",
    [
        {
            "method": "thread/realtime/transcript/done",
            "params": {
                "threadId": "thread-1",
                "role": "assistant",
                "text": "private-output",
            },
        },
        {
            "method": "item/tool/call",
            "params": {"threadId": "thread-1", "role": "user"},
        },
        {
            "method": "thread/realtime/unknown",
            "params": {"threadId": "thread-1"},
        },
        {
            "method": "thread/realtime/itemAdded",
            "params": {
                "threadId": "thread-1",
                "item": {
                    "type": "handoff_request",
                    "input_transcript": "valid-input",
                    "active_transcript": [
                        {"role": "user", "text": "valid-input"},
                        {"role": "assistant", "text": "assistant-output"},
                    ],
                },
            },
        },
    ],
)
def test_speech_handoff_app_validator_rejects_non_input_shapes(
    event: dict[str, Any],
) -> None:
    with pytest.raises(ProtocolError):
        bridge_service._validate_speech_handoff_app_event(event)


def test_speech_handoff_validators_accept_known_input_shapes() -> None:
    bridge_service._validate_speech_handoff_app_event(
        {
            "method": "thread/realtime/started",
            "params": {
                "threadId": "thread-1",
                "realtimeSessionId": "session-1",
                "version": "v3",
            },
        }
    )
    bridge_service._validate_speech_handoff_data_event(
        json.dumps(
            {
                "type": "turn.created",
                "turn": {"id": "input-turn", "role": "user"},
            }
        )
    )
    bridge_service._validate_speech_handoff_data_event(
        json.dumps(
            {
                "type": "turn.done",
                "turn": {"role": "user", "input_transcript": "private-input"},
            }
        )
    )


def test_speech_handoff_data_validator_accepts_correlated_input_turn_delta() -> None:
    boundary_state = bridge_service._SpeechHandoffBoundaryState()
    bridge_service._validate_speech_handoff_data_event(
        json.dumps(
            {
                "type": "turn.created",
                "turn": {"id": "input-turn", "role": "user"},
            }
        ),
        boundary_state=boundary_state,
    )
    bridge_service._validate_speech_handoff_data_event(
        json.dumps(
            {
                "type": "turn.delta",
                "turn_id": "input-turn",
                "delta": "known input",
            }
        ),
        boundary_state=boundary_state,
        known_input="the known input transcript",
    )


@pytest.mark.parametrize(
    ("event", "known_input", "use_state"),
    [
        (
            {
                "type": "turn.delta",
                "turn_id": "input-turn",
                "delta": "known input",
            },
            "the known input transcript",
            False,
        ),
        (
            {
                "type": "turn.delta",
                "turn_id": "other-turn",
                "delta": "known input",
            },
            "the known input transcript",
            True,
        ),
        (
            {
                "type": "turn.delta",
                "turn_id": "input-turn",
                "delta": "assistant output",
            },
            "the known input transcript",
            True,
        ),
    ],
)
def test_speech_handoff_data_validator_rejects_uncorrelated_turn_delta(
    event: dict[str, Any],
    known_input: str,
    use_state: bool,
) -> None:
    boundary_state = (
        bridge_service._SpeechHandoffBoundaryState(input_turn_id="input-turn")
        if use_state
        else None
    )
    with pytest.raises(ProtocolError):
        bridge_service._validate_speech_handoff_data_event(
            json.dumps(event),
            boundary_state=boundary_state,
            known_input=known_input,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"type": "turn.done", "turn": {"role": "assistant"}},
        {"type": "output_transcript.added", "text": "private-output"},
    ],
)
async def test_speech_handoff_watchdog_invalid_data_forces_cold_fallback(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    event: dict[str, Any],
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]
    state = bridge_app[bridge_service.STATE_KEY]

    fake_rpc.peers[0].data.put_nowait(json.dumps(event))
    for _ in range(100):
        if state._speech_session_offer is None and fake_rpc.peers[0].closed:
            break
        await asyncio.sleep(0)

    assert state._speech_session_offer is None
    assert fake_rpc.peers[0].closed
    payload = _synthesis_payload()
    payload["speech_session_handoff_token"] = token
    response = await client.post("/v1/synthesize", headers=AUTH, json=payload)

    assert response.status == 200
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 2
    assert not any(
        method == "thread/realtime/appendSpeech" for method, _ in fake_rpc.calls
    )
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 2


@pytest.mark.asyncio
async def test_speech_session_handoff_claim_rechecks_watchdog_invalidation(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]
    state = bridge_app[bridge_service.STATE_KEY]
    original_cancel = bridge_service._cancel_speech_offer_watchdog

    async def invalidate_after_claim(
        offer: bridge_service._SpeechSessionOffer,
    ) -> None:
        assert state._speech_session_offer is None
        offer.resource.invalidated = True
        await original_cancel(offer)

    monkeypatch.setattr(
        bridge_service,
        "_cancel_speech_offer_watchdog",
        invalidate_after_claim,
    )
    payload = _synthesis_payload()
    payload["speech_session_handoff_token"] = token

    response = await client.post("/v1/synthesize", headers=AUTH, json=payload)

    assert response.status == 200
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 2
    assert not any(
        method == "thread/realtime/appendSpeech" for method, _ in fake_rpc.calls
    )
    assert all(peer.closed for peer in fake_rpc.peers)
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 2


@pytest.mark.asyncio
async def test_speech_session_handoff_rejects_pre_offer_assistant_audio(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    fake_rpc.transcript_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/transcribe/stream", headers=AUTH)
    await websocket.send_json(
        _transcription_stream_start(
            language="en-US",
            speech_session_handoff={
                "version": 1,
                "voice": "cove",
                "language": "en-US",
            },
        )
    )
    assert (await websocket.receive_json())["type"] == "started"
    await websocket.send_bytes(b"\x00\x20" * 160)
    await websocket.send_json({"type": "end"})
    await asyncio.wait_for(fake_rpc.transcript_started.wait(), timeout=1)
    fake_rpc.peers[0].audio.put_nowait(b"\x01\x00")
    fake_rpc.transcript_gate.set()

    result = await websocket.receive_json(timeout=1)

    assert result["type"] == "result"
    assert "speech_session_handoff" not in result
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_warm_synthesis_failure_before_audio_retries_cold_once(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]
    fake_rpc.handoff_append_error = ProtocolError("private warm failure")
    payload = _synthesis_payload()
    payload["speech_session_handoff_token"] = token

    response = await client.post("/v1/synthesize", headers=AUTH, json=payload)

    assert response.status == 200
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 2
    assert (
        sum(method == "thread/realtime/appendSpeech" for method, _ in fake_rpc.calls)
        == 1
    )
    assert (
        sum(
            method == "thread/realtime/appendText"
            and str(params.get("text", "")).startswith("Vocalize only")
            for method, params in fake_rpc.calls
        )
        == 1
    )
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 2


@pytest.mark.asyncio
async def test_late_stt_audio_during_warm_append_forces_cold_fallback(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]
    fake_rpc.synthesis_append_gate = asyncio.Event()
    payload = _synthesis_payload()
    payload["speech_session_handoff_token"] = token

    synthesis = asyncio.create_task(
        client.post("/v1/synthesize", headers=AUTH, json=payload)
    )
    await asyncio.wait_for(fake_rpc.synthesis_append_started.wait(), timeout=1)
    fake_rpc.peers[0].audio.put_nowait(b"\x55\x00" * 48)
    fake_rpc.synthesis_append_gate.set()
    response = await asyncio.wait_for(synthesis, timeout=2)

    assert response.status == 200
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 2
    assert (
        sum(method == "thread/realtime/appendSpeech" for method, _ in fake_rpc.calls)
        == 1
    )
    assert (
        sum(
            method == "thread/realtime/appendText"
            and str(params.get("text", "")).startswith("Vocalize only")
            for method, params in fake_rpc.calls
        )
        == 1
    )
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 2


@pytest.mark.asyncio
async def test_warm_synthesis_failure_after_pcm_does_not_retry(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]

    async def fail_after_pcm(
        session: Any,
        _timeout: float,
        *,
        timing: Any = None,
        async_handle_chunk: Any = None,
    ) -> bytes:
        chunk = await session.recv_audio()
        assert chunk
        if async_handle_chunk is not None:
            await async_handle_chunk(chunk)
        if timing is not None:
            timing.first_audio_at = time.monotonic()
            timing.last_audio_at = timing.first_audio_at
        raise ProtocolError("failure after private PCM")

    monkeypatch.setattr(bridge_service, "_collect_speech_audio", fail_after_pcm)
    payload = _synthesis_payload()
    payload["speech_session_handoff_token"] = token

    response = await client.post("/v1/synthesize", headers=AUTH, json=payload)

    assert response.status == 400
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 1
    assert (
        sum(method == "thread/realtime/appendSpeech" for method, _ in fake_rpc.calls)
        == 1
    )
    assert not any(
        method == "thread/realtime/appendText"
        and str(params.get("text", "")).startswith("Vocalize only")
        for method, params in fake_rpc.calls
    )
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_speech_session_release_cleanup_delays_new_admission(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    transcription = await _request_speech_session_handoff(client)
    token = transcription["speech_session_handoff"]["token"]
    fake_rpc.realtime_stop_gate = asyncio.Event()
    releasing = asyncio.create_task(
        client.post(
            "/v1/speech-session/release",
            headers=AUTH,
            json={"speech_session_handoff_token": token},
        )
    )
    await asyncio.wait_for(fake_rpc.realtime_stop_started.wait(), timeout=1)

    contender = asyncio.create_task(
        client.post("/v1/synthesize", headers=AUTH, json=_synthesis_payload())
    )

    await asyncio.sleep(0)
    assert not contender.done()
    fake_rpc.realtime_stop_gate.set()
    released = await asyncio.wait_for(releasing, timeout=1)
    assert released.status == 204
    response = await asyncio.wait_for(contender, timeout=2)
    assert response.status == 200


@pytest.mark.asyncio
async def test_bridge_shutdown_disposes_speech_session_offer(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    await _request_speech_session_handoff(client)
    state = bridge_app[bridge_service.STATE_KEY]
    assert state._speech_session_offer is not None

    await state.close()

    assert state._speech_session_offer is None
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_bridge_shutdown_keeps_authoritative_cleanup_running(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    client = await aiohttp_client(bridge_app)
    await _request_speech_session_handoff(client)
    state = bridge_app[bridge_service.STATE_KEY]
    fake_rpc.realtime_stop_gate = asyncio.Event()

    shutdown = asyncio.create_task(state.close())
    await asyncio.wait_for(fake_rpc.realtime_stop_started.wait(), timeout=1)
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert state._close_task is not None
    assert not state._close_task.done()
    assert state._speech_cleanup_tasks
    fake_rpc.realtime_stop_gate.set()
    await asyncio.wait_for(asyncio.shield(state._close_task), timeout=1)

    assert not state._speech_cleanup_tasks
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1
    assert not fake_rpc.running


@pytest.mark.asyncio
async def test_v1_realtime_never_offers_speech_session_handoff(
    aiohttp_client: Any,
    fake_rpc: FakeRpc,
) -> None:
    app = create_app(
        BridgeConfig(bearer_token="test-token", realtime_version="v1"),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )
    client = await aiohttp_client(app)

    result = await _request_speech_session_handoff(client)

    assert result["type"] == "result"
    assert "speech_session_handoff" not in result
    assert fake_rpc.peers[0].closed
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_active_transcription_rejects_synthesis_and_realtime_without_queueing(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    """STT owns the shared channel until cleanup and then permits a retry."""
    fake_rpc.transcript_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    active = asyncio.create_task(
        client.post("/v1/transcribe", headers=AUTH, json=_transcription_payload())
    )
    await asyncio.wait_for(fake_rpc.transcript_started.wait(), timeout=1)

    synthesis = await asyncio.wait_for(
        client.post("/v1/synthesize", headers=AUTH, json=_synthesis_payload()),
        timeout=1,
    )
    realtime = await asyncio.wait_for(
        client.get("/v1/realtime", headers=AUTH), timeout=1
    )
    await _assert_busy(synthesis)
    await _assert_busy(realtime)

    fake_rpc.transcript_gate.set()
    transcription = await asyncio.wait_for(active, timeout=2)
    assert transcription.status == 200
    retry = await asyncio.wait_for(
        client.post("/v1/synthesize", headers=AUTH, json=_synthesis_payload()),
        timeout=2,
    )
    assert retry.status == 200


@pytest.mark.asyncio
async def test_active_synthesis_rejects_transcription_without_queueing(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    """TTS contention returns immediately and releases after successful cleanup."""
    fake_rpc.synthesis_append_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    active = asyncio.create_task(
        client.post("/v1/synthesize", headers=AUTH, json=_synthesis_payload())
    )
    await asyncio.wait_for(fake_rpc.synthesis_append_started.wait(), timeout=1)

    transcription = await asyncio.wait_for(
        client.post("/v1/transcribe", headers=AUTH, json=_transcription_payload()),
        timeout=1,
    )
    await _assert_busy(transcription)

    fake_rpc.synthesis_append_gate.set()
    synthesis = await asyncio.wait_for(active, timeout=2)
    assert synthesis.status == 200
    retry = await asyncio.wait_for(
        client.post("/v1/transcribe", headers=AUTH, json=_transcription_payload()),
        timeout=2,
    )
    assert retry.status == 200


@pytest.mark.asyncio
async def test_active_realtime_rejects_all_speech_admissions_then_releases(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    """A websocket holds the shared channel only for its complete lifetime."""
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/realtime", headers=AUTH)
    await websocket.send_json({"type": "start", "conversation_id": "exclusive-live"})
    assert (await websocket.receive_json())["type"] == "started"

    transcription = await asyncio.wait_for(
        client.post("/v1/transcribe", headers=AUTH, json=_transcription_payload()),
        timeout=1,
    )
    synthesis = await asyncio.wait_for(
        client.post("/v1/synthesize", headers=AUTH, json=_synthesis_payload()),
        timeout=1,
    )
    second_realtime = await asyncio.wait_for(
        client.get("/v1/realtime", headers=AUTH), timeout=1
    )
    await _assert_busy(transcription)
    await _assert_busy(synthesis)
    await _assert_busy(second_realtime)

    await websocket.send_json({"type": "stop"})
    await websocket.close()
    for _ in range(50):
        if not bridge_app[bridge_service.STATE_KEY]._speech_session_active:
            break
        await asyncio.sleep(0)
    assert not bridge_app[bridge_service.STATE_KEY]._speech_session_active

    retry = await asyncio.wait_for(
        client.post("/v1/synthesize", headers=AUTH, json=_synthesis_payload()),
        timeout=2,
    )
    assert retry.status == 200


@pytest.mark.asyncio
async def test_transcribe_rejects_audio_beyond_endpoint_deadline(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "MAX_TRANSCRIPTION_DURATION_SECONDS", 0.001)
    client = await aiohttp_client(bridge_app)
    pcm = b"\x01\x00" * 48
    response = await client.post(
        "/v1/transcribe",
        headers=AUTH,
        json={
            "audio": base64.b64encode(pcm).decode(),
            "format": "pcm",
            "sample_rate": 24_000,
            "channels": 1,
        },
    )

    assert response.status == 400
    assert "must not exceed" in (await response.json())["error"]
    assert not any(method == "thread/start" for method, _ in fake_rpc.calls)

    retry = await client.post("/v1/synthesize", headers=AUTH, json=_synthesis_payload())
    assert retry.status == 200


@pytest.mark.asyncio
async def test_transcribe_trims_silence_and_recomputes_feed_duration(
    aiohttp_client: Any,
    bridge_app: web.Application,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured_pcm = b""
    captured_duration = 0.0

    async def capture_attempt(
        _state: BridgeState,
        _payload: Mapping[str, Any],
        pcm: bytes,
        duration: float,
        _prompt: str,
    ) -> str:
        nonlocal captured_duration, captured_pcm
        captured_pcm = pcm
        captured_duration = duration
        return "Synthetic transcript"

    monkeypatch.setattr(bridge_service, "_run_transcription_attempt", capture_attempt)
    client = await aiohttp_client(bridge_app)
    silence = array("h", [0]) * (10 * bridge_service.REALTIME_SAMPLE_RATE)
    speech = array("h", [12_000, -12_000]) * (bridge_service.REALTIME_SAMPLE_RATE // 4)
    pcm = (silence + speech).tobytes()
    with caplog.at_level(logging.INFO, logger="bridge.service"):
        response = await client.post(
            "/v1/transcribe",
            headers=AUTH,
            json={
                "audio": base64.b64encode(pcm).decode(),
                "format": "pcm",
                "sample_rate": 24_000,
                "channels": 1,
            },
        )

    assert response.status == 200
    assert await response.json() == {"text": "Synthetic transcript"}
    assert captured_duration == pytest.approx(
        len(captured_pcm) / (bridge_service.REALTIME_SAMPLE_RATE * 2)
    )
    assert captured_duration == pytest.approx(0.82)
    assert "input_duration_seconds=10.500 trimmed_duration_seconds=0.820" in (
        caplog.text
    )


@pytest.mark.asyncio
async def test_transcribe_fragment_finalization_uses_meaningful_pcm_end(
    aiohttp_client: Any,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_deadline: float | None = None
    captured_timeout: float | None = None
    wait_called_at: float | None = None

    async def capture_finalization(
        _session: Any,
        timeout: float,
        *,
        fragment_finalization_at: float | None = None,
    ) -> str:
        nonlocal captured_deadline, captured_timeout, wait_called_at
        captured_deadline = fragment_finalization_at
        captured_timeout = timeout
        wait_called_at = asyncio.get_running_loop().time()
        return "Synthetic transcript"

    monkeypatch.setattr(
        bridge_service, "_wait_for_user_transcript", capture_finalization
    )
    app = create_app(
        BridgeConfig(bearer_token="test-token", silence_ms=1_000),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )
    client = await aiohttp_client(app)
    pcm = b"\x00\x40" * 2_400
    duration = len(pcm) / (bridge_service.REALTIME_SAMPLE_RATE * 2)

    response = await client.post(
        "/v1/transcribe",
        headers=AUTH,
        json={
            "audio": base64.b64encode(pcm).decode(),
            "format": "pcm",
            "sample_rate": 24_000,
            "channels": 1,
        },
    )

    assert response.status == 200
    assert captured_deadline is not None
    assert captured_timeout == pytest.approx(
        duration + 1.0 + bridge_service.TRANSCRIPTION_RESULT_TIMEOUT_SECONDS
    )
    assert wait_called_at is not None
    assert captured_deadline - wait_called_at == pytest.approx(duration, abs=0.02)
    assert len(fake_rpc.peers[-1].fed) == len(pcm) + len(
        bridge_service.silence_pcm16(1_000)
    )


@pytest.mark.asyncio
async def test_transcribe_timeout_logs_only_normalized_audio_diagnostics(
    aiohttp_client: Any,
    bridge_app: web.Application,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def timeout_transcript(*_: Any, **__: Any) -> str:
        raise TimeoutError

    monkeypatch.setattr(bridge_service, "_wait_for_user_transcript", timeout_transcript)
    client = await aiohttp_client(bridge_app)
    pcm = b"\x00\x40" * 240
    with caplog.at_level(logging.WARNING, logger="bridge.service"):
        response = await client.post(
            "/v1/transcribe",
            headers=AUTH,
            json={
                "audio": base64.b64encode(pcm).decode(),
                "format": "pcm",
                "sample_rate": 24_000,
                "channels": 1,
            },
        )

    assert response.status == 504
    assert await response.json() == {"error": "Codex operation timed out"}
    assert caplog.text.count("Realtime transcription attempt timed out") == (
        bridge_service.TRANSCRIPTION_MAX_ATTEMPTS
    )
    assert "stage=transcript normalized_duration_seconds=0.010" in caplog.text
    assert "normalized_peak=0.5000 normalized_rms=0.5000" in caplog.text


@pytest.mark.asyncio
async def test_transcribe_retries_after_terminal_event_timeout(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0
    private_transcript = "Private recovered transcript"

    async def flaky_transcript(*_: Any, **__: Any) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError
        return private_transcript

    monkeypatch.setattr(bridge_service, "_wait_for_user_transcript", flaky_transcript)
    client = await aiohttp_client(bridge_app)
    payload = _transcription_payload()
    payload["prompt"] = "private-vocabulary-marker"
    with caplog.at_level(logging.INFO, logger="bridge.service"):
        response = await client.post("/v1/transcribe", headers=AUTH, json=payload)

    assert response.status == 200
    assert await response.json() == {"text": private_transcript}
    assert attempts == 2
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 2
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 2
    assert [
        method
        for method, _ in fake_rpc.calls
        if method in {"thread/start", "thread/delete"}
    ] == ["thread/start", "thread/delete", "thread/start", "thread/delete"]
    timing_pattern = re.compile(
        r"Realtime transcription attempt timing: "
        r"thread_start_seconds=(\d+\.\d{3}) "
        r"realtime_handshake_seconds=(\d+\.\d{3}) "
        r"transcript_wait_seconds=(\d+\.\d{3}) "
        r"session_stop_peer_close_seconds=(\d+\.\d{3}) "
        r"thread_delete_seconds=(\d+\.\d{3}) total_seconds=(\d+\.\d{3})"
    )
    timing_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "bridge.service"
        and record.getMessage().startswith("Realtime transcription attempt timing:")
    ]
    assert len(timing_messages) == 2
    for message in timing_messages:
        match = timing_pattern.fullmatch(message)
        assert match is not None
        assert all(float(value) >= 0 for value in match.groups())
    service_log = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "bridge.service"
    )
    for private_value in (
        private_transcript,
        payload["audio"],
        payload["prompt"],
        "fake-offer",
        "fake-answer",
        "thread-1",
        "thread-2",
        "test-token",
    ):
        assert private_value not in service_log


@pytest.mark.asyncio
async def test_transcribe_does_not_wait_for_stalled_input_drain(
    aiohttp_client: Any,
    bridge_app: web.Application,
    fake_rpc: FakeRpc,
) -> None:
    fake_rpc.input_drain_gate = asyncio.Event()
    fake_rpc.transcript_gate = asyncio.Event()
    client = await aiohttp_client(bridge_app)
    pending = asyncio.create_task(
        client.post("/v1/transcribe", headers=AUTH, json=_transcription_payload())
    )
    await asyncio.wait_for(fake_rpc.input_drain_started.wait(), timeout=1)
    fake_rpc.transcript_gate.set()
    response = await asyncio.wait_for(pending, timeout=1)

    assert response.status == 200
    assert await response.json() == {"text": "Turn on the kitchen"}
    assert not fake_rpc.input_drain_gate.is_set()
    assert sum(method == "thread/start" for method, _ in fake_rpc.calls) == 1
    assert sum(method == "thread/delete" for method, _ in fake_rpc.calls) == 1


@pytest.mark.asyncio
async def test_transcribe_does_not_retry_ambiguous_thread_start_timeout(
    aiohttp_client: Any,
    bridge_app: web.Application,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = bridge_app[bridge_service.STATE_KEY]
    attempts = 0

    async def timeout_start(*_: Any, **__: Any) -> str:
        nonlocal attempts
        attempts += 1
        raise TimeoutError

    monkeypatch.setattr(state, "start_thread", timeout_start)
    client = await aiohttp_client(bridge_app)
    with caplog.at_level(logging.WARNING, logger="bridge.service"):
        response = await client.post(
            "/v1/transcribe", headers=AUTH, json=_transcription_payload()
        )

    assert response.status == 504
    assert attempts == 1
    assert (
        f"attempt=1/{bridge_service.TRANSCRIPTION_MAX_ATTEMPTS} stage=thread_start"
        in caplog.text
    )
    assert "reached its total deadline" not in caplog.text


@pytest.mark.asyncio
async def test_transcribe_accepts_component_metadata_shape(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    client = await aiohttp_client(bridge_app)
    pcm = b"\x01\x00\x03\x00" * 80
    response = await client.post(
        "/v1/transcribe",
        headers=AUTH,
        json={
            "audio": base64.b64encode(pcm).decode(),
            "format": "pcm",
            "metadata": {
                "language": "en-US",
                "codec": "pcm",
                "sample_rate": 16_000,
                "bit_rate": 16,
                "channels": 1,
            },
            "prompt": "The speaker may say Aurelio",
        },
    )
    assert response.status == 200
    assert await response.json() == {"text": "Turn on the kitchen", "language": "en-US"}
    assert len(fake_rpc.peers[-1].fed) > len(pcm)
    realtime_start = next(
        params for method, params in fake_rpc.calls if method == "thread/realtime/start"
    )
    assert realtime_start["version"] == "v3"
    assert realtime_start["transport"]["type"] == "webrtc"
    assert "Aurelio" in realtime_start["prompt"]
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls


@pytest.mark.asyncio
async def test_synthesize_returns_best_effort_wav(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    client = await aiohttp_client(bridge_app)
    response = await client.post(
        "/v1/synthesize",
        headers=AUTH,
        json={
            "text": "Welcome home",
            "language": "en-US",
            "voice": "cove",
            "format": "wav",
            "instructions": "Use a calm, quiet delivery",
        },
    )
    assert response.status == 200
    assert response.headers["X-Codex-Synthesis-Mode"] == "conversational-best-effort"
    with wave.open(BytesIO(await response.read()), "rb") as audio:
        assert audio.getframerate() == 24_000
        assert audio.getnchannels() == 1
        assert audio.readframes(audio.getnframes())
    realtime_start = next(
        params for method, params in fake_rpc.calls if method == "thread/realtime/start"
    )
    assert "calm, quiet" in realtime_start["prompt"]
    synthesis_turn = next(
        params
        for method, params in fake_rpc.calls
        if method == "thread/realtime/appendText"
        and str(params.get("text", "")).startswith("Vocalize only")
    )
    assert synthesis_turn["role"] == "user"
    assert "Welcome home" in synthesis_turn["text"]
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls


@pytest.mark.asyncio
async def test_synthesize_stream_yields_first_pcm_before_cleanup(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    client = await aiohttp_client(bridge_app)
    response = await client.post(
        "/v1/synthesize/stream",
        headers=AUTH,
        json=_synthesis_payload(),
    )

    assert response.status == 200
    assert response.content_type == "audio/wav"
    first_audio = await response.content.readexactly(44 + 960)
    assert not any(method == "thread/delete" for method, _ in fake_rpc.calls)

    complete_audio = first_audio + await response.read()
    with wave.open(BytesIO(complete_audio), "rb") as audio:
        assert audio.getframerate() == 24_000
        assert audio.getnchannels() == 1
        assert audio.getnframes() == 0xFFFFFFFF // 2
        assert audio.readframes(audio.getnframes()) == b"\x01\x00" * 480
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls


@pytest.mark.asyncio
async def test_synthesize_stream_can_return_error_before_first_pcm(
    aiohttp_client: Any,
    bridge_app: web.Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_collection(*_: Any, **__: Any) -> bytes:
        raise TimeoutError

    monkeypatch.setattr(bridge_service, "_collect_speech_audio", fail_collection)
    client = await aiohttp_client(bridge_app)

    response = await client.post(
        "/v1/synthesize/stream", headers=AUTH, json=_synthesis_payload()
    )

    assert response.status == 504
    assert response.content_type == "application/json"


@pytest.mark.asyncio
async def test_synthesize_logs_privacy_safe_numeric_timing(
    aiohttp_client: Any,
    bridge_app: web.Application,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = await aiohttp_client(bridge_app)
    private_values = (
        "private-spoken-text-marker",
        "private-language-marker",
        "private-voice-marker",
        "private-instructions-marker",
        "test-token",
        "thread-1",
        "fake-offer",
        "fake-answer",
    )
    with caplog.at_level(logging.INFO, logger="bridge.service"):
        response = await client.post(
            "/v1/synthesize",
            headers=AUTH,
            json={
                "text": private_values[0],
                "language": private_values[1],
                "voice": private_values[2],
                "format": "wav",
                "instructions": private_values[3],
            },
        )

    assert response.status == 200
    prefix = "Realtime synthesis attempt timing: "
    timing_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "bridge.service" and record.getMessage().startswith(prefix)
    ]
    assert len(timing_messages) == 1
    expected_fields = [
        "thread_start_seconds",
        "realtime_handshake_seconds",
        "append_text_rpc_seconds",
        "append_to_first_audio_seconds",
        "audio_collection_seconds",
        "last_audio_to_collection_end_seconds",
        "completion_to_collection_end_seconds",
        "session_stop_peer_close_seconds",
        "thread_delete_seconds",
        "total_to_response_ready_seconds",
    ]
    fields = timing_messages[0].removeprefix(prefix).split()
    assert [field.partition("=")[0] for field in fields] == expected_fields
    for field in fields:
        _, separator, numeric_value = field.partition("=")
        assert separator == "="
        assert re.fullmatch(r"\d+\.\d{3}", numeric_value)
        assert float(numeric_value) >= 0

    service_log = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "bridge.service"
    )
    for private_value in private_values:
        assert private_value not in service_log


@pytest.mark.asyncio
async def test_synthesize_logs_one_timing_summary_on_failure(
    aiohttp_client: Any,
    bridge_app: web.Application,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_collection(*_: Any, **__: Any) -> bytes:
        raise TimeoutError

    monkeypatch.setattr(bridge_service, "_collect_speech_audio", fail_collection)
    client = await aiohttp_client(bridge_app)
    prefix = "Realtime synthesis attempt timing: "
    with caplog.at_level(logging.INFO, logger="bridge.service"):
        response = await client.post(
            "/v1/synthesize", headers=AUTH, json=_synthesis_payload()
        )

    assert response.status == 504
    assert (
        sum(
            record.name == "bridge.service" and record.getMessage().startswith(prefix)
            for record in caplog.records
        )
        == 1
    )


@pytest.mark.asyncio
async def test_synthesize_rejects_unbounded_text(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    client = await aiohttp_client(bridge_app)
    response = await client.post(
        "/v1/synthesize",
        headers=AUTH,
        json={"text": "x" * 8_001, "format": "wav"},
    )

    assert response.status == 400
    assert "must not exceed" in (await response.json())["error"]
    assert not any(method == "thread/start" for method, _ in fake_rpc.calls)


@pytest.mark.asyncio
async def test_realtime_proxies_text_audio_and_stop(
    aiohttp_client: Any, bridge_app: web.Application, fake_rpc: FakeRpc
) -> None:
    client = await aiohttp_client(bridge_app)
    websocket = await client.ws_connect("/v1/realtime", headers=AUTH)
    await websocket.send_json({"type": "start", "conversation_id": "live-1"})
    started = await websocket.receive_json()
    assert started["type"] == "started"
    await websocket.send_json({"type": "text", "text": "Hello", "role": "user"})
    await websocket.send_json(
        {
            "type": "audio",
            "audio": base64.b64encode(b"\x00\x00" * 480).decode(),
            "sample_rate": 24_000,
            "channels": 1,
        }
    )
    fake_rpc.peers[-1].audio.put_nowait(b"\x02\x00" * 480)
    messages = [await websocket.receive_json(), await websocket.receive_json()]
    assert {message["type"] for message in messages} == {"audio", "transcript_done"}
    await websocket.send_json({"type": "stop"})
    await websocket.close()
    assert any(method == "thread/realtime/appendText" for method, _ in fake_rpc.calls)
    for _ in range(20):
        if any(method == "thread/delete" for method, _ in fake_rpc.calls):
            break
        await asyncio.sleep(0)
    assert (
        "thread/delete",
        {"threadId": "thread-1"},
    ) in fake_rpc.calls


@pytest.mark.asyncio
async def test_synthesis_collector_stops_after_terminal_event_with_continuous_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous remote silence cannot keep a completed synthesis alive."""
    monkeypatch.setattr(bridge_service, "SYNTHESIS_TAIL_GRACE_SECONDS", 0.03)
    session = FakeCollectorSession()

    async def produce_audio() -> None:
        while True:
            await session.audio.put(b"\x01\x00" * 24)
            await asyncio.sleep(0.002)

    producer = asyncio.create_task(produce_audio())
    collection = asyncio.create_task(bridge_service._collect_speech_audio(session, 1.0))
    try:
        await asyncio.sleep(0.01)
        session.data.put_nowait(json.dumps({"type": "turn.done"}))
        result = await asyncio.wait_for(collection, timeout=0.25)
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)

    assert result


@pytest.mark.asyncio
async def test_synthesis_collector_does_not_truncate_after_transcript_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript boundary is not treated as the end of audio playout."""
    monkeypatch.setattr(bridge_service, "SYNTHESIS_TAIL_GRACE_SECONDS", 0.03)
    session = FakeCollectorSession()
    marker = b"late-audio-marker"
    session.events.put_nowait(
        {
            "method": "thread/realtime/transcript/done",
            "params": {"role": "assistant", "text": "Rendered text"},
        }
    )

    async def produce_tail() -> None:
        for index in range(8):
            await session.audio.put(marker if index == 6 else b"\x02\x00" * 24)
            await asyncio.sleep(0.01)
        await session.data.put(json.dumps({"type": "turn.done"}))
        for _ in range(4):
            await session.audio.put(b"\x00\x00" * 24)
            await asyncio.sleep(0.01)

    producer = asyncio.create_task(produce_tail())
    try:
        result = await asyncio.wait_for(
            bridge_service._collect_speech_audio(session, 1.0), timeout=0.4
        )
    finally:
        await producer

    assert marker in result


@pytest.mark.asyncio
async def test_synthesis_collector_ignores_stale_completion_before_first_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "SYNTHESIS_TAIL_GRACE_SECONDS", 0.01)
    session = FakeCollectorSession()
    session.data.put_nowait(json.dumps({"type": "turn.done"}))
    collection = asyncio.create_task(bridge_service._collect_speech_audio(session, 1.0))

    await asyncio.sleep(0.03)
    assert not collection.done()
    session.audio.put_nowait(b"\x01\x00" * 24)
    await asyncio.sleep(0.01)
    session.data.put_nowait(json.dumps({"type": "turn.done"}))

    assert await asyncio.wait_for(collection, timeout=0.2)


@pytest.mark.asyncio
async def test_synthesis_collector_ignores_stale_turn_across_natural_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old STT turn cannot apply the short completed-turn idle cutoff."""
    monkeypatch.setattr(bridge_service, "SYNTHESIS_TAIL_GRACE_SECONDS", 0.01)
    session = FakeCollectorSession()
    marker = b"post-pause-audio"
    session.data.put_nowait(json.dumps({"type": "turn.done"}))
    session.audio.put_nowait(b"\x01\x00" * 24)
    collection = asyncio.create_task(bridge_service._collect_speech_audio(session, 2.0))

    await asyncio.sleep(0.7)
    assert not collection.done()
    session.audio.put_nowait(marker)
    session.data.put_nowait(json.dumps({"type": "turn.done"}))

    result = await asyncio.wait_for(collection, timeout=0.2)
    assert marker in result


@pytest.mark.asyncio
async def test_transcription_uses_v3_data_channel_final() -> None:
    """STT remains reliable when app-server omits its transcript notification."""
    session = FakeCollectorSession()
    session.data.put_nowait(
        json.dumps(
            {
                "type": "input_transcript.added",
                "item": {"type": "input_transcript", "text": "The front "},
            }
        )
    )
    session.data.put_nowait(
        json.dumps(
            {
                "type": "turn.done",
                "turn": {
                    "role": "user",
                    "transcript": "The front door is locked.",
                },
            }
        )
    )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "The front door is locked."


@pytest.mark.asyncio
async def test_handoff_transcription_rejects_simultaneous_assistant_data() -> None:
    """A terminal user transcript cannot hide a ready unsafe sibling event."""
    session = FakeCollectorSession()
    session.events.put_nowait(
        {
            "method": "thread/realtime/transcript/done",
            "params": {
                "threadId": "thread-1",
                "role": "user",
                "text": "Turn on the kitchen.",
            },
        }
    )
    session.data.put_nowait(
        json.dumps(
            {"type": "turn.done", "turn": {"role": "assistant", "text": "unsafe"}}
        )
    )

    with pytest.raises(ProtocolError):
        await asyncio.wait_for(
            bridge_service._wait_for_user_transcript(
                session, 1.0, strict_handoff_boundary=True
            ),
            timeout=0.2,
        )


@pytest.mark.asyncio
async def test_handoff_transcription_preserves_stt_when_reuse_is_invalidated() -> None:
    """Unsafe output disables reuse without discarding the valid STT result."""
    session = FakeCollectorSession()
    session.events.put_nowait(
        {
            "method": "thread/realtime/transcript/done",
            "params": {
                "threadId": "thread-1",
                "role": "user",
                "text": "Turn on the kitchen.",
            },
        }
    )
    session.data.put_nowait(
        json.dumps(
            {"type": "turn.done", "turn": {"role": "assistant", "text": "unsafe"}}
        )
    )
    boundary_state = bridge_service._SpeechHandoffBoundaryState()

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(
            session,
            1.0,
            strict_handoff_boundary=True,
            handoff_boundary_state=boundary_state,
        ),
        timeout=0.2,
    )

    assert transcript == "Turn on the kitchen."
    assert boundary_state.invalidated


@pytest.mark.asyncio
async def test_transcription_does_not_duplicate_normalized_and_raw_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "TRANSCRIPTION_FRAGMENT_QUIET_SECONDS", 0.01)
    session = FakeCollectorSession()
    for fragment in ("Open ", "the blinds."):
        session.events.put_nowait(
            {
                "method": "thread/realtime/transcript/delta",
                "params": {"role": "user", "delta": fragment},
            }
        )
        session.data.put_nowait(
            json.dumps(
                {
                    "type": "input_transcript.added",
                    "item": {"type": "input_transcript", "text": fragment},
                }
            )
        )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Open the blinds."


@pytest.mark.asyncio
async def test_transcription_replaces_replayed_raw_fragment_by_item_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "TRANSCRIPTION_FRAGMENT_QUIET_SECONDS", 0.01)
    session = FakeCollectorSession()
    for fragment in ("Turn", "Turn on the lights."):
        session.events.put_nowait(
            {
                "method": "thread/realtime/transcript/delta",
                "params": {"role": "user", "delta": fragment},
            }
        )
        session.data.put_nowait(
            json.dumps(
                {
                    "type": "input_transcript.added",
                    "item": {
                        "id": "provisional-item",
                        "type": "input_transcript",
                        "text": fragment,
                    },
                }
            )
        )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Turn on the lights."


@pytest.mark.asyncio
async def test_transcription_finalizes_nested_v3_fragments_after_quiet_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite STT audio does not need a terminal event after transcript chunks."""
    monkeypatch.setattr(bridge_service, "TRANSCRIPTION_FRAGMENT_QUIET_SECONDS", 0.01)
    session = FakeCollectorSession()
    for item_id, fragment in (
        ("fragment-1", "The front "),
        ("fragment-2", "door is locked."),
    ):
        session.data.put_nowait(
            json.dumps(
                {
                    "type": "input_transcript.added",
                    "item": {
                        "id": item_id,
                        "type": "input_transcript",
                        "text": fragment,
                    },
                }
            )
        )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "The front door is locked."


@pytest.mark.asyncio
async def test_transcription_waits_for_expected_audio_end_before_fragment_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "TRANSCRIPTION_FRAGMENT_QUIET_SECONDS", 0.001)
    session = FakeCollectorSession()
    session.data.put_nowait(
        json.dumps(
            {
                "type": "input_transcript.added",
                "item": {
                    "id": "fragment-1",
                    "type": "input_transcript",
                    "text": "Open the curtains.",
                },
            }
        )
    )
    loop = asyncio.get_running_loop()
    expected_audio_end = loop.time() + 0.1
    pending = asyncio.create_task(
        bridge_service._wait_for_user_transcript(
            session,
            1.0,
            fragment_finalization_at=expected_audio_end,
        )
    )

    await asyncio.sleep(0.01)
    assert not pending.done()
    transcript = await asyncio.wait_for(pending, timeout=0.3)

    assert transcript == "Open the curtains."
    assert loop.time() >= expected_audio_end


@pytest.mark.asyncio
async def test_transcription_does_not_return_partial_fragment_at_earlier_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "TRANSCRIPTION_FRAGMENT_QUIET_SECONDS", 0.001)
    session = FakeCollectorSession()
    session.data.put_nowait(
        json.dumps(
            {
                "type": "input_transcript.added",
                "item": {
                    "id": "fragment-1",
                    "type": "input_transcript",
                    "text": "Possibly partial",
                },
            }
        )
    )
    expected_audio_end = asyncio.get_running_loop().time() + 0.2

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            bridge_service._wait_for_user_transcript(
                session,
                0.02,
                fragment_finalization_at=expected_audio_end,
            ),
            timeout=0.2,
        )


@pytest.mark.asyncio
async def test_transcription_finalizes_app_server_deltas_after_quiet_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "TRANSCRIPTION_FRAGMENT_QUIET_SECONDS", 0.01)
    session = FakeCollectorSession()
    session.events.put_nowait(
        {
            "method": "thread/realtime/transcript/delta",
            "params": {"role": "user", "delta": "Turn on "},
        }
    )
    session.events.put_nowait(
        {
            "method": "thread/realtime/transcript/delta",
            "params": {"role": "user", "delta": "the kitchen."},
        }
    )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Turn on the kitchen."


@pytest.mark.asyncio
async def test_transcription_uses_handoff_item_as_terminal_transcript() -> None:
    session = FakeCollectorSession()
    session.events.put_nowait(
        {
            "method": "thread/realtime/itemAdded",
            "params": {
                "item": {
                    "type": "handoff_request",
                    "input_transcript": "Set the thermostat to twenty two.",
                }
            },
        }
    )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Set the thermostat to twenty two."


@pytest.mark.asyncio
async def test_transcription_uses_latest_user_handoff_transcript_entry() -> None:
    session = FakeCollectorSession()
    session.events.put_nowait(
        {
            "method": "thread/realtime/itemAdded",
            "params": {
                "item": {
                    "type": "handoff_request",
                    "input_transcript": "",
                    "active_transcript": [
                        {"role": "user", "text": "Earlier words"},
                        {"role": "assistant", "text": "Assistant response"},
                        {"role": "user", "text": "Close the garage door."},
                    ],
                }
            },
        }
    )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Close the garage door."


@pytest.mark.asyncio
async def test_transcription_uses_nested_v3_delegation_content() -> None:
    session = FakeCollectorSession()
    session.data.put_nowait(
        json.dumps(
            {
                "type": "delegation.created",
                "item": {
                    "type": "delegation",
                    "target": "client",
                    "content": [{"type": "input_text", "text": "Turn off the lights."}],
                },
            }
        )
    )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Turn off the lights."


@pytest.mark.asyncio
async def test_transcription_ignores_assistant_data_channel_turn() -> None:
    session = FakeCollectorSession()
    session.data.put_nowait(
        json.dumps(
            {
                "type": "turn.done",
                "text": "Top-level assistant text must also be ignored.",
                "turn": {
                    "role": "assistant",
                    "transcript": "This must not become user input.",
                },
            }
        )
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            bridge_service._wait_for_user_transcript(session, 0.02), timeout=0.2
        )


@pytest.mark.asyncio
async def test_transcription_prefers_terminal_data_when_session_closes() -> None:
    """A ready close event cannot mask a simultaneously ready final transcript."""
    session = FakeCollectorSession()
    session.events.put_nowait(
        {"method": "thread/realtime/closed", "params": {"reason": "completed"}}
    )
    session.data.put_nowait(
        json.dumps(
            {
                "type": "turn.done",
                "turn": {"role": "user", "transcript": "Lock the front door."},
            }
        )
    )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Lock the front door."


@pytest.mark.asyncio
async def test_transcription_drains_queued_fragments_after_session_close() -> None:
    session = FakeCollectorSession()
    session.events.put_nowait(
        {"method": "thread/realtime/closed", "params": {"reason": "completed"}}
    )
    for item_id, fragment in (
        ("fragment-1", "Lock the "),
        ("fragment-2", "front door."),
    ):
        session.data.put_nowait(
            json.dumps(
                {
                    "type": "input_transcript.added",
                    "item": {
                        "id": item_id,
                        "type": "input_transcript",
                        "text": fragment,
                    },
                }
            )
        )
    session.data.put_nowait(
        json.dumps(
            {
                "type": "turn.done",
                "turn": {"role": "user", "transcript": "Lock the front door."},
            }
        )
    )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Lock the front door."


@pytest.mark.asyncio
async def test_transcription_treats_empty_session_close_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "TRANSCRIPTION_FRAGMENT_QUIET_SECONDS", 0.01)
    session = FakeCollectorSession()
    session.events.put_nowait(
        {"method": "thread/realtime/closed", "params": {"reason": "transport"}}
    )

    with pytest.raises(TimeoutError, match="closed before transcription"):
        await asyncio.wait_for(
            bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
        )


@pytest.mark.asyncio
async def test_transcription_ignores_non_object_data_events() -> None:
    session = FakeCollectorSession()
    session.data.put_nowait("[]")
    session.data.put_nowait(
        json.dumps(
            {
                "type": "turn.done",
                "turn": {"role": "user", "transcript": "Open the front door."},
            }
        )
    )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Open the front door."


@pytest.mark.asyncio
async def test_transcription_ignores_non_string_delta() -> None:
    session = FakeCollectorSession()
    session.events.put_nowait(
        {
            "method": "thread/realtime/transcript/delta",
            "params": {"role": "user", "delta": None},
        }
    )
    session.data.put_nowait(
        json.dumps(
            {
                "type": "turn.done",
                "turn": {"role": "user", "transcript": "Close the front door."},
            }
        )
    )

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "Close the front door."


@pytest.mark.asyncio
async def test_transcription_fails_immediately_when_app_server_exits() -> None:
    session = FakeCollectorSession()
    session.events.put_nowait(
        {"method": "bridge/appServerExited", "params": {"returncode": 19}}
    )

    with pytest.raises(AppServerExited, match="status 19"):
        await asyncio.wait_for(
            bridge_service._wait_for_user_transcript(session, 10.0), timeout=0.2
        )
