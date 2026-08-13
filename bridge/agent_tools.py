"""Optional bounded external-agent tools for native realtime sessions."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .errors import BridgeError, ProtocolError

ASK_AGENT_TOOL_NAME = "ask_agent"
RECALL_MEMORY_TOOL_NAME = "recall_memory"
MAX_AGENT_ARGUMENT_CHARS = 4_000
MAX_AGENT_RESPONSE_BYTES = 64 * 1024
MAX_AGENT_ANSWER_CHARS = 8_000
MAX_AGENT_MATCHES = 32
MAX_AGENT_MATCH_CHARS = 1_000

AGENT_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "name": ASK_AGENT_TOOL_NAME,
        "description": (
            "Ask the optional external agent to handle memory-heavy, research, "
            "cross-application, or longer-running work. Never use this tool for "
            "Home Assistant entity state or smart-home control."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The complete task or question for the agent.",
                }
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": RECALL_MEMORY_TOOL_NAME,
        "description": (
            "Search the optional agent's memory for relevant prior facts or "
            "preferences. Never use it to control Home Assistant entities."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A concise memory search query.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
)
AGENT_TOOL_NAMES = frozenset(tool["name"] for tool in AGENT_TOOLS)


class AgentToolUnavailable(BridgeError):
    """The optional external agent did not return a trustworthy result."""


@dataclass(frozen=True, slots=True)
class AgentToolResult:
    """Canonical result returned to App Server."""

    success: bool
    result: Any


class AgentToolBroker:
    """Own an optional reusable HTTP client for agent recall and deep tasks."""

    def __init__(
        self,
        url: str | None,
        *,
        token: str | None,
        room: str,
        recall_timeout: float,
        task_timeout: float,
    ) -> None:
        self._url = url
        self._token = token
        self._room = room
        self._recall_timeout = recall_timeout
        self._task_timeout = task_timeout
        self._session: ClientSession | None = None
        self._calls_started = 0
        self._calls_succeeded = 0
        self._calls_failed = 0
        self._last_call_duration_ms: int | None = None

    @property
    def enabled(self) -> bool:
        """Return whether an external endpoint is configured."""
        return self._url is not None

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        """Return immutable advertised tools only when configured."""
        return AGENT_TOOLS if self.enabled else ()

    def owns(self, name: object) -> bool:
        """Return whether this enabled adapter owns a declared tool name."""
        return self.enabled and isinstance(name, str) and name in AGENT_TOOL_NAMES

    def health(self) -> dict[str, bool | int | None]:
        """Return content-free configuration and call counters."""
        return {
            "enabled": self.enabled,
            "calls_started": self._calls_started,
            "calls_succeeded": self._calls_succeeded,
            "calls_failed": self._calls_failed,
            "last_call_duration_ms": self._last_call_duration_ms,
        }

    async def close(self) -> None:
        """Close the lazily allocated client session."""
        session = self._session
        self._session = None
        if session is not None:
            await session.close()

    async def call(
        self,
        *,
        name: str,
        arguments: Mapping[str, Any],
    ) -> AgentToolResult:
        """Execute one declared call without exposing credentials or content."""
        if not self.owns(name) or self._url is None:
            raise ProtocolError("realtime model requested an undeclared agent tool")
        self._calls_started += 1
        started_at = time.monotonic()
        try:
            if name == ASK_AGENT_TOOL_NAME:
                question = _required_argument(arguments, "question")
                payload = {"question": question, "room": self._room}
                value = await self._post(payload, self._task_timeout)
                answer = _validated_answer(value)
                result: Any = {"answer": answer[:MAX_AGENT_ANSWER_CHARS]}
            else:
                query = _required_argument(arguments, "query")
                value = await self._post({"recall": query}, self._recall_timeout)
                raw_matches = _validated_matches(value)
                matches = [
                    item[:MAX_AGENT_MATCH_CHARS]
                    for item in raw_matches[:MAX_AGENT_MATCHES]
                    if isinstance(item, str)
                ]
                result = {"matches": matches}
        except (AgentToolUnavailable, ProtocolError):
            self._calls_failed += 1
            raise
        else:
            self._calls_succeeded += 1
            return AgentToolResult(success=True, result=result)
        finally:
            self._last_call_duration_ms = round((time.monotonic() - started_at) * 1_000)

    async def _post(self, payload: Mapping[str, Any], timeout: float) -> object:
        session = self._session
        if session is None or session.closed:
            session = ClientSession()
            self._session = session
        headers = {"Content-Type": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with session.post(
                self._url,
                json=dict(payload),
                headers=headers,
                timeout=ClientTimeout(total=timeout),
            ) as response:
                raw = await response.content.read(MAX_AGENT_RESPONSE_BYTES + 1)
                if response.status < 200 or response.status >= 300:
                    raise AgentToolUnavailable(
                        f"agent returned HTTP status {response.status}"
                    )
        except (TimeoutError, ClientError) as err:
            raise AgentToolUnavailable("agent request failed or timed out") from err
        if len(raw) > MAX_AGENT_RESPONSE_BYTES:
            raise AgentToolUnavailable("agent response exceeded the size limit")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            raise AgentToolUnavailable("agent returned invalid JSON") from err


def _required_argument(arguments: Mapping[str, Any], name: str) -> str:
    if set(arguments) != {name}:
        raise ProtocolError(f"agent tool requires exactly {name}")
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"agent tool {name} must be a non-empty string")
    if len(value) > MAX_AGENT_ARGUMENT_CHARS:
        raise ProtocolError(f"agent tool {name} exceeded the size limit")
    return value.strip()


def _validated_answer(value: object) -> str:
    answer = value.get("answer") if isinstance(value, Mapping) else None
    if not isinstance(answer, str) or not answer.strip():
        raise AgentToolUnavailable("agent returned no answer")
    return answer


def _validated_matches(value: object) -> list[object]:
    matches = value.get("matches") if isinstance(value, Mapping) else None
    if not isinstance(matches, list):
        raise AgentToolUnavailable("agent returned invalid memory matches")
    return matches
