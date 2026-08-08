from __future__ import annotations

import asyncio
import base64
import json
import wave
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web

from bridge import service as bridge_service
from bridge.config import BridgeConfig
from bridge.errors import AppServerExited, ProtocolError
from bridge.runtime import IsolatedCodexRuntime
from bridge.service import BridgeState, _codex_child_environment, create_app

AUTH = {"Authorization": "Bearer test-token"}


class FakeSubscription:
    def __init__(self, rpc: FakeRpc) -> None:
        self.rpc = rpc
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self.queue.get()
        return await asyncio.wait_for(self.queue.get(), timeout)

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
            task = asyncio.create_task(
                self.rpc.broadcast(
                    {
                        "method": "thread/realtime/transcript/done",
                        "params": {
                            "threadId": self.thread_id,
                            "role": "user",
                            "text": "Turn on the kitchen",
                        },
                    }
                )
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def wait_input_drained(self, timeout: float | None = None) -> None:
        return None

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
        await asyncio.gather(*self.tasks)


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
        if method == "thread/realtime/appendSpeech" or (
            method == "thread/realtime/appendText"
            and str(values.get("text", "")).startswith("Vocalize only")
        ):
            peer = self.peers[-1]
            peer.audio.put_nowait(b"\x01\x00" * 480)
            peer.data.put_nowait(json.dumps({"type": "turn.done"}))
            await self.broadcast(
                {
                    "method": "thread/realtime/transcript/done",
                    "params": {
                        "threadId": values["threadId"],
                        "role": "assistant",
                        "text": "Conversational rendering",
                    },
                }
            )
            return {}
        return {}

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
def bridge_app(fake_rpc: FakeRpc) -> web.Application:
    return create_app(
        BridgeConfig(bearer_token="test-token"),
        rpc=fake_rpc,
        peer_factory=fake_rpc.peer_factory,
    )


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
    session.data.put_nowait(json.dumps({"type": "turn.done"}))
    try:
        result = await asyncio.wait_for(
            bridge_service._collect_speech_audio(session, 1.0), timeout=0.25
        )
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
async def test_synthesis_terminal_before_audio_is_a_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_service, "SYNTHESIS_TAIL_GRACE_SECONDS", 0.01)
    session = FakeCollectorSession()
    session.data.put_nowait(json.dumps({"type": "turn.done"}))

    with pytest.raises(ProtocolError, match="produced no audio"):
        await asyncio.wait_for(
            bridge_service._collect_speech_audio(session, 1.0), timeout=0.2
        )


@pytest.mark.asyncio
async def test_transcription_uses_v3_data_channel_final() -> None:
    """STT remains reliable when app-server omits its transcript notification."""
    session = FakeCollectorSession()
    session.data.put_nowait(
        json.dumps(
            {
                "type": "input_transcript.added",
                "text": "The front door is locked.",
            }
        )
    )
    session.data.put_nowait(json.dumps({"type": "turn.done"}))

    transcript = await asyncio.wait_for(
        bridge_service._wait_for_user_transcript(session, 1.0), timeout=0.2
    )

    assert transcript == "The front door is locked."


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
