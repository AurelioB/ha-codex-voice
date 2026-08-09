from __future__ import annotations

import json

import pytest

from bridge.errors import ProtocolError
from bridge.realtime_wire import RealtimeWireProtocol, sanitized_data_control_event


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
