"""Bounded PulseAudio volume preparation for device WebRTC playback."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

MAX_VOLUME_PERCENT = 100
_PULSE_VOLUME_NORMAL = 65_536
_PACTL_ARGV = ("/usr/bin/pactl",)
_PULSE_RAW_VOLUME = re.compile(rb"([0-9]+)\s*/\s*[0-9]+%")


class PulsePlaybackError(RuntimeError):
    """Raised when the dedicated PulseAudio route cannot be prepared."""


class SinkVolumeController:
    """Prepare one Pulse sink at an exact bounded linear volume."""

    def set_and_verify(self, sink: str, volume_percent: int) -> None:
        """Set every sink channel and verify the resulting raw value."""
        raise NotImplementedError


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class PactlSinkVolumeController(SinkVolumeController):
    """Set one dedicated AEC sink through fixed, bounded ``pactl`` calls."""

    def __init__(self, *, run: Runner = subprocess.run) -> None:
        """Accept an injectable subprocess runner for deterministic tests."""
        self._run = run

    def set_and_verify(self, sink: str, volume_percent: int) -> None:
        """Set the virtual AEC sink and require exact raw-volume confirmation."""
        if (
            isinstance(volume_percent, bool)
            or not isinstance(volume_percent, int)
            or not 1 <= volume_percent <= MAX_VOLUME_PERCENT
        ):
            raise PulsePlaybackError("PulseAudio sink volume is out of bounds")
        raw_volume = _PULSE_VOLUME_NORMAL * volume_percent // 100
        try:
            set_result = self._run(
                [*_PACTL_ARGV, "set-sink-volume", sink, str(raw_volume)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
                shell=False,
            )
            if set_result.returncode != 0:
                raise PulsePlaybackError("PulseAudio sink volume could not be set")
            get_result = self._run(
                [*_PACTL_ARGV, "get-sink-volume", sink],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PulsePlaybackError("PulseAudio sink volume command failed") from exc
        if get_result.returncode != 0:
            raise PulsePlaybackError("PulseAudio sink volume could not be verified")
        values = [int(value) for value in _PULSE_RAW_VOLUME.findall(get_result.stdout)]
        if not values or any(value != raw_volume for value in values):
            raise PulsePlaybackError("PulseAudio sink volume verification failed")
