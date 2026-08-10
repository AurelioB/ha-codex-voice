from __future__ import annotations

import json

import pytest

from bridge.errors import ProtocolError
from bridge.realtime_wire import (
    RealtimeWireProtocol,
    parse_data_control_event,
    sanitized_data_control_event,
)


def test_legacy_realtime_wire_defaults_to_json_base64() -> None:
    protocol = RealtimeWireProtocol.negotiate({"type": "start"})

    assert protocol.version == 1
    assert protocol.audio_transport == "json_base64"
    assert protocol.uses_binary_audio is False
    assert protocol.started_fields() == {}


def test_binary_realtime_wire_negotiates_explicit_pcm_shape() -> None:
    protocol = RealtimeWireProtocol.negotiate(
        {
            "type": "start",
            "protocol_version": 2,
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
        }
    )

    assert protocol.uses_binary_audio is True
    assert protocol.started_fields() == {
        "protocol_version": 2,
        "audio_transport": "binary",
        "input_sample_rate": 16_000,
        "input_channels": 1,
        "output_sample_rate": 24_000,
        "output_channels": 1,
        "capabilities": {
            "binary_pcm16": True,
            "local_flush": True,
            "remote_cancel": False,
            "same_session_interrupt_ack": True,
        },
    }


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"protocol_version": True}, "protocol_version must be 1 or 2"),
        ({"protocol_version": 3}, "protocol_version must be 1 or 2"),
        (
            {"protocol_version": 1, "audio_transport": "binary"},
            "protocol_version 1 supports only JSON/base64 audio",
        ),
        (
            {"protocol_version": 2},
            "protocol_version 2 requires audio_transport 'binary'",
        ),
        (
            {"protocol_version": 2, "audio_transport": "binary"},
            "input_sample_rate must be an integer",
        ),
        (
            {
                "protocol_version": 2,
                "audio_transport": "binary",
                "input_sample_rate": 7_999,
            },
            "input_sample_rate must be between",
        ),
        (
            {
                "protocol_version": 2,
                "audio_transport": "binary",
                "input_sample_rate": 16_000,
                "input_channels": 2,
            },
            "protocol_version 2 requires input_channels 1",
        ),
        (
            {
                "protocol_version": 2,
                "audio_transport": "binary",
                "input_sample_rate": 16_000,
                "input_channels": 1,
                "model": "device-override",
            },
            "protocol_version 2 start contains unsupported fields: model",
        ),
        (
            {
                "protocol_version": 2,
                "audio_transport": "binary",
                "input_sample_rate": 16_000,
                "input_channels": 1,
                "tools": [],
            },
            "protocol_version 2 does not accept device tools",
        ),
        (
            {
                "protocol_version": 2,
                "audio_transport": "binary",
                "input_sample_rate": 16_000,
                "input_channels": 1,
                "prompt": "x" * 4_097,
            },
            "prompt must be non-empty text up to 4096 characters",
        ),
    ],
)
def test_realtime_wire_rejects_ambiguous_negotiation(
    overrides: dict[str, object], error: str
) -> None:
    with pytest.raises(ProtocolError, match=error):
        RealtimeWireProtocol.negotiate({"type": "start", **overrides})


def test_data_control_event_is_allowlisted_and_content_free() -> None:
    event = sanitized_data_control_event(
        json.dumps(
            {
                "type": "turn.done",
                "turn": {
                    "role": "assistant",
                    "transcript": "private spoken content",
                },
                "secret": "must not cross the device wire",
            }
        )
    )

    assert event == {"type": "control", "event_type": "turn.done"}


@pytest.mark.parametrize(
    "value",
    [
        {"type": "response.cancelled", "response": {"id": "private-id"}},
        {
            "type": "response.done",
            "response": {"id": "private-id", "status": "cancelled"},
        },
    ],
)
def test_data_control_event_recognizes_explicit_cancel_confirmation(
    value: dict[str, object],
) -> None:
    control = parse_data_control_event(json.dumps(value))

    assert control is not None
    assert control.response_cancelled is True
    assert control.response_id == "private-id"
    assert control.wire_value() == {
        "type": "control",
        "event_type": value["type"],
    }


def test_response_done_without_cancelled_status_is_not_confirmation() -> None:
    control = parse_data_control_event(
        json.dumps({"type": "response.done", "response": {"status": "completed"}})
    )

    assert control is not None
    assert control.response_cancelled is False
    assert control.response_id is None


@pytest.mark.parametrize("key", ["turn_id", "turnId"])
def test_data_control_event_preserves_turn_id_for_internal_correlation(
    key: str,
) -> None:
    control = parse_data_control_event(
        json.dumps({"type": "turn.done", key: "private-turn", "role": "assistant"})
    )

    assert control is not None
    assert control.turn_id == "private-turn"
    assert control.wire_value() == {"type": "control", "event_type": "turn.done"}


def test_data_control_event_prefers_nested_turn_id() -> None:
    control = parse_data_control_event(
        json.dumps(
            {
                "type": "turn.created",
                "turn": {"id": "nested-turn", "role": "assistant"},
                "turnId": "fallback-turn",
            }
        )
    )

    assert control is not None
    assert control.turn_id == "nested-turn"


def test_data_control_event_keeps_turn_transcript_internal() -> None:
    control = parse_data_control_event(
        json.dumps(
            {
                "type": "turn.done",
                "turn": {
                    "id": "user-turn",
                    "role": "user",
                    "transcript": "enciende la cocina",
                },
            }
        )
    )

    assert control is not None
    assert control.transcript == "enciende la cocina"
    assert control.wire_value() == {"type": "control", "event_type": "turn.done"}


@pytest.mark.parametrize("outer_role", ["assistant", "invalid", 7])
def test_data_control_event_rejects_conflicting_or_invalid_declared_roles(
    outer_role: object,
) -> None:
    control = parse_data_control_event(
        json.dumps(
            {
                "type": "turn.done",
                "role": outer_role,
                "turn": {
                    "id": "user-turn",
                    "role": "user",
                    "transcript": "private",
                },
            }
        )
    )

    assert control is not None
    assert control.role is None


def test_data_control_event_normalizes_equivalent_input_role() -> None:
    control = parse_data_control_event(
        json.dumps(
            {
                "type": "turn.created",
                "role": "input",
                "turn": {"id": "user-turn", "role": "user"},
            }
        )
    )

    assert control is not None
    assert control.role == "user"


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        json.dumps(["turn.done"]),
        json.dumps({"type": "input_transcript.added", "text": "private"}),
        json.dumps({"type": "provider.future_event", "payload": "private"}),
        b"\xff",
    ],
)
def test_data_control_event_drops_content_and_unknown_shapes(
    value: str | bytes,
) -> None:
    assert sanitized_data_control_event(value) is None
