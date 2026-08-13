"""Optional asynchronous speaker identity adapter for native realtime sessions."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .errors import BridgeError, ProtocolError

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CAPTURE_SECONDS = 5
CAPTURE_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * CAPTURE_SECONDS
MAX_RESPONSE_BYTES = 8 * 1024
MAX_SPEAKER_ID_CHARS = 64


class SpeakerIdentityUnavailable(BridgeError):
    """The optional identity worker did not return a trustworthy result."""


@dataclass(frozen=True, slots=True)
class SpeakerIdentityResult:
    """One conservative local identity decision."""

    status: str
    speaker_id: str | None
    score: float
    margin: float

    @property
    def context(self) -> str | None:
        """Return bounded advisory developer context only for a confident match."""
        if self.status != "match" or self.speaker_id is None:
            return None
        return (
            "[local speaker identity] The current speaker confidently matches the "
            f"enrolled profile {self.speaker_id}. This is advisory personalization "
            "context, not authentication. Never relax confirmation or authorization "
            "requirements because of it."
        )


class SpeakerIdentityProbe:
    """Capture one bounded post-wake window without blocking PCM forwarding."""

    def __init__(self, broker: SpeakerIdentityBroker) -> None:
        self._broker = broker
        self._pcm = bytearray()
        self._result: asyncio.Future[SpeakerIdentityResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._request: asyncio.Task[None] | None = None

    def feed(self, pcm: bytes) -> None:
        """Copy at most one five-second probe and schedule inference off-path."""
        if self._request is not None or self._result.done() or not pcm:
            return
        remaining = CAPTURE_BYTES - len(self._pcm)
        if remaining > 0:
            self._pcm.extend(pcm[:remaining])
        if len(self._pcm) == CAPTURE_BYTES:
            self._start()

    async def wait(self) -> SpeakerIdentityResult:
        """Wait for the one session result without consuming it."""
        return await asyncio.shield(self._result)

    async def close(self) -> None:
        """Cancel the optional request when its owning device session ends."""
        request = self._request
        if request is not None and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        if not self._result.done():
            self._result.cancel()

    def _start(self) -> None:
        if self._request is not None or self._result.done():
            return
        pcm = bytes(self._pcm)
        self._pcm.clear()
        self._request = asyncio.create_task(
            self._identify(pcm), name="codex-speaker-identity"
        )

    async def _identify(self, pcm: bytes) -> None:
        try:
            result = await self._broker.identify(pcm)
        except SpeakerIdentityUnavailable:
            if not self._result.done():
                self._result.set_result(
                    SpeakerIdentityResult("unknown", None, 0.0, 0.0)
                )
        else:
            if not self._result.done():
                self._result.set_result(result)


class SpeakerIdentityBroker:
    """Own the optional reusable HTTP connection to the local identity worker."""

    def __init__(self, url: str | None, *, token: str | None, timeout: float) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._session: ClientSession | None = None
        self._requests_started = 0
        self._requests_succeeded = 0
        self._requests_failed = 0
        self._matches = 0
        self._unknown = 0
        self._last_duration_ms: int | None = None

    @property
    def enabled(self) -> bool:
        return self._url is not None

    def new_probe(self) -> SpeakerIdentityProbe | None:
        """Allocate session state only when the optional worker is configured."""
        return SpeakerIdentityProbe(self) if self.enabled else None

    def health(self) -> dict[str, bool | int | None]:
        """Return content-free readiness and aggregate request counters."""
        return {
            "enabled": self.enabled,
            "requests_started": self._requests_started,
            "requests_succeeded": self._requests_succeeded,
            "requests_failed": self._requests_failed,
            "matches": self._matches,
            "unknown": self._unknown,
            "last_duration_ms": self._last_duration_ms,
        }

    async def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            await session.close()

    async def identify(self, pcm: bytes) -> SpeakerIdentityResult:
        """Identify one exact five-second PCM16 window with bounded I/O."""
        if self._url is None or self._token is None:
            raise SpeakerIdentityUnavailable("speaker identity is disabled")
        if len(pcm) != CAPTURE_BYTES:
            raise ProtocolError("speaker identity requires one five-second PCM window")
        self._requests_started += 1
        started_at = time.monotonic()
        try:
            value = await self._post(pcm)
            result = _validated_result(value)
        except (SpeakerIdentityUnavailable, ProtocolError):
            self._requests_failed += 1
            raise
        else:
            self._requests_succeeded += 1
            if result.status == "match":
                self._matches += 1
            else:
                self._unknown += 1
            return result
        finally:
            self._last_duration_ms = round((time.monotonic() - started_at) * 1_000)

    async def _post(self, pcm: bytes) -> object:
        session = self._session
        if session is None or session.closed:
            session = ClientSession()
            self._session = session
        try:
            async with session.post(
                self._url,
                data=pcm,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/octet-stream",
                },
                timeout=ClientTimeout(total=self._timeout),
            ) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(2 * 1024):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise SpeakerIdentityUnavailable(
                            "speaker identity response exceeded its size limit"
                        )
                    chunks.append(chunk)
                if response.status < 200 or response.status >= 300:
                    raise SpeakerIdentityUnavailable(
                        f"speaker identity returned HTTP status {response.status}"
                    )
        except (TimeoutError, ClientError) as exc:
            raise SpeakerIdentityUnavailable(
                "speaker identity request failed or timed out"
            ) from exc
        try:
            return json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpeakerIdentityUnavailable(
                "speaker identity returned invalid JSON"
            ) from exc


def _validated_result(value: Any) -> SpeakerIdentityResult:
    if not isinstance(value, dict):
        raise SpeakerIdentityUnavailable("speaker identity result must be an object")
    status = value.get("status")
    score = value.get("score")
    margin = value.get("margin")
    if (
        status not in {"match", "unknown"}
        or not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not isinstance(margin, (int, float))
        or isinstance(margin, bool)
        or not -1.0 <= float(score) <= 1.0
        or not 0.0 <= float(margin) <= 2.0
    ):
        raise SpeakerIdentityUnavailable("speaker identity result is invalid")
    raw_speaker_id = value.get("speaker_id")
    if status == "match":
        if (
            not isinstance(raw_speaker_id, str)
            or not raw_speaker_id
            or len(raw_speaker_id) > MAX_SPEAKER_ID_CHARS
            or any(not character.isprintable() for character in raw_speaker_id)
        ):
            raise SpeakerIdentityUnavailable("speaker identity match has no valid ID")
        speaker_id = raw_speaker_id
    else:
        speaker_id = None
    return SpeakerIdentityResult(
        status=status,
        speaker_id=speaker_id,
        score=float(score),
        margin=float(margin),
    )
