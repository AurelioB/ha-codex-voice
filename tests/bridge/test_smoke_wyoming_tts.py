import asyncio

import pytest
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.tts import Synthesize

from scripts import smoke_wyoming_tts


class FakeClient:
    def __init__(
        self,
        responses: list[Event] | None = None,
        *,
        block_connect: bool = False,
        refuse_connect: bool = False,
    ) -> None:
        self.responses = iter(responses or [])
        self.block_connect = block_connect
        self.refuse_connect = refuse_connect
        self.written: list[Event] = []

    async def __aenter__(self) -> "FakeClient":
        if self.refuse_connect:
            raise ConnectionRefusedError
        if self.block_connect:
            await asyncio.Event().wait()
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def write_event(self, event: Event) -> None:
        self.written.append(event)

    async def read_event(self) -> Event | None:
        return next(self.responses, None)


async def test_synthesize_reports_timing_and_audio_without_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(
        [
            AudioStart(rate=22_050, width=2, channels=1).event(),
            AudioChunk(
                rate=22_050,
                width=2,
                channels=1,
                audio=b"",
            ).event(),
            AudioChunk(
                rate=22_050,
                width=2,
                channels=1,
                audio=b"\x00" * 22_050,
            ).event(),
            AudioChunk(
                rate=22_050,
                width=2,
                channels=1,
                audio=b"\x00" * 22_050,
            ).event(),
            AudioStop().event(),
        ]
    )
    monkeypatch.setattr(
        smoke_wyoming_tts.AsyncClient,
        "from_uri",
        MockFromUri(client),
    )

    result = await smoke_wyoming_tts.synthesize(
        uri="tcp://tts.local:10200",
        timeout=1,
    )

    assert len(client.written) == 1
    request = Synthesize.from_event(client.written[0])
    assert request.text == smoke_wyoming_tts._KNOWN_TEXT
    assert result["duration_seconds"] >= 0
    assert result["time_to_first_audio_seconds"] >= 0
    assert result["audio"] == {
        "rate_hz": 22_050,
        "width_bytes": 2,
        "channels": 1,
        "chunk_count": 2,
        "bytes": 44_100,
        "duration_seconds": 1.0,
    }
    assert "text" not in repr(result).lower()
    assert smoke_wyoming_tts._KNOWN_TEXT not in repr(result)


async def test_timeout_covers_connection_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smoke_wyoming_tts.AsyncClient,
        "from_uri",
        MockFromUri(FakeClient(block_connect=True)),
    )

    with pytest.raises(TimeoutError):
        await smoke_wyoming_tts.synthesize(
            uri="tcp://unreachable:10200",
            timeout=0.01,
        )


async def test_startup_retry_recovers_from_refused_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_client = FakeClient(
        [
            AudioStart(rate=22_050, width=2, channels=1).event(),
            AudioChunk(
                rate=22_050,
                width=2,
                channels=1,
                audio=b"\x00\x00",
            ).event(),
            AudioStop().event(),
        ]
    )
    clients = iter([FakeClient(refuse_connect=True), ready_client])
    monkeypatch.setattr(
        smoke_wyoming_tts.AsyncClient,
        "from_uri",
        lambda _uri: next(clients),
    )

    result = await smoke_wyoming_tts.synthesize_with_startup_retry(
        uri="tcp://tts.local:10200",
        timeout=1,
        retry_seconds=0,
    )

    assert result["audio"]["bytes"] == 2


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        ([Event(type="error", data={"text": "private input"})], "returned an error"),
        (
            [
                AudioChunk(
                    rate=22_050,
                    width=2,
                    channels=1,
                    audio=b"\x00\x00",
                ).event()
            ],
            "audio before start",
        ),
        (
            [
                AudioStart(rate=22_050, width=2, channels=1).event(),
                AudioStop().event(),
            ],
            "returned no audio",
        ),
        (
            [
                AudioStart(rate=22_050, width=2, channels=1).event(),
                AudioChunk(
                    rate=22_050,
                    width=2,
                    channels=1,
                    audio=b"",
                ).event(),
                AudioStop().event(),
            ],
            "returned no audio",
        ),
        (
            [
                AudioStart(rate=22_050, width=2, channels=1).event(),
                AudioChunk(
                    rate=16_000,
                    width=2,
                    channels=1,
                    audio=b"\x00\x00",
                ).event(),
            ],
            "changed audio format",
        ),
    ],
)
async def test_malformed_or_error_response_is_text_free_bounded_failure(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Event],
    message: str,
) -> None:
    monkeypatch.setattr(
        smoke_wyoming_tts.AsyncClient,
        "from_uri",
        MockFromUri(FakeClient(responses)),
    )

    with pytest.raises(RuntimeError, match=message) as raised:
        await smoke_wyoming_tts.synthesize(
            uri="tcp://tts.local:10200",
            timeout=1,
        )

    assert "private input" not in str(raised.value)


class MockFromUri:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def __call__(self, _uri: str) -> FakeClient:
        return self.client
