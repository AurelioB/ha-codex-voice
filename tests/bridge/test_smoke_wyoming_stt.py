import asyncio
import wave
from pathlib import Path

import pytest
from wyoming.asr import Transcript
from wyoming.event import Event

from scripts import smoke_wyoming_stt


class FakeClient:
    def __init__(
        self,
        responses: list[Event] | None = None,
        *,
        block_connect: bool = False,
    ) -> None:
        self.responses = iter(responses or [])
        self.block_connect = block_connect
        self.written: list[Event] = []

    async def __aenter__(self) -> "FakeClient":
        if self.block_connect:
            await asyncio.Event().wait()
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def write_event(self, event: Event) -> None:
        self.written.append(event)

    async def read_event(self) -> Event | None:
        return next(self.responses, None)


@pytest.fixture
def pcm_wav(tmp_path: Path) -> Path:
    path = tmp_path / "speech.wav"
    with wave.open(str(path), "wb") as target:
        target.setframerate(16_000)
        target.setsampwidth(2)
        target.setnchannels(1)
        target.writeframes(b"\x01\x00" * 3_200)
    return path


async def test_transcribe_sends_finite_wyoming_stream(
    monkeypatch: pytest.MonkeyPatch,
    pcm_wav: Path,
) -> None:
    client = FakeClient([Transcript(text="ready").event()])
    monkeypatch.setattr(
        smoke_wyoming_stt.AsyncClient,
        "from_uri",
        MockFromUri(client),
    )

    transcript, duration = await smoke_wyoming_stt.transcribe(
        pcm_wav,
        uri="tcp://stt.local:10300",
        language="en",
        timeout=1,
    )

    assert transcript == "ready"
    assert duration >= 0
    assert [event.type for event in client.written] == [
        "transcribe",
        "audio-start",
        "audio-chunk",
        "audio-chunk",
        "audio-stop",
    ]


async def test_timeout_covers_connection_setup(
    monkeypatch: pytest.MonkeyPatch,
    pcm_wav: Path,
) -> None:
    monkeypatch.setattr(
        smoke_wyoming_stt.AsyncClient,
        "from_uri",
        MockFromUri(FakeClient(block_connect=True)),
    )

    with pytest.raises(TimeoutError):
        await smoke_wyoming_stt.transcribe(
            pcm_wav,
            uri="tcp://unreachable:10300",
            language="en",
            timeout=0.01,
        )


async def test_server_error_is_bounded_failure(
    monkeypatch: pytest.MonkeyPatch,
    pcm_wav: Path,
) -> None:
    client = FakeClient([Event(type="error", data={"text": "failed"})])
    monkeypatch.setattr(
        smoke_wyoming_stt.AsyncClient,
        "from_uri",
        MockFromUri(client),
    )

    with pytest.raises(RuntimeError, match="failed"):
        await smoke_wyoming_stt.transcribe(
            pcm_wav,
            uri="tcp://stt.local:10300",
            language="en",
            timeout=1,
        )


def test_output_hides_transcript_unless_explicitly_requested() -> None:
    private = smoke_wyoming_stt._result(
        "private words",
        0.4567,
        show_transcript=False,
    )
    explicit = smoke_wyoming_stt._result(
        "known phrase",
        0.4567,
        show_transcript=True,
    )

    assert private == {"duration_seconds": 0.457, "transcript_received": True}
    assert explicit["transcript"] == "known phrase"


class MockFromUri:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def __call__(self, _uri: str) -> FakeClient:
        return self.client
