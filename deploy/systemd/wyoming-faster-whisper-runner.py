"""Start pinned Wyoming faster-whisper with privacy and concurrency guards."""

from __future__ import annotations

import logging
import threading
from importlib import import_module
from pathlib import Path

_TRANSCRIPT_LOGGER = "wyoming_faster_whisper.dispatch_handler"
_SUPPORTED_VERSION = "3.5.0"


def main() -> None:
    """Suppress transcript logs, serialize native inference, and run the server."""
    package = import_module("wyoming_faster_whisper")
    server = import_module("wyoming_faster_whisper.__main__")
    models = import_module("wyoming_faster_whisper.models")
    handler = import_module("wyoming_faster_whisper.faster_whisper_handler")

    if package.__version__ != _SUPPORTED_VERSION:
        raise RuntimeError(
            f"unsupported wyoming-faster-whisper version: {package.__version__}"
        )

    inference_lock = threading.Lock()
    base_transcriber = handler.FasterWhisperTranscriber

    class SerializedFasterWhisperTranscriber(base_transcriber):
        """Keep the native inference lock until its worker thread exits."""

        def transcribe(
            self,
            wav_path: str | Path,
            language: str | None,
            beam_size: int = 5,
            initial_prompt: str | None = None,
        ) -> str:
            """Run only one native faster-whisper batch at a time."""
            with inference_lock:
                return super().transcribe(
                    wav_path,
                    language,
                    beam_size=beam_size,
                    initial_prompt=initial_prompt,
                )

    logging.getLogger(_TRANSCRIPT_LOGGER).setLevel(logging.WARNING)
    models.FasterWhisperTranscriber = SerializedFasterWhisperTranscriber
    server.run()


if __name__ == "__main__":
    main()
