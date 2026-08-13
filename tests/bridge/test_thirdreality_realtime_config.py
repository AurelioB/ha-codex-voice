from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from device.thirdreality.realtime_client.config import (
    BRIDGE_PCM_TRANSPORT,
    DEFAULT_AEC_SINK_VOLUME_CEILING_PERCENT,
    DEFAULT_AEC_TEST_VOLUME_PERCENT,
    DEFAULT_DIRECT_CAPTURE_GAIN_DB,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MAX_SESSION_SECONDS,
    DEFAULT_PLAYBACK_VOLUME_PERCENT,
    DEFAULT_PULSE_AEC_METHOD,
    DEFAULT_PULSE_AEC_SINK,
    DEFAULT_PULSE_AEC_SOURCE,
    DEVICE_WEBRTC_TRANSPORT,
    MAX_DIRECT_CAPTURE_GAIN_DB,
    MAX_REALTIME_VOLUME_PERCENT,
    NATIVE_AEC3_CAPTURE,
    NATIVE_CONVERSATION_MODE,
    PROVIDER_CONTROL_BARGE_IN_MODE,
    PULSEAUDIO_AEC_CAPTURE,
    ROLLOVER_BARGE_IN_MODE,
    UPSTREAM_BARGE_IN_MODE,
    ConfigError,
    RealtimeConfig,
    load_config,
    normalize_wake_phrase,
    realtime_start_message,
)


def _write_config(path: Path, value: dict[str, object], *, mode: int = 0o600) -> None:
    path.write_text(json.dumps(value))
    path.chmod(mode)


def _valid_config() -> dict[str, object]:
    return {
        "enabled": True,
        "url": "ws://192.0.2.10:8787/v1/realtime",
        "token": "0123456789abcdef",
        "wake_phrase": " Okay   Computer ",
    }


def test_secure_config_loads_bounded_defaults_without_exposing_token(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, _valid_config())

    config = load_config(path, expected_uid=os.getuid())

    assert isinstance(config, RealtimeConfig)
    assert config.connect_address == "192.0.2.10"
    assert config.wake_phrase == "okay computer"
    assert config.realtime_only is False
    assert config.wake_probability_cutoff is None
    assert config.voice is None
    assert config.prompt is None
    assert config.full_duplex is False
    assert config.media_transport == BRIDGE_PCM_TRANSPORT
    assert config.capture_backend == PULSEAUDIO_AEC_CAPTURE
    assert config.barge_in_mode == ROLLOVER_BARGE_IN_MODE
    assert config.direct_capture_gain_db == DEFAULT_DIRECT_CAPTURE_GAIN_DB
    assert config.pulse_aec_source is None
    assert config.pulse_aec_sink is None
    assert config.pulse_aec_method is None
    assert (
        config.aec_sink_volume_ceiling_percent
        == DEFAULT_AEC_SINK_VOLUME_CEILING_PERCENT
    )
    assert config.playback_volume_percent == DEFAULT_PLAYBACK_VOLUME_PERCENT
    assert config.aec_test_volume_percent == DEFAULT_AEC_TEST_VOLUME_PERCENT
    assert config.input_queue_bytes == 64 * 1024
    assert config.fallback_buffer_bytes == 64 * 1024
    assert config.io_timeout_seconds == 1.0
    assert config.idle_timeout_seconds == DEFAULT_IDLE_TIMEOUT_SECONDS
    assert config.max_session_seconds == DEFAULT_MAX_SESSION_SECONDS
    assert config.output_queue_bytes == 48 * 1024
    assert "0123456789abcdef" not in repr(config)


def test_config_loads_bounded_mexican_spanish_session_preferences_without_leak(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    private_prompt = (
        "Responde en español de México con un acento mexicano natural y estable."
    )
    _write_config(
        path,
        {
            **_valid_config(),
            "voice": "Cove",
            "prompt": private_prompt,
            "max_message_bytes": 2_048,
            "wake_probability_cutoff": 0.85,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.voice == "cove"
    assert config.prompt == private_prompt
    assert config.wake_probability_cutoff == 0.85
    assert private_prompt not in repr(config)


@pytest.mark.parametrize(
    "barge_in_mode",
    [PROVIDER_CONTROL_BARGE_IN_MODE, UPSTREAM_BARGE_IN_MODE],
)
def test_config_allows_continuous_peer_barge_modes_only_for_native_full_duplex_bridge(
    tmp_path: Path,
    barge_in_mode: str,
) -> None:
    path = tmp_path / "realtime.json"
    base = {
        **_valid_config(),
        "full_duplex": True,
        "capture_backend": NATIVE_AEC3_CAPTURE,
        "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
        "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
        "barge_in_mode": barge_in_mode,
    }
    _write_config(path, base)

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.barge_in_mode == barge_in_mode

    for incompatible, error in (
        ({**base, "full_duplex": False}, "native_aec3 capture requires"),
        (
            {**base, "capture_backend": PULSEAUDIO_AEC_CAPTURE},
            rf"{barge_in_mode} barge_in_mode requires",
        ),
        (
            {**base, "media_transport": DEVICE_WEBRTC_TRANSPORT},
            rf"{barge_in_mode} barge_in_mode requires",
        ),
    ):
        _write_config(path, incompatible)
        with pytest.raises(ConfigError, match=error):
            load_config(path, expected_uid=os.getuid())


def test_config_allows_normal_wake_phrase_only_for_realtime_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "wake_phrase": " Okay   Nabu ",
            "realtime_only": True,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.wake_phrase == "okay nabu"
    assert config.realtime_only is True


def test_realtime_start_message_hardcodes_native_conversation_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, _valid_config())
    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert realtime_start_message(config)["conversation_mode"] == (
        NATIVE_CONVERSATION_MODE
    )


def test_device_webrtc_start_carries_only_direct_offer_and_preferences(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "media_transport": DEVICE_WEBRTC_TRANSPORT,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
            "voice": "Cove",
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    offer = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    assert realtime_start_message(config, webrtc_sdp=offer) == {
        "type": "start",
        "protocol_version": 3,
        "conversation_mode": NATIVE_CONVERSATION_MODE,
        "transport": {"type": "webrtc", "sdp": offer},
        "voice": "cove",
    }


def test_device_webrtc_requires_full_duplex_and_an_offer(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {**_valid_config(), "media_transport": DEVICE_WEBRTC_TRANSPORT},
    )
    with pytest.raises(ConfigError, match="requires full_duplex"):
        load_config(path, expected_uid=os.getuid())

    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "media_transport": DEVICE_WEBRTC_TRANSPORT,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
        },
    )
    config = load_config(path, expected_uid=os.getuid())
    assert config is not None
    with pytest.raises(ConfigError, match="requires an SDP offer"):
        realtime_start_message(config)


def test_config_loads_normalized_device_webrtc_capture_gain(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "media_transport": DEVICE_WEBRTC_TRANSPORT,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
            "direct_capture_gain_db": 6,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.direct_capture_gain_db == 6.0
    assert isinstance(config.direct_capture_gain_db, float)


@pytest.mark.parametrize(
    "media_transport",
    [DEVICE_WEBRTC_TRANSPORT, BRIDGE_PCM_TRANSPORT],
)
def test_config_loads_native_aec3_for_full_duplex_transports(
    tmp_path: Path,
    media_transport: str,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "media_transport": media_transport,
            "capture_backend": NATIVE_AEC3_CAPTURE,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.capture_backend == NATIVE_AEC3_CAPTURE

    _write_config(
        path,
        {**_valid_config(), "capture_backend": NATIVE_AEC3_CAPTURE},
    )
    with pytest.raises(ConfigError, match="requires full_duplex"):
        load_config(path, expected_uid=os.getuid())


@pytest.mark.parametrize(
    "gain_db",
    [DEFAULT_DIRECT_CAPTURE_GAIN_DB, MAX_DIRECT_CAPTURE_GAIN_DB],
)
def test_config_accepts_device_webrtc_capture_gain_boundaries(
    tmp_path: Path,
    gain_db: float,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "media_transport": DEVICE_WEBRTC_TRANSPORT,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
            "direct_capture_gain_db": gain_db,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.direct_capture_gain_db == gain_db


@pytest.mark.parametrize(
    ("gain_db", "message"),
    [
        (True, "must be a number"),
        ("6", "must be a number"),
        (float("nan"), "outside its supported range"),
        (float("inf"), "outside its supported range"),
        (float("-inf"), "outside its supported range"),
        (-0.01, "outside its supported range"),
        (18.01, "outside its supported range"),
    ],
)
def test_config_rejects_invalid_device_webrtc_capture_gain(
    tmp_path: Path,
    gain_db: object,
    message: str,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "media_transport": DEVICE_WEBRTC_TRANSPORT,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
            "direct_capture_gain_db": gain_db,
        },
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path, expected_uid=os.getuid())


def test_config_loads_bridge_pcm_native_aec3_capture_gain(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "media_transport": BRIDGE_PCM_TRANSPORT,
            "capture_backend": NATIVE_AEC3_CAPTURE,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
            "direct_capture_gain_db": 6,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.capture_backend == NATIVE_AEC3_CAPTURE
    assert config.direct_capture_gain_db == 6.0


def test_config_rejects_nonzero_capture_gain_for_pulse_bridge_pcm(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, {**_valid_config(), "direct_capture_gain_db": 6})

    with pytest.raises(ConfigError, match="requires device_webrtc or native_aec3"):
        load_config(path, expected_uid=os.getuid())


def test_conversation_mode_is_not_a_user_configurable_setting(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, {**_valid_config(), "conversation_mode": "managed"})

    with pytest.raises(ConfigError, match="unsupported settings"):
        load_config(path, expected_uid=os.getuid())


def test_explicitly_disabled_secure_config_needs_no_credentials(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, {"enabled": False})

    assert load_config(path, expected_uid=os.getuid()) is None


def test_config_accepts_explicit_turn_taking_and_one_recorder_frame(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    value = {
        **_valid_config(),
        "full_duplex": False,
        "max_message_bytes": 2_048,
    }
    _write_config(path, value)

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.full_duplex is False
    assert config.max_message_bytes == 2_048


def test_config_migrates_legacy_bounded_aec_volume_to_both_controls(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
            "pulse_aec_method": "speex",
            "aec_test_volume_percent": 12,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.full_duplex is True
    assert config.pulse_aec_source == DEFAULT_PULSE_AEC_SOURCE
    assert config.pulse_aec_sink == DEFAULT_PULSE_AEC_SINK
    assert config.pulse_aec_method == "speex"
    assert config.aec_sink_volume_ceiling_percent == 12
    assert config.playback_volume_percent == 12
    assert config.aec_test_volume_percent == 12


@pytest.mark.parametrize("volume_percent", [80, 100])
def test_config_accepts_supported_high_volume_aec_and_playback(
    tmp_path: Path,
    volume_percent: int,
) -> None:
    assert MAX_REALTIME_VOLUME_PERCENT == 100
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
            "aec_sink_volume_ceiling_percent": volume_percent,
            "playback_volume_percent": volume_percent,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.aec_sink_volume_ceiling_percent == volume_percent
    assert config.playback_volume_percent == volume_percent


def test_config_accepts_playback_below_aec_sink_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
            "aec_sink_volume_ceiling_percent": 80,
            "playback_volume_percent": 60,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.aec_sink_volume_ceiling_percent == 80
    assert config.playback_volume_percent == 60
    assert config.aec_test_volume_percent == 60


def test_config_rejects_playback_above_aec_sink_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "aec_sink_volume_ceiling_percent": 40,
            "playback_volume_percent": 60,
        },
    )

    with pytest.raises(ConfigError, match="must not exceed"):
        load_config(path, expected_uid=os.getuid())


def test_legacy_direct_constructor_argument_still_couples_both_controls(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, _valid_config())
    config = load_config(path, expected_uid=os.getuid())
    assert config is not None

    legacy_config = replace(config, aec_test_volume_percent=60)

    assert legacy_config.aec_sink_volume_ceiling_percent == 60
    assert legacy_config.playback_volume_percent == 60
    assert legacy_config.aec_test_volume_percent == 60


def test_direct_config_rejects_volume_above_full_scale(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, _valid_config())
    config = load_config(path, expected_uid=os.getuid())
    assert config is not None

    with pytest.raises(ConfigError, match=r"playback_volume_percent.*1 through 100"):
        replace(config, playback_volume_percent=101)


def test_direct_config_rejects_invalid_wake_probability_cutoff(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, _valid_config())
    config = load_config(path, expected_uid=os.getuid())
    assert config is not None

    with pytest.raises(ConfigError, match=r"wake_probability_cutoff.*0.5 through 0.99"):
        replace(config, wake_probability_cutoff=True)


def test_direct_config_validates_realtime_only_and_normal_phrase(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, _valid_config())
    config = load_config(path, expected_uid=os.getuid())
    assert config is not None

    with pytest.raises(ConfigError, match=r"realtime_only.*boolean"):
        replace(config, realtime_only=1)
    with pytest.raises(ConfigError, match=r"wake_phrase.*normal wake phrase"):
        replace(config, wake_phrase="Okay Nabu")

    realtime_only = replace(
        config,
        wake_phrase=" Okay   Nabu ",
        realtime_only=True,
    )
    assert realtime_only.realtime_only is True


def test_wake_probability_cutoff_preserves_existing_positional_arguments() -> None:
    config = RealtimeConfig(
        "ws://192.0.2.10:8787/v1/realtime",
        "192.0.2.10",
        "token",
        "okay computer",
        1.0,
        2.0,
        1.0,
        10.0,
        30.0,
        5.0,
        2.0,
        64 * 1024,
        64 * 1024,
        48 * 1024,
        64 * 1024,
        False,
        BRIDGE_PCM_TRANSPORT,
        "cove",
    )

    assert config.voice == "cove"
    assert config.realtime_only is False
    assert config.wake_probability_cutoff is None


def test_full_duplex_defaults_to_existing_webrtc_aec_contract(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "full_duplex": True,
            "pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE,
            "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.pulse_aec_method == DEFAULT_PULSE_AEC_METHOD


@pytest.mark.parametrize("mode", [0o400, 0o604, 0o640, 0o666, 0o700])
def test_config_requires_exact_private_mode(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, _valid_config(), mode=mode)

    with pytest.raises(ConfigError, match="mode 0600"):
        load_config(path, expected_uid=os.getuid())


def test_config_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    link = tmp_path / "realtime.json"
    _write_config(target, _valid_config())
    link.symlink_to(target)

    with pytest.raises(ConfigError, match="opened safely"):
        load_config(link, expected_uid=os.getuid())


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"unknown": True}, "unsupported settings"),
        ({"url": "https://192.0.2.10/v1/realtime"}, "ws or wss"),
        ({"url": "ws://user@192.0.2.10/v1/realtime"}, "user information"),
        ({"url": "ws://192.0.2.10/réal-time"}, "ASCII encoded"),
        ({"token": "non-ascii-ñ"}, "ASCII"),
        ({"token": "line\nbreak"}, "control"),
        ({"voice": "cove/preview"}, "ASCII name"),
        ({"voice": "cové"}, "ASCII name"),
        ({"voice": "x" * 65}, "1 through 64"),
        ({"prompt": "   "}, "non-empty text"),
        ({"prompt": "line\nbreak"}, "control characters"),
        ({"prompt": "x" * 1_025}, "up to 1024"),
        ({"wake_phrase": "okay nabu"}, "distinct"),
        ({"realtime_only": 1}, "must be a boolean"),
        ({"wake_probability_cutoff": True}, "must be a number"),
        ({"wake_probability_cutoff": 0.49}, "outside its supported range"),
        ({"wake_probability_cutoff": 1.0}, "outside its supported range"),
        ({"connect_address": "bridge.local"}, "numeric IP"),
        ({"max_message_bytes": 2_047}, "outside its supported range"),
        ({"max_message_bytes": 65_535}, "PCM16 aligned"),
        (
            {"fallback_buffer_bytes": 64 * 1024, "input_queue_bytes": 32 * 1024},
            "must not exceed",
        ),
        ({"input_queue_bytes": 64 * 1024 + 2}, "outside its supported range"),
        ({"io_timeout_seconds": 3.1}, "outside its supported range"),
        ({"full_duplex": 1}, "boolean"),
        ({"media_transport": "rtp"}, "media_transport must be"),
        ({"full_duplex": True}, "requires explicit"),
        ({"pulse_aec_source": DEFAULT_PULSE_AEC_SOURCE}, "requires full_duplex"),
        ({"pulse_aec_method": "speex"}, "requires full_duplex"),
        ({"pulse_aec_method": "null"}, "must be 'adrian', 'speex', or 'webrtc'"),
        ({"pulse_aec_method": "WebRTC"}, "must be 'adrian', 'speex', or 'webrtc'"),
        ({"pulse_aec_method": None}, "must be 'adrian', 'speex', or 'webrtc'"),
        ({"pulse_aec_method": 1}, "must be 'adrian', 'speex', or 'webrtc'"),
        (
            {
                "full_duplex": True,
                "pulse_aec_source": "unsafe-source",
                "pulse_aec_sink": DEFAULT_PULSE_AEC_SINK,
            },
            "safe PulseAudio object name",
        ),
        ({"aec_test_volume_percent": True}, "must be an integer"),
        ({"aec_test_volume_percent": 0}, "outside its supported range"),
        ({"aec_test_volume_percent": 101}, "outside its supported range"),
        ({"playback_volume_percent": True}, "must be an integer"),
        ({"playback_volume_percent": 0}, "outside its supported range"),
        ({"playback_volume_percent": 101}, "outside its supported range"),
        (
            {"aec_sink_volume_ceiling_percent": 101},
            "outside its supported range",
        ),
        (
            {
                "aec_test_volume_percent": DEFAULT_AEC_TEST_VOLUME_PERCENT,
                "playback_volume_percent": DEFAULT_PLAYBACK_VOLUME_PERCENT,
            },
            "cannot be combined",
        ),
    ],
)
def test_config_rejects_unsafe_or_ambiguous_values(
    tmp_path: Path,
    override: dict[str, object],
    message: str,
) -> None:
    path = tmp_path / "realtime.json"
    value = {**_valid_config(), **override}
    _write_config(path, value)

    with pytest.raises(ConfigError, match=message):
        load_config(path, expected_uid=os.getuid())


def test_hostname_url_requires_numeric_connect_address(tmp_path: Path) -> None:
    path = tmp_path / "realtime.json"
    value = {
        **_valid_config(),
        "url": "wss://bridge.example.test/v1/realtime",
        "connect_address": "2001:db8::10",
    }
    _write_config(path, value)

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.connect_address == "2001:db8::10"


def test_config_rejects_start_payload_larger_than_message_bound(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "prompt": "á" * 400,
            "max_message_bytes": 2_048,
        },
    )

    with pytest.raises(ConfigError, match="do not fit within max_message_bytes"):
        load_config(path, expected_uid=os.getuid())


def test_maximum_safe_preferences_fit_the_fixed_text_and_message_bounds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "realtime.json"
    _write_config(
        path,
        {
            **_valid_config(),
            "voice": "v" * 64,
            # A non-BMP code point exercises ensure_ascii's worst-case surrogate
            # expansion while staying inside the device's character bound.
            "prompt": "🎙" * 1_024,
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    encoded = json.dumps(
        realtime_start_message(config), separators=(",", ":"), ensure_ascii=True
    ).encode()
    assert len(encoded) <= 16 * 1_024
    assert len(encoded) <= config.max_message_bytes


def test_normalize_wake_phrase_is_casefolded_and_whitespace_stable() -> None:
    assert normalize_wake_phrase("  OKAY\tComputer  ") == "okay computer"


def test_direct_config_construction_rejects_unverified_full_duplex() -> None:
    with pytest.raises(ConfigError, match="requires explicit"):
        RealtimeConfig(
            url="ws://192.0.2.10:8787/v1/realtime",
            connect_address="192.0.2.10",
            token="token",
            wake_phrase="okay computer",
            connect_timeout_seconds=1.0,
            handshake_timeout_seconds=2.0,
            io_timeout_seconds=1.0,
            idle_timeout_seconds=10.0,
            max_session_seconds=30.0,
            ping_interval_seconds=5.0,
            pong_timeout_seconds=2.0,
            input_queue_bytes=8_192,
            fallback_buffer_bytes=4_096,
            output_queue_bytes=8_192,
            max_message_bytes=4_096,
            full_duplex=True,
        )


def test_direct_config_construction_rejects_non_string_pulse_name() -> None:
    with pytest.raises(ConfigError, match="safe PulseAudio object name"):
        RealtimeConfig(
            url="ws://192.0.2.10:8787/v1/realtime",
            connect_address="192.0.2.10",
            token="token",
            wake_phrase="okay computer",
            connect_timeout_seconds=1.0,
            handshake_timeout_seconds=2.0,
            io_timeout_seconds=1.0,
            idle_timeout_seconds=10.0,
            max_session_seconds=30.0,
            ping_interval_seconds=5.0,
            pong_timeout_seconds=2.0,
            input_queue_bytes=8_192,
            fallback_buffer_bytes=4_096,
            output_queue_bytes=8_192,
            max_message_bytes=4_096,
            full_duplex=True,
            pulse_aec_source=1,  # type: ignore[arg-type]
            pulse_aec_sink=DEFAULT_PULSE_AEC_SINK,
        )
