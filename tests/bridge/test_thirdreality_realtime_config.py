from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from device.thirdreality.realtime_client.config import (
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
    assert config.voice is None
    assert config.prompt is None
    assert config.full_duplex is False
    assert config.input_queue_bytes == 64 * 1024
    assert config.fallback_buffer_bytes == 64 * 1024
    assert config.io_timeout_seconds == 1.0
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
        },
    )

    config = load_config(path, expected_uid=os.getuid())

    assert config is not None
    assert config.voice == "cove"
    assert config.prompt == private_prompt
    assert private_prompt not in repr(config)


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


@pytest.mark.parametrize("mode", [0o604, 0o640, 0o666])
def test_config_rejects_group_or_other_access(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "realtime.json"
    _write_config(path, _valid_config(), mode=mode)

    with pytest.raises(ConfigError, match="group or other"):
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
        ({"full_duplex": True}, "must be false"),
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
