"""Bounded in-process realtime session for the ThirdReality voice client."""

from __future__ import annotations

import json
import logging
import os
import re
import select
import shlex
import struct
import subprocess
import syslog
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import Enum
from math import isqrt
from typing import Any, NoReturn, Protocol

from .config import (
    BRIDGE_PCM_TRANSPORT,
    DEVICE_WEBRTC_TRANSPORT,
    NATIVE_AEC3_CAPTURE,
    NATIVE_CONVERSATION_MODE,
    PULSEAUDIO_AEC_CAPTURE,
    RealtimeConfig,
    realtime_start_message,
)
from .playback import (
    PactlSinkVolumeController,
    PulsePlaybackError,
    SinkVolumeController,
)
from .sidecar import (
    ControlMessage,
    PlaybackAudio,
    SidecarError,
    WebRtcSidecarClient,
)
from .websocket import Message, WebSocketClosed, WebSocketConnection, WebSocketError

_LOGGER = logging.getLogger("linux_voice_assistant.realtime")
_INPUT_BYTES_PER_SECOND = 16_000 * 2
_INPUT_ACTIVITY_SIGNAL_PEAK = 256
_LOCAL_BARGE_IN_SIGNAL_PEAK = 1_024
_LOCAL_BARGE_IN_SIGNAL_RMS = 384
_NATIVE_AEC3_LOCAL_BARGE_IN_POST_GAIN_PEAK = 256
_NATIVE_AEC3_LOCAL_BARGE_IN_POST_GAIN_RMS = 96
_LOCAL_BARGE_IN_FRAMES = 2
_LOCAL_BARGE_IN_AMBIGUOUS_FRAMES = 4
_LOCAL_BARGE_IN_REARM_QUIET_FRAMES = 8
_LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS = 0.512
_LOCAL_BARGE_IN_ANCHOR_REPAIR_SETTLE_SECONDS = 0.128
_LOCAL_BARGE_IN_ANCHOR_REPAIR_MAX_EVIDENCE_FRAMES = 8
_RENDER_ECHO_FEATURE_RATE = 4_000
_RENDER_ECHO_RENDER_DOWNSAMPLE = 6
_RENDER_ECHO_CAPTURE_DOWNSAMPLE = 4
_RENDER_ECHO_RING_SAMPLES = 4_096
_RENDER_ECHO_NOMINAL_PLAYOUT_SECONDS = 0.060
_RENDER_ECHO_DISCONTINUITY_SECONDS = 0.100
_RENDER_ECHO_MIN_DELAY_SAMPLES = 20 * _RENDER_ECHO_FEATURE_RATE // 1_000
_RENDER_ECHO_MAX_DELAY_SAMPLES = 320 * _RENDER_ECHO_FEATURE_RATE // 1_000
_RENDER_ECHO_COARSE_STEP_SAMPLES = 4 * _RENDER_ECHO_FEATURE_RATE // 1_000
_RENDER_ECHO_REFINE_STEP_SAMPLES = _RENDER_ECHO_FEATURE_RATE // 1_000
_RENDER_ECHO_FIR_TAPS = 24
_RENDER_ECHO_CALIBRATION_FRAMES = 3
_RENDER_ECHO_LOCKED_DELAY_RADIUS_SAMPLES = 24 * _RENDER_ECHO_FEATURE_RATE // 1_000
_RENDER_ECHO_MIN_RMS = 64
_RENDER_ECHO_CORRELATION_PERMILLE = 600
_RENDER_ECHO_NEAR_END_CORRELATION_PERMILLE = 350
_RENDER_ECHO_MAX_RESIDUAL_PERCENT = 45
_RENDER_ECHO_NEAR_END_RESIDUAL_PERCENT = 55
_RENDER_ECHO_NEAR_END_RESIDUAL_RMS = 256
_RENDER_ECHO_NLMS_STEP = 0.05
_RENDER_ECHO_NLMS_LEAKAGE = 0.9995
_PLAYBACK_VOLUME_RAMP_SAMPLES = 24_000 * 40 // 1_000
_MAX_INPUT_CATCH_UP_RATE = 2.0
_PACTL_ARGV = ("/usr/bin/pactl",)
_PULSE_ECHO_CANCEL_MODULE = "module-echo-cancel"
_PULSE_SOURCE_MASTER = "alsa_input.hw_0_2"
_PULSE_SINK_MASTER = "alsa_output.hw_0_1"
_PULSE_NATIVE_DRIVER = "protocol-native.c"
_PULSE_VOLUME_RAW = re.compile(r"([0-9]+)\s*/\s*[0-9]+%\s*/")
_PULSE_VOLUME_NORM = 65_536
_PHYSICAL_ANCHOR_REPAIR_TIMEOUT_SECONDS = 0.075
_MEDIA_ANCHOR_REPAIR_TIMEOUT_SECONDS = 0.250
_PAPLAY_ARGV = (
    "/usr/bin/paplay",
    "--raw",
    "--rate=24000",
    "--format=s16le",
    "--channels=1",
    "--latency-msec=60",
    "--process-time-msec=20",
)
_NETWORK_TICK_SECONDS = 0.02
_DIRECT_HANDSHAKE_TICK_SECONDS = 0.01
_DIRECT_CAPTURE_MAX_AGE_SECONDS = 2.25
_DIRECT_STARTUP_CAPTURE_MAX_AGE_SECONDS = 5.0
# Retain the two 64 ms / 2 KiB recorder frames that prove local speech. A
# larger history spends the replacement peer's independent 2.25-second RTP
# freshness budget without adding useful onset evidence.
_DIRECT_ROLLOVER_PREROLL_BYTES = 4 * 1024
_PLAYER_WRITE_BYTES = 24_000 * 2 * 20 // 1_000
_PLAYER_REAP_SECONDS = 0.5
_CONTROL_EVENTS = frozenset(
    {
        "input_audio_buffer.committed",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "output_audio_buffer.started",
        "output_audio_buffer.stopped",
        "response.created",
        "response.cancelled",
        "response.done",
        "session.started",
        "session.updated",
        "speaking.started",
        "speaking.stopped",
        "turn.created",
        "turn.done",
        "media.quiet",
        "media.started",
    }
)
_DIRECT_SEMANTIC_LIFECYCLE_EVENTS = frozenset(
    {
        "input_audio_buffer.committed",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "output_audio_buffer.started",
        "output_audio_buffer.stopped",
        "response.created",
        "response.cancelled",
        "response.done",
        "speaking.started",
        "speaking.stopped",
        "turn.created",
        "turn.done",
    }
)
_DIRECT_DIAGNOSTIC_LIFECYCLE_EVENTS = _CONTROL_EVENTS | frozenset(
    {
        "capture.direction.inactive",
        "capture.direction.recvonly",
        "capture.direction.sendonly",
        "capture.direction.sendrecv",
        "capture.direction.unknown",
        "capture.outbound_active",
        "capture.rtp_started",
        "error",
        "interrupt.fenced",
        "invalid_request_error",
        "playback.rtp_started",
    }
)
_DIRECT_DIAGNOSTIC_LIFECYCLE_COUNT_MAX = 999_999
_DIRECT_DIAGNOSTIC_PHASES = frozenset(
    {
        "preflight",
        "sidecar_offer",
        "bridge_connect",
        "bridge_answer",
        "peer_handshake",
        "bridge_ready",
        "runtime",
        "rollover",
        "remote_stop",
    }
)
_DIRECT_SYSLOG_STATUSES = frozenset({"ready", "waiting_output", "terminal"})
_DIRECT_SYSLOG_OUTCOMES = frozenset(
    {"live", "failed", "interrupted", "stopped", "remote_stopped"}
)
_DIRECT_SIDECAR_FAILURE_CODES = frozenset(
    {
        "answer_failed",
        "answer_state_invalid",
        "capture_audio_stale",
        "capture_metrics_output_failed",
        "capture_outside_session",
        "capture_rejected",
        "connection_failed",
        "control_direction_invalid",
        "data_channel_closed",
        "interrupt_failed",
        "interrupt_state_invalid",
        "ipc_send_failed",
        "lifecycle_output_failed",
        "media_fence_capture_timeout",
        "media_fence_timeout",
        "offer_failed",
        "offer_state_invalid",
        "output_backpressure",
        "packet_direction_invalid",
        "packet_too_large",
        "partial_packet",
        "peer_initialization_failed",
        "protocol_error",
        "provider_error",
        "receiver_boundary_reset_failed",
        "remote_audio_failed",
        "state_invalid",
        "state_output_failed",
        "stop_failed",
        "unexpected_media_track",
        "unsupported_receiver_boundary",
    }
)
_DIRECT_SYSLOG_INTERVAL_SECONDS = 5.0
_DIRECT_SYSLOG_COUNTER_MAX = 99_999_999
_DIRECT_SYSLOG_RECORD_MAX_BYTES = 220


class SessionState(Enum):
    """Externally observable lifecycle without private failure details."""

    NEW = "new"
    CONNECTING = "connecting"
    READY = "ready"
    INTERRUPTING = "interrupting"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class SubmitResult(Enum):
    """Result of a non-blocking microphone submission."""

    ACCEPTED = "accepted"
    GATED = "gated"
    FULL = "full"
    CLOSED = "closed"
    INVALID = "invalid"


class _EchoDecisionKind(Enum):
    """Content-free result from the local render-aware double-talk guard."""

    ECHO = "echo"
    NEAR_END = "near_end"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class _EchoDecision:
    """One epoch-scoped render comparison without retained capture content."""

    kind: _EchoDecisionKind
    epoch: int
    correlation_permille: int = 0
    delay_ms: int = 0
    reference_matched: bool = False


@dataclass(frozen=True, slots=True)
class _CaptureDisposition:
    """One raw capture frame's local and current-peer routing outcome."""

    local_interrupt: bool = False
    suppress_peer_epoch: int | None = None
    suppress_bridge: bool = False


@dataclass(frozen=True, slots=True)
class _AudioPacket:
    data: bytes
    captured_at: float
    capture_watermark: int
    suppress_peer_epoch: int | None = None
    suppress_bridge: bool = False


@dataclass(slots=True)
class _DirectStandby:
    """One offer-warm logical peer inside the active sidecar process."""

    sidecar: WebRtcSidecarClient
    offer_sdp: str | None = None
    peer_epoch: int | None = None


@dataclass(slots=True)
class _DirectPlaybackState:
    """Network-thread-owned generation and playout accounting."""

    active_generation: int | None = None
    newest_generation: int = 0
    retired_generation: int = 0
    expected_sample_index: int | None = None


@dataclass(slots=True)
class _DirectSessionDiagnostics:
    """Content-free aggregate diagnostics for one direct-media session."""

    started_at: float
    phase: str = "preflight"
    handshake_ready: bool = False
    peer_answer_applied: bool = False
    peer_connected: bool = False
    peer_data_ready: bool = False
    capture_packets: int = 0
    capture_bytes: int = 0
    capture_max_peak: int = 0
    capture_max_rms: int = 0
    capture_signal_frames: int = 0
    post_gain_max_peak: int = 0
    post_gain_max_rms: int = 0
    clipped_samples: int = 0
    clipped_frames: int = 0
    playback_signal_packets: int = 0
    playback_signal_bytes: int = 0
    playback_max_peak: int = 0
    playback_max_rms: int = 0
    echo_rejected_frames: int = 0
    echo_near_end_frames: int = 0
    echo_ambiguous_frames: int = 0
    provider_suppressed_frames: int = 0
    echo_max_correlation_permille: int = 0
    echo_last_delay_ms: int = 0
    lifecycle_events: dict[str, int] = field(default_factory=dict)
    failure_code: str | None = None
    _echo_lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def observe_peer_state(self, value: str) -> None:
        """Retain only the three fixed initial peer-readiness milestones."""
        if value == "answer.applied":
            self.peer_answer_applied = True
        elif value == "connected":
            self.peer_connected = True
        elif value == "data.ready":
            self.peer_data_ready = True

    def observe_capture(self, value: bytes, *, has_signal: bool) -> None:
        """Account for original microphone PCM after its IPC send succeeds."""
        peak, rms = _pcm_peak_and_rms(value)
        self.capture_packets += 1
        self.capture_bytes += len(value)
        self.capture_max_peak = max(self.capture_max_peak, peak)
        self.capture_max_rms = max(self.capture_max_rms, rms)
        if has_signal:
            self.capture_signal_frames += 1

    def observe_playback(self, value: bytes) -> None:
        """Account only for speech-scale playback received from the child."""
        if not _pcm_has_signal(value):
            return
        peak, rms = _pcm_peak_and_rms(value)
        self.playback_signal_packets += 1
        self.playback_signal_bytes += len(value)
        self.playback_max_peak = max(self.playback_max_peak, peak)
        self.playback_max_rms = max(self.playback_max_rms, rms)

    def observe_echo_decision(self, decision: _EchoDecision) -> None:
        """Retain only bounded content-free render-comparison aggregates."""
        with self._echo_lock:
            if decision.kind is _EchoDecisionKind.ECHO:
                self.echo_rejected_frames = min(
                    self.echo_rejected_frames + 1,
                    _DIRECT_SYSLOG_COUNTER_MAX,
                )
            elif decision.kind is _EchoDecisionKind.NEAR_END:
                self.echo_near_end_frames = min(
                    self.echo_near_end_frames + 1,
                    _DIRECT_SYSLOG_COUNTER_MAX,
                )
            else:
                self.echo_ambiguous_frames = min(
                    self.echo_ambiguous_frames + 1,
                    _DIRECT_SYSLOG_COUNTER_MAX,
                )
            self.echo_max_correlation_permille = max(
                self.echo_max_correlation_permille,
                min(1_000, max(0, decision.correlation_permille)),
            )
            self.echo_last_delay_ms = min(320, max(0, decision.delay_ms))

    def observe_provider_suppression(self) -> None:
        """Count one equal-length silence substitution without retaining PCM."""
        with self._echo_lock:
            self.provider_suppressed_frames = min(
                self.provider_suppressed_frames + 1,
                _DIRECT_SYSLOG_COUNTER_MAX,
            )

    def echo_summary(self) -> tuple[int, int, int, int, int, int]:
        """Snapshot capture-thread counters for network-thread diagnostics."""
        with self._echo_lock:
            return (
                self.echo_rejected_frames,
                self.echo_near_end_frames,
                self.echo_ambiguous_frames,
                self.provider_suppressed_frames,
                self.echo_max_correlation_permille,
                self.echo_last_delay_ms,
            )

    def observe_capture_metrics(
        self,
        values: dict[str, str | int | bool | float],
    ) -> None:
        """Merge one strict sidecar interval without retaining microphone PCM."""
        peak = values.get("post_gain_max_peak")
        rms = values.get("post_gain_max_rms")
        clipped_samples = values.get("clipped_samples")
        clipped_frames = values.get("clipped_frames")
        if not all(
            type(value) is int for value in (peak, rms, clipped_samples, clipped_frames)
        ):
            return
        assert isinstance(peak, int)
        assert isinstance(rms, int)
        assert isinstance(clipped_samples, int)
        assert isinstance(clipped_frames, int)
        self.post_gain_max_peak = max(self.post_gain_max_peak, peak)
        self.post_gain_max_rms = max(self.post_gain_max_rms, rms)
        self.clipped_samples = min(
            _DIRECT_SYSLOG_COUNTER_MAX,
            self.clipped_samples + clipped_samples,
        )
        self.clipped_frames = min(
            _DIRECT_SYSLOG_COUNTER_MAX,
            self.clipped_frames + clipped_frames,
        )

    def observe_lifecycle(self, event_type: object) -> None:
        """Count only fixed safe event names, collapsing everything else."""
        key = (
            event_type
            if isinstance(event_type, str)
            and event_type in _DIRECT_DIAGNOSTIC_LIFECYCLE_EVENTS
            else "other"
        )
        count = self.lifecycle_events.get(key, 0)
        self.lifecycle_events[key] = min(
            count + 1,
            _DIRECT_DIAGNOSTIC_LIFECYCLE_COUNT_MAX,
        )

    def observe_failure_code(self, value: object) -> None:
        """Preserve only one fixed child error classification."""
        if self.failure_code is not None:
            return
        self.failure_code = (
            value
            if isinstance(value, str) and value in _DIRECT_SIDECAR_FAILURE_CODES
            else "unknown"
        )

    def lifecycle_summary(self) -> str:
        """Return one bounded, deterministically ordered safe count string."""
        if not self.lifecycle_events:
            return "none"
        return ",".join(
            f"{event_type}:{self.lifecycle_events[event_type]}"
            for event_type in sorted(self.lifecycle_events)
        )


def _emit_direct_syslog_status(
    diagnostics: _DirectSessionDiagnostics,
    *,
    status: str,
    duration_ms: int,
    outcome: str,
) -> None:
    """Emit compact fixed-schema direct-media status records."""
    if status not in _DIRECT_SYSLOG_STATUSES or outcome not in _DIRECT_SYSLOG_OUTCOMES:
        return
    phase = (
        diagnostics.phase
        if diagnostics.phase in _DIRECT_DIAGNOSTIC_PHASES
        else "unknown"
    )

    def bounded(value: int) -> int:
        return min(max(0, value), _DIRECT_SYSLOG_COUNTER_MAX)

    def pcm_level(value: int) -> int:
        return min(32_768, bounded(value))

    def lifecycle_count(event_type: str) -> int:
        return min(
            bounded(diagnostics.lifecycle_events.get(event_type, 0)),
            _DIRECT_DIAGNOSTIC_LIFECYCLE_COUNT_MAX,
        )

    (
        echo_rejected_frames,
        echo_near_end_frames,
        echo_ambiguous_frames,
        provider_suppressed_frames,
        echo_max_correlation_permille,
        echo_last_delay_ms,
    ) = diagnostics.echo_summary()

    # Keep the state needed to classify an incident in the first record and
    # repeat the bounded status on every continuation. All labels and string
    # values come from fixed vocabularies; only non-negative counters vary.
    prefix = f"codex-voice direct_webrtc_status={status}"
    records = [
        (
            f"{prefix} record=state phase={phase} outcome={outcome} "
            f"duration_ms={bounded(duration_ms)} "
            f"handshake_ready={'yes' if diagnostics.handshake_ready else 'no'} "
            "peer_answer_applied="
            f"{'yes' if diagnostics.peer_answer_applied else 'no'} "
            f"peer_connected={'yes' if diagnostics.peer_connected else 'no'} "
            f"peer_data_ready={'yes' if diagnostics.peer_data_ready else 'no'}"
        ),
        (
            f"{prefix} record=media "
            f"capture_sent_packets={bounded(diagnostics.capture_packets)} "
            f"capture_signal_frames={bounded(diagnostics.capture_signal_frames)} "
            "playback_signal_packets="
            f"{bounded(diagnostics.playback_signal_packets)}"
        ),
        (
            f"{prefix} record=levels "
            f"capture_max_peak={pcm_level(diagnostics.capture_max_peak)} "
            f"capture_max_rms={pcm_level(diagnostics.capture_max_rms)} "
            f"playback_max_peak={pcm_level(diagnostics.playback_max_peak)} "
            f"playback_max_rms={pcm_level(diagnostics.playback_max_rms)}"
        ),
        (
            f"{prefix} record=gain "
            f"post_gain_max_peak={pcm_level(diagnostics.post_gain_max_peak)} "
            f"post_gain_max_rms={pcm_level(diagnostics.post_gain_max_rms)} "
            f"clipped_samples={bounded(diagnostics.clipped_samples)} "
            f"clipped_frames={bounded(diagnostics.clipped_frames)}"
        ),
        (
            f"{prefix} record=echo "
            f"rejected={bounded(echo_rejected_frames)} "
            f"near={bounded(echo_near_end_frames)} "
            f"ambiguous={bounded(echo_ambiguous_frames)} "
            f"provider_suppressed={bounded(provider_suppressed_frames)} "
            "max_corr_pm="
            f"{min(1_000, bounded(echo_max_correlation_permille))} "
            f"delay_ms={min(320, bounded(echo_last_delay_ms))}"
        ),
        (
            f"{prefix} record=transport "
            "direction_sendrecv="
            f"{lifecycle_count('capture.direction.sendrecv')} "
            "direction_sendonly="
            f"{lifecycle_count('capture.direction.sendonly')} "
            "direction_recvonly="
            f"{lifecycle_count('capture.direction.recvonly')} "
            "direction_inactive="
            f"{lifecycle_count('capture.direction.inactive')} "
            "direction_unknown="
            f"{lifecycle_count('capture.direction.unknown')} "
            "outbound_active="
            f"{lifecycle_count('capture.outbound_active')}"
        ),
        (
            f"{prefix} record=events_1 "
            f"capture.rtp_started={lifecycle_count('capture.rtp_started')} "
            f"playback.rtp_started={lifecycle_count('playback.rtp_started')} "
            "input_audio_buffer.speech_started="
            f"{lifecycle_count('input_audio_buffer.speech_started')} "
            "input_audio_buffer.speech_stopped="
            f"{lifecycle_count('input_audio_buffer.speech_stopped')}"
        ),
        (
            f"{prefix} record=events_2 "
            f"response.created={lifecycle_count('response.created')} "
            f"response.done={lifecycle_count('response.done')} "
            f"turn.created={lifecycle_count('turn.created')} "
            f"turn.done={lifecycle_count('turn.done')} "
            "output_audio_buffer.started="
            f"{lifecycle_count('output_audio_buffer.started')} "
            "output_audio_buffer.stopped="
            f"{lifecycle_count('output_audio_buffer.stopped')}"
        ),
    ]
    if status == "terminal" and outcome == "failed":
        failure_code = diagnostics.failure_code or "unknown"
        records.append(f"{prefix} record=failure code={failure_code}")
    for message in records:
        with suppress(Exception):  # diagnostics must never affect live media
            encoded = message.encode("ascii")
            if len(encoded) <= _DIRECT_SYSLOG_RECORD_MAX_BYTES:
                syslog.syslog(syslog.LOG_INFO, message)


class _DirectPendingOutput:
    """Bound replacement output until the bridge admits its peer epoch."""

    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._bytes = 0
        self._items: deque[ControlMessage | PlaybackAudio] = deque()

    def append(self, message: ControlMessage | PlaybackAudio) -> None:
        """Retain one ordered item or fail instead of exceeding the playout bound."""
        size = self._logical_size(message)
        if self._bytes + size > self._maximum_bytes:
            raise SidecarError("replacement output exceeded its pre-ack bound")
        self._items.append(message)
        self._bytes += size

    def drain(self) -> tuple[ControlMessage | PlaybackAudio, ...]:
        """Release the complete ordered batch after exact epoch acknowledgement."""
        items = tuple(self._items)
        self._items.clear()
        self._bytes = 0
        return items

    @staticmethod
    def _logical_size(message: ControlMessage | PlaybackAudio) -> int:
        if isinstance(message, PlaybackAudio):
            return 32 + len(message.pcm)
        size = 32 + len(message.type.encode("utf-8"))
        for key, value in message.values.items():
            size += len(key.encode("utf-8")) + len(str(value).encode("utf-8"))
        return size


class _BoundedAudioQueue:
    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._bytes = 0
        self._items: deque[_AudioPacket] = deque()
        self._lock = threading.Lock()

    def put(self, packet: _AudioPacket) -> bool:
        with self._lock:
            if self._bytes + len(packet.data) > self._maximum_bytes:
                return False
            self._items.append(packet)
            self._bytes += len(packet.data)
            return True

    def pop(self) -> tuple[_AudioPacket | None, int]:
        with self._lock:
            if not self._items:
                return None, 0
            packet = self._items.popleft()
            self._bytes -= len(packet.data)
            return packet, len(self._items)

    def replace_tail(
        self,
        expected: _AudioPacket,
        replacement: _AudioPacket,
    ) -> bool:
        """Annotate the newest admitted packet without changing queue pressure."""
        if len(expected.data) != len(replacement.data):
            return False
        with self._lock:
            if not self._items or self._items[-1] is not expected:
                return False
            self._items[-1] = replacement
            return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def drain(self) -> list[_AudioPacket]:
        """Atomically transfer every packet while preserving admission order."""
        with self._lock:
            packets = list(self._items)
            self._items.clear()
            self._bytes = 0
            return packets

    def prepend(self, packets: list[_AudioPacket]) -> bool:
        """Atomically prepend packets, rejecting the whole operation on pressure."""
        if not packets:
            return True
        added_bytes = sum(len(packet.data) for packet in packets)
        with self._lock:
            if self._bytes + added_bytes > self._maximum_bytes:
                return False
            self._items.extendleft(reversed(packets))
            self._bytes += added_bytes
            return True

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes


class _AudioPacer:
    """Prevent buffered microphone frames from bursting after startup."""

    def __init__(self) -> None:
        self._next_send_at: float | None = None

    def due(self, now: float) -> bool:
        return self._next_send_at is None or now >= self._next_send_at

    def sent(self, now: float, byte_count: int, *, catching_up: bool = False) -> None:
        # Anchor every frame at its actual send time. A delayed loop therefore
        # never sends several queued capture blocks back-to-back to catch up.
        rate = _MAX_INPUT_CATCH_UP_RATE if catching_up else 1.0
        self._next_send_at = now + byte_count / (_INPUT_BYTES_PER_SECOND * rate)

    def delay(self, now: float) -> float:
        if self._next_send_at is None:
            return 0.0
        return max(0.0, self._next_send_at - now)


class _PlayerLike(Protocol):
    """Playback surface shared by rollback and direct-media sessions."""

    def begin(self, epoch: int) -> None: ...

    def resume(self, epoch: int) -> None: ...

    def prepare(self) -> None: ...

    def enqueue(self, value: bytes) -> None: ...

    def finish(self, epoch: int) -> None: ...

    def abort(self) -> None: ...

    def service(self) -> None: ...

    @property
    def active(self) -> bool: ...


class _PlaybackAttenuator:
    """Apply bounded click-free software volume below one fixed AEC anchor."""

    def __init__(self, anchor_percent: int) -> None:
        if type(anchor_percent) is not int or not 1 <= anchor_percent <= 100:
            raise ValueError("playback volume anchor is invalid")
        self._anchor_percent = anchor_percent
        self._current_gain = 1.0
        self._target_gain = 1.0
        self._ramp_remaining = 0
        self._lock = threading.Lock()

    def request(self, volume_percent: int, *, ramp: bool) -> int:
        """Set a non-amplifying target and return the safely clamped percent."""
        if type(volume_percent) is not int:
            raise ValueError("playback volume must be an integer")
        applied = min(max(0, volume_percent), self._anchor_percent)
        ratio = applied / self._anchor_percent
        # PulseAudio's user-facing software volume follows its cubic curve.
        # Reproduce that curve below the fixed AEC anchor so a requested 30%
        # remains perceptually equivalent to the device's prior 30% setting.
        target_gain = ratio * ratio * ratio
        with self._lock:
            if not ramp:
                self._target_gain = target_gain
                self._current_gain = target_gain
                self._ramp_remaining = 0
            elif self._target_gain == target_gain:
                return applied
            elif self._current_gain == target_gain:
                self._target_gain = target_gain
                self._ramp_remaining = 0
            else:
                self._target_gain = target_gain
                self._ramp_remaining = _PLAYBACK_VOLUME_RAMP_SAMPLES
        return applied

    def scale(self, value: bytes) -> bytes:
        """Attenuate aligned mono PCM16 without ever amplifying or clipping it."""
        if not isinstance(value, bytes) or len(value) % 2:
            raise ValueError("playback PCM must contain aligned bytes")
        if not value:
            return value
        with self._lock:
            current = self._current_gain
            target = self._target_gain
            remaining = self._ramp_remaining
            if remaining == 0:
                if target == 1.0:
                    return value
                return _scale_pcm16(value, target)

            samples = len(value) // 2
            ramp_samples = min(samples, remaining)
            step = (target - current) / remaining
            scaled = bytearray(len(value))
            for index, (sample,) in enumerate(struct.iter_unpack("<h", value)):
                if index < ramp_samples:
                    gain = current + step * (index + 1)
                else:
                    gain = target
                struct.pack_into("<h", scaled, index * 2, round(sample * gain))
            self._ramp_remaining = remaining - ramp_samples
            self._current_gain = (
                target if self._ramp_remaining == 0 else current + step * ramp_samples
            )
            return bytes(scaled)


class _RenderEchoGuard:
    """Reject render-correlated local barge-in without touching capture PCM."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._epoch: int | None = None
        self._render_samples: deque[int] = deque()
        self._render_start_time: float | None = None
        self._render_end_time: float | None = None
        self._render_sample_tail: list[int] = []
        self._fir = [0.0] * _RENDER_ECHO_FIR_TAPS
        self._fir_valid_frames = 0
        self._calibration_delays: deque[int] = deque(
            maxlen=_LOCAL_BARGE_IN_ANCHOR_REPAIR_MAX_EVIDENCE_FRAMES
        )
        self._stable_delay_samples: int | None = None
        self._repair_generation = 0
        self._repair_active = False
        self._repair_qualified = False
        self._repair_seeded = False
        self._model_updates_frozen = threading.Event()

    def freeze_model_updates(self, timeout: float) -> bool:
        """Linearize a capture-adaptation fence within one caller budget."""
        self._model_updates_frozen.set()
        if not self._lock.acquire(timeout=max(0.0, timeout)):
            return False
        self._lock.release()
        return True

    def thaw_model_updates(self) -> None:
        """Allow capture adaptation after the transition boundary is published."""
        self._model_updates_frozen.clear()

    def begin_epoch(self, epoch: int, *, reset: bool) -> None:
        """Publish an output epoch and isolate its partial render framing."""
        with self._lock:
            if reset:
                self._clear_render_locked(reset_model=True)
            else:
                self._render_sample_tail.clear()
            self._epoch = epoch

    def deactivate(self) -> None:
        """Stop classifying while retaining a reusable player's recent tail."""
        with self._lock:
            self._epoch = None

    def reset(self) -> None:
        """Forget all render/model state at an abort or peer boundary."""
        with self._lock:
            self._epoch = None
            self._clear_render_locked(reset_model=True)

    def repair_boundary(self, epoch: int | None) -> bool:
        """Start an untrusted post-repair model generation from a safe FIR seed."""
        with self._lock:
            had_seed = (
                self._fir_valid_frames >= _RENDER_ECHO_CALIBRATION_FRAMES
                and self._stable_delay_samples is not None
            )
            seed = list(self._fir) if had_seed else None
            self._clear_render_locked(reset_model=True)
            if seed is not None:
                self._fir = seed
            self._repair_generation += 1
            self._repair_active = True
            self._repair_qualified = False
            self._repair_seeded = had_seed
            self._epoch = epoch
            return had_seed

    def repair_status(self, epoch: int) -> tuple[bool, bool]:
        """Return whether the current epoch belongs to a repair generation."""
        with self._lock:
            if self._epoch != epoch or not self._repair_active:
                return False, False
            return True, self._repair_qualified

    def observe_render(self, value: bytes, *, written_at: float) -> None:
        """Retain bounded 4 kHz features from exact PCM accepted by paplay."""
        if not value:
            return
        samples = [sample for (sample,) in struct.iter_unpack("<h", value)]
        with self._lock:
            combined = self._render_sample_tail + samples
            usable = (
                len(combined) // _RENDER_ECHO_RENDER_DOWNSAMPLE
            ) * _RENDER_ECHO_RENDER_DOWNSAMPLE
            self._render_sample_tail = combined[usable:]
            if not usable:
                return
            features = [
                sum(combined[index : index + _RENDER_ECHO_RENDER_DOWNSAMPLE])
                // _RENDER_ECHO_RENDER_DOWNSAMPLE
                for index in range(0, usable, _RENDER_ECHO_RENDER_DOWNSAMPLE)
            ]
            proposed_start = written_at + _RENDER_ECHO_NOMINAL_PLAYOUT_SECONDS
            render_end = self._render_end_time
            if (
                render_end is None
                or proposed_start > render_end + _RENDER_ECHO_DISCONTINUITY_SECONDS
            ):
                # A reusable paplay child may remain alive across an ordinary
                # inter-response gap. Rebase only the timestamped render ring;
                # the already qualified acoustic model remains valid for the
                # next media epoch. Hard epoch/session boundaries call reset().
                self._clear_render_locked(reset_model=False)
                self._render_start_time = proposed_start
                self._render_end_time = proposed_start
                render_end = proposed_start
            elif proposed_start > render_end:
                gap_samples = round(
                    (proposed_start - render_end) * _RENDER_ECHO_FEATURE_RATE
                )
                if gap_samples:
                    self._append_render_locked([0] * gap_samples)
            self._append_render_locked(features)

    def classify(  # noqa: C901 - one bounded render/capture decision pipeline
        self,
        value: bytes,
        *,
        captured_at: float,
        output_epoch: int,
        calibrating: bool,
        update_model: bool = True,
    ) -> _EchoDecision | None:
        """Classify one signal-bearing capture frame against recent render."""
        capture_samples = [sample for (sample,) in struct.iter_unpack("<h", value)]
        usable = (
            len(capture_samples) // _RENDER_ECHO_CAPTURE_DOWNSAMPLE
        ) * _RENDER_ECHO_CAPTURE_DOWNSAMPLE
        if not usable:
            return None
        capture = tuple(
            sum(capture_samples[index : index + _RENDER_ECHO_CAPTURE_DOWNSAMPLE])
            // _RENDER_ECHO_CAPTURE_DOWNSAMPLE
            for index in range(0, usable, _RENDER_ECHO_CAPTURE_DOWNSAMPLE)
        )
        with self._lock:
            if self._epoch != output_epoch:
                return None
            render = tuple(self._render_samples)
            render_start_time = self._render_start_time
            weights = tuple(self._fir)
            fir_valid_frames = self._fir_valid_frames
            stable_delay = self._stable_delay_samples
            repair_generation = self._repair_generation
            repair_pending = self._repair_active and not self._repair_qualified
            repair_seeded = self._repair_seeded
        if render_start_time is None or not render:
            return _EchoDecision(_EchoDecisionKind.NEAR_END, output_epoch)
        calibrated = (
            fir_valid_frames >= _RENDER_ECHO_CALIBRATION_FRAMES
            and stable_delay is not None
        )
        training = calibrating or repair_pending
        if not training and not calibrated:
            # An untrained same-frame fit could erase genuine double-talk.
            return _EchoDecision(_EchoDecisionKind.NEAR_END, output_epoch)

        capture_mean = sum(capture) // len(capture)
        centered_capture = tuple(sample - capture_mean for sample in capture)
        capture_energy = sum(sample * sample for sample in centered_capture)
        if capture_energy == 0:
            return _EchoDecision(_EchoDecisionKind.NEAR_END, output_epoch)
        capture_start_time = captured_at - len(capture) / _RENDER_ECHO_FEATURE_RATE
        render_floor_energy = _RENDER_ECHO_MIN_RMS * _RENDER_ECHO_MIN_RMS * len(capture)

        def correlation_at(
            delay_samples: int,
        ) -> tuple[int, int, int, int, int] | None:
            reference_time = (
                capture_start_time - delay_samples / _RENDER_ECHO_FEATURE_RATE
            )
            start = round(
                (reference_time - render_start_time) * _RENDER_ECHO_FEATURE_RATE
            )
            if start < 0 or start + len(capture) > len(render):
                return None
            reference_mean = sum(render[start : start + len(capture)]) // len(capture)
            reference_energy = 0
            dot = 0
            for index, capture_sample in enumerate(centered_capture):
                render_sample = render[start + index] - reference_mean
                reference_energy += render_sample * render_sample
                dot += capture_sample * render_sample
            if reference_energy < render_floor_energy:
                return None
            denominator = isqrt(capture_energy * reference_energy)
            if denominator == 0:
                return None
            correlation = min(1_000, abs(dot) * 1_000 // denominator)
            return correlation, start, dot, reference_energy, reference_mean

        if calibrated and not repair_pending:
            assert stable_delay is not None
            minimum_delay = max(
                _RENDER_ECHO_MIN_DELAY_SAMPLES,
                stable_delay - _RENDER_ECHO_LOCKED_DELAY_RADIUS_SAMPLES,
            )
            maximum_delay = min(
                _RENDER_ECHO_MAX_DELAY_SAMPLES,
                stable_delay + _RENDER_ECHO_LOCKED_DELAY_RADIUS_SAMPLES,
            )
        else:
            minimum_delay = _RENDER_ECHO_MIN_DELAY_SAMPLES
            maximum_delay = _RENDER_ECHO_MAX_DELAY_SAMPLES

        best: tuple[int, int, int, int, int, int] | None = None
        for delay in range(
            minimum_delay,
            maximum_delay + 1,
            _RENDER_ECHO_COARSE_STEP_SAMPLES,
        ):
            candidate = correlation_at(delay)
            if candidate is None:
                continue
            correlation, start, dot, reference_energy, reference_mean = candidate
            if best is None or correlation > best[0]:
                best = (
                    correlation,
                    delay,
                    start,
                    dot,
                    reference_energy,
                    reference_mean,
                )
        if best is None:
            # Missing, stale, or quiet render must not erase genuine speech.
            return _EchoDecision(_EchoDecisionKind.NEAR_END, output_epoch)

        coarse_delay = best[1]
        refine_radius = _RENDER_ECHO_COARSE_STEP_SAMPLES
        for delay in range(
            max(minimum_delay, coarse_delay - refine_radius),
            min(maximum_delay, coarse_delay + refine_radius) + 1,
            _RENDER_ECHO_REFINE_STEP_SAMPLES,
        ):
            candidate = correlation_at(delay)
            if candidate is None:
                continue
            correlation, start, dot, reference_energy, reference_mean = candidate
            if correlation > best[0]:
                best = (
                    correlation,
                    delay,
                    start,
                    dot,
                    reference_energy,
                    reference_mean,
                )

        (
            correlation,
            delay_samples,
            reference_start,
            dot,
            reference_energy,
            reference_mean,
        ) = best
        enough_history = reference_start >= _RENDER_ECHO_FIR_TAPS - 1
        if not training and not enough_history:
            return _EchoDecision(
                _EchoDecisionKind.NEAR_END,
                output_epoch,
                correlation_permille=correlation,
                delay_ms=round(delay_samples * 1_000 / _RENDER_ECHO_FEATURE_RATE),
                reference_matched=True,
            )
        residual_energy = capture_energy
        if (calibrated or repair_pending) and enough_history:
            residual_energy = 0
            for index, capture_sample in enumerate(centered_capture):
                prediction = 0.0
                for tap, coefficient in enumerate(weights):
                    prediction += coefficient * (
                        render[reference_start + index - tap] - reference_mean
                    )
                error = capture_sample - prediction
                residual_energy += round(error * error)
        residual_percent = min(100, residual_energy * 100 // capture_energy)
        residual_rms = isqrt(residual_energy // len(centered_capture))

        residual_near_end = (
            residual_percent >= _RENDER_ECHO_NEAR_END_RESIDUAL_PERCENT
            and residual_rms >= _RENDER_ECHO_NEAR_END_RESIDUAL_RMS
        )
        if repair_pending:
            residual_is_trusted = repair_seeded or fir_valid_frames >= 2
            if correlation <= _RENDER_ECHO_NEAR_END_CORRELATION_PERMILLE or (
                residual_is_trusted and residual_near_end
            ):
                kind = _EchoDecisionKind.NEAR_END
            elif correlation >= _RENDER_ECHO_CORRELATION_PERMILLE:
                kind = _EchoDecisionKind.ECHO
            else:
                kind = _EchoDecisionKind.AMBIGUOUS
        elif training:
            kind = (
                _EchoDecisionKind.ECHO
                if correlation >= _RENDER_ECHO_CORRELATION_PERMILLE
                else _EchoDecisionKind.AMBIGUOUS
            )
        elif (
            correlation >= _RENDER_ECHO_CORRELATION_PERMILLE
            and residual_percent <= _RENDER_ECHO_MAX_RESIDUAL_PERCENT
        ):
            kind = _EchoDecisionKind.ECHO
        elif (
            correlation <= _RENDER_ECHO_NEAR_END_CORRELATION_PERMILLE
            or residual_near_end
        ):
            kind = _EchoDecisionKind.NEAR_END
        else:
            kind = _EchoDecisionKind.AMBIGUOUS

        if (
            update_model
            and repair_pending
            and enough_history
            and kind is not _EchoDecisionKind.ECHO
        ):
            with self._lock:
                if (
                    not self._model_updates_frozen.is_set()
                    and self._epoch == output_epoch
                    and self._repair_generation == repair_generation
                    and not self._repair_qualified
                ):
                    self._fir_valid_frames = 0
                    self._calibration_delays.clear()

        if (
            update_model
            and training
            and kind is _EchoDecisionKind.ECHO
            and enough_history
        ):
            updated = list(weights)
            if not fir_valid_frames:
                updated[0] = max(-4.0, min(4.0, dot / reference_energy))
            for index, capture_sample in enumerate(centered_capture):
                if index % 4:
                    continue
                reference_vector = [
                    render[reference_start + index - tap] - reference_mean
                    for tap in range(_RENDER_ECHO_FIR_TAPS)
                ]
                prediction = sum(
                    coefficient * sample
                    for coefficient, sample in zip(
                        updated,
                        reference_vector,
                        strict=True,
                    )
                )
                error = capture_sample - prediction
                norm = 1 + sum(sample * sample for sample in reference_vector)
                adjustment = _RENDER_ECHO_NLMS_STEP * error / norm
                for tap, sample in enumerate(reference_vector):
                    updated[tap] = max(
                        -4.0,
                        min(
                            4.0,
                            updated[tap] * _RENDER_ECHO_NLMS_LEAKAGE
                            + adjustment * sample,
                        ),
                    )
            with self._lock:
                if (
                    not self._model_updates_frozen.is_set()
                    and self._epoch == output_epoch
                ):
                    self._fir = updated
                    if (
                        repair_pending
                        and self._repair_generation == repair_generation
                        and not self._repair_qualified
                    ):
                        proposed_delays = [*self._calibration_delays, delay_samples]
                        if (
                            proposed_delays
                            and max(proposed_delays) - min(proposed_delays)
                            > _RENDER_ECHO_LOCKED_DELAY_RADIUS_SAMPLES
                        ):
                            self._fir_valid_frames = 0
                            self._calibration_delays.clear()
                        candidate_number = self._fir_valid_frames + 1
                        independently_valid = (
                            candidate_number <= 2
                            or residual_percent <= _RENDER_ECHO_MAX_RESIDUAL_PERCENT
                        )
                        if independently_valid:
                            self._fir_valid_frames = candidate_number
                            self._calibration_delays.append(delay_samples)
                            if self._fir_valid_frames >= 2:
                                # Keep residual discrimination after a later
                                # near-end frame resets only the consecutive
                                # qualification proof. Otherwise sustained
                                # double-talk would loop ECHO/ECHO/NEAR_END.
                                self._repair_seeded = True
                        else:
                            # A third-frame residual failure breaks the proof
                            # sequence. Keep the just-updated FIR only as the
                            # next generation's untrusted training seed.
                            self._fir_valid_frames = 0
                            self._calibration_delays.clear()
                        if self._fir_valid_frames >= _RENDER_ECHO_CALIBRATION_FRAMES:
                            ordered_delays = sorted(self._calibration_delays)
                            self._stable_delay_samples = ordered_delays[
                                len(ordered_delays) // 2
                            ]
                            self._repair_qualified = True
                            self._repair_seeded = False
                    else:
                        self._fir_valid_frames = min(
                            self._fir_valid_frames + 1,
                            _DIRECT_SYSLOG_COUNTER_MAX,
                        )
                        self._calibration_delays.append(delay_samples)
                        ordered_delays = sorted(self._calibration_delays)
                        self._stable_delay_samples = ordered_delays[
                            len(ordered_delays) // 2
                        ]

        return _EchoDecision(
            kind,
            output_epoch,
            correlation_permille=correlation,
            delay_ms=round(delay_samples * 1_000 / _RENDER_ECHO_FEATURE_RATE),
            reference_matched=True,
        )

    def _append_render_locked(self, samples: list[int]) -> None:
        """Append continuous features and retain one exact bounded timeline."""
        if not samples:
            return
        if self._render_start_time is None or self._render_end_time is None:
            raise RuntimeError("render timeline was not initialized")
        self._render_samples.extend(samples)
        self._render_end_time += len(samples) / _RENDER_ECHO_FEATURE_RATE
        overflow = len(self._render_samples) - _RENDER_ECHO_RING_SAMPLES
        for _ in range(max(0, overflow)):
            self._render_samples.popleft()
        if overflow > 0:
            self._render_start_time += overflow / _RENDER_ECHO_FEATURE_RATE

    def _clear_render_locked(self, *, reset_model: bool) -> None:
        """Clear bounded render framing while holding the guard lock."""
        self._render_samples.clear()
        self._render_start_time = None
        self._render_end_time = None
        self._render_sample_tail.clear()
        if reset_model:
            self._fir = [0.0] * _RENDER_ECHO_FIR_TAPS
            self._fir_valid_frames = 0
            self._calibration_delays.clear()
            self._stable_delay_samples = None
            self._repair_active = False
            self._repair_qualified = False
            self._repair_seeded = False


class _PcmPlayer:
    """Own one fixed-argv paplay child and its bounded non-blocking stdin."""

    def __init__(
        self,
        maximum_bytes: int,
        *,
        sink: str | None = None,
        volume_percent: int | None = None,
        exact_sink_volume: bool = False,
        volume_controller: SinkVolumeController | None = None,
        pcm_transform: Callable[[bytes], bytes] | None = None,
        write_observer: Callable[[bytes], None] | None = None,
        write_allowed: Callable[[], bool] | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self._maximum_bytes = maximum_bytes
        self._popen = popen
        self._sink = sink
        self._exact_sink_volume = exact_sink_volume
        self._volume_percent = volume_percent
        self._volume_controller = volume_controller or PactlSinkVolumeController()
        self._pcm_transform = pcm_transform
        self._write_observer = write_observer
        self._write_allowed = write_allowed
        self._volume = (
            None
            if volume_percent is None
            else (
                _PULSE_VOLUME_NORM
                if exact_sink_volume
                else _PULSE_VOLUME_NORM * volume_percent // 100
            )
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._stdin: Any = None
        self._pending = bytearray()
        self._staged = bytearray()
        self._finish_when_drained = False
        self._epoch: int | None = None
        self._volume_prepared = False
        self._reap_pending: list[subprocess.Popen[bytes]] = []

    def prepare(self) -> None:
        """Set and verify the dedicated AEC sink before response media."""
        if not self._exact_sink_volume:
            return
        if self._volume_prepared:
            return
        if self._sink is None or self._volume_percent is None:
            raise WebSocketError("direct playback volume is not configured")
        try:
            self._volume_controller.set_and_verify(
                self._sink,
                self._volume_percent,
            )
        except PulsePlaybackError as exc:
            raise WebSocketError(
                "direct playback volume could not be prepared"
            ) from exc
        self._volume_prepared = True

    def begin(self, epoch: int) -> None:
        self.abort()
        if self._exact_sink_volume and not self._volume_prepared:
            self.prepare()
        try:
            argv = list(_PAPLAY_ARGV)
            if self._sink is not None:
                argv.append(f"--device={self._sink}")
            if self._volume is not None:
                argv.append(f"--volume={self._volume}")
            process = self._popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                shell=False,
            )
        except Exception as exc:
            raise WebSocketError("paplay could not be started") from exc
        if process.stdin is None:
            _terminate_and_reap(process)
            raise WebSocketError("paplay did not expose stdin")
        try:
            os.set_blocking(process.stdin.fileno(), False)
        except Exception as exc:
            with suppress(Exception):
                process.stdin.close()
            _terminate_and_reap(process)
            raise WebSocketError("paplay stdin could not be configured") from exc
        self._process = process
        self._stdin = process.stdin
        self._finish_when_drained = False
        self._epoch = epoch

    def resume(self, epoch: int) -> None:
        """Advance a media-fenced epoch without discarding audible tail."""
        if self._process is None or self._stdin is None:
            self.begin(epoch)
            return
        if self._exact_sink_volume and not self._volume_prepared:
            self.prepare()
        self._epoch = epoch
        self._finish_when_drained = False

    def enqueue(self, value: bytes) -> None:
        if self._process is None or self._stdin is None or self._epoch is None:
            raise WebSocketError("audio arrived outside a speaking epoch")
        if len(self._pending) + len(self._staged) + len(value) > self._maximum_bytes:
            raise WebSocketError("playback queue exceeded its bound")
        self._pending.extend(value)

    def finish(self, epoch: int) -> None:
        if self._epoch != epoch:
            return
        self._finish_when_drained = True
        self.service()

    def service(self) -> None:
        self._reap_killed()
        process = self._process
        if process is None:
            return
        if process.poll() is not None:
            if self._pending or self._staged:
                raise WebSocketError("paplay exited before consuming output")
            self._reap_finished()
            return
        if not self._staged and self._pending:
            raw = bytes(self._pending[:_PLAYER_WRITE_BYTES])
            try:
                staged = self._pcm_transform(raw) if self._pcm_transform else raw
            except Exception as exc:
                raise WebSocketError("playback PCM transform failed") from exc
            if not isinstance(staged, bytes) or len(staged) != len(raw):
                raise WebSocketError("playback PCM transform changed framing")
            del self._pending[: len(raw)]
            self._staged.extend(staged)
        if self._staged and self._stdin is not None:
            # This is the final PCM crossing boundary. The session-level check
            # can race a capture-thread fail-close after player.service() is
            # entered; re-check its atomic fence immediately before os.write.
            if self._write_allowed is not None and not self._write_allowed():
                return
            try:
                written = os.write(self._stdin.fileno(), self._staged)
            except BlockingIOError:
                written = 0
            except (BrokenPipeError, OSError) as exc:
                raise WebSocketError("paplay rejected output") from exc
            if written:
                accepted = bytes(self._staged[:written])
                del self._staged[:written]
                if self._write_observer is not None:
                    with suppress(Exception):
                        self._write_observer(accepted)
        if (
            self._finish_when_drained
            and not self._pending
            and not self._staged
            and self._stdin is not None
        ):
            self._stdin.close()
            self._stdin = None
        if process.poll() is not None:
            self._reap_finished()

    def abort(self) -> None:
        process = self._process
        stdin = self._stdin
        self._process = None
        self._stdin = None
        self._pending.clear()
        self._staged.clear()
        self._finish_when_drained = False
        self._epoch = None
        if process is not None:
            with suppress(Exception):
                process.kill()
            self._reap_pending.append(process)
        if stdin is not None:
            with suppress(Exception):
                stdin.close()

    def close(self, timeout: float = _PLAYER_REAP_SECONDS) -> None:
        """Kill owned playback and reap it within one bounded deadline."""
        self.abort()
        deadline = time.monotonic() + max(0.0, timeout)
        while self._reap_pending and time.monotonic() < deadline:
            self._reap_killed()
            if self._reap_pending:
                time.sleep(0.005)
        self._reap_killed()

    @property
    def active(self) -> bool:
        """Return whether queued or child-buffered output can still be audible."""
        return self._process is not None or bool(self._pending) or bool(self._staged)

    def _reap_finished(self) -> None:
        assert self._process is not None
        process = self._process
        self._process = None
        self._stdin = None
        self._epoch = None
        self._finish_when_drained = False
        try:
            process.wait(timeout=_PLAYER_REAP_SECONDS)
        except Exception as exc:
            raise WebSocketError("paplay could not be reaped") from exc

    def _reap_killed(self) -> None:
        remaining: list[subprocess.Popen[bytes]] = []
        for process in self._reap_pending:
            try:
                if process.poll() is None:
                    remaining.append(process)
                    continue
                process.wait(timeout=0)
            except Exception:  # noqa: BLE001 - SIGKILL already prevents playback.
                if process.poll() is None:
                    remaining.append(process)
        self._reap_pending = remaining


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Best-effort bounded child cleanup that never escapes daemon teardown."""
    try:
        running = process.poll() is None
    except Exception:  # noqa: BLE001 - malformed/failed process handle
        running = True
    if running:
        with suppress(Exception):
            process.terminate()
    try:
        process.wait(timeout=_PLAYER_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    except Exception:  # noqa: BLE001 - still attempt the stronger cleanup
        pass
    else:
        return
    with suppress(Exception):
        process.kill()
    # A second TimeoutExpired/OSError must not prevent session terminal state.
    with suppress(Exception):
        process.wait(timeout=_PLAYER_REAP_SECONDS)


def _verify_pulseaudio_aec(config: RealtimeConfig) -> None:
    """Fail closed unless the configured echo-cancel endpoints are active.

    The voice process starts PulseAudio with module loading disabled, so this
    check deliberately observes startup state instead of trying to mutate it.
    It also runs before the bridge socket is opened: full-duplex audio is never
    submitted when the capture or playback route could bypass AEC.
    """
    if not config.full_duplex:
        return
    native_capture = config.capture_backend == NATIVE_AEC3_CAPTURE
    if native_capture and os.environ.get("CODEX_AEC3_ACTIVE") != "1":
        raise WebSocketError("native AEC3 capture is not active")
    source = config.pulse_aec_source
    sink = config.pulse_aec_sink
    method = config.pulse_aec_method
    if source is None or sink is None or method is None:
        raise WebSocketError("PulseAudio echo cancellation is not configured")

    timeout = config.io_timeout_seconds
    default_source = _pactl_output(("get-default-source",), timeout=timeout).strip()
    default_sink = _pactl_output(("get-default-sink",), timeout=timeout).strip()
    if default_source != source or default_sink != sink:
        raise WebSocketError("PulseAudio echo cancellation is not active")

    modules = _pactl_output(("list", "short", "modules"), timeout=timeout)
    if not _has_expected_aec_module(modules, source=source, sink=sink, method=method):
        raise WebSocketError("PulseAudio echo cancellation is not active")

    if not native_capture:
        sources = _pactl_output(("list", "short", "sources"), timeout=timeout)
        source_index = _pulse_object_index(sources, source)
        source_outputs = _pactl_output(
            ("--format=json", "list", "source-outputs"), timeout=timeout
        )
        if source_index is None or not _process_capture_uses_source(
            source_outputs, source_index=source_index, process_id=os.getpid()
        ):
            raise WebSocketError("PulseAudio echo cancellation is not active")

    _verify_aec_sink_volume(config)


def _verify_aec_sink_volume(config: RealtimeConfig) -> None:
    """Fail closed if the selected AEC sink is above the canary ceiling."""
    sink = config.pulse_aec_sink
    if sink is None:
        raise WebSocketError("PulseAudio echo cancellation is not configured")
    sink_volume = _pactl_output(
        ("get-sink-volume", sink), timeout=config.io_timeout_seconds
    )
    if not _sink_volume_within_ceiling(
        sink_volume, ceiling=config.aec_sink_volume_ceiling_percent
    ):
        raise WebSocketError("PulseAudio echo cancellation is not active")


def _repair_aec_sink_volume(
    config: RealtimeConfig,
    *,
    transaction_timeout_seconds: float = _MEDIA_ANCHOR_REPAIR_TIMEOUT_SECONDS,
) -> bool:
    """Restore the direct sink's exact anchor and report whether it drifted."""
    sink = config.pulse_aec_sink
    if sink is None:
        raise WebSocketError("PulseAudio echo cancellation is not configured")
    deadline = time.monotonic() + transaction_timeout_seconds

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise WebSocketError("PulseAudio playback anchor repair timed out")
        return value

    sink_volume = _pactl_output(("get-sink-volume", sink), timeout=remaining())
    remaining()
    channels = _sink_volume_channels(sink_volume)
    if not channels or any(channel != channels[0] for channel in channels):
        raise WebSocketError("PulseAudio playback anchor could not be verified")
    raw_anchor = _PULSE_VOLUME_NORM * config.playback_volume_percent // 100
    if all(channel == raw_anchor for channel in channels):
        return False

    def run_bounded(
        arguments: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        kwargs["timeout"] = remaining()
        kwargs.pop("check", None)
        result = subprocess.run(arguments, check=False, **kwargs)
        remaining()
        return result

    try:
        PactlSinkVolumeController(run=run_bounded).set_and_verify(
            sink,
            config.playback_volume_percent,
        )
    except PulsePlaybackError as exc:
        raise WebSocketError(
            "PulseAudio playback anchor could not be repaired"
        ) from exc
    return True


def _has_expected_aec_module(
    modules: str, *, source: str, sink: str, method: str
) -> bool:
    expected_arguments = {
        f"source_master={_PULSE_SOURCE_MASTER}",
        f"sink_master={_PULSE_SINK_MASTER}",
        f"source_name={source}",
        f"sink_name={sink}",
        f"aec_method={method}",
        "use_master_format=1",
    }
    for line in modules.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or fields[1] != _PULSE_ECHO_CANCEL_MODULE:
            continue
        try:
            arguments = shlex.split(fields[2], comments=False, posix=True)
        except ValueError:
            return False
        if (
            len(arguments) == len(expected_arguments)
            and set(arguments) == expected_arguments
        ):
            return True
    return False


def _pulse_object_index(objects: str, name: str) -> str | None:
    matches = []
    for line in objects.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == name and fields[0].isdigit():
            matches.append(fields[0])
    if len(matches) != 1:
        return None
    return matches[0]


def _process_capture_uses_source(
    source_outputs: str, *, source_index: str, process_id: int
) -> bool:
    try:
        decoded = json.loads(source_outputs)
    except json.JSONDecodeError:
        return False
    if not isinstance(decoded, list):
        return False
    expected_source = int(source_index)
    process_outputs = []
    for candidate in decoded:
        if not isinstance(candidate, dict):
            return False
        properties = candidate.get("properties")
        if not isinstance(properties, dict):
            continue
        if properties.get("application.process.id") == str(process_id):
            process_outputs.append(candidate)
    return bool(process_outputs) and all(
        candidate.get("driver") == _PULSE_NATIVE_DRIVER
        and candidate.get("source") == expected_source
        and candidate.get("corked") is False
        for candidate in process_outputs
    )


def _sink_volume_within_ceiling(value: str, *, ceiling: int) -> bool:
    channels = _sink_volume_channels(value)
    raw_ceiling = _PULSE_VOLUME_NORM * ceiling // 100
    return bool(channels) and all(channel <= raw_ceiling for channel in channels)


def _sink_volume_channels(value: str) -> list[int]:
    """Return every raw PulseAudio channel value from one bounded probe."""
    return [int(match) for match in _PULSE_VOLUME_RAW.findall(value)]


def _pactl_output(arguments: tuple[str, ...], *, timeout: float) -> str:
    """Run one fixed-binary, bounded, content-free PulseAudio probe."""
    try:
        result = subprocess.run(
            [*_PACTL_ARGV, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            close_fds=True,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WebSocketError(
            "PulseAudio echo cancellation could not be verified"
        ) from exc
    if result.returncode != 0:
        raise WebSocketError("PulseAudio echo cancellation could not be verified")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebSocketError(
            "PulseAudio echo cancellation could not be verified"
        ) from exc


_SESSIONS: weakref.WeakSet[RealtimeSession] = weakref.WeakSet()
_SESSIONS_LOCK = threading.Lock()
_PREWARM_LOCK = threading.Condition()
_PREWARMED_SIDECAR_COUNT = 1
_GLOBAL_SIDECAR_PROCESS_CAP = 1
_SIDECAR_SLOT_WAIT_SECONDS = 1.0
_SIDECAR_SLOT_POLL_SECONDS = 0.02
_PREWARMED_SIDECARS: deque[WebRtcSidecarClient] = deque()
_GLOBAL_SIDECAR_PROCESSES: dict[int, WebRtcSidecarClient] = {}
_SHUTTING_DOWN = False


def _prune_global_sidecars_locked() -> None:
    """Forget only children whose process exit has actually been observed."""
    for identity, sidecar in tuple(_GLOBAL_SIDECAR_PROCESSES.items()):
        if sidecar.process.poll() is not None:
            _GLOBAL_SIDECAR_PROCESSES.pop(identity, None)


def _launch_global_sidecar_locked() -> WebRtcSidecarClient:
    """Launch one child only when the hard process-wide slot cap permits it."""
    _prune_global_sidecars_locked()
    if len(_GLOBAL_SIDECAR_PROCESSES) >= _GLOBAL_SIDECAR_PROCESS_CAP:
        raise SidecarError("all device WebRTC process slots are occupied")
    sidecar = WebRtcSidecarClient.launch()
    _GLOBAL_SIDECAR_PROCESSES[id(sidecar)] = sidecar
    return sidecar


def prewarm_device_webrtc() -> bool:
    """Keep exactly one device WebRTC worker warm between voice sessions."""
    with _PREWARM_LOCK:
        if _SHUTTING_DOWN:
            return False
        _prune_global_sidecars_locked()
        retained: deque[WebRtcSidecarClient] = deque()
        while _PREWARMED_SIDECARS:
            current = _PREWARMED_SIDECARS.popleft()
            if not current.closed and current.process.poll() is None:
                _GLOBAL_SIDECAR_PROCESSES[id(current)] = current
                retained.append(current)
                continue
            with suppress(Exception):
                current.close()
        _PREWARMED_SIDECARS.extend(retained)
        while len(_PREWARMED_SIDECARS) < _PREWARMED_SIDECAR_COUNT:
            try:
                candidate = _launch_global_sidecar_locked()
            except Exception:  # noqa: BLE001 - optional prewarm fails closed on wake
                _LOGGER.warning("ThirdReality WebRTC prewarm failed", exc_info=False)
                return False
            _PREWARMED_SIDECARS.append(candidate)
        return True


def _take_prewarmed_sidecar() -> WebRtcSidecarClient:
    """Transfer a warm peer without exceeding the one-worker global cap."""
    deadline = time.monotonic() + _SIDECAR_SLOT_WAIT_SECONDS
    with _PREWARM_LOCK:
        while True:
            if _SHUTTING_DOWN:
                raise SidecarError("device WebRTC process is shutting down")
            _prune_global_sidecars_locked()
            while _PREWARMED_SIDECARS:
                sidecar = _PREWARMED_SIDECARS.popleft()
                if not sidecar.closed and sidecar.process.poll() is None:
                    _GLOBAL_SIDECAR_PROCESSES[id(sidecar)] = sidecar
                    return sidecar
                with suppress(Exception):
                    sidecar.close()
                _prune_global_sidecars_locked()
            if len(_GLOBAL_SIDECAR_PROCESSES) < _GLOBAL_SIDECAR_PROCESS_CAP:
                return _launch_global_sidecar_locked()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SidecarError("all device WebRTC process slots are occupied")
            _PREWARM_LOCK.wait(min(_SIDECAR_SLOT_POLL_SECONDS, remaining))


def _close_prewarmed_sidecar(*, timeout: float = 1.0) -> None:
    """Release every idle isolated peer during process shutdown."""
    global _SHUTTING_DOWN  # noqa: PLW0603
    deadline = time.monotonic() + max(0.0, timeout)
    with _PREWARM_LOCK:
        _SHUTTING_DOWN = True
        sidecars = tuple(_PREWARMED_SIDECARS)
        _PREWARMED_SIDECARS.clear()
    for sidecar in sidecars:
        with suppress(Exception):
            sidecar.close(timeout=max(0.0, deadline - time.monotonic()))
    with _PREWARM_LOCK:
        _prune_global_sidecars_locked()
        _PREWARM_LOCK.notify_all()


class RealtimeSession:
    """One fresh bridge session driven by one bounded network thread."""

    def __init__(
        self,
        config: RealtimeConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        connection_factory: Callable[..., WebSocketConnection] = (
            WebSocketConnection.connect
        ),
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        aec_verifier: Callable[[RealtimeConfig], None] | None = None,
        volume_guard: Callable[[RealtimeConfig], None] | None = None,
        anchor_reconciler: Callable[[RealtimeConfig], bool] | None = None,
        sidecar_factory: Callable[[], WebRtcSidecarClient] | None = None,
        direct_player_factory: Callable[[int, str], _PlayerLike] | None = None,
    ) -> None:
        """Create an unstarted, single-use session."""
        self._config = config
        self._clock = clock
        self._connection_factory = connection_factory
        self._popen = popen
        self._aec_verifier = aec_verifier or _verify_pulseaudio_aec
        # Tests and downstream embedders that supply a complete AEC verifier
        # retain a hermetic guard by default. Production uses the cheaper sink
        # check for every new response after the complete startup preflight.
        self._volume_guard = volume_guard or (
            aec_verifier if aec_verifier is not None else _verify_aec_sink_volume
        )
        self._anchor_reconciler = anchor_reconciler or (
            (lambda _config: False)
            if aec_verifier is not None
            else _repair_aec_sink_volume
        )
        self._uses_global_sidecar = sidecar_factory is None
        self._sidecar_factory = sidecar_factory or _take_prewarmed_sidecar
        self._direct_observes_rendered_playback = direct_player_factory is None
        self._playback_attenuator = _PlaybackAttenuator(
            min(
                self._config.playback_volume_percent,
                self._config.aec_sink_volume_ceiling_percent,
            )
        )
        self._playback_volume_percent = min(
            self._config.playback_volume_percent,
            self._config.aec_sink_volume_ceiling_percent,
        )
        self._render_echo_guard = (
            _RenderEchoGuard()
            if self._config.full_duplex
            and (
                self._config.media_transport == BRIDGE_PCM_TRANSPORT
                or self._config.capture_backend != NATIVE_AEC3_CAPTURE
            )
            else None
        )
        self._direct_player_factory = direct_player_factory or (
            lambda maximum_bytes, sink: _PcmPlayer(
                maximum_bytes,
                sink=sink,
                volume_percent=self._config.playback_volume_percent,
                exact_sink_volume=True,
                pcm_transform=self._playback_attenuator.scale,
                write_observer=self._observe_direct_playback_write,
                write_allowed=lambda: not self._direct_output_fenced.is_set(),
                popen=self._popen,
            )
        )
        self._audio = _BoundedAudioQueue(config.input_queue_bytes)
        self._accepted_capture_watermark = 0
        self._sent_capture_watermark = 0
        self._state = SessionState.NEW
        self._audio_send_lock = threading.Lock()
        self._direct_output_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._terminal = threading.Event()
        self._ready_at: float | None = None
        self._wake_network = threading.Event()
        self._stop_requested = threading.Event()
        self._interrupt_requested = threading.Event()
        self._live_capture_opened = threading.Event()
        self._interrupt_preserve_session = True
        self._direct_output_fenced = threading.Event()
        self._local_anchor_transition = threading.Event()
        self._output_active = threading.Event()
        self._local_output_epoch: int | None = None
        self._direct_peer_epoch = 1
        self._local_retired_barge_in_epoch: int | None = None
        self._local_barge_in_requested_epoch: int | None = None
        self._local_barge_in_requested_watermark: int | None = None
        self._local_barge_in_frames = 0
        self._local_barge_in_ambiguous_frames = 0
        self._local_barge_in_rearm_required = False
        self._local_barge_in_quiet_frames = 0
        self._local_barge_in_settle_until = 0.0
        self._local_anchor_settle_required = False
        self._local_anchor_model_trained = False
        self._local_anchor_requalification_pending = False
        self._local_anchor_requalification_evidence_frames = 0
        self._local_anchor_requalification_failed = threading.Event()
        self._local_barge_in_lock = threading.Lock()
        self._suppressed_output_epoch: int | None = None
        self._direct_preroll: deque[_AudioPacket] = deque()
        self._direct_preroll_bytes = 0
        self._direct_rollover_failed = threading.Event()
        self._context_loss_rollovers = 0
        self._thread: threading.Thread | None = None
        self._ever_ready = False
        self._direct_diagnostics: _DirectSessionDiagnostics | None = None
        self._direct_render_observation_tail = b""

    @property
    def state(self) -> SessionState:
        """Return the current lifecycle state."""
        with self._state_lock:
            return self._state

    @property
    def ready(self) -> bool:
        """Return whether protocol v2 negotiation completed."""
        return self._ready.is_set()

    @property
    def ready_at(self) -> float | None:
        """Return the monotonic instant when startup became usable."""
        with self._state_lock:
            return self._ready_at

    @property
    def output_active(self) -> bool:
        """Return whether the bridge owns the current speaking epoch."""
        return self._output_active.is_set()

    @property
    def failed_before_ready(self) -> bool:
        """Return whether startup ended before a usable session existed."""
        return self._terminal.is_set() and not self._ever_ready

    @property
    def terminal(self) -> bool:
        """Return whether the session thread has completed cleanup."""
        return self._terminal.is_set()

    @property
    def context_loss_rollovers(self) -> int:
        """Return the number of fresh peers that could not retain prior context."""
        with self._state_lock:
            return self._context_loss_rollovers

    def start(self) -> None:
        """Start exactly one network thread; a session object is never reusable."""
        with self._state_lock:
            if self._state is not SessionState.NEW:
                raise RuntimeError("realtime session has already been started")
            self._state = SessionState.CONNECTING
        thread = threading.Thread(
            target=self._run,
            name="thirdreality-realtime",
            daemon=True,
        )
        self._thread = thread
        with _SESSIONS_LOCK:
            _SESSIONS.add(self)
        try:
            thread.start()
        except Exception:
            with _SESSIONS_LOCK:
                _SESSIONS.discard(self)
            with self._state_lock:
                self._state = SessionState.FAILED
            self._terminal.set()
            raise

    def notify_live_capture_opened(self) -> None:
        """Permit direct rollover prewarm after the audible ready boundary."""
        if self._config.media_transport != DEVICE_WEBRTC_TRANSPORT:
            return
        with self._state_lock:
            if self._state is not SessionState.READY:
                return
            self._live_capture_opened.set()
        self._wake_network.set()

    def submit_audio(self, value: bytes) -> SubmitResult:
        """Queue one microphone frame without blocking the capture thread."""
        if not isinstance(value, bytes) or not value or len(value) % 2:
            return SubmitResult.INVALID
        if len(value) > self._config.max_message_bytes:
            return SubmitResult.INVALID
        # Half-duplex remains the default. Full-duplex construction requires
        # explicit AEC routing, and _run verifies that live PulseAudio topology
        # before opening the bridge socket.
        captured_at = self._clock()
        with self._state_lock:
            if self._direct_output_fenced.is_set():
                return SubmitResult.CLOSED
            if self._state not in {
                SessionState.CONNECTING,
                SessionState.READY,
                SessionState.INTERRUPTING,
            }:
                return SubmitResult.CLOSED
            if self._output_active.is_set() and not self._config.full_duplex:
                return SubmitResult.GATED
            capture_watermark = self._accepted_capture_watermark + 1
            packet = _AudioPacket(value, captured_at, capture_watermark)
            if not self._audio.put(packet):
                if (
                    self._config.media_transport == DEVICE_WEBRTC_TRANSPORT
                    and self._state is SessionState.INTERRUPTING
                ):
                    # Rollover capture is loss-intolerant. Once its bounded
                    # queue fills, close admission and let the network thread
                    # make the failure terminal instead of dropping speech.
                    self._direct_rollover_failed.set()
                    self._state = SessionState.STOPPING
                    self._wake_network.set()
                    return SubmitResult.FULL
                local_interrupt = False
                if self._config.media_transport == DEVICE_WEBRTC_TRANSPORT:
                    disposition = self._detect_local_barge_in(
                        value,
                        captured_at=captured_at,
                        capture_watermark=None,
                    )
                    local_interrupt = disposition.local_interrupt
                if self._local_anchor_requalification_failed.is_set():
                    self._fail_direct_anchor_reconciliation_state_locked()
                elif local_interrupt:
                    # The causal speech frame was not admitted, so a fresh
                    # peer could not replay the complete trigger. Kill output
                    # through normal direct-session teardown instead of
                    # fabricating a capture watermark.
                    self._direct_output_fenced.set()
                    self._interrupt_preserve_session = False
                    self._interrupt_requested.set()
                    self._state = SessionState.STOPPING
                    self._audio.clear()
                    self._reset_local_barge_in_detection()
                    self._wake_network.set()
                return SubmitResult.FULL
            self._accepted_capture_watermark = capture_watermark
            # Keep admission and detection on the same side of an interrupt or
            # stop transition. The transition owns the same state lock and can
            # therefore clear every packet and pending detector result admitted
            # before it, while later submissions observe STOPPING.
            disposition = self._detect_local_barge_in(
                value,
                captured_at=captured_at,
                capture_watermark=capture_watermark,
            )
            if (
                disposition.suppress_peer_epoch is not None
                or disposition.suppress_bridge
            ):
                annotated = replace(
                    packet,
                    suppress_peer_epoch=disposition.suppress_peer_epoch,
                    suppress_bridge=disposition.suppress_bridge,
                )
                if not self._audio.replace_tail(packet, annotated):
                    self._fail_direct_anchor_reconciliation_state_locked()
                    return SubmitResult.CLOSED
            if self._local_anchor_requalification_failed.is_set():
                self._fail_direct_anchor_reconciliation_state_locked()
        self._wake_network.set()
        return SubmitResult.ACCEPTED

    def request_playback_volume(self, volume_percent: int) -> int:
        """Apply dynamic direct-media volume without mutating the leased AEC sink."""
        with self._state_lock:
            if self._state not in {
                SessionState.NEW,
                SessionState.CONNECTING,
                SessionState.READY,
                SessionState.INTERRUPTING,
            }:
                raise RuntimeError("realtime session no longer accepts volume changes")
            applied = self._playback_attenuator.request(
                volume_percent,
                ramp=self._state is not SessionState.NEW,
            )
            self._playback_volume_percent = applied
        self._wake_network.set()
        return applied

    def reconcile_playback_volume(self, volume_percent: int) -> int:
        """Repair out-of-band sink drift and retain the requested audible level."""
        if type(volume_percent) is not int:
            raise ValueError("playback volume must be an integer")
        deadline = time.monotonic() + _PHYSICAL_ANCHOR_REPAIR_TIMEOUT_SECONDS
        transition_settle_until = self._arm_local_anchor_repair_transition(deadline)
        output_lock_acquired = False
        try:
            if not self._direct_output_lock.acquire(
                timeout=self._remaining_anchor_repair_budget_or_fence(deadline)
            ):
                self._fence_direct_anchor_reconciliation_nowait()
                raise WebSocketError("playback anchor repair lock timed out")
            output_lock_acquired = True

            if not self._state_lock.acquire(
                timeout=self._remaining_anchor_repair_budget_or_fence(deadline)
            ):
                self._fence_direct_anchor_reconciliation_nowait()
                raise WebSocketError("playback anchor state lock timed out")
            try:
                if self._state not in {
                    SessionState.CONNECTING,
                    SessionState.READY,
                    SessionState.INTERRUPTING,
                }:
                    raise RuntimeError(
                        "realtime session no longer accepts volume reconciliation"
                    )
            finally:
                self._state_lock.release()

            try:
                repaired = self._reconcile_playback_anchor(
                    transaction_timeout_seconds=(
                        self._remaining_anchor_repair_budget_or_fence(deadline)
                    )
                )
            except Exception:
                # No direct PCM may cross the output boundary after an anchor
                # check fails. Capture remains byte-for-byte unchanged until the
                # normal bounded session teardown observes this terminal fence.
                self._fence_direct_anchor_reconciliation_nowait()
                raise

            if not self._state_lock.acquire(
                timeout=self._remaining_anchor_repair_budget_or_fence(deadline)
            ):
                self._fence_direct_anchor_reconciliation_nowait()
                raise WebSocketError("playback anchor state lock timed out")
            try:
                if self._state not in {
                    SessionState.CONNECTING,
                    SessionState.READY,
                    SessionState.INTERRUPTING,
                }:
                    raise RuntimeError(
                        "realtime session stopped during volume reconciliation"
                    )
                applied = self._playback_attenuator.request(
                    volume_percent,
                    ramp=True,
                )
                self._playback_volume_percent = applied
            finally:
                self._state_lock.release()
            if repaired and not self._reset_local_echo_after_anchor_repair(
                settle_until=transition_settle_until,
                lock_deadline=deadline,
            ):
                self._fence_direct_anchor_reconciliation_nowait()
                self._raise_anchor_repair_boundary_timeout()
            self._remaining_anchor_repair_budget_or_fence(deadline)
            self._wake_network.set()
            return applied
        finally:
            if output_lock_acquired:
                self._direct_output_lock.release()
            self._finish_local_anchor_repair_transition()

    @staticmethod
    def _raise_anchor_repair_boundary_timeout() -> NoReturn:
        """Raise the fixed post-repair boundary failure."""
        raise WebSocketError("playback anchor repair boundary timed out")

    def _reconcile_playback_anchor(
        self,
        *,
        transaction_timeout_seconds: float = _MEDIA_ANCHOR_REPAIR_TIMEOUT_SECONDS,
    ) -> bool:
        """Run one exact fixed-sink check through the injected bounded guard."""
        repaired = (
            _repair_aec_sink_volume(
                self._config,
                transaction_timeout_seconds=transaction_timeout_seconds,
            )
            if self._anchor_reconciler is _repair_aec_sink_volume
            else self._anchor_reconciler(self._config)
        )
        if not isinstance(repaired, bool):
            raise WebSocketError("playback anchor reconciler returned invalid state")
        return repaired

    def _reconcile_media_started_anchor(self) -> None:
        """Check one media boundary under its caller-owned output lock."""
        deadline = time.monotonic() + _MEDIA_ANCHOR_REPAIR_TIMEOUT_SECONDS
        transition_settle_until = self._arm_local_anchor_repair_transition(deadline)
        try:
            repaired = self._reconcile_playback_anchor(
                transaction_timeout_seconds=self._remaining_anchor_repair_budget(
                    deadline
                )
            )
            if repaired and not self._reset_local_echo_after_anchor_repair(
                settle_until=transition_settle_until,
                lock_deadline=deadline,
            ):
                self._fence_direct_anchor_reconciliation_nowait()
                self._raise_anchor_repair_boundary_timeout()
            self._remaining_anchor_repair_budget(deadline)
        except Exception:
            self._fence_direct_anchor_reconciliation_nowait()
            raise
        finally:
            # No-drift probes never alter the wall-clock settle deadline. An
            # actual repair already published its one entry-anchored 128 ms
            # boundary before this atomic transition marker is released.
            self._finish_local_anchor_repair_transition()

    @staticmethod
    def _remaining_anchor_repair_budget(deadline: float) -> float:
        """Return one positive whole-transaction budget or fail closed."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebSocketError("playback anchor repair timed out")
        return remaining

    def _remaining_anchor_repair_budget_or_fence(self, deadline: float) -> float:
        """Return remaining time, atomically fencing output on expiration."""
        try:
            return self._remaining_anchor_repair_budget(deadline)
        except WebSocketError:
            self._fence_direct_anchor_reconciliation_nowait()
            raise

    def _arm_local_anchor_repair_transition(self, deadline: float) -> float:
        """Atomically suppress stale local evidence before any blocking wait."""
        self._local_anchor_transition.set()
        if self._render_echo_guard is not None and not (
            self._render_echo_guard.freeze_model_updates(
                max(0.0, deadline - time.monotonic())
            )
        ):
            self._fence_direct_anchor_reconciliation_nowait()
            raise WebSocketError("playback anchor model fence timed out")
        return self._clock() + _LOCAL_BARGE_IN_ANCHOR_REPAIR_SETTLE_SECONDS

    def _finish_local_anchor_repair_transition(self) -> None:
        """Release the capture/model transition marker without taking its lock."""
        self._local_anchor_transition.clear()
        if self._render_echo_guard is not None:
            self._render_echo_guard.thaw_model_updates()

    def _fence_direct_anchor_reconciliation_nowait(self) -> None:
        """Close the PCM boundary without waiting on capture-owned locks."""
        self._direct_output_fenced.set()
        self._interrupt_preserve_session = False
        self._interrupt_requested.set()
        if self._state_lock.acquire(blocking=False):
            try:
                self._state = SessionState.STOPPING
                self._audio.clear()
            finally:
                self._state_lock.release()
        self._wake_network.set()

    def _fail_direct_anchor_reconciliation_locked(self) -> None:
        """Fence output after a failed repair while leaving capture PCM untouched."""
        with self._state_lock:
            self._fail_direct_anchor_reconciliation_state_locked()

    def _fail_direct_anchor_reconciliation_state_locked(self) -> None:
        """Fence output while the caller already owns the lifecycle lock."""
        self._direct_output_fenced.set()
        self._interrupt_preserve_session = False
        self._interrupt_requested.set()
        self._state = SessionState.STOPPING
        self._audio.clear()
        self._reset_local_barge_in_detection()
        self._wake_network.set()

    def _detect_local_barge_in(  # noqa: C901 - one atomic capture decision
        self,
        value: bytes,
        *,
        captured_at: float,
        capture_watermark: int | None,
    ) -> _CaptureDisposition:
        """Classify raw capture once for local rollover and current-peer egress."""
        if not self._config.full_duplex:
            return _CaptureDisposition()
        with self._local_barge_in_lock:
            transitioning = self._local_anchor_transition.is_set()
            settling = captured_at < self._local_barge_in_settle_until
            peak, rms = _pcm_peak_and_rms(value)
            has_signal = _levels_have_local_barge_in_signal(
                peak,
                rms,
                capture_backend=self._config.capture_backend,
                direct_capture_gain_db=self._config.direct_capture_gain_db,
            )
            provider_threshold = max(
                1,
                round(
                    _INPUT_ACTIVITY_SIGNAL_PEAK
                    / (10 ** (self._config.direct_capture_gain_db / 20))
                ),
            )
            provider_candidate = peak >= provider_threshold
            output_epoch = self._local_output_epoch
            peer_epoch = self._direct_peer_epoch
            requalifying = self._local_anchor_requalification_pending
            decision: _EchoDecision | None = None
            if (
                provider_candidate
                and output_epoch is not None
                and self._render_echo_guard is not None
            ):
                decision = self._render_echo_guard.classify(
                    value,
                    captured_at=captured_at,
                    output_epoch=output_epoch,
                    calibrating=settling or requalifying,
                    update_model=not transitioning,
                )
                diagnostics = self._direct_diagnostics
                if diagnostics is not None and decision is not None:
                    diagnostics.observe_echo_decision(decision)
                if requalifying and decision is not None:
                    repair_started, repair_qualified = (
                        self._render_echo_guard.repair_status(output_epoch)
                    )
                    if repair_started and repair_qualified:
                        self._local_anchor_requalification_pending = False
                        self._local_anchor_requalification_evidence_frames = 0
                        requalifying = False
                    elif repair_started and decision.kind in {
                        _EchoDecisionKind.ECHO,
                        _EchoDecisionKind.AMBIGUOUS,
                    }:
                        self._local_anchor_requalification_evidence_frames += 1
                        if (
                            self._local_anchor_requalification_evidence_frames
                            >= _LOCAL_BARGE_IN_ANCHOR_REPAIR_MAX_EVIDENCE_FRAMES
                        ):
                            self._direct_output_fenced.set()
                            self._local_anchor_requalification_failed.set()

            transitioning = transitioning or self._local_anchor_transition.is_set()
            suppress_peer_epoch: int | None = None
            suppress_bridge = False
            if provider_candidate and output_epoch is not None:
                if decision is None:
                    suppress = transitioning or settling
                elif (
                    decision.reference_matched
                    and decision.correlation_permille
                    <= _RENDER_ECHO_NEAR_END_CORRELATION_PERMILLE
                ):
                    # A decorrelated signal is clear near-end evidence even
                    # while the model is calibrating and labels it ambiguous.
                    suppress = False
                elif decision.kind in {
                    _EchoDecisionKind.ECHO,
                    _EchoDecisionKind.AMBIGUOUS,
                }:
                    suppress = True
                elif not decision.reference_matched:
                    suppress = transitioning or settling
                else:
                    # Residual-heavy double-talk may still correlate with the
                    # rendered voice. Keep it raw for local rollover/preroll,
                    # but expose it only to the fresh peer after that rollover.
                    suppress = (
                        decision.correlation_permille
                        > _RENDER_ECHO_NEAR_END_CORRELATION_PERMILLE
                    )
                if suppress:
                    if self._config.media_transport == DEVICE_WEBRTC_TRANSPORT:
                        suppress_peer_epoch = peer_epoch
                    elif decision is None or decision.kind in {
                        _EchoDecisionKind.ECHO,
                        _EchoDecisionKind.AMBIGUOUS,
                    }:
                        # Protocol v2 retains one provider peer across a local
                        # interruption. Erase only playback-correlated capture;
                        # a residual-heavy NEAR_END decision must remain raw on
                        # that same peer instead of relying on v3 replay.
                        suppress_bridge = True

            def disposition(*, local_interrupt: bool = False) -> _CaptureDisposition:
                return _CaptureDisposition(
                    local_interrupt=local_interrupt,
                    suppress_peer_epoch=suppress_peer_epoch,
                    suppress_bridge=suppress_bridge,
                )

            render_suppressed_for_bridge = (
                self._config.media_transport == BRIDGE_PCM_TRANSPORT and suppress_bridge
            )
            effective_signal = has_signal and not (
                render_suppressed_for_bridge
                or (decision is not None and decision.kind is _EchoDecisionKind.ECHO)
            )
            if transitioning or settling:
                self._local_barge_in_requested_epoch = None
                self._local_barge_in_requested_watermark = None
                self._local_barge_in_frames = 0
                self._local_barge_in_ambiguous_frames = 0
                return disposition()
            if self._local_anchor_requalification_failed.is_set():
                self._local_barge_in_requested_epoch = None
                self._local_barge_in_requested_watermark = None
                self._local_barge_in_frames = 0
                self._local_barge_in_ambiguous_frames = 0
                return disposition()
            if (
                requalifying
                and decision is not None
                and decision.kind
                in {
                    _EchoDecisionKind.ECHO,
                    _EchoDecisionKind.AMBIGUOUS,
                }
            ):
                # Correlated evidence belongs to the untrusted repair model.
                # Suppress it until three independent frames qualify the new
                # delay, or the bounded eight-frame fail-closed limit fires.
                self._local_barge_in_requested_epoch = None
                self._local_barge_in_requested_watermark = None
                self._local_barge_in_frames = 0
                self._local_barge_in_ambiguous_frames = 0
                return disposition()
            if self._local_barge_in_rearm_required:
                if effective_signal:
                    self._local_barge_in_quiet_frames = 0
                else:
                    self._local_barge_in_quiet_frames += 1
                    if (
                        self._local_barge_in_quiet_frames
                        >= _LOCAL_BARGE_IN_REARM_QUIET_FRAMES
                    ):
                        self._local_barge_in_rearm_required = False
                        self._local_barge_in_quiet_frames = 0
                # One continuous utterance may retire only one peer epoch.
                # Rearm solely after bounded quiet capture, even if the
                # replacement starts speaking before that utterance ends.
                return disposition()
            if output_epoch is None:
                if (
                    self._local_retired_barge_in_epoch is not None
                    and self._local_barge_in_requested_epoch
                    == self._local_retired_barge_in_epoch
                    and self._local_barge_in_requested_watermark is not None
                ):
                    # A provider quiet boundary may retire output after capture
                    # has already qualified its exact generation. Preserve the
                    # causal watermark until the network loop rolls the peer.
                    return disposition()
                self._local_barge_in_requested_epoch = None
                self._local_barge_in_requested_watermark = None
                self._local_barge_in_frames = 0
                self._local_barge_in_ambiguous_frames = 0
                return disposition()
            if self._local_barge_in_requested_epoch is not None:
                return disposition()
            if (
                not has_signal
                or render_suppressed_for_bridge
                or (decision is not None and decision.kind is _EchoDecisionKind.ECHO)
            ):
                self._local_barge_in_frames = 0
                self._local_barge_in_ambiguous_frames = 0
                return disposition()
            # Every consecutive non-echo signal frame contributes to the
            # bounded fail-open path. NEAR_END additionally retains its faster
            # two-consecutive-frame path; AMBIGUOUS resets only that fast path,
            # not the shared evidence window.
            self._local_barge_in_ambiguous_frames += 1
            if decision is not None and decision.kind is _EchoDecisionKind.AMBIGUOUS:
                self._local_barge_in_frames = 0
            else:
                self._local_barge_in_frames += 1
            if (
                self._local_barge_in_frames < _LOCAL_BARGE_IN_FRAMES
                and self._local_barge_in_ambiguous_frames
                < _LOCAL_BARGE_IN_AMBIGUOUS_FRAMES
            ):
                return disposition()
            self._local_barge_in_frames = 0
            self._local_barge_in_ambiguous_frames = 0
            if capture_watermark is None:
                return disposition(local_interrupt=True)
            self._local_barge_in_requested_epoch = output_epoch
            self._local_barge_in_requested_watermark = capture_watermark
            return disposition()

    def _set_local_output_epoch(
        self,
        output_epoch: int | None,
        *,
        settle_barge_in: bool = False,
        preserve_echo_model: bool = False,
        preserve_pending_barge_in: bool = False,
    ) -> None:
        """Publish one network-thread-owned playback generation to capture."""
        with self._local_barge_in_lock:
            if settle_barge_in:
                settle_seconds = (
                    _LOCAL_BARGE_IN_ANCHOR_REPAIR_SETTLE_SECONDS
                    if preserve_echo_model
                    else _LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS
                )
                self._local_barge_in_settle_until = max(
                    self._local_barge_in_settle_until,
                    self._clock() + settle_seconds,
                )
            if self._render_echo_guard is not None:
                if output_epoch is None:
                    self._render_echo_guard.deactivate()
                else:
                    self._direct_render_observation_tail = b""
                    self._render_echo_guard.begin_epoch(
                        output_epoch,
                        reset=settle_barge_in and not preserve_echo_model,
                    )
                    if self._local_anchor_requalification_pending:
                        repair_started, _ = self._render_echo_guard.repair_status(
                            output_epoch
                        )
                        if not repair_started:
                            self._render_echo_guard.repair_boundary(output_epoch)
            pending_retired_epoch: int | None = None
            if (
                preserve_pending_barge_in
                and self._local_barge_in_requested_watermark is not None
            ):
                if (
                    self._local_retired_barge_in_epoch is not None
                    and self._local_barge_in_requested_epoch
                    == self._local_retired_barge_in_epoch
                ):
                    # A quiet boundary and the following media start can share
                    # one bounded sidecar drain. Carry the already-qualified
                    # predecessor request across that whole ordered batch.
                    pending_retired_epoch = self._local_retired_barge_in_epoch
                elif (
                    output_epoch is None
                    and self._local_barge_in_requested_epoch == self._local_output_epoch
                ):
                    pending_retired_epoch = self._local_output_epoch
            self._local_output_epoch = output_epoch
            self._local_retired_barge_in_epoch = pending_retired_epoch
            if pending_retired_epoch is None:
                self._local_barge_in_requested_epoch = None
                self._local_barge_in_requested_watermark = None
            self._local_barge_in_frames = 0
            self._local_barge_in_ambiguous_frames = 0
            self._local_anchor_requalification_evidence_frames = 0
            if output_epoch is None:
                self._output_active.clear()
            else:
                self._output_active.set()

    def _set_direct_peer_epoch(self, peer_epoch: int) -> None:
        """Publish the peer identity used by capture-side suppression tags."""
        if peer_epoch < 1:
            raise ValueError("direct peer epoch must be positive")
        with self._local_barge_in_lock:
            self._direct_peer_epoch = peer_epoch

    def _reset_local_barge_in_detection(self) -> None:
        """Discard detector state without changing network-owned playback."""
        with self._local_barge_in_lock:
            self._local_barge_in_requested_epoch = None
            self._local_barge_in_requested_watermark = None
            self._local_retired_barge_in_epoch = None
            self._local_barge_in_frames = 0
            self._local_barge_in_ambiguous_frames = 0
            self._local_barge_in_rearm_required = False
            self._local_barge_in_quiet_frames = 0
            self._local_barge_in_settle_until = 0.0
            self._local_anchor_settle_required = False
            self._local_anchor_model_trained = False
            self._local_anchor_requalification_pending = False
            self._local_anchor_requalification_evidence_frames = 0
            self._local_anchor_requalification_failed.clear()
            self._finish_local_anchor_repair_transition()
            self._reset_direct_render_reference()

    def _reset_local_echo_after_anchor_repair(
        self,
        *,
        settle_until: float | None = None,
        lock_deadline: float | None = None,
    ) -> bool:
        """Fence unsafe render history while preserving a trained echo path."""
        if lock_deadline is None:
            self._local_barge_in_lock.acquire()
        else:
            try:
                remaining = self._remaining_anchor_repair_budget(lock_deadline)
            except WebSocketError:
                return False
            if not self._local_barge_in_lock.acquire(timeout=remaining):
                return False
        try:
            self._local_barge_in_requested_epoch = None
            self._local_barge_in_requested_watermark = None
            self._local_barge_in_frames = 0
            self._local_barge_in_ambiguous_frames = 0
            self._local_barge_in_quiet_frames = 0
            output_epoch = self._local_output_epoch
            self._direct_render_observation_tail = b""
            had_seed = True
            if self._render_echo_guard is not None:
                had_seed = self._render_echo_guard.repair_boundary(output_epoch)
                self._local_anchor_requalification_pending = True
                self._local_anchor_requalification_evidence_frames = 0
                self._local_anchor_requalification_failed.clear()
            if output_epoch is None:
                self._local_anchor_settle_required = True
                self._local_anchor_model_trained = had_seed
                return True
            self._local_anchor_settle_required = False
            self._local_anchor_model_trained = had_seed
            self._local_barge_in_settle_until = max(
                self._local_barge_in_settle_until,
                (
                    self._clock() + _LOCAL_BARGE_IN_ANCHOR_REPAIR_SETTLE_SECONDS
                    if settle_until is None
                    else settle_until
                ),
            )
            return True
        finally:
            self._local_barge_in_lock.release()

    def _take_local_anchor_settle_required(self) -> tuple[bool, bool]:
        """Consume an idle anchor repair at the next audible epoch boundary."""
        with self._local_barge_in_lock:
            required = self._local_anchor_settle_required
            calibrated = self._local_anchor_model_trained
            self._local_anchor_settle_required = False
            self._local_anchor_model_trained = False
            return required, calibrated

    def _anchor_requalification_pending(self) -> bool:
        """Return whether a repaired path still needs independent evidence."""
        with self._local_barge_in_lock:
            return self._local_anchor_requalification_pending

    def _retired_barge_in_pending(self) -> bool:
        """Return whether a qualified predecessor still owns a raw rollover."""
        with self._local_barge_in_lock:
            return (
                self._local_retired_barge_in_epoch is not None
                and self._local_barge_in_requested_epoch
                == self._local_retired_barge_in_epoch
                and self._local_barge_in_requested_watermark is not None
            )

    def _reset_direct_render_reference(self) -> None:
        """Discard partial diagnostics and render features at a hard boundary."""
        self._direct_render_observation_tail = b""
        if self._render_echo_guard is not None:
            self._render_echo_guard.reset()

    def _abort_player(self, player: _PlayerLike) -> None:
        """Abort playback together with every render-derived classifier tail."""
        self._reset_direct_render_reference()
        player.abort()

    def _remember_direct_preroll(self, packet: _AudioPacket) -> None:
        """Retain recent capture already sent during the active response.

        The trigger frame can already have crossed the old peer before the
        network thread observes the detector request. Keeping only sent frames
        here makes the rollover merge disjoint from the unsent live queue.
        """
        if not self._output_active.is_set():
            return
        packet_bytes = len(packet.data)
        while (
            self._direct_preroll
            and self._direct_preroll_bytes + packet_bytes
            > _DIRECT_ROLLOVER_PREROLL_BYTES
        ):
            removed = self._direct_preroll.popleft()
            self._direct_preroll_bytes -= len(removed.data)
        if packet_bytes > _DIRECT_ROLLOVER_PREROLL_BYTES:
            # Recorder callbacks are normally 2 KiB. Never trim or restamp an
            # oversized callback, and never exceed the explicit replay bound.
            self._direct_preroll.clear()
            self._direct_preroll_bytes = 0
            return
        self._direct_preroll.append(packet)
        self._direct_preroll_bytes += packet_bytes

    def _clear_direct_preroll(self) -> None:
        """Forget capture associated with the previous assistant response."""
        self._direct_preroll.clear()
        self._direct_preroll_bytes = 0

    def _begin_direct_rollover_capture(self, trigger_watermark: int) -> None:
        """Prepend sent preroll to unsent capture at one atomic cut boundary."""
        with self._state_lock:
            if self._state is not SessionState.READY:
                raise SidecarError("direct rollover began outside ready state")
            replay = list(self._direct_preroll)
            if replay and any(
                packet.capture_watermark > self._sent_capture_watermark
                for packet in replay
            ):
                raise SidecarError("direct rollover preroll crossed its send boundary")
            if trigger_watermark <= self._sent_capture_watermark and not any(
                packet.capture_watermark == trigger_watermark for packet in replay
            ):
                raise SidecarError("direct rollover trigger left the replay window")
            if not self._audio.prepend(replay):
                self._direct_rollover_failed.set()
                self._state = SessionState.STOPPING
                raise SidecarError("direct rollover capture exceeded its bound")
            self._state = SessionState.INTERRUPTING
            self._clear_direct_preroll()

    def _flush_local_barge_in(
        self,
        player: _PlayerLike,
        *,
        output_epoch: int | None,
        last_output_epoch: int,
    ) -> tuple[int | None, int | None]:
        """Apply a capture request and return output plus its send watermark."""
        with self._local_barge_in_lock:
            requested_epoch = self._local_barge_in_requested_epoch
            if requested_epoch is None:
                # Network polls run more frequently than the fixed recorder
                # callback. No request is not a detector boundary: preserve a
                # qualifying partial count until the next microphone frame.
                return output_epoch, None
            requested_watermark = self._local_barge_in_requested_watermark
            retired_request = requested_epoch == self._local_retired_barge_in_epoch
            self._local_barge_in_requested_epoch = None
            self._local_barge_in_requested_watermark = None
            self._local_retired_barge_in_epoch = None
            self._local_barge_in_frames = 0
            self._local_barge_in_ambiguous_frames = 0
            matches_current_output = (
                requested_epoch == output_epoch
                or retired_request
                or (
                    output_epoch is None
                    and requested_epoch == last_output_epoch
                    and player.active
                )
            )
            if not matches_current_output or (
                requested_epoch != self._local_output_epoch and not retired_request
            ):
                return output_epoch, None
            # Arm only after the network thread commits a current-generation
            # interruption. A stale capture request must not suppress a later,
            # genuinely new utterance on a different output epoch.
            self._local_barge_in_rearm_required = True
            self._local_barge_in_quiet_frames = 0
            # Disable capture-side detection before the potentially blocking
            # player reap. Only this network-thread path mutates playback state.
            self._local_output_epoch = None
            self._output_active.clear()
        self._abort_player(player)
        assert requested_epoch is not None
        assert requested_watermark is not None
        self._suppressed_output_epoch = requested_epoch
        return None, requested_watermark

    def interrupt(self, *, preserve_session: bool = True) -> None:
        """Flush local output and request bounded provider interruption.

        Explicit direct-session interruption always closes the peer because it
        has no AEC-qualified speech evidence. Automatic local barge-in uses a
        fresh device peer in the direct network loop. Bridge-PCM sessions keep
        their separately negotiated same-session interruption behavior.
        """
        if self._terminal.is_set():
            return
        if self._config.media_transport == DEVICE_WEBRTC_TRANSPORT:
            self._direct_output_fenced.set()
        with (
            self._audio_send_lock,
            self._direct_output_lock,
            self._state_lock,
        ):
            if self._interrupt_requested.is_set():
                self._interrupt_preserve_session = (
                    self._interrupt_preserve_session and preserve_session
                )
            else:
                self._interrupt_preserve_session = preserve_session
            self._interrupt_requested.set()
            if (
                not self._interrupt_preserve_session
                or self._config.media_transport == DEVICE_WEBRTC_TRANSPORT
            ):
                self._state = SessionState.STOPPING
            elif self._state in {SessionState.CONNECTING, SessionState.READY}:
                self._state = (
                    SessionState.INTERRUPTING
                    if self._config.media_transport == DEVICE_WEBRTC_TRANSPORT
                    else SessionState.STOPPING
                )
            self._audio.clear()
            self._reset_local_barge_in_detection()
        self._wake_network.set()

    def stop(self) -> None:
        """Request bounded normal session shutdown."""
        if self._terminal.is_set():
            return
        self._direct_output_fenced.set()
        with (
            self._audio_send_lock,
            self._direct_output_lock,
            self._state_lock,
        ):
            self._stop_requested.set()
            if self._state in {
                SessionState.CONNECTING,
                SessionState.READY,
                SessionState.INTERRUPTING,
            }:
                self._state = SessionState.STOPPING
            self._audio.clear()
            self._reset_local_barge_in_detection()
        self._wake_network.set()

    def join(self, timeout: float) -> bool:
        """Wait a bounded interval and report whether the thread exited."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def _run(self) -> None:
        """Select the explicitly configured media transport."""
        if self._config.media_transport == DEVICE_WEBRTC_TRANSPORT:
            self._run_device_webrtc()
            return
        self._run_bridge_pcm()

    def _send_bridge_audio(
        self,
        connection: WebSocketConnection,
    ) -> tuple[_AudioPacket | None, int]:
        """Send one queued bridge frame on the explicit-stop boundary."""
        # A distinct send boundary lets the recorder keep admitting capture
        # while a bounded WebSocket write blocks. Stop/interrupt acquire this
        # lock before changing state, so a transition that returns has either
        # waited for this reserved send or prevented its dequeue.
        with self._audio_send_lock:
            with self._state_lock:
                if (
                    self._stop_requested.is_set()
                    or self._interrupt_requested.is_set()
                    or self._state
                    not in {
                        SessionState.CONNECTING,
                        SessionState.READY,
                        SessionState.INTERRUPTING,
                    }
                ):
                    return None, 0
                packet, remaining_packets = self._audio.pop()
                if packet is None:
                    return None, 0
            provider_pcm = (
                bytes(len(packet.data))
                if packet.suppress_bridge
                else _apply_capture_gain_pcm16(
                    packet.data,
                    self._config.direct_capture_gain_db,
                )
            )
            connection.send_binary(provider_pcm)
            self._sent_capture_watermark = packet.capture_watermark
            return packet, remaining_packets

    def _run_bridge_pcm(  # noqa: C901
        self,
    ) -> None:
        connection: WebSocketConnection | None = None
        if self._config.full_duplex:
            sink = self._config.pulse_aec_sink
            assert sink is not None
            player = self._direct_player_factory(
                self._config.output_queue_bytes,
                sink,
            )
        else:
            player = _PcmPlayer(
                self._config.output_queue_bytes,
                popen=self._popen,
            )
        failed = False
        try:
            started_at = self._clock()
            if self._config.full_duplex:
                # Resolve the fixed sink anchor before the bridge/provider is
                # declared ready. This keeps blocking pactl work out of the
                # first response and prevents clipping its first audio frame.
                player.prepare()
                self._aec_verifier(self._config)
            connection = self._connection_factory(
                url=self._config.url,
                connect_address=self._config.connect_address,
                token=self._config.token,
                connect_timeout_seconds=self._config.connect_timeout_seconds,
                io_timeout_seconds=self._config.io_timeout_seconds,
                max_message_bytes=self._config.max_message_bytes,
            )
            connection.send_json(realtime_start_message(self._config))
            last_semantic_activity = self._clock()
            handshake_deadline = min(
                started_at + self._config.max_session_seconds,
                last_semantic_activity + self._config.handshake_timeout_seconds,
            )
            self._wait_for_started(connection, handshake_deadline)
            with self._state_lock:
                self._ever_ready = True
                self._ready_at = time.monotonic()
                self._ready.set()
                if self._state is SessionState.CONNECTING:
                    self._state = SessionState.READY

            pacer = _AudioPacer()
            last_semantic_activity = self._clock()
            next_ping_at = last_semantic_activity + self._config.ping_interval_seconds
            pending_ping: bytes | None = None
            pong_deadline: float | None = None
            output_epoch: int | None = None
            last_output_epoch = 0
            interrupt_sent = False
            interrupt_deadline: float | None = None

            while True:
                now = self._clock()
                _check_deadlines(
                    now,
                    started_at=started_at,
                    last_activity=last_semantic_activity,
                    max_session_seconds=self._config.max_session_seconds,
                    idle_timeout_seconds=self._config.idle_timeout_seconds,
                    pending_ping=pending_ping,
                    pong_deadline=pong_deadline,
                )

                if self._interrupt_requested.is_set():
                    self._set_local_output_epoch(None)
                    self._abort_player(player)
                    self._audio.clear()
                    if output_epoch is not None:
                        self._suppressed_output_epoch = output_epoch
                    output_epoch = None
                    if not interrupt_sent:
                        connection.send_json({"type": "interrupt"})
                        interrupt_sent = True
                        interrupt_deadline = now + self._config.io_timeout_seconds
                    elif interrupt_deadline is not None and now >= interrupt_deadline:
                        return
                elif self._stop_requested.is_set():
                    self._set_local_output_epoch(None)
                    self._abort_player(player)
                    self._audio.clear()
                    connection.send_json({"type": "stop"})
                    return
                else:
                    output_epoch, _ = self._flush_local_barge_in(
                        player,
                        output_epoch=output_epoch,
                        last_output_epoch=last_output_epoch,
                    )

                player.service()
                if output_epoch is None and not player.active:
                    self._set_local_output_epoch(None)
                if not interrupt_sent and pacer.due(now):
                    packet, remaining_packets = self._send_bridge_audio(connection)
                    if packet is not None:
                        if _pcm_has_signal(packet.data):
                            last_semantic_activity = self._clock()
                        # A bounded 2x catch-up drains only a startup backlog;
                        # once one live frame remains, exact microphone pacing
                        # resumes. This avoids both an unbounded burst and a
                        # permanent handshake-sized delay behind live speech.
                        pacer.sent(
                            now,
                            len(packet.data),
                            catching_up=remaining_packets > 0,
                        )

                if pending_ping is None and now >= next_ping_at:
                    pending_ping = os.urandom(8)
                    connection.send_ping(pending_ping)
                    pong_deadline = now + self._config.pong_timeout_seconds
                    next_ping_at = now + self._config.ping_interval_seconds

                ping_wait = (
                    max(0.0, next_ping_at - now)
                    if pending_ping is None
                    else max(0.0, (pong_deadline or now) - now)
                )
                timeout = min(
                    _NETWORK_TICK_SECONDS,
                    pacer.delay(now) or _NETWORK_TICK_SECONDS,
                    ping_wait,
                    max(
                        0.0,
                        started_at + self._config.max_session_seconds - now,
                    ),
                    max(
                        0.0,
                        last_semantic_activity
                        + self._config.idle_timeout_seconds
                        - now,
                    ),
                )
                if not _socket_readable(connection, timeout):
                    self._wake_network.clear()
                    continue
                message = connection.receive_message()
                if message is None:
                    continue
                if message.kind == "pong":
                    if message.data == pending_ping:
                        pending_ping = None
                        pong_deadline = None
                    continue
                action, output_epoch, last_output_epoch, semantic = (
                    self._handle_message(
                        message,
                        player,
                        output_epoch=output_epoch,
                        last_output_epoch=last_output_epoch,
                    )
                )
                if semantic:
                    last_semantic_activity = self._clock()
                if action == "stop":
                    return
                if interrupt_sent and action == "interrupted":
                    return
                if interrupt_sent and action == "interrupt_resumed":
                    self._set_local_output_epoch(None)
                    with self._state_lock:
                        preserve_session = self._interrupt_preserve_session
                        if preserve_session:
                            self._interrupt_requested.clear()
                            if self._state is SessionState.STOPPING:
                                self._state = SessionState.READY
                    if not preserve_session:
                        connection.send_json({"type": "stop"})
                        return
                    interrupt_sent = False
                    interrupt_deadline = None
                    self._suppressed_output_epoch = None
                    last_semantic_activity = self._clock()
        except (OSError, TimeoutError, ValueError, WebSocketError):
            failed = not (
                self._stop_requested.is_set() or self._interrupt_requested.is_set()
            )
            if failed:
                _LOGGER.warning("ThirdReality realtime session failed")
        except Exception:  # noqa: BLE001 - never escape the vendor daemon thread
            failed = True
            _LOGGER.warning("ThirdReality realtime session failed", exc_info=False)
        finally:
            try:
                self._set_local_output_epoch(None)
                self._audio.clear()
                with suppress(Exception):
                    self._abort_player(player)
                if connection is not None:
                    with suppress(Exception):
                        connection.send_close()
                    with suppress(Exception):
                        connection.close()
            finally:
                with self._state_lock:
                    self._state = (
                        SessionState.FAILED if failed else SessionState.STOPPED
                    )
                self._terminal.set()
                with _SESSIONS_LOCK:
                    _SESSIONS.discard(self)

    def _run_device_webrtc(  # noqa: C901
        self,
    ) -> None:
        """Terminate WebRTC on the device and keep the bridge signaling-only."""
        diagnostics = _DirectSessionDiagnostics(started_at=time.monotonic())
        self._direct_diagnostics = diagnostics
        connection: WebSocketConnection | None = None
        sidecar: WebRtcSidecarClient | None = None
        standby: _DirectStandby | None = None
        player: _PlayerLike | None = None
        failed = False
        capture_ages_ms: deque[float] = deque(maxlen=256)
        try:
            started_at = self._clock()
            sink = self._config.pulse_aec_sink
            if sink is None:
                raise WebSocketError(  # noqa: TRY301
                    "direct WebRTC playback has no AEC sink"
                )
            player = self._direct_player_factory(
                self._config.output_queue_bytes,
                sink,
            )
            # Perform the bounded sink mutation before negotiation. The live
            # media loop must never block on pactl while it owns VAD, interrupt,
            # IPC draining, and immediate paplay termination.
            player.prepare()
            # Assist/TTS playback may leave the shared dedicated AEC sink above
            # the direct-session ceiling. Restore and verify the configured
            # exact volume first, then prove the complete echo-cancel topology
            # and live capture route before opening any network connection.
            self._aec_verifier(self._config)
            state = _DirectPlaybackState()
            diagnostics.phase = "sidecar_offer"
            sidecar = self._sidecar_factory()
            negotiation_started_at = self._clock()
            handshake_deadline = min(
                started_at + self._config.max_session_seconds,
                negotiation_started_at + self._config.handshake_timeout_seconds,
            )

            sidecar.request_offer(
                direct_capture_gain_db=self._config.direct_capture_gain_db,
            )
            offer_sdp = self._wait_for_direct_offer(sidecar, handshake_deadline)
            diagnostics.phase = "bridge_connect"
            connection = self._connection_factory(
                url=self._config.url,
                connect_address=self._config.connect_address,
                token=self._config.token,
                connect_timeout_seconds=self._config.connect_timeout_seconds,
                io_timeout_seconds=self._config.io_timeout_seconds,
                max_message_bytes=self._config.max_message_bytes,
            )
            connection.send_json(
                realtime_start_message(self._config, webrtc_sdp=offer_sdp)
            )
            diagnostics.phase = "bridge_answer"
            answer_sdp = self._wait_for_direct_answer(
                connection,
                sidecar,
                handshake_deadline,
            )
            sidecar.set_answer(answer_sdp)
            startup_capture_sample_end = self._freeze_direct_startup_capture(0)
            diagnostics.phase = "peer_handshake"

            pacer = _AudioPacer()
            sample_index = 0
            capture_committed = self._maybe_commit_direct_capture(
                sidecar,
                sample_index=sample_index,
                startup_sample_end=startup_capture_sample_end,
                committed=False,
            )
            ready_states: set[str] = set()
            required_states = {
                "answer.applied",
                "capture.ready",
                "connected",
                "data.ready",
            }
            while not required_states.issubset(ready_states):
                self._raise_if_direct_startup_cancelled(handshake_deadline)
                now = self._clock()
                # Local signal may be persistent ambient noise. It remains a
                # capture metric but cannot prove provider-side activity.
                sample_index, _ = self._send_direct_audio(
                    sidecar,
                    pacer,
                    peer_epoch=1,
                    sample_index=sample_index,
                    now=now,
                    capture_ages_ms=capture_ages_ms,
                    capture_max_age_seconds=(
                        _DIRECT_CAPTURE_MAX_AGE_SECONDS
                        if capture_committed
                        else _DIRECT_STARTUP_CAPTURE_MAX_AGE_SECONDS
                    ),
                )
                capture_committed = self._maybe_commit_direct_capture(
                    sidecar,
                    sample_index=sample_index,
                    startup_sample_end=startup_capture_sample_end,
                    committed=capture_committed,
                )
                controls, _ = self._drain_direct_sidecar(
                    sidecar,
                    player,
                    state,
                )
                for control in controls:
                    if control.type in required_states:
                        diagnostics.observe_peer_state(control.type)
                        ready_states.add(control.type)
                    else:
                        raise SidecarError(  # noqa: TRY301
                            "sidecar emitted an unexpected handshake event"
                        )
                self._reject_direct_bridge_message_if_ready(connection)
                self._wait_direct_tick(pacer, now, handshake_deadline)

            connection.send_json({"type": "transport_ready", "protocol_version": 3})
            diagnostics.phase = "bridge_ready"
            while True:
                self._raise_if_direct_startup_cancelled(handshake_deadline)
                now = self._clock()
                sample_index, _ = self._send_direct_audio(
                    sidecar,
                    pacer,
                    peer_epoch=1,
                    sample_index=sample_index,
                    now=now,
                    capture_ages_ms=capture_ages_ms,
                )
                controls, _ = self._drain_direct_sidecar(sidecar, player, state)
                if controls:
                    raise SidecarError(  # noqa: TRY301
                        "sidecar emitted an unexpected startup event"
                    )
                if _socket_readable(connection, 0):
                    message = connection.receive_message()
                    if message is None:
                        continue
                    if message.kind == "pong":
                        continue
                    if message.kind != "text":
                        raise WebSocketError(  # noqa: TRY301
                            "direct WebRTC startup received non-JSON data"
                        )
                    value = _json_object(message.data)
                    if value.get("type") == "error":
                        raise WebSocketError(  # noqa: TRY301
                            "bridge rejected direct WebRTC startup"
                        )
                    _validate_direct_started(value)
                    break
                self._wait_direct_tick(pacer, now, handshake_deadline)

            diagnostics.handshake_ready = True
            diagnostics.phase = "runtime"
            with self._state_lock:
                self._ever_ready = True
                self._ready_at = time.monotonic()
                self._ready.set()
                if self._state is SessionState.CONNECTING:
                    self._state = SessionState.READY
            _emit_direct_syslog_status(
                diagnostics,
                status="ready",
                duration_ms=int((time.monotonic() - diagnostics.started_at) * 1_000),
                outcome="live",
            )

            standby = None
            initial_standby_requested = False
            peer_epoch = 1

            last_semantic_activity = self._clock()
            next_syslog_at = last_semantic_activity + _DIRECT_SYSLOG_INTERVAL_SECONDS
            next_ping_at = last_semantic_activity + self._config.ping_interval_seconds
            pending_ping: bytes | None = None
            pong_deadline: float | None = None

            while True:
                now = self._clock()
                self._raise_if_direct_rollover_failed()
                _check_deadlines(
                    now,
                    started_at=started_at,
                    last_activity=last_semantic_activity,
                    max_session_seconds=self._config.max_session_seconds,
                    idle_timeout_seconds=self._config.idle_timeout_seconds,
                    pending_ping=pending_ping,
                    pong_deadline=pong_deadline,
                )

                if self._interrupt_requested.is_set():
                    self._abort_player(player)
                    self._set_local_output_epoch(None)
                    if state.active_generation is not None:
                        state.retired_generation = max(
                            state.retired_generation,
                            state.active_generation,
                        )
                    self._audio.clear()
                    sidecar.stop()
                    connection.send_json({"type": "stop"})
                    return
                if self._stop_requested.is_set():
                    self._abort_player(player)
                    self._set_local_output_epoch(None)
                    self._audio.clear()
                    sidecar.stop()
                    connection.send_json({"type": "stop"})
                    return
                if not initial_standby_requested and self._live_capture_opened.is_set():
                    initial_standby_requested = True
                    standby = self._start_direct_standby(sidecar)
                previous_generation = state.active_generation
                active_generation, requested_watermark = self._flush_local_barge_in(
                    player,
                    output_epoch=previous_generation,
                    last_output_epoch=state.newest_generation,
                )
                if requested_watermark is not None and active_generation is None:
                    interrupted_generation = (
                        previous_generation
                        if previous_generation is not None
                        else state.newest_generation
                    )
                    state.retired_generation = max(
                        state.retired_generation,
                        interrupted_generation,
                    )
                    state.active_generation = None
                    self._begin_direct_rollover_capture(requested_watermark)
                    last_semantic_activity = now
                    peer_epoch += 1
                    self._set_direct_peer_epoch(peer_epoch)
                    diagnostics.phase = "rollover"
                    (
                        sidecar,
                        state,
                        pacer,
                        sample_index,
                        context_retained,
                    ) = self._rollover_direct_peer(
                        connection,
                        sidecar,
                        standby,
                        player,
                        epoch=peer_epoch,
                        session_deadline=(
                            started_at + self._config.max_session_seconds
                        ),
                        capture_ages_ms=capture_ages_ms,
                    )
                    diagnostics.phase = "runtime"
                    if not context_retained:
                        with self._state_lock:
                            self._context_loss_rollovers += 1
                        _LOGGER.warning(
                            "ThirdReality direct rollover did not retain context"
                        )
                    standby = self._start_direct_standby(sidecar)
                    last_semantic_activity = self._clock()
                    next_ping_at = (
                        last_semantic_activity + self._config.ping_interval_seconds
                    )
                    pending_ping = None
                    pong_deadline = None
                    continue
                # A capture callback or the preceding lifecycle drain may have
                # qualified an interruption. Service queued output only after
                # that decision is atomically consumed at the top of the loop.
                self._service_direct_player(player)
                sample_index, _ = self._send_direct_audio(
                    sidecar,
                    pacer,
                    peer_epoch=peer_epoch,
                    sample_index=sample_index,
                    now=now,
                    capture_ages_ms=capture_ages_ms,
                )
                controls, output_semantic = self._drain_direct_sidecar(
                    sidecar,
                    player,
                    state,
                )
                controls, standby = self._update_direct_standby_from_controls(
                    controls,
                    standby,
                )
                if controls:
                    raise SidecarError(  # noqa: TRY301
                        "sidecar emitted an unexpected runtime event"
                    )
                if output_semantic:
                    last_semantic_activity = self._clock()

                if now >= next_syslog_at:
                    if diagnostics.playback_signal_packets == 0:
                        _emit_direct_syslog_status(
                            diagnostics,
                            status="waiting_output",
                            duration_ms=int(
                                (time.monotonic() - diagnostics.started_at) * 1_000
                            ),
                            outcome="live",
                        )
                    next_syslog_at = now + _DIRECT_SYSLOG_INTERVAL_SECONDS

                if pending_ping is None and now >= next_ping_at:
                    pending_ping = os.urandom(8)
                    connection.send_ping(pending_ping)
                    pong_deadline = now + self._config.pong_timeout_seconds
                    next_ping_at = now + self._config.ping_interval_seconds

                if _socket_readable(connection, 0):
                    message = connection.receive_message()
                    if message is None:
                        continue
                    if message.kind == "pong":
                        if message.data == pending_ping:
                            pending_ping = None
                            pong_deadline = None
                        continue
                    if message.kind != "text":
                        raise WebSocketError(  # noqa: TRY301
                            "direct WebRTC bridge carried unexpected media"
                        )
                    value = _json_object(message.data)
                    message_type = value.get("type")
                    if message_type == "pong":
                        continue
                    if message_type == "stopped":
                        diagnostics.phase = "remote_stop"
                        return
                    if message_type == "error":
                        raise WebSocketError(  # noqa: TRY301
                            "direct WebRTC bridge reported an error"
                        )
                    raise WebSocketError(  # noqa: TRY301
                        "unsupported direct WebRTC bridge control"
                    )

                ping_wait = (
                    max(0.0, next_ping_at - now)
                    if pending_ping is None
                    else max(0.0, (pong_deadline or now) - now)
                )
                timeout = min(
                    _NETWORK_TICK_SECONDS,
                    pacer.delay(now) or _NETWORK_TICK_SECONDS,
                    ping_wait,
                    max(0.0, started_at + self._config.max_session_seconds - now),
                    max(
                        0.0,
                        last_semantic_activity
                        + self._config.idle_timeout_seconds
                        - now,
                    ),
                )
                self._wake_network.wait(timeout)
                self._wake_network.clear()
        except (
            OSError,
            PulsePlaybackError,
            SidecarError,
            TimeoutError,
            ValueError,
            WebSocketError,
        ) as exc:
            failed = not (
                self._stop_requested.is_set() or self._interrupt_requested.is_set()
            )
            if failed:
                _LOGGER.warning(
                    "ThirdReality direct WebRTC session failed: phase=%s error=%s",
                    diagnostics.phase,
                    type(exc).__name__,
                    exc_info=False,
                )
        except Exception as exc:  # noqa: BLE001 - never escape vendor daemon thread
            failed = True
            _LOGGER.warning(
                "ThirdReality direct WebRTC session failed: phase=%s error=%s",
                diagnostics.phase,
                type(exc).__name__,
                exc_info=False,
            )
        finally:
            try:
                self._set_local_output_epoch(None)
                self._audio.clear()
                if player is not None:
                    with suppress(Exception):
                        self._abort_player(player)
                    close_player = getattr(player, "close", None)
                    if callable(close_player):
                        with suppress(Exception):
                            close_player(timeout=0.5)
                if sidecar is not None:
                    with suppress(Exception):
                        sidecar.stop()
                    with suppress(Exception):
                        sidecar.close()
                if connection is not None:
                    with suppress(Exception):
                        connection.send_close()
                    with suppress(Exception):
                        connection.close()
            finally:
                capture_age_p95_ms = 0.0
                capture_age_max_ms = 0.0
                if capture_ages_ms:
                    ordered_ages = sorted(capture_ages_ms)
                    p95_index = max(0, int(len(ordered_ages) * 0.95) - 1)
                    capture_age_p95_ms = ordered_ages[p95_index]
                    capture_age_max_ms = ordered_ages[-1]
                if failed:
                    outcome = "failed"
                elif self._interrupt_requested.is_set():
                    outcome = "interrupted"
                elif self._stop_requested.is_set():
                    outcome = "stopped"
                else:
                    outcome = "remote_stopped"
                duration_ms = max(
                    0,
                    int((time.monotonic() - diagnostics.started_at) * 1_000),
                )
                _emit_direct_syslog_status(
                    diagnostics,
                    status="terminal",
                    duration_ms=duration_ms,
                    outcome=outcome,
                )
                _LOGGER.info(
                    "ThirdReality direct WebRTC session summary: "
                    "handshake_ready=%s phase=%s peer_answer_applied=%s "
                    "peer_connected=%s peer_data_ready=%s "
                    "capture_sent_packets=%d capture_sent_bytes=%d "
                    "capture_max_peak=%d capture_max_rms=%d "
                    "capture_signal_frames=%d "
                    "post_gain_max_peak=%d post_gain_max_rms=%d "
                    "clipped_samples=%d clipped_frames=%d "
                    "lifecycle_events=%s "
                    "playback_signal_packets=%d playback_signal_bytes=%d "
                    "playback_max_peak=%d playback_max_rms=%d "
                    "capture_age_p95_ms=%.1f capture_age_max_ms=%.1f "
                    "duration_ms=%d outcome=%s",
                    "yes" if diagnostics.handshake_ready else "no",
                    diagnostics.phase,
                    "yes" if diagnostics.peer_answer_applied else "no",
                    "yes" if diagnostics.peer_connected else "no",
                    "yes" if diagnostics.peer_data_ready else "no",
                    diagnostics.capture_packets,
                    diagnostics.capture_bytes,
                    diagnostics.capture_max_peak,
                    diagnostics.capture_max_rms,
                    diagnostics.capture_signal_frames,
                    diagnostics.post_gain_max_peak,
                    diagnostics.post_gain_max_rms,
                    diagnostics.clipped_samples,
                    diagnostics.clipped_frames,
                    diagnostics.lifecycle_summary(),
                    diagnostics.playback_signal_packets,
                    diagnostics.playback_signal_bytes,
                    diagnostics.playback_max_peak,
                    diagnostics.playback_max_rms,
                    capture_age_p95_ms,
                    capture_age_max_ms,
                    duration_ms,
                    outcome,
                )
                if self._uses_global_sidecar:
                    try:
                        prewarm_device_webrtc()
                    except Exception:  # noqa: BLE001 - terminal must still publish
                        _LOGGER.warning(
                            "ThirdReality WebRTC replenishment failed",
                            exc_info=False,
                        )
                with self._state_lock:
                    self._state = (
                        SessionState.FAILED if failed else SessionState.STOPPED
                    )
                self._terminal.set()
                with _SESSIONS_LOCK:
                    _SESSIONS.discard(self)

    def _wait_for_direct_offer(
        self,
        sidecar: WebRtcSidecarClient,
        deadline: float,
    ) -> str:
        """Wait for exactly one content-free sidecar offer."""
        while True:
            self._raise_if_direct_startup_cancelled(deadline)
            if _socket_readable(sidecar, 0):
                for message in sidecar.drain_messages():
                    self._observe_direct_sidecar_message(message)
                    if (
                        isinstance(message, ControlMessage)
                        and message.type == "offer"
                        and isinstance(message.values.get("sdp"), str)
                    ):
                        return str(message.values["sdp"])
                    if isinstance(message, ControlMessage) and message.type == "error":
                        raise SidecarError("sidecar could not create an offer")
                    raise SidecarError("sidecar emitted an unexpected offer event")
            self._wake_network.wait(_DIRECT_HANDSHAKE_TICK_SECONDS)
            self._wake_network.clear()

    def _wait_for_direct_answer(
        self,
        connection: WebSocketConnection,
        sidecar: WebRtcSidecarClient,
        deadline: float,
    ) -> str:
        """Wait for one strict bridge answer while monitoring the sidecar."""
        while True:
            self._raise_if_direct_startup_cancelled(deadline)
            if _socket_readable(sidecar, 0):
                messages = sidecar.drain_messages()
                if messages:
                    for message in messages:
                        self._observe_direct_sidecar_message(message)
                    raise SidecarError("sidecar failed while awaiting SDP answer")
            if _socket_readable(connection, 0):
                message = connection.receive_message()
                if message is None:
                    continue
                if message.kind == "pong":
                    continue
                if message.kind != "text":
                    raise WebSocketError("direct WebRTC answer must be JSON")
                value = _json_object(message.data)
                if value.get("type") == "error":
                    raise WebSocketError("bridge rejected direct WebRTC startup")
                return _direct_answer_sdp(value)
            self._wake_network.wait(_DIRECT_HANDSHAKE_TICK_SECONDS)
            self._wake_network.clear()

    def _start_direct_standby(
        self,
        active: WebRtcSidecarClient,
    ) -> _DirectStandby | None:
        """Best-effort prewarm one logical peer in the active worker process."""
        try:
            active.request_standby_offer(
                direct_capture_gain_db=self._config.direct_capture_gain_db,
            )
            return _DirectStandby(active)
        except Exception:  # noqa: BLE001 - active conversation remains usable
            _LOGGER.warning(
                "ThirdReality direct rollover prewarm failed",
                exc_info=False,
            )
            return None

    def _update_direct_standby_from_controls(
        self,
        controls: list[ControlMessage],
        standby: _DirectStandby | None,
    ) -> tuple[list[ControlMessage], _DirectStandby | None]:
        """Consume standby controls interleaved with active-peer media."""
        remaining: list[ControlMessage] = []
        for control in controls:
            if control.type == "standby.offer":
                if standby is None or standby.offer_sdp is not None:
                    raise SidecarError("sidecar emitted an unexpected standby offer")
                sdp, peer_epoch = self._direct_standby_offer(control)
                standby.offer_sdp = sdp
                standby.peer_epoch = peer_epoch
                continue
            if control.type == "standby.failed":
                if standby is None:
                    raise SidecarError("sidecar emitted an unexpected standby failure")
                failed_epoch = control.values.get("peer_epoch")
                if type(failed_epoch) is not int or failed_epoch < 1:
                    raise SidecarError("sidecar standby failure epoch is invalid")
                standby = None
                _LOGGER.warning(
                    "ThirdReality direct rollover prewarm failed",
                    exc_info=False,
                )
                continue
            remaining.append(control)
        return remaining, standby

    @staticmethod
    def _direct_standby_offer(control: ControlMessage) -> tuple[str, int]:
        """Validate one logical standby offer and its process-local epoch."""
        sdp = control.values.get("sdp")
        peer_epoch = control.values.get("peer_epoch")
        if (
            not isinstance(sdp, str)
            or not sdp.startswith("v=0")
            or "\x00" in sdp
            or type(peer_epoch) is not int
            or peer_epoch < 1
        ):
            raise SidecarError("sidecar emitted an invalid standby offer")
        return sdp, peer_epoch

    def _wait_for_direct_standby_offer(
        self,
        standby: _DirectStandby,
        deadline: float,
    ) -> None:
        """Wait boundedly for the in-process standby while discarding old output."""
        while standby.offer_sdp is None:
            self._raise_if_direct_startup_cancelled(deadline)
            if not _socket_readable(standby.sidecar, 0):
                self._wake_network.wait(_DIRECT_HANDSHAKE_TICK_SECONDS)
                self._wake_network.clear()
                continue
            controls: list[ControlMessage] = []
            for message in standby.sidecar.drain_messages(maximum=8):
                self._observe_direct_sidecar_message(message)
                if isinstance(message, PlaybackAudio):
                    continue
                if message.type in {"capture.metrics", "lifecycle"}:
                    continue
                if message.type == "error":
                    raise SidecarError("device WebRTC sidecar reported an error")
                controls.append(message)
            controls, current = self._update_direct_standby_from_controls(
                controls,
                standby,
            )
            if current is None:
                raise SidecarError("direct rollover standby failed")
            if controls:
                raise SidecarError("sidecar emitted an unexpected standby event")

    def _rollover_direct_peer(
        self,
        connection: WebSocketConnection,
        old_sidecar: WebRtcSidecarClient,
        standby: _DirectStandby | None,
        player: _PlayerLike,
        *,
        epoch: int,
        session_deadline: float,
        capture_ages_ms: deque[float],
    ) -> tuple[
        WebRtcSidecarClient,
        _DirectPlaybackState,
        _AudioPacer,
        int,
        bool,
    ]:
        """Promote one in-process peer without releasing the outer voice session."""
        now = self._clock()
        deadline = min(
            session_deadline,
            now + self._config.handshake_timeout_seconds,
        )
        pending_output = _DirectPendingOutput(self._config.output_queue_bytes)
        try:
            if standby is None:
                raise SidecarError(  # noqa: TRY301
                    "direct rollover has no healthy offer-warm standby"
                )
            if standby.sidecar is not old_sidecar:
                raise SidecarError(  # noqa: TRY301
                    "direct rollover standby escaped its active worker"
                )
            self._wait_for_direct_standby_offer(standby, deadline)
            offer_sdp = standby.offer_sdp
            if offer_sdp is None or standby.peer_epoch != epoch:
                raise SidecarError(  # noqa: TRY301
                    "direct rollover standby epoch is incompatible"
                )
            if old_sidecar.closed:
                raise SidecarError(  # noqa: TRY301
                    "direct rollover replacement exited before use"
                )

            # One ordered command fences and stops the active logical peer,
            # then promotes the offer-warm peer inside the same OS process.
            # Send bridge rollover immediately afterward so the old provider's
            # close cannot win the device-control race.
            try:
                old_sidecar.promote_standby(epoch)
            except Exception as exc:
                with suppress(Exception):
                    old_sidecar.close(timeout=0.0)
                raise SidecarError(
                    "direct sidecar standby could not be promoted"
                ) from exc

            connection.send_json(
                {
                    "type": "rollover",
                    "protocol_version": 3,
                    "epoch": epoch,
                    "transport": {"type": "webrtc", "sdp": offer_sdp},
                }
            )
            # Capture sent after the promote command is ordered behind the
            # runtime's stop/swap barrier and reaches only the promoted peer.
            pacer = _AudioPacer()
            sample_index = 0
            answer_sdp, sample_index = self._wait_for_direct_rollover_answer(
                connection,
                old_sidecar,
                deadline,
                epoch=epoch,
                pacer=pacer,
                sample_index=sample_index,
                capture_ages_ms=capture_ages_ms,
                pending_output=pending_output,
            )
            old_sidecar.set_answer(answer_sdp)

            # Every replacement peer owns a new contiguous RTP input timeline.
            # Capture watermarks remain process-global solely for dedupe, while
            # original capture timestamps remain unchanged as freshness proof.
            playback_state = _DirectPlaybackState()
            startup_capture_sample_end = self._freeze_direct_startup_capture(
                sample_index
            )
            capture_committed = self._maybe_commit_direct_capture(
                old_sidecar,
                sample_index=sample_index,
                startup_sample_end=startup_capture_sample_end,
                committed=False,
            )
            required_states = {
                "answer.applied",
                "capture.ready",
                "connected",
                "data.ready",
            }
            ready_states: set[str] = set()
            while not required_states.issubset(ready_states):
                self._raise_if_direct_startup_cancelled(deadline)
                now = self._clock()
                sample_index, _ = self._send_direct_audio(
                    old_sidecar,
                    pacer,
                    peer_epoch=epoch,
                    sample_index=sample_index,
                    now=now,
                    capture_ages_ms=capture_ages_ms,
                    capture_max_age_seconds=(
                        _DIRECT_CAPTURE_MAX_AGE_SECONDS
                        if capture_committed
                        else _DIRECT_STARTUP_CAPTURE_MAX_AGE_SECONDS
                    ),
                )
                capture_committed = self._maybe_commit_direct_capture(
                    old_sidecar,
                    sample_index=sample_index,
                    startup_sample_end=startup_capture_sample_end,
                    committed=capture_committed,
                )
                controls = self._drain_direct_handshake_sidecar(
                    old_sidecar,
                    pending_output,
                )
                for control in controls:
                    if control.type in required_states:
                        ready_states.add(control.type)
                    else:
                        raise SidecarError(  # noqa: TRY301
                            "sidecar emitted an unexpected rollover event"
                        )
                self._reject_direct_bridge_message_if_ready(connection)
                self._wait_direct_tick(pacer, now, deadline)

            connection.send_json(
                {
                    "type": "rollover_transport_ready",
                    "protocol_version": 3,
                    "epoch": epoch,
                }
            )
            context_retained = self._wait_for_direct_rollover_started(
                connection,
                old_sidecar,
                pacer,
                sample_index=sample_index,
                deadline=deadline,
                epoch=epoch,
                capture_ages_ms=capture_ages_ms,
                pending_output=pending_output,
            )
            sample_index = context_retained[0]
            self._replay_direct_pending_output(
                pending_output,
                old_sidecar,
                player,
                playback_state,
            )
            with self._state_lock:
                if self._state is not SessionState.INTERRUPTING:
                    raise WebSocketClosed(  # noqa: TRY301
                        "direct rollover was cancelled"
                    )
                self._state = SessionState.READY
            return (
                old_sidecar,
                playback_state,
                pacer,
                sample_index,
                context_retained[1],
            )
        except Exception:
            with suppress(Exception):
                old_sidecar.close(timeout=0.0)
            raise

    def _wait_for_direct_rollover_answer(
        self,
        connection: WebSocketConnection,
        sidecar: WebRtcSidecarClient,
        deadline: float,
        *,
        epoch: int,
        pacer: _AudioPacer,
        sample_index: int,
        capture_ages_ms: deque[float],
        pending_output: _DirectPendingOutput,
    ) -> tuple[str, int]:
        """Wait for both the ordered promotion barrier and bridge SDP answer."""
        answer_sdp: str | None = None
        promoted = False
        while answer_sdp is None or not promoted:
            self._raise_if_direct_startup_cancelled(deadline)
            now = self._clock()
            sample_index, _ = self._send_direct_audio(
                sidecar,
                pacer,
                peer_epoch=epoch,
                sample_index=sample_index,
                now=now,
                capture_ages_ms=capture_ages_ms,
                capture_max_age_seconds=_DIRECT_STARTUP_CAPTURE_MAX_AGE_SECONDS,
            )
            if _socket_readable(sidecar, 0):
                for sidecar_message in sidecar.drain_messages(maximum=8):
                    self._observe_direct_sidecar_message(sidecar_message)
                    if not promoted:
                        if (
                            isinstance(sidecar_message, ControlMessage)
                            and sidecar_message.type == "standby.promoted"
                        ):
                            acknowledged_epoch = sidecar_message.values.get(
                                "peer_epoch"
                            )
                            if acknowledged_epoch != epoch:
                                raise SidecarError(
                                    "sidecar promoted an incompatible standby epoch"
                                )
                            promoted = True
                            continue
                        if isinstance(
                            sidecar_message, ControlMessage
                        ) and sidecar_message.type in {"error", "standby.failed"}:
                            raise SidecarError("sidecar standby promotion failed")
                        # The promotion acknowledgement is an ordered fence:
                        # everything before it belongs to the retired peer.
                        continue
                    self._append_direct_pending_handshake_output(
                        sidecar_message,
                        pending_output,
                    )
            if _socket_readable(connection, 0):
                message = connection.receive_message()
                if message is None:
                    continue
                if message.kind == "pong":
                    continue
                if message.kind != "text":
                    raise WebSocketError("direct rollover answer must be JSON")
                value = _json_object(message.data)
                if value.get("type") == "error":
                    raise WebSocketError("bridge rejected direct rollover")
                if answer_sdp is not None:
                    raise WebSocketError("bridge emitted duplicate rollover answer")
                answer_sdp = _direct_rollover_answer_sdp(value, epoch=epoch)
            self._wait_direct_tick(pacer, now, deadline)
        return answer_sdp, sample_index

    def _append_direct_pending_handshake_output(
        self,
        message: ControlMessage | PlaybackAudio,
        pending_output: _DirectPendingOutput,
    ) -> None:
        """Retain only new-peer output observed after the promotion barrier."""
        if isinstance(message, PlaybackAudio):
            pending_output.append(message)
            return
        if message.type == "capture.metrics":
            return
        if message.type in {"error", "standby.failed"}:
            raise SidecarError("device WebRTC sidecar reported an error")
        if message.type != "lifecycle":
            raise SidecarError("sidecar emitted an unexpected promotion event")
        event_type = message.values.get("event_type")
        generation = message.values.get("generation")
        if (
            not isinstance(event_type, str)
            or type(generation) is not int
            or generation < 0
            or event_type in {"error", "invalid_request_error"}
            or event_type.endswith("_error")
        ):
            raise SidecarError("sidecar lifecycle metadata is invalid")
        pending_output.append(message)

    def _wait_for_direct_rollover_started(
        self,
        connection: WebSocketConnection,
        sidecar: WebRtcSidecarClient,
        pacer: _AudioPacer,
        *,
        sample_index: int,
        deadline: float,
        epoch: int,
        capture_ages_ms: deque[float],
        pending_output: _DirectPendingOutput,
    ) -> tuple[int, bool]:
        """Drain ordered capture until the bridge starts the replacement epoch."""
        while True:
            self._raise_if_direct_startup_cancelled(deadline)
            now = self._clock()
            sample_index, _ = self._send_direct_audio(
                sidecar,
                pacer,
                peer_epoch=epoch,
                sample_index=sample_index,
                now=now,
                capture_ages_ms=capture_ages_ms,
            )
            controls = self._drain_direct_handshake_sidecar(sidecar, pending_output)
            if controls:
                raise SidecarError("sidecar emitted an unexpected rollover event")
            if _socket_readable(connection, 0):
                message = connection.receive_message()
                if message is None:
                    continue
                if message.kind == "pong":
                    continue
                if message.kind != "text":
                    raise WebSocketError("direct rollover start must be JSON")
                value = _json_object(message.data)
                if value.get("type") == "error":
                    raise WebSocketError("bridge rejected direct rollover")
                retained = _direct_rollover_context_retained(value, epoch=epoch)
                return sample_index, retained
            self._wait_direct_tick(pacer, now, deadline)

    def _raise_if_direct_startup_cancelled(self, deadline: float) -> None:
        """Apply the single startup deadline and stop boundary."""
        self._raise_if_direct_rollover_failed()
        if self._stop_requested.is_set() or self._interrupt_requested.is_set():
            raise WebSocketClosed("direct WebRTC startup was cancelled")
        if self._clock() >= deadline:
            raise TimeoutError("direct WebRTC startup timed out")

    def _freeze_direct_startup_capture(self, sample_index: int) -> int:
        """Freeze one finite queued prefix in the fresh peer's sample timeline."""
        if sample_index < 0:
            raise ValueError("direct capture sample index cannot be negative")
        with self._state_lock:
            if self._state not in {
                SessionState.CONNECTING,
                SessionState.INTERRUPTING,
            }:
                raise SidecarError("direct startup capture froze outside negotiation")
            # ``submit_audio`` holds this same lifecycle lock while appending,
            # so the queue byte snapshot is a linearized, finite prefix. Later
            # recorder callbacks necessarily append on the post-commit side.
            with self._audio._lock:  # noqa: SLF001 - one internal queue barrier.
                queued_bytes = self._audio._bytes  # noqa: SLF001
        if queued_bytes % 2:
            raise SidecarError("direct startup capture is not aligned PCM16")
        return sample_index + queued_bytes // 2

    @staticmethod
    def _maybe_commit_direct_capture(
        sidecar: WebRtcSidecarClient,
        *,
        sample_index: int,
        startup_sample_end: int,
        committed: bool,
    ) -> bool:
        """Send the ordered commit immediately after the frozen prefix."""
        if committed or sample_index < startup_sample_end:
            return committed
        if sample_index != startup_sample_end:
            raise SidecarError("direct startup capture crossed its commit boundary")
        sidecar.commit_capture()
        return True

    def _raise_if_direct_rollover_failed(self) -> None:
        """Make loss-intolerant rollover queue pressure terminal."""
        if self._direct_rollover_failed.is_set():
            raise SidecarError("direct rollover capture failed")

    def _wait_direct_tick(
        self,
        pacer: _AudioPacer,
        now: float,
        deadline: float,
    ) -> None:
        """Yield a bounded handshake tick without delaying queued microphone PCM."""
        timeout = min(
            _DIRECT_HANDSHAKE_TICK_SECONDS,
            pacer.delay(now) or _DIRECT_HANDSHAKE_TICK_SECONDS,
            max(0.0, deadline - now),
        )
        self._wake_network.wait(timeout)
        self._wake_network.clear()

    def _reject_direct_bridge_message_if_ready(
        self,
        connection: WebSocketConnection,
    ) -> None:
        """Fail closed if the bridge speaks before transport readiness."""
        if not _socket_readable(connection, 0):
            return
        message = connection.receive_message()
        if message is None:
            return
        if message.kind == "pong":
            return
        if message.kind == "text" and _json_object(message.data).get("type") == "error":
            raise WebSocketError("bridge rejected direct WebRTC transport")
        raise WebSocketError("bridge sent an unexpected direct WebRTC message")

    def _send_direct_audio(
        self,
        sidecar: WebRtcSidecarClient,
        pacer: _AudioPacer,
        *,
        peer_epoch: int,
        sample_index: int,
        now: float,
        capture_ages_ms: deque[float],
        capture_max_age_seconds: float = _DIRECT_CAPTURE_MAX_AGE_SECONDS,
    ) -> tuple[int, bool]:
        """Send at most one timestamped frame, preserving bounded startup catch-up."""
        if not pacer.due(now):
            return sample_index, False
        # Linearize dequeue and the non-blocking IPC send with explicit stop
        # and interrupt. A transition that returns has either cleared this
        # packet before dequeue or waited until its send completed.
        with self._state_lock:
            if (
                self._stop_requested.is_set()
                or self._interrupt_requested.is_set()
                or self._state
                not in {
                    SessionState.CONNECTING,
                    SessionState.READY,
                    SessionState.INTERRUPTING,
                }
            ):
                return sample_index, False
            packet, remaining_packets = self._audio.pop()
            if packet is None:
                return sample_index, False
            age_seconds = max(0.0, now - packet.captured_at)
            if age_seconds > capture_max_age_seconds:
                raise SidecarError("direct capture packet exceeded its age bound")
            provider_pcm = (
                bytes(len(packet.data))
                if packet.suppress_peer_epoch == peer_epoch
                else packet.data
            )
            sidecar.send_audio(
                provider_pcm,
                sample_index=sample_index,
                capture_monotonic_ns=max(
                    0,
                    int(packet.captured_at * 1_000_000_000),
                ),
            )
            self._sent_capture_watermark = max(
                self._sent_capture_watermark,
                packet.capture_watermark,
            )
            self._remember_direct_preroll(packet)
        provider_suppressed = provider_pcm is not packet.data
        has_signal = _pcm_has_signal(provider_pcm)
        diagnostics = self._direct_diagnostics
        if diagnostics is not None:
            diagnostics.observe_capture(provider_pcm, has_signal=has_signal)
            if provider_suppressed:
                diagnostics.observe_provider_suppression()
        capture_ages_ms.append(age_seconds * 1_000)
        sample_index += len(packet.data) // 2
        pacer.sent(
            now,
            len(packet.data),
            catching_up=remaining_packets > 0,
        )
        return sample_index, has_signal

    def _observe_direct_sidecar_message(
        self,
        message: ControlMessage | PlaybackAudio,
    ) -> None:
        """Update direct diagnostics without retaining any media or metadata."""
        diagnostics = self._direct_diagnostics
        if diagnostics is None:
            return
        if isinstance(message, PlaybackAudio):
            if not self._direct_observes_rendered_playback:
                diagnostics.observe_playback(message.pcm)
            return
        if message.type == "capture.metrics":
            diagnostics.observe_capture_metrics(message.values)
            return
        if message.type == "error":
            diagnostics.observe_failure_code(message.values.get("code"))
            return
        if message.type == "lifecycle":
            event_type = message.values.get("event_type")
            diagnostics.observe_lifecycle(event_type)
            if isinstance(event_type, str) and (
                event_type in {"error", "invalid_request_error"}
                or event_type.endswith("_error")
            ):
                # The strict peer maps every sanitized provider error to this
                # fixed child classification. Record it immediately rather
                # than relying on the following error control fitting in the
                # same bounded drain batch.
                diagnostics.observe_failure_code("provider_error")

    def _observe_direct_playback_write(self, value: bytes) -> None:
        """Account only for transformed PCM actually accepted by paplay."""
        combined = self._direct_render_observation_tail + value
        aligned_bytes = len(combined) - (len(combined) % 2)
        self._direct_render_observation_tail = combined[aligned_bytes:]
        diagnostics = self._direct_diagnostics
        if aligned_bytes:
            rendered = combined[:aligned_bytes]
            if diagnostics is not None:
                diagnostics.observe_playback(rendered)
            if self._render_echo_guard is not None:
                self._render_echo_guard.observe_render(
                    rendered,
                    written_at=self._clock(),
                )

    def _direct_output_allowed(self) -> bool:
        """Return whether direct media may cross the explicit output boundary."""
        with self._state_lock:
            return (
                not self._direct_output_fenced.is_set()
                and not self._stop_requested.is_set()
                and not self._interrupt_requested.is_set()
                and self._state
                in {
                    SessionState.CONNECTING,
                    SessionState.READY,
                    SessionState.INTERRUPTING,
                }
            )

    def _service_direct_player(self, player: _PlayerLike) -> None:
        """Write queued PCM only on the stop/interrupt-linearized side."""
        with self._direct_output_lock:
            if not self._direct_output_allowed():
                return
            player.service()

    def _drain_direct_sidecar(
        self,
        sidecar: WebRtcSidecarClient,
        player: _PlayerLike,
        state: _DirectPlaybackState,
    ) -> tuple[list[ControlMessage], bool]:
        """Consume bounded IPC, returning only non-media state messages."""
        with self._direct_output_lock:
            if not self._direct_output_allowed():
                return [], False
            if not _socket_readable(sidecar, 0):
                return [], False
            controls: list[ControlMessage] = []
            semantic = False
            messages = sidecar.drain_messages(maximum=8)
            # Observe the complete ordered batch first. Provider failures emit
            # a sanitized lifecycle error followed by a fixed child error
            # code; the lifecycle handler raises, so observing lazily would
            # lose the decisive second classification.
            for message in messages:
                self._observe_direct_sidecar_message(message)
            for message in messages:
                if isinstance(message, PlaybackAudio):
                    admitted = self._handle_direct_playback(message, player, state)
                    if admitted and _pcm_has_signal(message.pcm):
                        semantic = True
                    continue
                if message.type == "capture.metrics":
                    continue
                if message.type == "error":
                    raise SidecarError("device WebRTC sidecar reported an error")
                if message.type == "lifecycle":
                    if self._handle_direct_lifecycle(message, sidecar, player, state):
                        semantic = True
                    continue
                controls.append(message)
            return controls, semantic

    def _drain_direct_handshake_sidecar(
        self,
        sidecar: WebRtcSidecarClient,
        pending_output: _DirectPendingOutput,
    ) -> list[ControlMessage]:
        """Admit readiness while retaining ordered output behind the bridge ack."""
        with self._direct_output_lock:
            if not self._direct_output_allowed():
                return []
            if not _socket_readable(sidecar, 0):
                return []
            controls: list[ControlMessage] = []
            messages = sidecar.drain_messages(maximum=8)
            for message in messages:
                self._observe_direct_sidecar_message(message)
            for message in messages:
                if isinstance(message, PlaybackAudio):
                    pending_output.append(message)
                    continue
                if message.type == "capture.metrics":
                    continue
                if message.type == "error":
                    raise SidecarError("device WebRTC sidecar reported an error")
                if message.type == "lifecycle":
                    event_type = message.values.get("event_type")
                    generation = message.values.get("generation")
                    if (
                        not isinstance(event_type, str)
                        or type(generation) is not int
                        or generation < 0
                    ):
                        raise SidecarError("sidecar lifecycle metadata is invalid")
                    if event_type in {
                        "error",
                        "invalid_request_error",
                    } or event_type.endswith("_error"):
                        raise SidecarError("realtime provider reported an error")
                    if event_type == "interrupt.fenced":
                        raise SidecarError("unsolicited replacement interrupt fence")
                    # The ordered data channel can surface content-free session or
                    # media lifecycle immediately after transport readiness. Keep
                    # all of it ordered with decoded audio, but do not expose it to
                    # the player before the bridge admits the replacement epoch.
                    pending_output.append(message)
                    continue
                controls.append(message)
            return controls

    def _replay_direct_pending_output(
        self,
        pending_output: _DirectPendingOutput,
        sidecar: WebRtcSidecarClient,
        player: _PlayerLike,
        state: _DirectPlaybackState,
    ) -> None:
        """Apply a pre-ack batch through the normal ordered media handlers."""
        with self._direct_output_lock:
            if not self._direct_output_allowed():
                raise WebSocketClosed("direct output boundary was closed")
            for message in pending_output.drain():
                if isinstance(message, PlaybackAudio):
                    self._handle_direct_playback(message, player, state)
                    continue
                if message.type != "lifecycle":
                    raise SidecarError("pending replacement output was invalid")
                self._handle_direct_lifecycle(message, sidecar, player, state)

    def _handle_direct_playback(
        self,
        message: PlaybackAudio,
        player: _PlayerLike,
        state: _DirectPlaybackState,
    ) -> bool:
        """Render contiguous PCM only inside a sidecar-proven media epoch."""
        samples = len(message.pcm) // 2
        if message.generation < 1:
            raise SidecarError("playback generation must be positive")
        if (
            state.expected_sample_index is not None
            and message.sample_index != state.expected_sample_index
        ):
            raise SidecarError("playback sample sequence is not contiguous")
        state.expected_sample_index = message.sample_index + samples
        if message.generation <= state.retired_generation:
            return False
        if message.generation < state.newest_generation:
            return False
        if message.generation > state.newest_generation:
            raise SidecarError("playback arrived before its media boundary")
        if state.active_generation != message.generation:
            raise SidecarError("playback arrived outside its active media epoch")
        if self._retired_barge_in_pending():
            # A new provider response can share the same ordered drain as the
            # predecessor's quiet boundary. The qualified user utterance owns
            # the next network transition, so never queue this peer's tail.
            return False
        player.enqueue(message.pcm)
        return True

    def _handle_direct_lifecycle(
        self,
        message: ControlMessage,
        sidecar: WebRtcSidecarClient,
        player: _PlayerLike,
        state: _DirectPlaybackState,
    ) -> bool:
        """Apply sanitized provider lifecycle without transcript exposure."""
        event_type = message.values.get("event_type")
        generation = message.values.get("generation")
        if not isinstance(event_type, str) or not isinstance(generation, int):
            raise SidecarError("sidecar lifecycle metadata is invalid")
        if generation < 0:
            raise SidecarError("sidecar lifecycle generation is invalid")
        if event_type == "media.started":
            if generation <= 0:
                raise SidecarError("media generation must be positive")
            if generation <= state.retired_generation:
                return False
            if generation <= state.newest_generation:
                raise SidecarError("media generation did not advance")
            if state.active_generation is not None:
                raise SidecarError("media generation overlapped its predecessor")
            if self._retired_barge_in_pending():
                # Capture already qualified a predecessor interruption. Admit
                # just enough lifecycle state to retire this peer coherently;
                # do not clear preroll, repair the anchor, or start new output
                # before the network loop consumes the causal watermark.
                state.newest_generation = generation
                state.active_generation = generation
                return True
            # This handler runs under _direct_output_lock. Restore and verify the
            # exact fixed anchor before either a fresh or resumed paplay child
            # can accept bytes; a failed repair propagates to terminal cleanup.
            self._reconcile_media_started_anchor()
            player_was_active = player.active
            if player_was_active:
                resume = getattr(player, "resume", None)
                if not callable(resume):
                    raise SidecarError("playback cannot resume a media epoch")
                resume(generation)
            else:
                player.begin(generation)
            self._clear_direct_preroll()
            (
                anchor_settle_required,
                _anchor_model_trained,
            ) = self._take_local_anchor_settle_required()
            # A repair generation is untrusted even when it retained no FIR
            # seed. Preserve that generation across this epoch boundary so it
            # can perform a full-range post-repair qualification instead of
            # being mistaken for an ordinary freshly reset playback settle.
            repair_requalification_pending = self._anchor_requalification_pending()
            preserve_echo_model = (
                anchor_settle_required or repair_requalification_pending
            )
            self._set_local_output_epoch(
                generation,
                settle_barge_in=(
                    not player_was_active
                    or anchor_settle_required
                    or repair_requalification_pending
                ),
                preserve_echo_model=preserve_echo_model,
                preserve_pending_barge_in=True,
            )
            state.newest_generation = generation
            state.active_generation = generation
            self._suppressed_output_epoch = None
            return True

        if event_type == "media.quiet":
            if generation == state.active_generation:
                self._set_local_output_epoch(
                    None,
                    preserve_pending_barge_in=True,
                )
                state.retired_generation = max(state.retired_generation, generation)
                state.active_generation = None
                return True
            return False

        if event_type == "interrupt.fenced":
            raise SidecarError("unsolicited same-peer interrupt fence")

        if event_type == "error":
            raise SidecarError("realtime provider reported an error")
        return event_type in _DIRECT_SEMANTIC_LIFECYCLE_EVENTS

    def _wait_for_started(
        self, connection: WebSocketConnection, deadline: float
    ) -> None:
        while True:
            if self._stop_requested.is_set() or self._interrupt_requested.is_set():
                raise WebSocketClosed("realtime startup was cancelled")
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError("realtime start acknowledgement timed out")
            if not _socket_readable(connection, min(_NETWORK_TICK_SECONDS, remaining)):
                continue
            message = connection.receive_message()
            if message is None:
                continue
            if message.kind == "pong":
                continue
            if message.kind != "text":
                raise WebSocketError("realtime start must be acknowledged with JSON")
            value = _json_object(message.data)
            if value.get("type") == "error":
                raise WebSocketError("bridge rejected realtime startup")
            _validate_started(value)
            return

    def _handle_message(  # noqa: C901 - one bounded protocol decoder
        self,
        message: Message,
        player: _PlayerLike,
        *,
        output_epoch: int | None,
        last_output_epoch: int,
    ) -> tuple[str | None, int | None, int, bool]:
        if message.kind == "binary":
            if output_epoch is None:
                if self._suppressed_output_epoch is not None:
                    return None, None, last_output_epoch, False
                raise WebSocketError("binary output arrived outside a speaking epoch")
            assert isinstance(message.data, bytes)
            player.enqueue(message.data)
            return None, output_epoch, last_output_epoch, True
        if message.kind != "text":
            raise WebSocketError("unexpected WebSocket message kind")
        value = _json_object(message.data)
        message_type = value.get("type")
        if message_type == "control":
            event_type = value.get("event_type")
            if not isinstance(event_type, str) or event_type not in _CONTROL_EVENTS:
                raise WebSocketError("unsupported realtime control event")
            if event_type == "speaking.started":
                epoch = _output_epoch(value)
                if epoch <= last_output_epoch:
                    return None, output_epoch, last_output_epoch, False
                if output_epoch is not None:
                    raise WebSocketError(
                        "overlapping speaking epochs are not supported"
                    )
                # A new monotonic epoch is an unambiguous boundary after any
                # locally suppressed response tail.
                self._suppressed_output_epoch = None
                if self._config.full_duplex:
                    self._volume_guard(self._config)
                # Retire any draining predecessor and its pending detector
                # result before publishing this fresh monotonic generation.
                self._set_local_output_epoch(None)
                player.begin(epoch)
                self._set_local_output_epoch(epoch, settle_barge_in=True)
                return None, epoch, epoch, True
            if event_type == "speaking.stopped":
                epoch = _output_epoch(value)
                if output_epoch is None:
                    if self._suppressed_output_epoch == epoch:
                        self._suppressed_output_epoch = None
                        return None, None, last_output_epoch, True
                    if epoch <= last_output_epoch:
                        return None, None, last_output_epoch, False
                    raise WebSocketError("future speaking stop has no active epoch")
                if epoch != output_epoch:
                    if epoch < output_epoch:
                        return None, output_epoch, last_output_epoch, False
                    raise WebSocketError("speaking stop does not match active epoch")
                player.finish(epoch)
                return None, None, last_output_epoch, True
            if event_type == "input_audio_buffer.speech_started":
                if self._config.full_duplex and (
                    output_epoch is not None or player.active
                ):
                    self._set_local_output_epoch(None)
                    self._abort_player(player)
                    suppressed_epoch = output_epoch or last_output_epoch
                    if suppressed_epoch > 0:
                        self._suppressed_output_epoch = suppressed_epoch
                    return None, None, last_output_epoch, True
            return None, output_epoch, last_output_epoch, True
        if message_type == "pong":
            return None, output_epoch, last_output_epoch, False
        if message_type == "stopped":
            self._set_local_output_epoch(None)
            self._abort_player(player)
            reason = value.get("reason")
            if reason == "interrupt":
                fresh_session_required = value.get("fresh_session_required")
                remote_cancelled = value.get("remote_cancelled")
                if fresh_session_required is True and remote_cancelled is False:
                    self._suppressed_output_epoch = None
                    return "interrupted", None, last_output_epoch, True
                if fresh_session_required is False and remote_cancelled is True:
                    self._suppressed_output_epoch = None
                    return "interrupt_resumed", None, last_output_epoch, True
                if (
                    fresh_session_required is False
                    and remote_cancelled is False
                    and value.get("continuation_safe") is True
                ):
                    # Broker-managed realtime invalidates the bridge-owned
                    # executor/output generation locally. The voice transport
                    # may therefore remain open even when the provider did not
                    # confirm cancellation of its side-effect-free frontend.
                    self._suppressed_output_epoch = None
                    return "interrupt_resumed", None, last_output_epoch, True
                raise WebSocketError("bridge returned incompatible interrupt semantics")
            return "stop", None, last_output_epoch, True
        if message_type == "error":
            raise WebSocketError("bridge reported a realtime session error")
        raise WebSocketError("unsupported realtime JSON message")


def shutdown_all_sessions(timeout: float = 2.0) -> None:
    """Best-effort bounded cleanup for process exit."""
    global _SHUTTING_DOWN  # noqa: PLW0603
    deadline = time.monotonic() + max(0.0, timeout)
    with _PREWARM_LOCK:
        _SHUTTING_DOWN = True
        prewarmed = tuple(_PREWARMED_SIDECARS)
        _PREWARMED_SIDECARS.clear()
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS)
    # Signal every active media owner before spending any shared deadline on
    # the idle prewarm. This guarantees local playback kill/flush begins even
    # when an isolated child is wedged during process exit.
    for session in sessions:
        session.stop()
    for sidecar in prewarmed:
        with suppress(Exception):
            sidecar.close(timeout=max(0.0, deadline - time.monotonic()))
    for session in sessions:
        session.join(max(0.0, deadline - time.monotonic()))


def _socket_readable(endpoint: Any, timeout: float) -> bool:
    pending = getattr(endpoint, "pending", None)
    if callable(pending) and pending() > 0:
        return True
    transport = getattr(endpoint, "transport", endpoint)
    transport_pending = getattr(transport, "pending", None)
    if callable(transport_pending) and transport_pending() > 0:
        return True
    readable, _, _ = select.select([transport], [], [], max(0.0, timeout))
    return bool(readable)


def _json_object(value: str | bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebSocketError("realtime control must be valid JSON") from exc
    if not isinstance(decoded, dict):
        raise WebSocketError("realtime control must be a JSON object")
    return decoded


def _pcm_has_signal(value: bytes) -> bool:
    """Recognize speech-scale input without treating ambient floor as activity."""
    return any(
        abs(sample) >= _INPUT_ACTIVITY_SIGNAL_PEAK
        for (sample,) in struct.iter_unpack("<h", value)
    )


def _pcm_peak_and_rms(value: bytes) -> tuple[int, int]:
    """Return content-free integer peak and RMS for one aligned PCM16 frame."""
    sample_count = len(value) // 2
    if sample_count == 0:
        return 0, 0
    peak = 0
    energy = 0
    for (sample,) in struct.iter_unpack("<h", value):
        magnitude = abs(sample)
        peak = max(peak, magnitude)
        energy += sample * sample
    return peak, isqrt(energy // sample_count)


def _scale_pcm16(value: bytes, gain: float) -> bytes:
    """Apply one non-amplifying gain to aligned little-endian PCM16."""
    if not 0.0 <= gain <= 1.0:
        raise ValueError("PCM gain is outside its attenuation bound")
    if len(value) % 2:
        raise ValueError("PCM must contain complete samples")
    if gain == 1.0 or not value:
        return value
    scaled = bytearray(len(value))
    for index, (sample,) in enumerate(struct.iter_unpack("<h", value)):
        struct.pack_into("<h", scaled, index * 2, round(sample * gain))
    return bytes(scaled)


def _apply_capture_gain_pcm16(value: bytes, gain_db: float) -> bytes:
    """Apply the configured bridge capture gain once with PCM16 saturation."""
    if len(value) % 2:
        raise ValueError("capture PCM must contain complete samples")
    if not value or gain_db == 0.0:
        return value
    gain = 10 ** (gain_db / 20)
    amplified = bytearray(len(value))
    for index, (sample,) in enumerate(struct.iter_unpack("<h", value)):
        scaled = int(sample * gain)
        if scaled > 32_767:
            scaled = 32_767
        elif scaled < -32_768:
            scaled = -32_768
        struct.pack_into("<h", amplified, index * 2, scaled)
    return bytes(amplified)


def _levels_have_local_barge_in_signal(
    peak: int,
    rms: int,
    *,
    capture_backend: str,
    direct_capture_gain_db: float,
) -> bool:
    """Apply the capture-path-specific peak and sustained-energy boundary."""
    if capture_backend == NATIVE_AEC3_CAPTURE:
        gain = 10 ** (direct_capture_gain_db / 20)
        return (
            peak * gain >= _NATIVE_AEC3_LOCAL_BARGE_IN_POST_GAIN_PEAK
            and rms * gain >= _NATIVE_AEC3_LOCAL_BARGE_IN_POST_GAIN_RMS
        )
    return peak >= _LOCAL_BARGE_IN_SIGNAL_PEAK and rms >= _LOCAL_BARGE_IN_SIGNAL_RMS


def _pcm_has_local_barge_in_signal(
    value: bytes,
    *,
    capture_backend: str = PULSEAUDIO_AEC_CAPTURE,
    direct_capture_gain_db: float = 0.0,
) -> bool:
    """Require both a speech-scale peak and sustained frame energy."""
    peak, rms = _pcm_peak_and_rms(value)
    return _levels_have_local_barge_in_signal(
        peak,
        rms,
        capture_backend=capture_backend,
        direct_capture_gain_db=direct_capture_gain_db,
    )


def _validate_started(value: dict[str, Any]) -> None:
    expected = {
        "type": "started",
        "protocol_version": 2,
        "conversation_mode": NATIVE_CONVERSATION_MODE,
        "audio_transport": "binary",
        "input_sample_rate": 16_000,
        "input_channels": 1,
        "output_sample_rate": 24_000,
        "output_channels": 1,
    }
    if not _is_exact_integer(value.get("protocol_version"), 2) or any(
        value.get(key) != expected_value for key, expected_value in expected.items()
    ):
        raise WebSocketError("bridge returned an incompatible realtime protocol")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, dict):
        raise WebSocketError("bridge omitted realtime capabilities")
    if capabilities.get("binary_pcm16") is not True:
        raise WebSocketError("bridge does not support binary PCM16")
    if capabilities.get("local_flush") is not True:
        raise WebSocketError("bridge does not support local flush")
    if capabilities.get("remote_cancel") is not False:
        raise WebSocketError("bridge returned incompatible cancel semantics")
    if capabilities.get("same_session_interrupt_ack") is not True:
        raise WebSocketError("bridge omitted same-session interrupt acknowledgement")
    if capabilities.get("server_owned_media") is not True:
        raise WebSocketError("bridge does not own the realtime media peer")
    if capabilities.get("native_end_conversation") is not True:
        raise WebSocketError("bridge omitted native end-conversation support")


def _direct_answer_sdp(value: dict[str, Any]) -> str:
    """Validate the distinct protocol-v3 SDP answer without accepting extras."""
    if set(value) != {"type", "protocol_version", "transport"}:
        raise WebSocketError("bridge returned an incompatible WebRTC answer")
    if value.get("type") != "answer" or not _is_exact_integer(
        value.get("protocol_version"), 3
    ):
        raise WebSocketError("bridge returned an incompatible WebRTC answer")
    transport = value.get("transport")
    if not isinstance(transport, dict) or set(transport) != {"type", "sdp"}:
        raise WebSocketError("bridge returned an incompatible WebRTC transport")
    sdp = transport.get("sdp")
    if transport.get("type") != "webrtc" or not isinstance(sdp, str) or not sdp:
        raise WebSocketError("bridge returned an invalid WebRTC SDP answer")
    if "\x00" in sdp or not sdp.startswith("v=0"):
        raise WebSocketError("bridge returned an invalid WebRTC SDP answer")
    return sdp


def _direct_rollover_answer_sdp(value: dict[str, Any], *, epoch: int) -> str:
    """Validate one exact epoch-tagged replacement SDP answer."""
    if set(value) != {"type", "protocol_version", "epoch", "transport"}:
        raise WebSocketError("bridge returned an incompatible rollover answer")
    if (
        value.get("type") != "rollover_answer"
        or not _is_exact_integer(value.get("protocol_version"), 3)
        or not _is_exact_integer(value.get("epoch"), epoch)
    ):
        raise WebSocketError("bridge returned an incompatible rollover answer")
    transport = value.get("transport")
    if not isinstance(transport, dict) or set(transport) != {"type", "sdp"}:
        raise WebSocketError("bridge returned an incompatible rollover transport")
    sdp = transport.get("sdp")
    if transport.get("type") != "webrtc" or not isinstance(sdp, str) or not sdp:
        raise WebSocketError("bridge returned an invalid rollover SDP answer")
    if "\x00" in sdp or not sdp.startswith("v=0"):
        raise WebSocketError("bridge returned an invalid rollover SDP answer")
    return sdp


def _direct_rollover_context_retained(
    value: dict[str, Any],
    *,
    epoch: int,
) -> bool:
    """Validate one exact content-free replacement-start acknowledgement."""
    if set(value) != {
        "type",
        "protocol_version",
        "epoch",
        "context_retained",
    }:
        raise WebSocketError("bridge returned an incompatible rollover start")
    retained = value.get("context_retained")
    if (
        value.get("type") != "rollover_started"
        or not _is_exact_integer(value.get("protocol_version"), 3)
        or not _is_exact_integer(value.get("epoch"), epoch)
        or not isinstance(retained, bool)
    ):
        raise WebSocketError("bridge returned an incompatible rollover start")
    return retained


def _validate_direct_started(value: dict[str, Any]) -> None:
    """Require the content-free signaling-only protocol-v3 acknowledgement."""
    expected = {
        "type": "started",
        "version": "v3",
        "protocol_version": 3,
        "conversation_mode": NATIVE_CONVERSATION_MODE,
        "transport": "webrtc",
        "audio_over_bridge": False,
        "sideband_control": True,
    }
    if not _is_exact_integer(value.get("protocol_version"), 3) or value != expected:
        raise WebSocketError("bridge returned an incompatible direct WebRTC protocol")


def _is_exact_integer(value: object, expected: int) -> bool:
    """Reject JSON booleans and floats even when Python considers them equal."""
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _output_epoch(value: dict[str, Any]) -> int:
    epoch = value.get("output_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise WebSocketError("output_epoch must be a positive integer")
    return epoch


def _check_deadlines(
    now: float,
    *,
    started_at: float,
    last_activity: float,
    max_session_seconds: float,
    idle_timeout_seconds: float,
    pending_ping: bytes | None,
    pong_deadline: float | None,
) -> None:
    if now - started_at >= max_session_seconds:
        raise WebSocketError("realtime session exceeded its hard limit")
    if now - last_activity >= idle_timeout_seconds:
        raise WebSocketError("realtime session exceeded its idle limit")
    if pending_ping is not None and pong_deadline is not None and now >= pong_deadline:
        raise WebSocketError("realtime pong timed out")
