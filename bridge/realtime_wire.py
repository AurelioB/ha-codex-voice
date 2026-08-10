"""Versioned device-facing wire contract for realtime bridge sessions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .audio import MAX_PCM_SAMPLE_RATE, MIN_PCM_SAMPLE_RATE, REALTIME_SAMPLE_RATE
from .errors import ProtocolError

LEGACY_PROTOCOL_VERSION = 1
BINARY_PROTOCOL_VERSION = 2
BINARY_AUDIO_TRANSPORT = "binary"
V2_START_FIELDS = frozenset(
    {
        "audio_transport",
        "conversation_id",
        "input_channels",
        "input_sample_rate",
        "prompt",
        "protocol_version",
        "type",
        "voice",
    }
)

# These events carry useful lifecycle signals, but their provider payloads may
# also contain transcripts or other conversation content. Only the type name is
# ever exposed to a device client.
DATA_CONTROL_EVENT_TYPES = frozenset(
    {
        "input_audio_buffer.committed",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "output_audio_buffer.started",
        "output_audio_buffer.stopped",
        "response.cancelled",
        "response.created",
        "response.done",
        "session.context.appended",
        "session.started",
        "session.updated",
        "turn.created",
        "turn.done",
    }
)


@dataclass(frozen=True, slots=True)
class RealtimeDataControl:
    """Internal content-free view of an allowlisted provider data event."""

    event_type: str
    role: str | None
    response_cancelled: bool = False
    response_id: str | None = None
    turn_id: str | None = None
    transcript: str | None = None

    def wire_value(self) -> dict[str, str]:
        return {"type": "control", "event_type": self.event_type}


@dataclass(frozen=True, slots=True)
class RealtimeWireProtocol:
    """Negotiated device-facing audio framing for one realtime socket."""

    version: int
    audio_transport: str
    input_sample_rate: int
    input_channels: int

    @property
    def uses_binary_audio(self) -> bool:
        return self.version == BINARY_PROTOCOL_VERSION

    @classmethod
    def negotiate(cls, start: Mapping[str, Any]) -> RealtimeWireProtocol:
        """Validate a start object without silently changing wire formats."""
        raw_version = start.get("protocol_version", LEGACY_PROTOCOL_VERSION)
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise ProtocolError("protocol_version must be 1 or 2")
        if raw_version == LEGACY_PROTOCOL_VERSION:
            transport = start.get("audio_transport")
            if transport not in {None, "json_base64"}:
                raise ProtocolError(
                    "protocol_version 1 supports only JSON/base64 audio"
                )
            return cls(
                version=LEGACY_PROTOCOL_VERSION,
                audio_transport="json_base64",
                input_sample_rate=REALTIME_SAMPLE_RATE,
                input_channels=1,
            )
        if raw_version != BINARY_PROTOCOL_VERSION:
            raise ProtocolError("protocol_version must be 1 or 2")
        if start.get("audio_transport") != BINARY_AUDIO_TRANSPORT:
            raise ProtocolError("protocol_version 2 requires audio_transport 'binary'")
        sample_rate = _required_integer(start, "input_sample_rate")
        if not MIN_PCM_SAMPLE_RATE <= sample_rate <= MAX_PCM_SAMPLE_RATE:
            raise ProtocolError(
                f"input_sample_rate must be between {MIN_PCM_SAMPLE_RATE} and "
                f"{MAX_PCM_SAMPLE_RATE} Hz"
            )
        channels = _required_integer(start, "input_channels")
        if channels != 1:
            raise ProtocolError("protocol_version 2 requires input_channels 1")
        _validate_v2_start(start)
        return cls(
            version=BINARY_PROTOCOL_VERSION,
            audio_transport=BINARY_AUDIO_TRANSPORT,
            input_sample_rate=sample_rate,
            input_channels=channels,
        )

    def started_fields(self) -> dict[str, Any]:
        """Return fields added to the common started acknowledgement."""
        if not self.uses_binary_audio:
            return {}
        return {
            "protocol_version": self.version,
            "audio_transport": self.audio_transport,
            "input_sample_rate": self.input_sample_rate,
            "input_channels": self.input_channels,
            "output_sample_rate": REALTIME_SAMPLE_RATE,
            "output_channels": 1,
            "capabilities": {
                "binary_pcm16": True,
                "local_flush": True,
                "remote_cancel": False,
                "same_session_interrupt_ack": True,
            },
        }


def sanitized_data_control_event(value: str | bytes) -> dict[str, str] | None:
    """Return an allowlisted content-free provider control event."""
    control = parse_data_control_event(value)
    return control.wire_value() if control is not None else None


def parse_data_control_event(value: str | bytes) -> RealtimeDataControl | None:
    """Parse only lifecycle metadata needed for safe output gating."""
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    event_type = decoded.get("type")
    if not isinstance(event_type, str) or event_type not in DATA_CONTROL_EVENT_TYPES:
        return None
    role = _event_role(decoded)
    return RealtimeDataControl(
        event_type=event_type,
        role=role,
        response_cancelled=_event_confirms_response_cancelled(decoded, event_type),
        response_id=_event_response_id(decoded),
        turn_id=_event_turn_id(decoded),
        transcript=_event_transcript(decoded),
    )


def _required_integer(value: Mapping[str, Any], key: str) -> int:
    candidate = value.get(key)
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        raise ProtocolError(f"{key} must be an integer")
    return candidate


def _event_role(value: Mapping[str, Any]) -> str | None:
    candidates: list[object] = []
    if "role" in value:
        candidates.append(value.get("role"))
    for key in ("turn", "response", "item"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and "role" in nested:
            candidates.append(nested.get("role"))
    roles: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            return None
        normalized = candidate.lower()
        if normalized not in {"assistant", "input", "output", "user"}:
            return None
        normalized = {"input": "user", "output": "assistant"}.get(
            normalized, normalized
        )
        roles.add(normalized)
    return roles.pop() if len(roles) == 1 else None


def _event_confirms_response_cancelled(
    value: Mapping[str, Any], event_type: str
) -> bool:
    """Recognize only provider events that explicitly confirm cancellation."""
    if event_type == "response.cancelled":
        return True
    if event_type != "response.done":
        return False
    response = value.get("response")
    status = response.get("status") if isinstance(response, Mapping) else None
    if status is None:
        status = value.get("status")
    return isinstance(status, str) and status.lower() in {"cancelled", "canceled"}


def _event_response_id(value: Mapping[str, Any]) -> str | None:
    """Retain a response correlation id internally without exposing it."""
    response = value.get("response")
    if isinstance(response, Mapping) and isinstance(response.get("id"), str):
        return response["id"] or None
    for key in ("response_id", "responseId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _event_turn_id(value: Mapping[str, Any]) -> str | None:
    """Retain a turn correlation id internally without exposing it."""
    turn = value.get("turn")
    if isinstance(turn, Mapping) and isinstance(turn.get("id"), str):
        return turn["id"] or None
    for key in ("turn_id", "turnId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _event_transcript(value: Mapping[str, Any]) -> str | None:
    """Retain a completed turn transcript internally without exposing it."""
    turn = value.get("turn")
    nested = turn.get("transcript") if isinstance(turn, Mapping) else None
    if isinstance(nested, str):
        return nested
    transcript = value.get("transcript")
    return transcript if isinstance(transcript, str) else None


def _validate_v2_start(start: Mapping[str, Any]) -> None:
    if "tools" in start:
        raise ProtocolError("protocol_version 2 does not accept device tools")
    unsupported = sorted(set(start) - V2_START_FIELDS)
    if unsupported:
        raise ProtocolError(
            "protocol_version 2 start contains unsupported fields: "
            + ", ".join(unsupported)
        )
    _optional_bounded_text(start, "conversation_id", maximum=128)
    _optional_bounded_text(start, "voice", maximum=64)
    _optional_bounded_text(start, "prompt", maximum=4_096)


def _optional_bounded_text(value: Mapping[str, Any], key: str, *, maximum: int) -> None:
    if key not in value:
        return
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate or len(candidate) > maximum:
        raise ProtocolError(f"{key} must be non-empty text up to {maximum} characters")
