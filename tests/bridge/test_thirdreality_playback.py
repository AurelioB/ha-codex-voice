from __future__ import annotations

import subprocess
from typing import Any

import pytest

from device.thirdreality.realtime_client.playback import (
    PactlSinkVolumeController,
    PulsePlaybackError,
)


@pytest.mark.parametrize("volume", [37, 80, 100])
def test_pactl_volume_controller_uses_fixed_argv_and_exact_raw_units(
    volume: int,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    raw = 65_536 * volume // 100

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        stdout = (
            f"Volume: mono: {raw} / {volume}% / -8.64 dB\n".encode()
            if "get-sink-volume" in argv
            else b""
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    PactlSinkVolumeController(run=run).set_and_verify(
        "codex_echo_cancel_sink",
        volume,
    )

    assert [call[0] for call in calls] == [
        [
            "/usr/bin/pactl",
            "set-sink-volume",
            "codex_echo_cancel_sink",
            str(raw),
        ],
        [
            "/usr/bin/pactl",
            "get-sink-volume",
            "codex_echo_cancel_sink",
        ],
    ]
    assert all(call[1]["shell"] is False for call in calls)
    assert all(call[1]["timeout"] == 1.0 for call in calls)


@pytest.mark.parametrize("volume", [0, 101, True])
def test_pactl_volume_controller_rejects_out_of_bounds_values(volume: Any) -> None:
    with pytest.raises(PulsePlaybackError, match="out of bounds"):
        PactlSinkVolumeController().set_and_verify("sink", volume)


def test_pactl_volume_controller_fails_closed_on_wrong_result() -> None:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(b"Volume: mono: 1 / 0%\n" if "get-sink-volume" in argv else b""),
        )

    with pytest.raises(PulsePlaybackError, match="verification failed"):
        PactlSinkVolumeController(run=run).set_and_verify("sink", 60)


def test_pactl_volume_controller_maps_timeouts_to_content_free_error() -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(PulsePlaybackError, match="command failed"):
        PactlSinkVolumeController(run=run).set_and_verify("sink", 60)
