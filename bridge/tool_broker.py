"""Home Assistant-owned tool broker for subscription realtime sessions."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from .errors import BridgeBusyError, BridgeError, ProtocolError

TOOL_BROKER_PROTOCOL_VERSION = 1
MAX_AUTHORITY_ID_CHARS = 256
MAX_LANGUAGE_CHARS = 35
MAX_INSTRUCTIONS_CHARS = 64 * 1024
MAX_TOOL_COUNT = 128
MAX_TOOL_NAME_CHARS = 256
MAX_TOOL_SCHEMA_BYTES = 64 * 1024
MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_TOOL_RESULT_BYTES = 64 * 1024
MAX_TOOL_BROKER_MESSAGE_BYTES = 256 * 1024
MAX_PENDING_TOOL_CALLS = 16
MAX_RETIRED_TOOL_CALLS = 128
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,255}\Z")


class ToolBrokerUnavailable(BridgeError):
    """The Home Assistant tool authority is unavailable or changed."""


@dataclass(frozen=True, slots=True)
class ToolBrokerSnapshot:
    """Immutable authority snapshot bound to one realtime session."""

    generation: str
    authority_id: str
    language: str
    instructions: str
    tools: tuple[dict[str, Any], ...]
    tool_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class ToolBrokerResult:
    """Canonical result returned by Home Assistant."""

    success: bool
    result: Any


@dataclass(slots=True)
class _PendingCall:
    generation: str
    future: asyncio.Future[ToolBrokerResult]


class HomeAssistantToolBroker:
    """Own one authenticated HA authority connection and bounded calls."""

    def __init__(self, *, timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS) -> None:
        if timeout <= 0:
            raise ValueError("tool broker timeout must be positive")
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._websocket: web.WebSocketResponse | None = None
        self._snapshot: ToolBrokerSnapshot | None = None
        self._pending: dict[str, _PendingCall] = {}
        self._retired: OrderedDict[str, str] = OrderedDict()

    @property
    def snapshot(self) -> ToolBrokerSnapshot | None:
        """Return the current immutable authority snapshot, if registered."""
        return self._snapshot

    async def register(
        self,
        websocket: web.WebSocketResponse,
        value: Mapping[str, Any],
    ) -> ToolBrokerSnapshot:
        """Register one HA-owned authority connection."""
        snapshot = _validated_registration(value)
        stale_pending: tuple[_PendingCall, ...] = ()
        async with self._lock:
            if self._websocket is not None and not self._websocket.closed:
                raise BridgeBusyError(
                    "a Home Assistant tool authority is already connected"
                )
            if self._websocket is not None:
                stale_pending = tuple(self._pending.values())
                self._pending.clear()
                self._retired.clear()
            generation = secrets.token_urlsafe(18)
            snapshot = ToolBrokerSnapshot(
                generation=generation,
                authority_id=snapshot.authority_id,
                language=snapshot.language,
                instructions=snapshot.instructions,
                tools=snapshot.tools,
                tool_names=snapshot.tool_names,
            )
            self._websocket = websocket
            self._snapshot = snapshot
        for call in stale_pending:
            if not call.future.done():
                call.future.set_exception(
                    ToolBrokerUnavailable(
                        "Home Assistant tool authority changed; outcome unknown"
                    )
                )
        await self._send_json(
            websocket,
            {
                "type": "registered",
                "protocol_version": TOOL_BROKER_PROTOCOL_VERSION,
                "generation": snapshot.generation,
            },
        )
        return snapshot

    async def unregister(self, websocket: web.WebSocketResponse) -> None:
        """Detach an authority and fail every unresolved call exactly once."""
        async with self._lock:
            if websocket is not self._websocket:
                return
            self._websocket = None
            self._snapshot = None
            pending = tuple(self._pending.values())
            self._pending.clear()
            self._retired.clear()
        for call in pending:
            if not call.future.done():
                call.future.set_exception(
                    ToolBrokerUnavailable(
                        "Home Assistant tool authority disconnected; outcome unknown"
                    )
                )

    async def handle_message(
        self,
        websocket: web.WebSocketResponse,
        value: Mapping[str, Any],
    ) -> None:
        """Handle one component-originated broker message."""
        if websocket is not self._websocket:
            raise ToolBrokerUnavailable("stale Home Assistant tool authority")
        message_type = value.get("type")
        if message_type == "ping":
            await self._send_json(websocket, {"type": "pong"})
            return
        if message_type != "tool_result":
            raise ProtocolError("unsupported Home Assistant tool broker message")

        snapshot = self._snapshot
        generation = value.get("generation")
        call_id = value.get("call_id")
        success = value.get("success")
        if (
            snapshot is None
            or generation != snapshot.generation
            or not isinstance(call_id, str)
            or not call_id
            or not isinstance(success, bool)
        ):
            raise ProtocolError("invalid Home Assistant tool result correlation")
        _bounded_json(value.get("result"), MAX_TOOL_RESULT_BYTES, "tool result")
        pending = self._pending.pop(call_id, None)
        if pending is None:
            if self._retired.pop(call_id, None) == generation:
                return
            raise ProtocolError("unknown or stale Home Assistant tool result")
        if pending.generation != generation:
            raise ProtocolError("unknown or stale Home Assistant tool result")
        if pending.future.done():
            raise ProtocolError("duplicate Home Assistant tool result")
        pending.future.set_result(
            ToolBrokerResult(success=success, result=value.get("result"))
        )

    async def call(
        self,
        snapshot: ToolBrokerSnapshot,
        *,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolBrokerResult:
        """Execute one allowlisted tool through the bound HA authority."""
        current = self._snapshot
        websocket = self._websocket
        if (
            current is None
            or current.generation != snapshot.generation
            or websocket is None
            or websocket.closed
        ):
            raise ToolBrokerUnavailable("Home Assistant tool authority changed")
        if name not in snapshot.tool_names:
            raise ProtocolError(
                "realtime model requested an undeclared Home Assistant tool"
            )
        normalized_arguments = dict(arguments)
        _bounded_json(normalized_arguments, MAX_TOOL_ARGUMENT_BYTES, "tool arguments")
        if len(self._pending) >= MAX_PENDING_TOOL_CALLS:
            raise ToolBrokerUnavailable(
                "too many Home Assistant tool calls are pending"
            )

        call_id = secrets.token_urlsafe(18)
        future: asyncio.Future[ToolBrokerResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[call_id] = _PendingCall(snapshot.generation, future)
        completed = False
        try:
            await self._send_json(
                websocket,
                {
                    "type": "tool_call",
                    "generation": snapshot.generation,
                    "call_id": call_id,
                    "name": name,
                    "arguments": normalized_arguments,
                },
            )
            try:
                async with asyncio.timeout(self._timeout):
                    # asyncio.timeout cancels the current task at its
                    # deadline. Shield the correlation future so a result
                    # arriving on that boundary cannot make handle_message()
                    # set a cancelled future and tear down the authority
                    # socket. The finally block retires the call ID and
                    # cancels the now-unreachable future deterministically.
                    result = await asyncio.shield(future)
                    completed = True
                    return result
            except TimeoutError as exc:
                raise ToolBrokerUnavailable(
                    "Home Assistant tool call timed out; outcome unknown, do not retry"
                ) from exc
        finally:
            self._pending.pop(call_id, None)
            current = self._snapshot
            if (
                not completed
                and current is not None
                and current.generation == snapshot.generation
            ):
                self._retired[call_id] = snapshot.generation
                self._retired.move_to_end(call_id)
                while len(self._retired) > MAX_RETIRED_TOOL_CALLS:
                    self._retired.popitem(last=False)
            if not future.done():
                future.cancel()

    async def _send_json(
        self,
        websocket: web.WebSocketResponse,
        value: Mapping[str, Any],
    ) -> None:
        async with self._send_lock:
            try:
                await websocket.send_json(dict(value))
            except (ConnectionError, RuntimeError) as exc:
                raise ToolBrokerUnavailable(
                    "Home Assistant tool authority connection failed"
                ) from exc


def _validated_registration(value: Mapping[str, Any]) -> ToolBrokerSnapshot:
    if value.get("type") != "register":
        raise ProtocolError("first Home Assistant tool broker message must register")
    if value.get("protocol_version") != TOOL_BROKER_PROTOCOL_VERSION:
        raise ProtocolError("unsupported Home Assistant tool broker protocol")
    authority_id = _bounded_text(
        value.get("authority_id"), MAX_AUTHORITY_ID_CHARS, "authority_id"
    )
    language = _bounded_text(value.get("language"), MAX_LANGUAGE_CHARS, "language")
    language = _canonical_language(language)
    instructions = _bounded_text(
        value.get("instructions", ""),
        MAX_INSTRUCTIONS_CHARS,
        "instructions",
        allow_empty=True,
    )
    raw_tools = value.get("tools")
    if not isinstance(raw_tools, list) or len(raw_tools) > MAX_TOOL_COUNT:
        raise ProtocolError("Home Assistant broker tools must be a bounded list")
    tools: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, Mapping):
            raise ProtocolError("Home Assistant broker tool must be an object")
        name = raw_tool.get("name")
        description = raw_tool.get("description", "")
        parameters = raw_tool.get("parameters", raw_tool.get("inputSchema"))
        if (
            not isinstance(name, str)
            or len(name) > MAX_TOOL_NAME_CHARS
            or _TOOL_NAME.fullmatch(name) is None
            or name in names
        ):
            raise ProtocolError(
                "Home Assistant broker tool name is invalid or duplicate"
            )
        if not isinstance(description, str) or len(description) > 8 * 1024:
            raise ProtocolError("Home Assistant broker tool description is invalid")
        if not isinstance(parameters, Mapping):
            raise ProtocolError("Home Assistant broker tool schema must be an object")
        normalized_schema = dict(parameters)
        _bounded_json(normalized_schema, MAX_TOOL_SCHEMA_BYTES, "tool schema")
        tools.append(
            {
                "name": name,
                "description": description,
                "parameters": normalized_schema,
            }
        )
        names.add(name)
    return ToolBrokerSnapshot(
        generation="",
        authority_id=authority_id,
        language=language,
        instructions=instructions,
        tools=tuple(tools),
        tool_names=frozenset(names),
    )


def _bounded_text(
    value: object,
    maximum: int,
    label: str,
    *,
    allow_empty: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > maximum
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        raise ProtocolError(f"Home Assistant broker {label} is invalid")
    return value


def _bounded_json(value: object, maximum: int, label: str) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"Home Assistant broker {label} is not JSON") from exc
    if len(encoded) > maximum:
        raise ProtocolError(f"Home Assistant broker {label} exceeds its size limit")


def _canonical_language(value: str) -> str:
    """Validate and canonicalize the bounded Home Assistant BCP-47 tag."""
    parts = value.split("-")
    primary = parts[0]
    if (
        not 2 <= len(primary) <= 8
        or not primary.isascii()
        or not primary.isalpha()
        or any(
            not 1 <= len(part) <= 8 or not part.isascii() or not part.isalnum()
            for part in parts[1:]
        )
    ):
        raise ProtocolError("Home Assistant broker language must be a valid BCP-47 tag")
    normalized = [primary.lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)
