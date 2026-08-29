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
DEVICE_WEBRTC_PROTOCOL_VERSION = 3
BINARY_AUDIO_TRANSPORT = "binary"
DEVICE_WEBRTC_TRANSPORT = "webrtc"
MAX_WEBRTC_SDP_BYTES = 16 * 1024
MAX_DIRECT_WEBRTC_EPOCHS = 1_024
V2_START_FIELDS = frozenset(
    {
        "audio_transport",
        "conversation_mode",
        "conversation_id",
        "input_channels",
        "input_sample_rate",
        "prompt",
        "protocol_version",
        "type",
        "voice",
    }
)
V3_START_FIELDS = frozenset(
    {
        "conversation_mode",
        "conversation_id",
        "prompt",
        "protocol_version",
        "transport",
        "type",
        "voice",
    }
)
V3_TRANSPORT_FIELDS = frozenset({"sdp", "type"})
V3_ROLLOVER_FIELDS = frozenset({"epoch", "protocol_version", "transport", "type"})
V3_ROLLOVER_READY_FIELDS = frozenset({"epoch", "protocol_version", "type"})
LEGACY_PCM_START_FIELDS = frozenset(
    {"audio_transport", "input_channels", "input_sample_rate"}
)

# These events carry useful lifecycle signals, but their provider payloads may
# also contain transcripts or other conversation content. Only the type name
# and, for a user turn start, the bounded role marker are exposed to a device
# client.
DATA_CONTROL_EVENT_TYPES = frozenset(
    {
        "input_audio_buffer.committed",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "output_audio_buffer.started",
        "output_audio_buffer.stopped",
        "output_audio_buffer.cleared",
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
        value = {"type": "control", "event_type": self.event_type}
        if self.event_type == "turn.created" and self.role == "user":
            value["role"] = "user"
        return value


@dataclass(frozen=True, slots=True)
class DirectWebRtcRollover:
    """One exact device request to replace a direct-media peer epoch."""

    epoch: int
    offer_sdp: str

    def answer_message(self, sdp: str) -> dict[str, Any]:
        """Build the exact answer shape for this replacement epoch."""
        return {
            "type": "rollover_answer",
            "protocol_version": DEVICE_WEBRTC_PROTOCOL_VERSION,
            "epoch": self.epoch,
            "transport": {
                "type": DEVICE_WEBRTC_TRANSPORT,
                "sdp": _validated_webrtc_sdp(sdp, description="answer"),
            },
        }

    def started_message(self, *, context_retained: bool) -> dict[str, Any]:
        """Build the exact post-readiness acknowledgement for this epoch."""
        return {
            "type": "rollover_started",
            "protocol_version": DEVICE_WEBRTC_PROTOCOL_VERSION,
            "epoch": self.epoch,
            "context_retained": context_retained,
        }


@dataclass(frozen=True, slots=True)
class RealtimeWireProtocol:
    """Negotiated device-facing audio framing for one realtime socket."""

    version: int
    audio_transport: str
    input_sample_rate: int
    input_channels: int
    conversation_mode: str | None = None
    webrtc_offer_sdp: str | None = None

    @property
    def uses_binary_audio(self) -> bool:
        return self.version == BINARY_PROTOCOL_VERSION

    @property
    def uses_direct_webrtc(self) -> bool:
        """Return whether media flows directly between the device and provider."""
        return self.version == DEVICE_WEBRTC_PROTOCOL_VERSION

    @property
    def requests_native_conversation(self) -> bool:
        """Return whether the client explicitly selected native realtime voice."""
        return self.conversation_mode == "native"

    @classmethod
    def negotiate(cls, start: Mapping[str, Any]) -> RealtimeWireProtocol:
        """Validate a start object without silently changing wire formats."""
        raw_version = start.get("protocol_version", LEGACY_PROTOCOL_VERSION)
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise ProtocolError("protocol_version must be 1, 2, or 3")
        if raw_version == LEGACY_PROTOCOL_VERSION:
            if "conversation_mode" in start:
                raise ProtocolError("conversation_mode requires protocol_version 2")
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
                conversation_mode=None,
            )
        if raw_version == BINARY_PROTOCOL_VERSION:
            if start.get("audio_transport") != BINARY_AUDIO_TRANSPORT:
                raise ProtocolError(
                    "protocol_version 2 requires audio_transport 'binary'"
                )
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
                conversation_mode=(
                    "native" if start.get("conversation_mode") == "native" else None
                ),
            )
        if raw_version != DEVICE_WEBRTC_PROTOCOL_VERSION:
            raise ProtocolError("protocol_version must be 1, 2, or 3")
        offer_sdp = _validate_v3_start(start)
        return cls(
            version=DEVICE_WEBRTC_PROTOCOL_VERSION,
            audio_transport=DEVICE_WEBRTC_TRANSPORT,
            # Preserve the established typed shape for callers that inspect the
            # contract. Direct-media callers must branch on uses_direct_webrtc.
            input_sample_rate=REALTIME_SAMPLE_RATE,
            input_channels=1,
            conversation_mode="native",
            webrtc_offer_sdp=offer_sdp,
        )

    def answer_fields(self, sdp: str) -> dict[str, Any]:
        """Return fields for the distinct answer message sent to a v3 device."""
        if not self.uses_direct_webrtc:
            raise ProtocolError("WebRTC answers require protocol_version 3")
        answer_sdp = _validated_webrtc_sdp(sdp, description="answer")
        return {
            "protocol_version": DEVICE_WEBRTC_PROTOCOL_VERSION,
            "transport": {"type": DEVICE_WEBRTC_TRANSPORT, "sdp": answer_sdp},
        }

    def started_fields(self) -> dict[str, Any]:
        """Return fields added to the common started acknowledgement."""
        if self.uses_direct_webrtc:
            return {
                "protocol_version": DEVICE_WEBRTC_PROTOCOL_VERSION,
                "conversation_mode": "native",
                "transport": DEVICE_WEBRTC_TRANSPORT,
                "audio_over_bridge": False,
                "sideband_control": True,
            }
        if not self.uses_binary_audio:
            return {}
        fields: dict[str, Any] = {
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
                "server_owned_media": True,
                "native_end_conversation": self.requests_native_conversation,
            },
        }
        if self.conversation_mode is not None:
            fields["conversation_mode"] = self.conversation_mode
        return fields


def sanitized_data_control_event(value: str | bytes) -> dict[str, str] | None:
    """Return an allowlisted content-free provider control event."""
    control = parse_data_control_event(value)
    return control.wire_value() if control is not None else None


def parse_direct_webrtc_rollover(
    value: Mapping[str, Any], *, expected_epoch: int
) -> DirectWebRtcRollover:
    """Validate an exact, strictly consecutive direct-peer rollover request."""
    if expected_epoch > MAX_DIRECT_WEBRTC_EPOCHS:
        raise ProtocolError("direct WebRTC rollover epoch limit reached")
    if set(value) != V3_ROLLOVER_FIELDS:
        raise ProtocolError("malformed direct WebRTC rollover request")
    protocol_version = value.get("protocol_version")
    if (
        value.get("type") != "rollover"
        or not isinstance(protocol_version, int)
        or isinstance(protocol_version, bool)
        or protocol_version != DEVICE_WEBRTC_PROTOCOL_VERSION
    ):
        raise ProtocolError("malformed direct WebRTC rollover request")
    epoch = value.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        raise ProtocolError("direct WebRTC rollover epoch must be an integer")
    if epoch > MAX_DIRECT_WEBRTC_EPOCHS:
        raise ProtocolError("direct WebRTC rollover epoch limit reached")
    if epoch != expected_epoch:
        raise ProtocolError(f"direct WebRTC rollover epoch must be {expected_epoch}")
    transport = value.get("transport")
    if not isinstance(transport, Mapping) or set(transport) != V3_TRANSPORT_FIELDS:
        raise ProtocolError("malformed direct WebRTC rollover transport")
    if transport.get("type") != DEVICE_WEBRTC_TRANSPORT:
        raise ProtocolError("direct WebRTC rollover transport must be WebRTC")
    return DirectWebRtcRollover(
        epoch=epoch,
        offer_sdp=_validated_webrtc_sdp(transport.get("sdp"), description="offer"),
    )


def validate_direct_webrtc_rollover_ready(
    value: Mapping[str, Any], *, expected_epoch: int
) -> None:
    """Validate the exact readiness acknowledgement for one replacement epoch."""
    protocol_version = value.get("protocol_version")
    epoch = value.get("epoch")
    if (
        set(value) != V3_ROLLOVER_READY_FIELDS
        or value.get("type") != "rollover_transport_ready"
        or not isinstance(protocol_version, int)
        or isinstance(protocol_version, bool)
        or protocol_version != DEVICE_WEBRTC_PROTOCOL_VERSION
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch != expected_epoch
    ):
        raise ProtocolError(
            "expected protocol_version 3 rollover_transport_ready "
            f"acknowledgement for epoch {expected_epoch}"
        )


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
    if "conversation_mode" in start and start.get("conversation_mode") != "native":
        raise ProtocolError("conversation_mode must be 'native'")
    _optional_bounded_text(start, "conversation_id", maximum=128)
    _optional_bounded_text(start, "voice", maximum=64)
    _optional_bounded_text(start, "prompt", maximum=4_096)


def _validate_v3_start(start: Mapping[str, Any]) -> str:
    if start.get("type") != "start":
        raise ProtocolError("protocol_version 3 requires message type 'start'")
    if "tools" in start:
        raise ProtocolError("protocol_version 3 does not accept device tools")
    legacy_fields = sorted(set(start) & LEGACY_PCM_START_FIELDS)
    if legacy_fields:
        raise ProtocolError(
            "protocol_version 3 does not accept legacy PCM fields: "
            + ", ".join(legacy_fields)
        )
    unsupported = sorted(set(start) - V3_START_FIELDS)
    if unsupported:
        raise ProtocolError(
            "protocol_version 3 start contains unsupported fields: "
            + ", ".join(unsupported)
        )
    if start.get("conversation_mode") != "native":
        raise ProtocolError("protocol_version 3 requires conversation_mode 'native'")
    _optional_bounded_text(start, "conversation_id", maximum=128)
    _optional_bounded_text(start, "voice", maximum=64)
    _optional_bounded_text(start, "prompt", maximum=4_096)

    transport = start.get("transport")
    if not isinstance(transport, Mapping):
        raise ProtocolError("protocol_version 3 requires a WebRTC transport object")
    unsupported_transport = sorted(set(transport) - V3_TRANSPORT_FIELDS)
    if unsupported_transport:
        raise ProtocolError(
            "protocol_version 3 transport contains unsupported fields: "
            + ", ".join(unsupported_transport)
        )
    if transport.get("type") != DEVICE_WEBRTC_TRANSPORT:
        raise ProtocolError("protocol_version 3 requires transport type 'webrtc'")
    return _validated_webrtc_sdp(transport.get("sdp"), description="offer")


def _validated_webrtc_sdp(value: object, *, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"WebRTC {description} SDP must be non-empty text")
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProtocolError(
            f"WebRTC {description} SDP must contain valid Unicode text"
        ) from exc
    if encoded_size > MAX_WEBRTC_SDP_BYTES:
        raise ProtocolError(
            f"WebRTC {description} SDP exceeds {MAX_WEBRTC_SDP_BYTES} bytes"
        )
    if "\x00" in value:
        raise ProtocolError(f"WebRTC {description} SDP must not contain NUL bytes")

    lines = value.splitlines()
    if not lines or lines[0] != "v=0":
        raise ProtocolError(f"WebRTC {description} SDP must start with 'v=0'")
    if not any(line.startswith("m=audio ") for line in lines):
        raise ProtocolError(f"WebRTC {description} SDP must contain an audio m-line")
    if not any(line.startswith("m=application ") for line in lines):
        raise ProtocolError(
            f"WebRTC {description} SDP must contain an application m-line"
        )
    return value


def _optional_bounded_text(value: Mapping[str, Any], key: str, *, maximum: int) -> None:
    if key not in value:
        return
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate or len(candidate) > maximum:
        raise ProtocolError(f"{key} must be non-empty text up to {maximum} characters")
