import hashlib
import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

_ROOT = Path(__file__).parents[2]
_SYSTEMD = _ROOT / "deploy" / "systemd"
_RUNNER_PATH = _SYSTEMD / "wyoming-piper-runner.py"
_MODEL_LOCK_PATH = _SYSTEMD / "wyoming-piper-model.lock.json"
_VOICE = "es_MX-ald-medium"
_REVISION = "0622afc867cf0388684853ecdf59a498b489949d"


def _load_runner() -> ModuleType:
    spec = spec_from_file_location("wyoming_piper_runner_test", _RUNNER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_modules(
    version: str = "2.3.1",
) -> tuple[dict[str, object], type[object]]:
    class FakeSessionOptions:
        pass

    modules = {
        "wyoming_piper": SimpleNamespace(__version__=version),
        "onnxruntime": SimpleNamespace(SessionOptions=FakeSessionOptions),
        "wyoming_piper.download": SimpleNamespace(
            get_voices=Mock(), ensure_voice_exists=Mock(), find_voice=Mock()
        ),
        "wyoming_piper.__main__": SimpleNamespace(run=Mock()),
    }
    return modules, FakeSessionOptions


def _locked_model(runner: ModuleType, model_dir: Path) -> object:
    return runner._LockedModel(
        voice=_VOICE,
        revision=_REVISION,
        model_dir=model_dir,
        model_path=model_dir / f"{_VOICE}.onnx",
        config_path=model_dir / f"{_VOICE}.onnx.json",
        catalog={_VOICE: {"key": _VOICE}},
    )


def _mock_verified_model(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    model_dir: Path,
) -> object:
    locked_model = _locked_model(runner, model_dir)
    monkeypatch.setenv("HA_CODEX_TTS_VOICE", _VOICE)
    monkeypatch.setattr(
        runner, "_load_and_verify_locked_model", Mock(return_value=locked_model)
    )
    return locked_model


def test_runner_configures_public_onnx_session_options_before_invocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    modules, real_session_options = _runtime_modules()
    observed_options = []

    def run() -> None:
        observed_options.append(modules["onnxruntime"].SessionOptions())
        assert set(modules["wyoming_piper.download"].get_voices(tmp_path)) == {_VOICE}

    modules["wyoming_piper.__main__"].run.side_effect = run
    _mock_verified_model(runner, monkeypatch, tmp_path)
    monkeypatch.setenv("HA_CODEX_TTS_THREADS", "6")
    monkeypatch.setattr(runner, "import_module", modules.__getitem__)

    runner.main()

    assert len(observed_options) == 1
    options = observed_options[0]
    assert isinstance(options, real_session_options)
    assert options.intra_op_num_threads == 6
    assert options.inter_op_num_threads == 1
    modules["wyoming_piper.__main__"].run.assert_called_once_with()


def test_runner_defaults_to_four_inference_threads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    modules, _real_session_options = _runtime_modules()
    _mock_verified_model(runner, monkeypatch, tmp_path)
    monkeypatch.delenv("HA_CODEX_TTS_THREADS", raising=False)
    monkeypatch.setattr(runner, "import_module", modules.__getitem__)

    runner.main()

    options = modules["onnxruntime"].SessionOptions()
    assert options.intra_op_num_threads == 4
    assert options.inter_op_num_threads == 1


@pytest.mark.parametrize("thread_count", ["", "zero", "0", "-1", "65"])
def test_runner_rejects_invalid_thread_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    thread_count: str,
) -> None:
    runner = _load_runner()
    modules, real_session_options = _runtime_modules()
    _mock_verified_model(runner, monkeypatch, tmp_path)
    monkeypatch.setenv("HA_CODEX_TTS_THREADS", thread_count)
    monkeypatch.setattr(runner, "import_module", modules.__getitem__)

    with pytest.raises(ValueError, match="HA_CODEX_TTS_THREADS"):
        runner.main()

    assert modules["onnxruntime"].SessionOptions is real_session_options
    modules["wyoming_piper.__main__"].run.assert_not_called()


def test_runner_fails_closed_for_unreviewed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    modules, real_session_options = _runtime_modules("2.3.2")
    verify_model = Mock()
    monkeypatch.setattr(runner, "_load_and_verify_locked_model", verify_model)
    monkeypatch.setattr(runner, "import_module", modules.__getitem__)

    with pytest.raises(RuntimeError, match=r"unsupported.*2\.3\.2"):
        runner.main()

    assert modules["onnxruntime"].SessionOptions is real_session_options
    verify_model.assert_not_called()
    modules["wyoming_piper.__main__"].run.assert_not_called()


def test_runner_requires_configured_voice_to_match_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    modules, _real_session_options = _runtime_modules()
    locked_model = _locked_model(runner, tmp_path)
    monkeypatch.setattr(
        runner, "_load_and_verify_locked_model", Mock(return_value=locked_model)
    )
    monkeypatch.setenv("HA_CODEX_TTS_VOICE", "en_US-lessac-medium")
    monkeypatch.setattr(runner, "import_module", modules.__getitem__)

    with pytest.raises(RuntimeError, match="locked voice"):
        runner.main()

    modules["wyoming_piper.__main__"].run.assert_not_called()


def test_runner_restricts_catalog_lookup_and_downloads_to_locked_voice(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    locked_model = _locked_model(runner, tmp_path)
    original_get_voices = Mock()
    original_ensure_voice_exists = Mock()
    download = SimpleNamespace(
        get_voices=original_get_voices,
        ensure_voice_exists=original_ensure_voice_exists,
        find_voice=Mock(),
    )

    runner._restrict_downloads(download, locked_model)

    assert download.get_voices(tmp_path) == locked_model.catalog
    assert download.find_voice(_VOICE, [tmp_path]) == (
        locked_model.model_path,
        locked_model.config_path,
    )
    download.ensure_voice_exists(_VOICE, [tmp_path], tmp_path, locked_model.catalog)
    with pytest.raises(RuntimeError, match="unsupported Piper voice"):
        download.ensure_voice_exists(
            "en_US-lessac-medium",
            [tmp_path],
            tmp_path,
            locked_model.catalog,
        )
    with pytest.raises(RuntimeError, match="updates are disabled"):
        download.get_voices(tmp_path, update_voices=True)
    with pytest.raises(RuntimeError, match="download directory"):
        download.get_voices(tmp_path / "other")
    original_get_voices.assert_not_called()
    original_ensure_voice_exists.assert_not_called()


def test_model_lock_contains_only_exact_revision_voice_files() -> None:
    lock = json.loads(_MODEL_LOCK_PATH.read_text())
    runner = _load_runner()

    assert runner._load_lock(_MODEL_LOCK_PATH) == lock
    assert lock["schema_version"] == 1
    assert lock["voice"] == _VOICE
    assert lock["revision"] == _REVISION
    assert lock["files"] == [
        {
            "filename": f"{_VOICE}.onnx",
            "url": (
                "https://huggingface.co/rhasspy/piper-voices/resolve/"
                f"{_REVISION}/es/es_MX/ald/medium/{_VOICE}.onnx"
            ),
            "size_bytes": 63201294,
            "sha256": (
                "019b3803293c93e34a206dd2e53a3889209a514e786fd7144f7b70196c579b63"
            ),
        },
        {
            "filename": f"{_VOICE}.onnx.json",
            "url": (
                "https://huggingface.co/rhasspy/piper-voices/resolve/"
                f"{_REVISION}/es/es_MX/ald/medium/{_VOICE}.onnx.json"
            ),
            "size_bytes": 4878,
            "sha256": (
                "5a71498158e04afc8099bfd019c7e87c68eb9d042505a2b1a87e5c1ac2b1a61d"
            ),
        },
    ]


def test_runner_rejects_changed_lock_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    changed_lock = tmp_path / "model.lock.json"
    changed_lock.write_bytes(_MODEL_LOCK_PATH.read_bytes() + b"\n")

    with pytest.raises(RuntimeError, match="integrity verification"):
        runner._load_lock(changed_lock)

    monkeypatch.setattr(
        runner, "_SUPPORTED_LOCK_SHA256", hashlib.sha256(b"not json").hexdigest()
    )
    changed_lock.write_bytes(b"not json")
    with pytest.raises(RuntimeError, match="lock is invalid"):
        runner._load_lock(changed_lock)


def test_runner_verifies_exact_file_size_and_sha256(tmp_path: Path) -> None:
    runner = _load_runner()
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"verified model")
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()

    runner._verify_file(model_path, 14, digest)

    model_path.write_bytes(b"wrong")
    with pytest.raises(RuntimeError, match="wrong size"):
        runner._verify_file(model_path, 14, digest)
    model_path.write_bytes(b"tampered model")
    with pytest.raises(RuntimeError, match="wrong SHA-256"):
        runner._verify_file(model_path, 14, digest)


def test_unit_uses_pinned_private_runtime_and_model_directory() -> None:
    unit = (_SYSTEMD / "wyoming-piper.service").read_text()

    assert "wyoming-tts-venv/bin/python" in unit
    assert "wyoming-piper-runner.py" in unit
    assert "-m wyoming_piper" not in unit
    assert "Environment=HA_CODEX_TTS_URI=tcp://127.0.0.1:10200" in unit
    assert "Environment=HA_CODEX_TTS_VOICE=es_MX-ald-medium" in unit
    assert "Environment=HA_CODEX_TTS_THREADS=4" in unit
    assert (
        "Environment=HA_CODEX_TTS_MODEL_DIR=%h/.local/share/ha-codex-voice/models/piper"
        in unit
    )
    assert (
        "Environment=HA_CODEX_TTS_MODEL_LOCK=%h/.local/share/ha-codex-voice/"
        "wyoming-piper-model.lock.json" in unit
    )
    assert "EnvironmentFile=-%h/.config/ha-codex-voice/local-tts.env" in unit
    assert "--data-dir ${HA_CODEX_TTS_MODEL_DIR}" in unit
    assert "--download-dir ${HA_CODEX_TTS_MODEL_DIR}" in unit


def test_unit_limits_home_and_model_write_access() -> None:
    unit = (_SYSTEMD / "wyoming-piper.service").read_text()

    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=tmpfs" in unit
    assert "BindReadOnlyPaths=%h/.local/share/ha-codex-voice/wyoming-tts-venv" in unit
    assert (
        "BindReadOnlyPaths=%h/.local/share/ha-codex-voice/wyoming-piper-runner.py"
        in unit
    )
    assert (
        "BindReadOnlyPaths=%h/.local/share/ha-codex-voice/"
        "wyoming-piper-model.lock.json" in unit
    )
    assert "BindReadOnlyPaths=%h/.local/share/ha-codex-voice/models/piper" in unit
    assert "BindPaths=%h/.local/share/ha-codex-voice/models/piper" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "PrivateTmp=true" in unit
    assert "ProtectProc=invisible" in unit
    assert "CapabilityBoundingSet=" in unit


def test_env_example_keeps_loopback_default_and_documents_lan_override() -> None:
    env_example = (_SYSTEMD / "local-tts.env.example").read_text()

    assert "HA_CODEX_TTS_URI=tcp://127.0.0.1:10200" in env_example
    assert "# HA_CODEX_TTS_URI=tcp://192.168.1.10:10200" in env_example
    assert "HA_CODEX_TTS_VOICE=es_MX-ald-medium" in env_example
    assert "HA_CODEX_TTS_THREADS=4" in env_example
    assert "values from 1 through 64" in env_example
    assert "0.0.0.0" not in env_example


def test_requirements_lock_pins_complete_piper_runtime() -> None:
    requirements = (
        (_SYSTEMD / "wyoming-piper-requirements.lock").read_text().splitlines()
    )

    assert requirements
    assert all("==" in requirement for requirement in requirements)
    assert "wyoming-piper==2.3.1" in requirements
    assert "piper-tts==1.6.0" in requirements
    assert "wyoming==1.10.0" in requirements
    assert "onnxruntime==1.28.0" in requirements
