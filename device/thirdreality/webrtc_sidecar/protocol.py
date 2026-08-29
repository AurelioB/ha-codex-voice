"""Bounded, transcript-free IPC protocol for the WebRTC sidecar."""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from math import isfinite
from typing import Any

WIRE_VERSION = 1
CONTROL_KIND = 1
CAPTURE_AUDIO_KIND = 2
PLAYBACK_AUDIO_KIND = 3
MAX_PACKET_BYTES = 64 * 1024
MAX_CONTROL_BYTES = 48 * 1024
MAX_CAPTURE_AUDIO_BYTES = 8 * 1024
MAX_PLAYBACK_AUDIO_BYTES = 16 * 1024
MAX_SDP_CHARACTERS = 32 * 1024
_PCM_SAMPLE_WIDTH = 2
_AUDIO_HEADER = struct.Struct("!BBHIQQI")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_SAFE_ROLE = frozenset({"assistant", "developer", "system", "tool", "user"})
_SAFE_RESPONSE_STATUS = frozenset(
    {"in_progress", "completed", "cancelled", "failed", "incomplete"}
)
_RTP_STARTED_LIFECYCLE_TYPES = frozenset(
    {"capture.rtp_started", "playback.rtp_started"}
)
_INTERNAL_LIFECYCLE_TYPES = (
    frozenset(
        {
            "media.started",
            "media.quiet",
            "interrupt.fenced",
            "capture.metrics",
            "capture.direction.inactive",
            "capture.direction.recvonly",
            "capture.direction.sendonly",
            "capture.direction.sendrecv",
            "capture.direction.unknown",
            "capture.outbound_active",
        }
    )
    | _RTP_STARTED_LIFECYCLE_TYPES
)
_PARENT_CONTROL_TYPES = frozenset(
    {
        "capture.commit",
        "create_offer",
        "standby.create_offer",
        "standby.promote",
        "set_answer",
        "response.interrupt",
        "stop",
        "shutdown",
    }
)
_CHILD_CONTROL_TYPES = frozenset(
    {
        "offer",
        "standby.offer",
        "standby.promoted",
        "standby.failed",
        "answer.applied",
        "connected",
        "capture.ready",
        "data.ready",
        "lifecycle",
        "capture.metrics",
        "stopped",
        "shutdown.complete",
        "error",
    }
)
_CONTROL_TYPES = _PARENT_CONTROL_TYPES | _CHILD_CONTROL_TYPES


class ProtocolError(ValueError):
    """Raised when a sidecar IPC packet violates its strict contract."""


@dataclass(frozen=True, slots=True)
class ControlMessage:
    """One validated JSON control packet."""

    type: str
    values: dict[str, str | int | bool | float]


@dataclass(frozen=True, slots=True)
class CaptureAudio:
    """One parent-to-sidecar microphone PCM packet."""

    sample_index: int
    capture_monotonic_ns: int
    pcm: bytes


@dataclass(frozen=True, slots=True)
class PlaybackAudio:
    """One sidecar-to-parent decoded playback PCM packet."""

    generation: int
    sample_index: int
    media_timestamp: int
    pcm: bytes


Packet = ControlMessage | CaptureAudio | PlaybackAudio


def encode_control(
    message_type: str,
    **values: str | int | bool | float,
) -> bytes:
    """Encode one allowlisted, bounded control message."""
    _validate_control(message_type, values)
    body = json.dumps(
        {"type": message_type, **values},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(body) > MAX_CONTROL_BYTES:
        raise ProtocolError("sidecar control packet exceeds its bound")
    return bytes((CONTROL_KIND, WIRE_VERSION)) + body


def encode_capture_audio(
    pcm: bytes,
    *,
    sample_index: int,
    capture_monotonic_ns: int,
) -> bytes:
    """Encode one timestamped 16 kHz mono PCM16 capture packet."""
    _validate_pcm(pcm, maximum=MAX_CAPTURE_AUDIO_BYTES, name="capture")
    _validate_uint64(sample_index, "capture sample index")
    _validate_uint64(capture_monotonic_ns, "capture monotonic timestamp")
    return (
        _AUDIO_HEADER.pack(
            CAPTURE_AUDIO_KIND,
            WIRE_VERSION,
            0,
            0,
            sample_index,
            capture_monotonic_ns,
            len(pcm),
        )
        + pcm
    )


def encode_playback_audio(value: PlaybackAudio) -> bytes:
    """Encode one generation-tagged 24 kHz mono PCM16 playback packet."""
    _validate_pcm(value.pcm, maximum=MAX_PLAYBACK_AUDIO_BYTES, name="playback")
    _validate_uint32(value.generation, "playback generation")
    _validate_uint64(value.sample_index, "playback sample index")
    _validate_uint64(value.media_timestamp, "playback media timestamp")
    return (
        _AUDIO_HEADER.pack(
            PLAYBACK_AUDIO_KIND,
            WIRE_VERSION,
            0,
            value.generation,
            value.sample_index,
            value.media_timestamp,
            len(value.pcm),
        )
        + value.pcm
    )


def decode_packet(packet: bytes) -> Packet:
    """Decode and validate one complete ``SOCK_SEQPACKET`` message."""
    if not packet or len(packet) > MAX_PACKET_BYTES:
        raise ProtocolError("sidecar packet has an invalid size")
    kind = packet[0]
    if kind == CONTROL_KIND:
        return _decode_control(packet)
    if kind in {CAPTURE_AUDIO_KIND, PLAYBACK_AUDIO_KIND}:
        return _decode_audio(packet)
    raise ProtocolError("sidecar packet has an unknown kind")


def sanitize_provider_lifecycle(message: str | bytes) -> dict[str, str | int] | None:
    """Return only provider-controlled lifecycle tokens and identifiers.

    Transcripts, audio payloads, model text, arguments, and arbitrary nested
    values never cross the device IPC boundary.
    """
    if isinstance(message, bytes):
        if len(message) > MAX_CONTROL_BYTES:
            return None
        try:
            text = message.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(message, str):
        text = message
        if len(text.encode("utf-8")) > MAX_CONTROL_BYTES:
            return None
    else:
        return None
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(decoded, dict):
        return None
    event_type = _safe_token(decoded.get("type"))
    if event_type is None or event_type in _INTERNAL_LIFECYCLE_TYPES:
        return None

    item = decoded.get("item") if isinstance(decoded.get("item"), dict) else {}
    response = (
        decoded.get("response") if isinstance(decoded.get("response"), dict) else {}
    )
    turn = decoded.get("turn") if isinstance(decoded.get("turn"), dict) else {}
    error = decoded.get("error") if isinstance(decoded.get("error"), dict) else {}
    result: dict[str, str | int] = {"event_type": event_type}
    role = _safe_role(decoded.get("role"), item.get("role"), turn.get("role"))
    if role is not None:
        result["role"] = role
    for output_key, candidates in (
        ("item_id", (decoded.get("item_id"), item.get("id"))),
        ("response_id", (decoded.get("response_id"), response.get("id"))),
        ("turn_id", (decoded.get("turn_id"), turn.get("id"))),
    ):
        identifier = _first_safe_token(*candidates)
        if identifier is not None:
            result[output_key] = identifier
    generation = decoded.get("generation")
    if isinstance(generation, int) and not isinstance(generation, bool):
        if 0 <= generation <= 0xFFFFFFFF:
            result["provider_generation"] = generation
    response_status = response.get("status")
    if isinstance(response_status, str) and response_status in _SAFE_RESPONSE_STATUS:
        result["response_status"] = response_status
    error_event_id = _safe_token(error.get("event_id"))
    if error_event_id is not None:
        result["error_event_id"] = error_event_id
    for output_key, value in (
        ("error_type", error.get("type")),
        ("error_code", error.get("code")),
        ("error_param", error.get("param")),
    ):
        token = _safe_token(value)
        if token is not None:
            result[output_key] = token
    return result


def _decode_control(packet: bytes) -> ControlMessage:
    if len(packet) < 3 or packet[1] != WIRE_VERSION:
        raise ProtocolError("sidecar control packet has an invalid version")
    body = packet[2:]
    if not body or len(body) > MAX_CONTROL_BYTES:
        raise ProtocolError("sidecar control packet has an invalid size")
    try:
        decoded = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("sidecar control packet is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("sidecar control packet must be an object")
    message_type = decoded.pop("type", None)
    if not isinstance(message_type, str):
        raise ProtocolError("sidecar control packet has no valid type")
    values: dict[str, str | int | bool | float] = {}
    for key, value in decoded.items():
        if not isinstance(key, str) or not isinstance(
            value,
            (str, int, bool, float),
        ):
            raise ProtocolError("sidecar control packet has an invalid field")
        values[key] = value
    _validate_control(message_type, values)
    return ControlMessage(type=message_type, values=values)


def _decode_audio(packet: bytes) -> CaptureAudio | PlaybackAudio:
    if len(packet) < _AUDIO_HEADER.size:
        raise ProtocolError("sidecar audio packet is truncated")
    kind, version, flags, generation, sample_index, timestamp, length = (
        _AUDIO_HEADER.unpack_from(packet)
    )
    if version != WIRE_VERSION or flags != 0:
        raise ProtocolError("sidecar audio packet has invalid metadata")
    pcm = packet[_AUDIO_HEADER.size :]
    if length != len(pcm):
        raise ProtocolError("sidecar audio packet length does not match its payload")
    if kind == CAPTURE_AUDIO_KIND:
        if generation != 0:
            raise ProtocolError("capture audio cannot carry a generation")
        _validate_pcm(pcm, maximum=MAX_CAPTURE_AUDIO_BYTES, name="capture")
        return CaptureAudio(
            sample_index=sample_index,
            capture_monotonic_ns=timestamp,
            pcm=pcm,
        )
    _validate_pcm(pcm, maximum=MAX_PLAYBACK_AUDIO_BYTES, name="playback")
    return PlaybackAudio(
        generation=generation,
        sample_index=sample_index,
        media_timestamp=timestamp,
        pcm=pcm,
    )


def _validate_control(
    message_type: str,
    values: dict[str, str | int | bool | float],
) -> None:
    if message_type not in _CONTROL_TYPES:
        raise ProtocolError("sidecar control type is not allowed")
    allowed: dict[str, frozenset[str]] = {
        "capture.commit": frozenset(),
        "create_offer": frozenset({"direct_capture_gain_db"}),
        "standby.create_offer": frozenset({"direct_capture_gain_db"}),
        "standby.promote": frozenset({"peer_epoch"}),
        "set_answer": frozenset({"sdp"}),
        "response.interrupt": frozenset(),
        "stop": frozenset(),
        "shutdown": frozenset(),
        "offer": frozenset({"sdp"}),
        "standby.offer": frozenset({"sdp", "peer_epoch"}),
        "standby.promoted": frozenset({"peer_epoch"}),
        "standby.failed": frozenset({"peer_epoch"}),
        "answer.applied": frozenset(),
        "connected": frozenset(),
        "capture.ready": frozenset(),
        "data.ready": frozenset(),
        "lifecycle": frozenset(
            {
                "event_type",
                "role",
                "item_id",
                "response_id",
                "turn_id",
                "generation",
                "provider_generation",
                "response_status",
                "error_event_id",
                "error_type",
                "error_code",
                "error_param",
            }
        ),
        "capture.metrics": frozenset(
            {
                "post_gain_max_peak",
                "post_gain_max_rms",
                "clipped_samples",
                "clipped_frames",
            }
        ),
        "stopped": frozenset(),
        "shutdown.complete": frozenset(),
        "error": frozenset({"code"}),
    }
    if set(values) - allowed[message_type]:
        raise ProtocolError("sidecar control packet contains an unexpected field")
    if (
        message_type
        in {
            "standby.promote",
            "standby.offer",
            "standby.promoted",
            "standby.failed",
        }
        and set(values) != allowed[message_type]
    ):
        raise ProtocolError("sidecar standby control has invalid fields")
    if "direct_capture_gain_db" in values:
        _validate_capture_gain(values["direct_capture_gain_db"])
    if message_type in {"set_answer", "offer", "standby.offer"}:
        sdp = values.get("sdp")
        if not isinstance(sdp, str) or not sdp or len(sdp) > MAX_SDP_CHARACTERS:
            raise ProtocolError("sidecar SDP has an invalid size")
    if "peer_epoch" in values:
        value = values["peer_epoch"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 0xFFFFFFFF
        ):
            raise ProtocolError("sidecar peer epoch is invalid")
    if "response_id" in values and _safe_token(values["response_id"]) is None:
        raise ProtocolError("sidecar response identifier is invalid")
    if message_type == "error" and _safe_token(values.get("code")) is None:
        raise ProtocolError("sidecar error code is invalid")
    if message_type == "capture.metrics":
        _validate_capture_metrics(values, allowed[message_type])
    if message_type == "lifecycle":
        event_type = _safe_token(values.get("event_type"))
        if event_type is None:
            raise ProtocolError("sidecar lifecycle event type is invalid")
        if event_type in _RTP_STARTED_LIFECYCLE_TYPES and set(values) != {
            "event_type",
            "generation",
        }:
            raise ProtocolError("sidecar RTP lifecycle has invalid fields")
        if "role" in values and values["role"] not in _SAFE_ROLE:
            raise ProtocolError("sidecar lifecycle role is invalid")
        for key in ("item_id", "response_id", "turn_id"):
            if key in values and _safe_token(values[key]) is None:
                raise ProtocolError("sidecar lifecycle identifier is invalid")
        if (
            "response_status" in values
            and values["response_status"] not in _SAFE_RESPONSE_STATUS
        ):
            raise ProtocolError("sidecar lifecycle response status is invalid")
        if "error_event_id" in values and _safe_token(values["error_event_id"]) is None:
            raise ProtocolError("sidecar lifecycle error identifier is invalid")
        for key in ("error_type", "error_code", "error_param"):
            if key in values and _safe_token(values[key]) is None:
                raise ProtocolError("sidecar lifecycle error token is invalid")
        for key in ("generation", "provider_generation"):
            if key in values:
                value = values[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= 0xFFFFFFFF
                ):
                    raise ProtocolError("sidecar lifecycle generation is invalid")


def _validate_capture_gain(value: str | int | bool | float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not isfinite(value))
        or not 0 <= value <= 18
    ):
        raise ProtocolError("sidecar capture gain is invalid")


def _validate_capture_metrics(
    values: dict[str, str | int | bool | float],
    required: frozenset[str],
) -> None:
    if set(values) != required:
        raise ProtocolError("sidecar capture metrics have invalid fields")
    for key in ("post_gain_max_peak", "post_gain_max_rms"):
        value = values[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 32_768
        ):
            raise ProtocolError("sidecar capture level is invalid")
    for key in ("clipped_samples", "clipped_frames"):
        value = values[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFFFFFF
        ):
            raise ProtocolError("sidecar capture clipping count is invalid")


def _validate_pcm(pcm: bytes, *, maximum: int, name: str) -> None:
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % _PCM_SAMPLE_WIDTH:
        raise ProtocolError(f"sidecar {name} PCM must be non-empty PCM16")
    if len(pcm) > maximum:
        raise ProtocolError(f"sidecar {name} PCM exceeds its bound")


def _validate_uint32(value: int, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 0xFFFFFFFF
    ):
        raise ProtocolError(f"{name} is invalid")


def _validate_uint64(value: int, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 0xFFFFFFFFFFFFFFFF
    ):
        raise ProtocolError(f"{name} is invalid")


def _safe_token(value: Any) -> str | None:
    return value if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value) else None


def _first_safe_token(*values: Any) -> str | None:
    for value in values:
        if (candidate := _safe_token(value)) is not None:
            return candidate
    return None


def _safe_role(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value in _SAFE_ROLE:
            return value
    return None
