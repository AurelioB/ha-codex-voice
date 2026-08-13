from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.speaker_identity import (
    CAPTURE_BYTES,
    SpeakerIdentityBroker,
    SpeakerIdentityUnavailable,
)


@pytest.mark.asyncio
async def test_disabled_broker_allocates_no_probe_or_client() -> None:
    broker = SpeakerIdentityBroker(None, token=None, timeout=4.0)

    assert broker.new_probe() is None
    assert broker.health() == {
        "enabled": False,
        "requests_started": 0,
        "requests_succeeded": 0,
        "requests_failed": 0,
        "matches": 0,
        "unknown": 0,
        "last_duration_ms": None,
    }
    await broker.close()


@pytest.mark.asyncio
async def test_probe_submits_one_bounded_window_and_returns_advisory_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = SpeakerIdentityBroker(
        "http://127.0.0.1:8790/identify",
        token="speaker-specific-token-123456",
        timeout=4.0,
    )
    observed: list[bytes] = []

    async def fake_post(pcm: bytes) -> Any:
        observed.append(pcm)
        return {
            "status": "match",
            "speaker_id": "owner",
            "score": 0.81,
            "margin": 0.29,
        }

    monkeypatch.setattr(broker, "_post", fake_post)
    probe = broker.new_probe()
    assert probe is not None

    probe.feed(b"\x01\x00" * (CAPTURE_BYTES // 4))
    await asyncio.sleep(0)
    assert observed == []
    probe.feed(b"\x02\x00" * CAPTURE_BYTES)
    result = await asyncio.wait_for(probe.wait(), timeout=1)

    assert len(observed) == 1
    assert len(observed[0]) == CAPTURE_BYTES
    assert result.status == "match"
    assert result.speaker_id == "owner"
    assert result.context is not None
    assert "not authentication" in result.context
    assert broker.health()["matches"] == 1
    await broker.close()


@pytest.mark.asyncio
async def test_worker_failure_becomes_unknown_without_failing_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = SpeakerIdentityBroker(
        "http://127.0.0.1:8790/identify",
        token="speaker-specific-token-123456",
        timeout=4.0,
    )

    async def failed_post(_pcm: bytes) -> Any:
        raise SpeakerIdentityUnavailable("offline")

    monkeypatch.setattr(broker, "_post", failed_post)
    probe = broker.new_probe()
    assert probe is not None
    probe.feed(b"\x01\x00" * (CAPTURE_BYTES // 2))

    result = await asyncio.wait_for(probe.wait(), timeout=1)

    assert result.status == "unknown"
    assert result.context is None
    assert broker.health()["requests_failed"] == 1
    await broker.close()
