"""Non-blocking upload of explicitly enabled wake-word captures."""

from __future__ import annotations

import queue
import threading
import urllib.error
import urllib.request
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .config import RealtimeConfig

MIN_WAKE_SAMPLE_BYTES = 16_000
MAX_WAKE_SAMPLE_BYTES = 128_000
_MAX_PENDING_UPLOADS = 4
_UPLOAD_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class _WakeSample:
    chunks: tuple[bytes, ...]
    size: int


class WakeSampleUploader:
    """Own one lazy bounded worker; recorder callbacks only enqueue references."""

    def __init__(
        self,
        config: RealtimeConfig,
        *,
        opener: object = urllib.request.urlopen,
    ) -> None:
        """Create a dormant uploader for one immutable device configuration."""
        self._url = _sample_url(config.url)
        self._token = config.token
        self._phrase = config.wake_phrase
        self._opener = opener
        self._queue: queue.Queue[_WakeSample | None] = queue.Queue(
            maxsize=_MAX_PENDING_UPLOADS
        )
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self.accepted = 0
        self.dropped = 0
        self.succeeded = 0
        self.failed = 0

    def submit(self, chunks: Sequence[bytes]) -> bool:
        """Queue immutable chunks without joining or performing network I/O."""
        retained = tuple(chunk for chunk in chunks if chunk)
        size = sum(map(len, retained))
        if (
            self._closed
            or size < MIN_WAKE_SAMPLE_BYTES
            or size > MAX_WAKE_SAMPLE_BYTES
            or size % 2
        ):
            self.dropped += 1
            return False
        self._ensure_worker()
        try:
            self._queue.put_nowait(_WakeSample(retained, size))
        except queue.Full:
            self.dropped += 1
            return False
        self.accepted += 1
        return True

    def close(self) -> None:
        """Request worker shutdown without blocking process termination."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with suppress(queue.Full):
                self._queue.put_nowait(None)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._closed or self._thread is not None:
                return
            worker = threading.Thread(
                target=self._run,
                name="thirdreality-wake-sample-upload",
                daemon=True,
            )
            self._thread = worker
            worker.start()

    def _run(self) -> None:
        while True:
            sample = self._queue.get()
            if sample is None:
                return
            try:
                request = urllib.request.Request(
                    self._url,
                    data=b"".join(sample.chunks),
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "audio/L16; rate=16000; channels=1",
                        "X-Voice-Wake-Phrase": self._phrase,
                    },
                    method="POST",
                )
                response = self._opener(request, timeout=_UPLOAD_TIMEOUT_SECONDS)
                with response:
                    if not 200 <= response.status < 300:
                        raise urllib.error.HTTPError(
                            self._url,
                            response.status,
                            "wake sample upload failed",
                            response.headers,
                            None,
                        )
                    response.read(1_024)
            except (OSError, urllib.error.URLError, ValueError):
                self.failed += 1
            else:
                self.succeeded += 1


def _sample_url(realtime_url: str) -> str:
    parsed = urlsplit(realtime_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit((scheme, parsed.netloc, "/v1/voice-lab/wake-sample", "", ""))
