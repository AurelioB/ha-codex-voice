import logging
import threading
import time
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

_ROOT = Path(__file__).parents[2]
_RUNNER_PATH = _ROOT / "deploy" / "systemd" / "wyoming-faster-whisper-runner.py"


def _load_runner() -> ModuleType:
    spec = spec_from_file_location("wyoming_stt_runner_test", _RUNNER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_modules(version: str = "3.5.0") -> tuple[dict[str, object], object]:
    state_lock = threading.Lock()
    state = SimpleNamespace(active=0, maximum=0)

    class FakeTranscriber:
        def transcribe(
            self,
            wav_path: str | Path,
            language: str | None,
            beam_size: int = 5,
            initial_prompt: str | None = None,
        ) -> str:
            del wav_path, language, beam_size, initial_prompt
            with state_lock:
                state.active += 1
                state.maximum = max(state.maximum, state.active)
            time.sleep(0.04)
            with state_lock:
                state.active -= 1
            return "ready"

    return (
        {
            "wyoming_faster_whisper": SimpleNamespace(__version__=version),
            "wyoming_faster_whisper.__main__": SimpleNamespace(run=Mock()),
            "wyoming_faster_whisper.models": SimpleNamespace(
                FasterWhisperTranscriber=FakeTranscriber
            ),
            "wyoming_faster_whisper.faster_whisper_handler": SimpleNamespace(
                FasterWhisperTranscriber=FakeTranscriber
            ),
        },
        state,
    )


def test_runner_suppresses_transcripts_and_serializes_native_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    modules, state = _runtime_modules()
    transcript_logger = logging.getLogger("wyoming_faster_whisper.dispatch_handler")
    previous_level = transcript_logger.level
    monkeypatch.setattr(runner, "import_module", modules.__getitem__)

    try:
        runner.main()
        assert transcript_logger.level == logging.WARNING

        models = modules["wyoming_faster_whisper.models"]
        transcriber_type = models.FasterWhisperTranscriber
        transcriber = transcriber_type()
        threads = [
            threading.Thread(target=transcriber.transcribe, args=("test.wav", "en"))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert state.maximum == 1
        modules["wyoming_faster_whisper.__main__"].run.assert_called_once_with()
    finally:
        transcript_logger.setLevel(previous_level)


def test_runner_fails_closed_for_unreviewed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    modules, _state = _runtime_modules("3.6.0")
    monkeypatch.setattr(runner, "import_module", modules.__getitem__)

    with pytest.raises(RuntimeError, match=r"unsupported.*3\.6\.0"):
        runner.main()

    modules["wyoming_faster_whisper.__main__"].run.assert_not_called()


def test_unit_uses_pinned_private_runner_and_model() -> None:
    unit = (_ROOT / "deploy" / "systemd" / "wyoming-faster-whisper.service").read_text()
    requirements = (
        _ROOT / "deploy" / "systemd" / "wyoming-stt-requirements.lock"
    ).read_text()

    assert "wyoming-stt-runner.py" in unit
    assert "models/faster-whisper-base" in unit
    assert "ProtectHome=tmpfs" in unit
    assert "BindReadOnlyPaths=%h/.local/share/ha-codex-voice" in unit
    assert "ProtectProc=invisible" in unit
    assert "wyoming-faster-whisper==3.5.0" in requirements
