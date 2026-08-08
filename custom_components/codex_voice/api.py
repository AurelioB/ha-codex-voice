"""Authenticated client for the local Codex Voice bridge."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from aiohttp import (
    ClientError,
    ClientResponse,
    ClientSession,
    ClientTimeout,
    WSMsgType,
)

from .const import (
    CONVERSATION_TIMEOUT,
    HEALTH_TIMEOUT,
    MAX_AUDIO_BYTES,
    MAX_SYNTHESIZED_AUDIO_BYTES,
    MAX_TOOL_CALLS,
    REQUEST_TIMEOUT,
)

_JSON_CONTENT_TYPES: Final = ("application/json", "text/json")
_WAV_CONTENT_TYPES: Final = ("audio/wav", "audio/wave", "audio/x-wav")

JsonObject = dict[str, Any]
DeltaHandler = Callable[[str], Awaitable[None]]
ToolHandler = Callable[["BridgeToolCall"], Awaitable[JsonObject]]


class BridgeError(Exception):
    """Base class for bridge errors."""


class BridgeAuthenticationError(BridgeError):
    """The bridge rejected the configured access token."""


class BridgeConnectionError(BridgeError):
    """The bridge could not be reached."""


class BridgeProtocolError(BridgeError):
    """The bridge returned an invalid or incompatible response."""


class BridgeBusyError(BridgeError):
    """The bridge cannot accept another speech operation right now."""


class BridgeQuotaError(BridgeError):
    """The ChatGPT subscription quota is exhausted."""


@dataclass(frozen=True, slots=True)
class BridgeToolCall:
    """A tool request received during a conversation turn."""

    call_id: str
    name: str
    arguments: JsonObject
    request_id: int | str | None = None


@dataclass(frozen=True, slots=True)
class BridgeAudio:
    """Audio returned by the bridge."""

    data: bytes
    audio_format: str
    sample_rate: int = 24000
    channels: int = 1
    sample_width: int = 2


def normalize_bridge_url(url: str) -> str:
    """Return a stable bridge URL without a trailing slash."""
    return url.strip().rstrip("/")


class BridgeClient:
    """Small, explicit client for the bridge's supported API surface."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        access_token: str,
    ) -> None:
        """Initialize the bridge client."""
        self._session = session
        self.base_url = normalize_bridge_url(base_url)
        self._headers = {"Authorization": f"Bearer {access_token}"}

    def _url(self, path: str) -> str:
        """Build a bridge URL."""
        return f"{self.base_url}{path}"

    async def async_health(self) -> JsonObject:
        """Return bridge health and authentication metadata."""
        try:
            async with self._session.get(
                self._url("/health"),
                headers=self._headers,
                timeout=ClientTimeout(total=HEALTH_TIMEOUT),
            ) as response:
                await self._raise_for_status(response)
                payload = await self._read_json(response)
        except BridgeError:
            raise
        except (TimeoutError, ClientError) as err:
            raise BridgeConnectionError(
                "Unable to connect to the Codex bridge"
            ) from err

        if payload.get("status") not in (None, "ok", "ready"):
            raise BridgeConnectionError("The Codex bridge is not ready")
        return payload

    async def async_converse(
        self,
        start: Mapping[str, Any],
        *,
        async_handle_delta: DeltaHandler,
        async_handle_tool: ToolHandler,
    ) -> JsonObject:
        """Run one conversation turn over the bridge WebSocket."""
        try:
            async with asyncio.timeout(CONVERSATION_TIMEOUT):
                async with self._session.ws_connect(
                    self._url("/v1/conversation"),
                    headers=self._headers,
                    heartbeat=30,
                ) as websocket:
                    await websocket.send_json({"type": "start", **start})
                    tool_calls = 0
                    received_delta = False
                    async for message in websocket:
                        if message.type is WSMsgType.ERROR:
                            raise BridgeConnectionError(
                                "The Codex bridge WebSocket failed"
                            )
                        if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
                            break
                        if message.type is not WSMsgType.TEXT:
                            continue

                        event = self._decode_event(message.data)
                        event_type = event.get("type")
                        if event_type == "delta":
                            delta = event.get("delta", event.get("content", ""))
                            if not isinstance(delta, str):
                                raise BridgeProtocolError(
                                    "Conversation delta must be text"
                                )
                            if delta:
                                received_delta = True
                                await async_handle_delta(delta)
                            continue

                        if event_type == "tool_call":
                            tool_calls += 1
                            if tool_calls > MAX_TOOL_CALLS:
                                raise BridgeProtocolError(
                                    "Conversation exceeded the tool-call limit"
                                )
                            tool_call = self._decode_tool_call(event)
                            result = await async_handle_tool(tool_call)
                            await websocket.send_json(
                                {
                                    "type": "tool_result",
                                    "request_id": tool_call.request_id,
                                    "call_id": tool_call.call_id,
                                    "result": result,
                                    "success": "error" not in result,
                                }
                            )
                            continue

                        if event_type == "done":
                            final_text = event.get("text", event.get("content"))
                            if (
                                not received_delta
                                and isinstance(final_text, str)
                                and final_text
                            ):
                                await async_handle_delta(final_text)
                            return event

                        if event_type == "error":
                            self._raise_event_error(event)

                        if event_type not in (
                            "started",
                            "start",
                            "item",
                            "event",
                            "pong",
                        ):
                            raise BridgeProtocolError(
                                f"Unknown conversation event: {event_type!r}"
                            )
        except BridgeError:
            raise
        except TimeoutError as err:
            raise BridgeConnectionError("The conversation request timed out") from err
        except ClientError as err:
            raise BridgeConnectionError("The conversation connection failed") from err

        raise BridgeConnectionError("The conversation connection closed unexpectedly")

    async def async_transcribe(
        self,
        audio: bytes,
        metadata: Mapping[str, Any],
        *,
        prompt: str | None,
    ) -> str:
        """Transcribe raw PCM with the bridge."""
        if len(audio) > MAX_AUDIO_BYTES:
            raise BridgeProtocolError("Audio input exceeds the 16 MiB limit")

        payload: JsonObject = {
            "audio": base64.b64encode(audio).decode("ascii"),
            "encoding": "base64",
            "format": "pcm",
            **dict(metadata),
        }
        if prompt:
            payload["prompt"] = prompt

        response_payload = await self._async_post_json("/v1/transcribe", payload)
        text = response_payload.get("text", response_payload.get("transcript"))
        if not isinstance(text, str):
            raise BridgeProtocolError("Transcription response did not contain text")
        return text

    async def async_synthesize(
        self,
        text: str,
        *,
        language: str,
        voice: str,
        instructions: str | None,
    ) -> BridgeAudio:
        """Synthesize text and return WAV or PCM audio."""
        payload: JsonObject = {
            "text": text,
            "language": language,
            "voice": voice,
            "format": "wav",
        }
        if instructions:
            payload["instructions"] = instructions

        try:
            async with self._session.post(
                self._url("/v1/synthesize"),
                headers=self._headers,
                json=payload,
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                await self._raise_for_status(response)
                content_type = response.content_type.lower()
                if content_type in _JSON_CONTENT_TYPES:
                    result = await self._read_json(response)
                    return self._decode_audio_json(result)
                if content_type not in _WAV_CONTENT_TYPES:
                    raise BridgeProtocolError(
                        f"Synthesis returned unsupported content type {content_type!r}"
                    )
                audio = await self._read_bounded_audio(response)
                self._validate_wav_header(audio)
                return BridgeAudio(data=audio, audio_format="wav")
        except BridgeError:
            raise
        except (TimeoutError, ClientError) as err:
            raise BridgeConnectionError("Speech synthesis failed") from err

    async def _async_post_json(self, path: str, payload: JsonObject) -> JsonObject:
        """POST JSON and return a JSON object."""
        try:
            async with self._session.post(
                self._url(path),
                headers=self._headers,
                json=payload,
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                await self._raise_for_status(response)
                return await self._read_json(response)
        except BridgeError:
            raise
        except (TimeoutError, ClientError) as err:
            raise BridgeConnectionError("The Codex bridge request failed") from err

    @staticmethod
    async def _raise_for_status(response: ClientResponse) -> None:
        """Translate HTTP errors into stable integration errors."""
        if response.status < 400:
            return
        if response.status in (401, 403):
            raise BridgeAuthenticationError("The bridge access token was rejected")
        if response.status == 409:
            raise BridgeBusyError("The bridge is busy")
        if response.status == 429:
            raise BridgeQuotaError("The subscription quota is exhausted")
        if response.status >= 500:
            raise BridgeConnectionError(f"The bridge returned HTTP {response.status}")
        raise BridgeProtocolError(f"The bridge returned HTTP {response.status}")

    @staticmethod
    async def _read_json(response: ClientResponse) -> JsonObject:
        """Read and validate an object JSON response."""
        try:
            payload = await response.json(content_type=None)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as err:
            raise BridgeProtocolError("The bridge returned invalid JSON") from err
        if not isinstance(payload, dict):
            raise BridgeProtocolError("The bridge JSON response must be an object")
        return payload

    @staticmethod
    async def _read_bounded_audio(response: ClientResponse) -> bytes:
        """Stream an audio response while enforcing the configured size cap."""
        audio = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            audio.extend(chunk)
            if len(audio) > MAX_SYNTHESIZED_AUDIO_BYTES:
                raise BridgeProtocolError("Synthesized audio exceeds the 50 MiB limit")
        return bytes(audio)

    @staticmethod
    def _decode_event(raw_event: str) -> JsonObject:
        """Decode a WebSocket event."""
        try:
            event = json.loads(raw_event)
        except (json.JSONDecodeError, TypeError) as err:
            raise BridgeProtocolError("The bridge sent invalid WebSocket JSON") from err
        if not isinstance(event, dict):
            raise BridgeProtocolError("The bridge WebSocket event must be an object")
        return event

    @staticmethod
    def _decode_tool_call(event: JsonObject) -> BridgeToolCall:
        """Validate a tool-call event."""
        call_id = event.get("id", event.get("call_id"))
        request_id = event.get("request_id")
        name = event.get("name", event.get("tool_name"))
        arguments = event.get("arguments", event.get("args", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as err:
                raise BridgeProtocolError("Tool arguments are invalid JSON") from err
        if not isinstance(call_id, str) or not call_id:
            raise BridgeProtocolError("Tool call is missing an id")
        if not isinstance(name, str) or not name:
            raise BridgeProtocolError("Tool call is missing a name")
        if not isinstance(arguments, dict):
            raise BridgeProtocolError("Tool arguments must be an object")
        if request_id is not None and not isinstance(request_id, (int, str)):
            raise BridgeProtocolError("Tool call request id is invalid")
        return BridgeToolCall(
            call_id=call_id,
            name=name,
            arguments=arguments,
            request_id=request_id,
        )

    @staticmethod
    def _raise_event_error(event: JsonObject) -> None:
        """Translate a bridge error event."""
        code = event.get("code")
        message = event.get("message", event.get("error"))
        detail = message if isinstance(message, str) else "The bridge reported an error"
        if code in ("invalid_auth", "authentication_required"):
            raise BridgeAuthenticationError(detail)
        if code in ("quota_exhausted", "rate_limited"):
            raise BridgeQuotaError(detail)
        if code in ("busy", "operation_in_progress"):
            raise BridgeBusyError(detail)
        raise BridgeProtocolError(detail)

    @staticmethod
    def _decode_audio_json(payload: JsonObject) -> BridgeAudio:
        """Decode a base64 audio response."""
        encoded_audio = payload.get("audio")
        if not isinstance(encoded_audio, str):
            raise BridgeProtocolError("Synthesis response did not contain audio")
        try:
            audio = base64.b64decode(encoded_audio, validate=True)
        except (ValueError, TypeError) as err:
            raise BridgeProtocolError("Synthesis audio was not valid base64") from err
        BridgeClient._validate_audio_size(audio)

        audio_format = payload.get("format", "pcm")
        if not isinstance(audio_format, str):
            raise BridgeProtocolError("Synthesis audio format must be text")
        if audio_format.lower() in ("wav", "wave"):
            BridgeClient._validate_wav_header(audio)
        return BridgeAudio(
            data=audio,
            audio_format=audio_format.lower(),
            sample_rate=BridgeClient._positive_int(payload, "sample_rate", 24000),
            channels=BridgeClient._positive_int(payload, "channels", 1),
            sample_width=BridgeClient._positive_int(payload, "sample_width", 2),
        )

    @staticmethod
    def _positive_int(payload: JsonObject, key: str, default: int) -> int:
        """Read a positive integer from a bridge response."""
        value = payload.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise BridgeProtocolError(f"Synthesis {key} must be a positive integer")
        return value

    @staticmethod
    def _validate_audio_size(audio: bytes) -> None:
        """Reject unexpectedly large synthesis responses."""
        if len(audio) > MAX_SYNTHESIZED_AUDIO_BYTES:
            raise BridgeProtocolError("Synthesized audio exceeds the 50 MiB limit")

    @staticmethod
    def _validate_wav_header(audio: bytes) -> None:
        """Reject mislabeled or truncated WAV responses."""
        if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise BridgeProtocolError("Synthesis response was not a valid WAV file")
