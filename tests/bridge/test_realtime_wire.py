from __future__ import annotations

import json

import pytest

from bridge.errors import ProtocolError
from bridge.realtime_wire import (
    MAX_DIRECT_WEBRTC_EPOCHS,
    MAX_WEBRTC_SDP_BYTES,
    RealtimeWireProtocol,
    parse_data_control_event,
    parse_direct_webrtc_rollover,
    sanitized_data_control_event,
    validate_direct_webrtc_rollover_ready,
)

VALID_WEBRTC_OFFER = (
    "v=0\r\n"
    "o=- 123 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "a=mid:0\r\n"
    "a=ice-ufrag:deviceIce\r\n"
    "a=ice-pwd:deviceEphemeralIcePassword123\r\n"
    "a=fingerprint:sha-256 00:11:22:33\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "a=mid:1\r\n"
    "a=sctp-port:5000\r\n"
)
VALID_WEBRTC_ANSWER = VALID_WEBRTC_OFFER.replace("o=- 123", "o=- 456")


def test_legacy_realtime_wire_defaults_to_json_base64() -> None:
    protocol = RealtimeWireProtocol.negotiate({"type": "start"})

    assert protocol.version == 1
    assert protocol.audio_transport == "json_base64"
    assert protocol.uses_binary_audio is False
    assert protocol.conversation_mode is None
    assert protocol.requests_native_conversation is False
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
    assert protocol.conversation_mode is None
    assert protocol.requests_native_conversation is False
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
            "server_owned_media": True,
            "native_end_conversation": False,
        },
    }


def test_binary_realtime_wire_retains_and_echoes_native_conversation_mode() -> None:
    protocol = RealtimeWireProtocol.negotiate(
        {
            "type": "start",
            "protocol_version": 2,
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
            "conversation_mode": "native",
        }
    )

    assert protocol.conversation_mode == "native"
    assert protocol.requests_native_conversation is True
    assert protocol.started_fields()["conversation_mode"] == "native"
    assert protocol.started_fields()["capabilities"]["server_owned_media"] is True
    assert protocol.started_fields()["capabilities"]["native_end_conversation"] is True


def test_direct_webrtc_wire_retains_device_offer_and_advertises_direct_media() -> None:
    protocol = RealtimeWireProtocol.negotiate(
        {
            "type": "start",
            "protocol_version": 3,
            "conversation_mode": "native",
            "conversation_id": "living-room",
            "prompt": "Habla espanol de Mexico.",
            "voice": "cove",
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        }
    )

    assert protocol.version == 3
    assert protocol.audio_transport == "webrtc"
    assert protocol.input_sample_rate == 24_000
    assert protocol.input_channels == 1
    assert protocol.uses_binary_audio is False
    assert protocol.uses_direct_webrtc is True
    assert protocol.requests_native_conversation is True
    assert protocol.webrtc_offer_sdp == VALID_WEBRTC_OFFER
    assert protocol.started_fields() == {
        "protocol_version": 3,
        "conversation_mode": "native",
        "transport": "webrtc",
        "audio_over_bridge": False,
        "sideband_control": True,
    }


def test_direct_webrtc_answer_preserves_ice_but_never_adds_auth_credentials() -> None:
    protocol = RealtimeWireProtocol.negotiate(
        {
            "type": "start",
            "protocol_version": 3,
            "conversation_mode": "native",
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        }
    )

    assert protocol.answer_fields(VALID_WEBRTC_ANSWER) == {
        "protocol_version": 3,
        "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_ANSWER},
    }
    encoded = json.dumps(protocol.answer_fields(VALID_WEBRTC_ANSWER))
    assert "ice-ufrag" in encoded
    assert "ice-pwd" in encoded
    assert "oauth" not in encoded.casefold()
    assert "bearer" not in encoded.casefold()
    assert "call_id" not in encoded


def test_direct_webrtc_rollover_round_trip_uses_exact_epoch_shapes() -> None:
    request = parse_direct_webrtc_rollover(
        {
            "type": "rollover",
            "protocol_version": 3,
            "epoch": 2,
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        },
        expected_epoch=2,
    )

    assert request.epoch == 2
    assert request.offer_sdp == VALID_WEBRTC_OFFER
    assert request.answer_message(VALID_WEBRTC_ANSWER) == {
        "type": "rollover_answer",
        "protocol_version": 3,
        "epoch": 2,
        "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_ANSWER},
    }
    assert request.started_message(context_retained=True) == {
        "type": "rollover_started",
        "protocol_version": 3,
        "epoch": 2,
        "context_retained": True,
    }
    validate_direct_webrtc_rollover_ready(
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 2,
        },
        expected_epoch=2,
    )


@pytest.mark.parametrize(
    "message",
    [
        {
            "type": "rollover",
            "protocol_version": 3,
            "epoch": 1,
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        },
        {
            "type": "rollover",
            "protocol_version": 3,
            "epoch": 3,
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        },
        {
            "type": "rollover",
            "protocol_version": 3,
            "epoch": True,
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        },
        {
            "type": "rollover",
            "protocol_version": 3,
            "epoch": 2.0,
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        },
        {
            "type": "rollover",
            "protocol_version": 3.0,
            "epoch": 2,
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        },
        {
            "type": "rollover",
            "protocol_version": True,
            "epoch": 2,
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        },
        {
            "type": "rollover",
            "protocol_version": 3,
            "epoch": 2,
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
            "unexpected": True,
        },
        {
            "type": "rollover",
            "protocol_version": 3,
            "epoch": 2,
            "transport": {
                "type": "webrtc",
                "sdp": VALID_WEBRTC_OFFER,
                "token": "must-not-pass",
            },
        },
    ],
)
def test_direct_webrtc_rollover_rejects_stale_skipped_or_malformed_epoch(
    message: dict[str, object],
) -> None:
    with pytest.raises(ProtocolError):
        parse_direct_webrtc_rollover(message, expected_epoch=2)


@pytest.mark.parametrize(
    "message",
    [
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 1,
        },
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 2.0,
        },
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3.0,
            "epoch": 2,
        },
        {
            "type": "rollover_transport_ready",
            "protocol_version": True,
            "epoch": 2,
        },
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 2,
            "extra": True,
        },
        {"type": "transport_ready", "protocol_version": 3, "epoch": 2},
    ],
)
def test_direct_webrtc_rollover_ready_requires_exact_current_epoch(
    message: dict[str, object],
) -> None:
    with pytest.raises(ProtocolError, match="rollover_transport_ready"):
        validate_direct_webrtc_rollover_ready(message, expected_epoch=2)


def test_direct_webrtc_rollover_bounds_epoch_churn() -> None:
    with pytest.raises(ProtocolError, match="epoch limit reached"):
        parse_direct_webrtc_rollover(
            {
                "type": "rollover",
                "protocol_version": 3,
                "epoch": MAX_DIRECT_WEBRTC_EPOCHS + 1,
                "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
            },
            expected_epoch=MAX_DIRECT_WEBRTC_EPOCHS + 1,
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"protocol_version": True}, "protocol_version must be 1, 2, or 3"),
        ({"protocol_version": 4}, "protocol_version must be 1, 2, or 3"),
        (
            {"protocol_version": 1, "audio_transport": "binary"},
            "protocol_version 1 supports only JSON/base64 audio",
        ),
        (
            {"conversation_mode": "native"},
            "conversation_mode requires protocol_version 2",
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
                "conversation_mode": "managed",
            },
            "conversation_mode must be 'native'",
        ),
        (
            {
                "protocol_version": 2,
                "audio_transport": "binary",
                "input_sample_rate": 16_000,
                "input_channels": 1,
                "conversation_mode": None,
            },
            "conversation_mode must be 'native'",
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


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"type": "speech"}, "requires message type 'start'"),
        ({}, "requires conversation_mode 'native'"),
        ({"conversation_mode": "managed"}, "requires conversation_mode 'native'"),
        ({"conversation_mode": None}, "requires conversation_mode 'native'"),
        (
            {"conversation_mode": "native"},
            "requires a WebRTC transport object",
        ),
        (
            {"conversation_mode": "native", "transport": "webrtc"},
            "requires a WebRTC transport object",
        ),
        (
            {
                "conversation_mode": "native",
                "transport": {"type": "websocket", "sdp": VALID_WEBRTC_OFFER},
            },
            "requires transport type 'webrtc'",
        ),
        (
            {
                "conversation_mode": "native",
                "transport": {"type": "webrtc"},
            },
            "offer SDP must be non-empty text",
        ),
        (
            {
                "conversation_mode": "native",
                "transport": {"type": "webrtc", "sdp": 7},
            },
            "offer SDP must be non-empty text",
        ),
        (
            {
                "conversation_mode": "native",
                "transport": {
                    "type": "webrtc",
                    "sdp": VALID_WEBRTC_OFFER,
                    "oauth_token": "must-not-cross-wire",
                },
            },
            "transport contains unsupported fields: oauth_token",
        ),
        (
            {
                "conversation_mode": "native",
                "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
                "audio_transport": "binary",
            },
            "does not accept legacy PCM fields: audio_transport",
        ),
        (
            {
                "conversation_mode": "native",
                "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
                "input_sample_rate": 16_000,
                "input_channels": 1,
            },
            "does not accept legacy PCM fields: input_channels, input_sample_rate",
        ),
        (
            {
                "conversation_mode": "native",
                "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
                "tools": [],
            },
            "protocol_version 3 does not accept device tools",
        ),
        (
            {
                "conversation_mode": "native",
                "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
                "model": "device-override",
            },
            "protocol_version 3 start contains unsupported fields: model",
        ),
        (
            {
                "conversation_mode": "native",
                "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
                "prompt": "x" * 4_097,
            },
            "prompt must be non-empty text up to 4096 characters",
        ),
    ],
)
def test_direct_webrtc_rejects_ambiguous_negotiation(
    overrides: dict[str, object], error: str
) -> None:
    with pytest.raises(ProtocolError, match=error):
        RealtimeWireProtocol.negotiate(
            {"type": "start", "protocol_version": 3, **overrides}
        )


@pytest.mark.parametrize(
    ("sdp", "error"),
    [
        ("v=0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n", "audio m-line"),
        ("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n", "application m-line"),
        (
            (
                "o=- 123 2 IN IP4 127.0.0.1\r\n"
                "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
            ),
            "must start with 'v=0'",
        ),
        (VALID_WEBRTC_OFFER + "\x00", "must not contain NUL bytes"),
        (
            "v=0\r\n"
            "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
            "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
            + ("a=x\r\n" * (MAX_WEBRTC_SDP_BYTES // 3)),
            f"exceeds {MAX_WEBRTC_SDP_BYTES} bytes",
        ),
    ],
)
def test_direct_webrtc_rejects_malformed_or_oversized_offer(
    sdp: str, error: str
) -> None:
    with pytest.raises(ProtocolError, match=error):
        RealtimeWireProtocol.negotiate(
            {
                "type": "start",
                "protocol_version": 3,
                "conversation_mode": "native",
                "transport": {"type": "webrtc", "sdp": sdp},
            }
        )


def test_direct_webrtc_accepts_offer_at_exact_byte_limit() -> None:
    padding_size = MAX_WEBRTC_SDP_BYTES - len(VALID_WEBRTC_OFFER.encode())
    offer = VALID_WEBRTC_OFFER + ("x" * padding_size)

    protocol = RealtimeWireProtocol.negotiate(
        {
            "type": "start",
            "protocol_version": 3,
            "conversation_mode": "native",
            "transport": {"type": "webrtc", "sdp": offer},
        }
    )

    assert len(offer.encode()) == MAX_WEBRTC_SDP_BYTES
    assert protocol.webrtc_offer_sdp == offer


def test_webrtc_answer_fields_require_v3_and_validate_answer() -> None:
    binary = RealtimeWireProtocol.negotiate(
        {
            "type": "start",
            "protocol_version": 2,
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
        }
    )
    with pytest.raises(ProtocolError, match="answers require protocol_version 3"):
        binary.answer_fields(VALID_WEBRTC_ANSWER)

    direct = RealtimeWireProtocol.negotiate(
        {
            "type": "start",
            "protocol_version": 3,
            "conversation_mode": "native",
            "transport": {"type": "webrtc", "sdp": VALID_WEBRTC_OFFER},
        }
    )
    with pytest.raises(ProtocolError, match="answer SDP must contain an audio m-line"):
        direct.answer_fields(
            "v=0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
        )


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


def test_user_turn_start_exposes_only_the_bounded_role_marker() -> None:
    control = parse_data_control_event(
        json.dumps(
            {
                "type": "turn.created",
                "turn": {
                    "id": "private-turn",
                    "role": "user",
                    "transcript": "private spoken content",
                },
            }
        )
    )

    assert control is not None
    assert control.wire_value() == {
        "type": "control",
        "event_type": "turn.created",
        "role": "user",
    }


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
