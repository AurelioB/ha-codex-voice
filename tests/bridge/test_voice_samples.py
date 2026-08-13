from __future__ import annotations

import json
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from bridge.errors import ProtocolError
from bridge.voice_samples import VoiceSampleInbox, VoiceSampleUnavailable
from device.thirdreality.realtime_client.config import RealtimeConfig
from device.thirdreality.realtime_client.voice_samples import WakeSampleUploader


def test_opt_in_wake_inbox_stores_private_audio_and_labels_explicit_false_wake(
    tmp_path: Path,
) -> None:
    root = tmp_path / "voice-samples"
    inbox = VoiceSampleInbox(str(root))
    pcm = (1_000).to_bytes(2, "little", signed=True) * 16_000

    inbox.store_wake(pcm, phrase="Okay Nabu")

    metadata_path = next((root / "inbox").glob("wake-*.json"))
    wav_path = next((root / "inbox").glob("wake-*.wav"))
    record = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert record["kind"] == "unreviewed-wake"
    assert record["detector_outcome"] == "hit"
    assert record["phrase"] == "Okay Nabu"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(wav_path.stat().st_mode) == 0o600

    assert inbox.mark_latest_false_wake() == {
        "status": "marked",
        "do_not_retry": True,
    }
    record = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert record["kind"] == "wake-negative"
    assert record["detector_outcome"] == "false-activation"
    assert inbox.health() == {
        "enabled": True,
        "samples_stored": 1,
        "false_wakes_labeled": 1,
        "failures": 0,
    }


def test_disabled_or_malformed_wake_collection_fails_closed(tmp_path: Path) -> None:
    disabled = VoiceSampleInbox(None)
    assert disabled.tools == ()
    assert disabled.owns("mark_false_wake") is False
    with pytest.raises(VoiceSampleUnavailable, match="disabled"):
        disabled.store_wake(b"\0" * 32_000, phrase="okay nabu")

    enabled = VoiceSampleInbox(str(tmp_path / "enabled"))
    with pytest.raises(ProtocolError, match=r"0\.5 to 4 seconds"):
        enabled.store_wake(b"\0\0", phrase="okay nabu")
    with pytest.raises(VoiceSampleUnavailable, match="no recent"):
        enabled.mark_latest_false_wake()


def test_device_uploader_joins_pcm_only_on_its_bounded_worker() -> None:
    delivered = threading.Event()
    observed: dict[str, object] = {}

    class Response:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _maximum: int) -> bytes:
            delivered.set()
            return b"{}"

    def open_request(request: object, *, timeout: float) -> Response:
        observed["request"] = request
        observed["timeout"] = timeout
        return Response()

    config = RealtimeConfig(
        url="ws://192.0.2.10:8787/v1/realtime",
        connect_address="192.0.2.10",
        token="device-secret",
        wake_phrase="okay computer",
        connect_timeout_seconds=1,
        handshake_timeout_seconds=5,
        io_timeout_seconds=1,
        idle_timeout_seconds=30,
        max_session_seconds=60,
        ping_interval_seconds=15,
        pong_timeout_seconds=5,
        input_queue_bytes=64 * 1024,
        fallback_buffer_bytes=64 * 1024,
        output_queue_bytes=48 * 1024,
        max_message_bytes=2_048,
        full_duplex=False,
    )
    uploader = WakeSampleUploader(config, opener=open_request)
    chunks = [bytes((index, 0)) * 1_024 for index in range(16)]

    assert uploader.submit(chunks) is True
    assert delivered.wait(1)
    request: Any = observed["request"]
    assert request.full_url == ("http://192.0.2.10:8787/v1/voice-lab/wake-sample")
    assert request.data == b"".join(chunks)
    assert request.get_header("Authorization") == "Bearer device-secret"
    assert request.get_header("X-voice-wake-phrase") == "okay computer"
    assert uploader.accepted == 1
    assert uploader.succeeded == 1
    assert uploader.failed == 0
    uploader.close()
