"""Optional asynchronous speaker identity adapter for native realtime sessions."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
    display_name: str | None
    score: float
    margin: float
    ha_person_id: str | None = None

    @property
    def context(self) -> str | None:
        """Return bounded advisory developer context only for a confident match."""
        if self.status != "match" or self.speaker_id is None:
            return None
        profile_kind = (
            "Home Assistant person" if self.ha_person_id else "local voice profile"
        )
        return (
            "[local speaker identity] The current speaker confidently matches the "
            f"enrolled {profile_kind} {self.display_name or self.speaker_id}. "
            "Address and personalize for that person when relevant. This is advisory "
            "personalization "
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
            result = await self._broker.process_probe(pcm)
        except SpeakerIdentityUnavailable:
            if not self._result.done():
                self._result.set_result(
                    SpeakerIdentityResult("unknown", None, None, 0.0, 0.0)
                )
        else:
            if not self._result.done():
                self._result.set_result(result)


class SpeakerIdentityBroker:
    """Own the optional reusable HTTP connection to the local identity worker."""

    def __init__(self, url: str | None, *, token: str | None, timeout: float) -> None:
        self._url = url
        if url is None:
            self._base_url = None
        else:
            parsed = urlsplit(url)
            self._base_url = urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path.removesuffix("/identify"),
                    "",
                    "",
                )
            ).rstrip("/")
        self._token = token
        self._timeout = timeout
        self._session: ClientSession | None = None
        self._requests_started = 0
        self._requests_succeeded = 0
        self._requests_failed = 0
        self._matches = 0
        self._unknown = 0
        self._last_duration_ms: int | None = None
        self._active_enrollment_id: str | None = None
        self._armed_test_expected: str | object | None = _NO_TEST
        self._last_test: dict[str, object] | None = None

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
            "enrollment_active": self._active_enrollment_id is not None,
            "test_armed": self._armed_test_expected is not _NO_TEST,
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

    async def process_probe(self, pcm: bytes) -> SpeakerIdentityResult:
        """Route one post-wake probe to enrollment, validation, or recognition."""
        enrollment_id = self._active_enrollment_id
        if enrollment_id is not None:
            await self._request_json(
                "POST",
                f"/enrollments/{enrollment_id}/samples",
                data=pcm,
                content_type="application/octet-stream",
            )
            return SpeakerIdentityResult("unknown", None, None, 0.0, 0.0)
        expected = self._armed_test_expected
        if expected is not _NO_TEST:
            self._armed_test_expected = _NO_TEST
            value = await self._post(pcm, include_disabled=True)
            result = _validated_result(value)
            self._last_test = {
                "expected_speaker_id": expected,
                "observed_speaker_id": result.speaker_id,
                "status": result.status,
                "score": result.score,
                "margin": result.margin,
                "passed": (
                    result.status == "unknown"
                    if expected is None
                    else result.speaker_id == expected
                ),
                "completed_at": int(time.time()),
            }
            return SpeakerIdentityResult("unknown", None, None, 0.0, 0.0)
        return await self.identify(pcm)

    async def management_status(self) -> dict[str, object]:
        value = await self._request_json("GET", "/status")
        if not isinstance(value, dict):
            raise SpeakerIdentityUnavailable("speaker identity status is invalid")
        enrollments = value.get("enrollments")
        if isinstance(enrollments, list) and len(enrollments) == 1:
            enrollment = enrollments[0]
            if isinstance(enrollment, dict) and isinstance(
                enrollment.get("speaker_id"), str
            ):
                self._active_enrollment_id = enrollment["speaker_id"]
        elif enrollments == []:
            self._active_enrollment_id = None
        value["last_test"] = self._last_test
        value["test_armed"] = self._armed_test_expected is not _NO_TEST
        return value

    async def start_enrollment(self, value: dict[str, object]) -> dict[str, object]:
        result = await self._request_json("POST", "/enrollments", json_value=value)
        if not isinstance(result, dict) or not isinstance(
            result.get("speaker_id"), str
        ):
            raise SpeakerIdentityUnavailable("speaker enrollment response is invalid")
        self._active_enrollment_id = result["speaker_id"]
        return result

    async def complete_enrollment(self, speaker_id: str) -> dict[str, object]:
        result = await self._request_json(
            "POST", f"/enrollments/{speaker_id}/complete", json_value={}
        )
        self._active_enrollment_id = None
        if not isinstance(result, dict):
            raise SpeakerIdentityUnavailable("speaker profile response is invalid")
        return result

    async def cancel_enrollment(self, speaker_id: str) -> None:
        await self._request_json("DELETE", f"/enrollments/{speaker_id}")
        self._active_enrollment_id = None

    async def update_profile(
        self, speaker_id: str, value: dict[str, object]
    ) -> dict[str, object]:
        result = await self._request_json(
            "PATCH", f"/profiles/{speaker_id}", json_value=value
        )
        if not isinstance(result, dict):
            raise SpeakerIdentityUnavailable("speaker profile response is invalid")
        return result

    async def delete_profile(self, speaker_id: str) -> None:
        await self._request_json("DELETE", f"/profiles/{speaker_id}")

    async def update_settings(self, value: dict[str, object]) -> dict[str, object]:
        result = await self._request_json("PATCH", "/settings", json_value=value)
        if not isinstance(result, dict):
            raise SpeakerIdentityUnavailable("speaker settings response is invalid")
        return result

    def arm_test(self, expected_speaker_id: str | None) -> None:
        if self._armed_test_expected is not _NO_TEST:
            raise ProtocolError("a speaker identity test is already armed")
        self._armed_test_expected = expected_speaker_id

    async def _post(self, pcm: bytes, *, include_disabled: bool = False) -> object:
        suffix = "?include_disabled=true" if include_disabled else ""
        return await self._request_json(
            "POST",
            f"/identify{suffix}",
            data=pcm,
            content_type="application/octet-stream",
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        json_value: object | None = None,
        content_type: str | None = None,
    ) -> object:
        if self._base_url is None or self._token is None:
            raise SpeakerIdentityUnavailable("speaker identity is disabled")
        session = self._session
        if session is None or session.closed:
            session = ClientSession()
            self._session = session
        headers = {"Authorization": f"Bearer {self._token}"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            async with session.request(
                method,
                f"{self._base_url}{path}",
                data=data,
                json=json_value,
                headers=headers,
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
                body = b"".join(chunks)
                if response.status < 200 or response.status >= 300:
                    if response.status == 400:
                        try:
                            error_value = json.loads(body)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            error_value = None
                        if (
                            isinstance(error_value, dict)
                            and isinstance(error_value.get("error"), str)
                            and error_value["error"]
                        ):
                            raise ProtocolError(error_value["error"])
                    raise SpeakerIdentityUnavailable(
                        f"speaker identity returned HTTP status {response.status}"
                    )
        except (TimeoutError, ClientError) as exc:
            raise SpeakerIdentityUnavailable(
                "speaker identity request failed or timed out"
            ) from exc
        try:
            return json.loads(body)
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
    raw_display_name = value.get("display_name")
    raw_ha_person_id = value.get("ha_person_id")
    if status == "match":
        if (
            not isinstance(raw_speaker_id, str)
            or not raw_speaker_id
            or len(raw_speaker_id) > MAX_SPEAKER_ID_CHARS
            or any(not character.isprintable() for character in raw_speaker_id)
        ):
            raise SpeakerIdentityUnavailable("speaker identity match has no valid ID")
        speaker_id = raw_speaker_id
        if raw_display_name is None:
            display_name = speaker_id
        elif (
            not isinstance(raw_display_name, str)
            or not raw_display_name
            or len(raw_display_name) > 128
            or any(not character.isprintable() for character in raw_display_name)
        ):
            raise SpeakerIdentityUnavailable(
                "speaker identity match has no valid display name"
            )
        else:
            display_name = raw_display_name
        if raw_ha_person_id is None:
            ha_person_id = None
        elif (
            not isinstance(raw_ha_person_id, str)
            or not raw_ha_person_id
            or len(raw_ha_person_id) > 256
            or any(not character.isprintable() for character in raw_ha_person_id)
        ):
            raise SpeakerIdentityUnavailable(
                "speaker identity match has no valid Home Assistant person ID"
            )
        else:
            ha_person_id = raw_ha_person_id
    else:
        speaker_id = None
        display_name = None
        ha_person_id = None
    return SpeakerIdentityResult(
        status=status,
        speaker_id=speaker_id,
        display_name=display_name,
        score=float(score),
        margin=float(margin),
        ha_person_id=ha_person_id,
    )


_NO_TEST = object()
