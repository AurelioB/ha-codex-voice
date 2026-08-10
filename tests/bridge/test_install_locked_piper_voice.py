import hashlib
import io
import json
import stat
from pathlib import Path

import pytest

from scripts import install_locked_piper_voice as installer

_ROOT = Path(__file__).parents[2]
_PRODUCTION_LOCK = _ROOT / "deploy" / "systemd" / "wyoming-piper-model.lock.json"


def _lock_for(payload: bytes, *, filename: str = "voice.onnx") -> dict[str, object]:
    revision = "abc123"
    return {
        "schema_version": 1,
        "voice": "test-voice",
        "revision": revision,
        "files": [
            {
                "filename": filename,
                "url": (
                    "https://huggingface.co/rhasspy/piper-voices/resolve/"
                    f"{revision}/test/{filename}"
                ),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }


def _write_lock(tmp_path: Path, value: object) -> Path:
    lock_path = tmp_path / "model.lock.json"
    lock_path.write_text(json.dumps(value), encoding="utf-8")
    return lock_path


def test_production_lock_selects_exact_mexican_spanish_artifacts() -> None:
    model_lock = installer.load_lock(_PRODUCTION_LOCK)

    assert model_lock.voice == "es_MX-ald-medium"
    assert model_lock.revision == "0622afc867cf0388684853ecdf59a498b489949d"
    assert [
        (file.filename, file.size_bytes, file.sha256) for file in model_lock.files
    ] == [
        (
            "es_MX-ald-medium.onnx",
            63_201_294,
            "019b3803293c93e34a206dd2e53a3889209a514e786fd7144f7b70196c579b63",
        ),
        (
            "es_MX-ald-medium.onnx.json",
            4_878,
            "5a71498158e04afc8099bfd019c7e87c68eb9d042505a2b1a87e5c1ac2b1a61d",
        ),
    ]


def test_lock_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    lock_path = tmp_path / "duplicate.lock.json"
    lock_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")

    with pytest.raises(installer.LockValidationError, match="duplicate JSON key"):
        installer.load_lock(lock_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("filename", "../voice.onnx", "filename is unsafe"),
        ("url", "http://huggingface.co/voice.onnx", "must use https"),
        (
            "url",
            "https://example.com/rhasspy/piper-voices/resolve/abc123/test/voice.onnx",
            "must use https://huggingface.co",
        ),
        ("size_bytes", 0, "size_bytes must be an integer"),
    ],
)
def test_lock_rejects_unsafe_or_unbounded_file_entries(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    lock = _lock_for(b"voice")
    lock["files"][0][field] = value

    with pytest.raises(installer.LockValidationError, match=message):
        installer.load_lock(_write_lock(tmp_path, lock))


def test_installer_streams_verifies_and_atomically_installs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"verified voice bytes"
    lock_path = _write_lock(tmp_path, _lock_for(payload))
    target_dir = tmp_path / "models"
    calls: list[tuple[str, float]] = []

    def fake_urlopen(url: str, *, timeout: float) -> io.BytesIO:
        calls.append((url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(installer, "urlopen", fake_urlopen)

    installed = installer.install_locked_voice(lock_path, target_dir, timeout=12.5)

    target = target_dir / "voice.onnx"
    assert installed == ("voice.onnx",)
    assert target.read_bytes() == payload
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target_dir.stat().st_mode) == 0o700
    assert calls == [
        (
            "https://huggingface.co/rhasspy/piper-voices/resolve/abc123/test/voice.onnx",
            12.5,
        )
    ]
    assert not list(target_dir.glob(".piper-download-*"))

    monkeypatch.setattr(
        installer,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("valid files must not download"),
    )
    assert installer.install_locked_voice(lock_path, target_dir) == ()


def test_failed_digest_preserves_existing_target_and_cleans_temp_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_path = _write_lock(tmp_path, _lock_for(b"good"))
    target_dir = tmp_path / "models"
    target_dir.mkdir()
    target = target_dir / "voice.onnx"
    target.write_bytes(b"existing invalid file")
    monkeypatch.setattr(
        installer, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"evil")
    )

    with pytest.raises(installer.DownloadIntegrityError, match="SHA-256"):
        installer.install_locked_voice(lock_path, target_dir)

    assert target.read_bytes() == b"existing invalid file"
    assert not list(target_dir.glob(".piper-download-*"))


@pytest.mark.parametrize("response", [b"go", b"good plus trailing bytes"])
def test_installer_rejects_short_or_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: bytes,
) -> None:
    lock_path = _write_lock(tmp_path, _lock_for(b"good"))
    target_dir = tmp_path / "models"
    monkeypatch.setattr(
        installer, "urlopen", lambda *_args, **_kwargs: io.BytesIO(response)
    )

    with pytest.raises(installer.DownloadIntegrityError):
        installer.install_locked_voice(lock_path, target_dir)

    assert not (target_dir / "voice.onnx").exists()
    assert not list(target_dir.glob(".piper-download-*"))


def test_installer_rejects_symlink_target_directory(tmp_path: Path) -> None:
    lock_path = _write_lock(tmp_path, _lock_for(b"good"))
    real_dir = tmp_path / "real-models"
    real_dir.mkdir()
    target_dir = tmp_path / "models"
    target_dir.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(RuntimeError, match="non-symlink directory"):
        installer.install_locked_voice(lock_path, target_dir)


@pytest.mark.parametrize("timeout", [0, -1, 601])
def test_installer_rejects_unbounded_timeout(tmp_path: Path, timeout: float) -> None:
    lock_path = _write_lock(tmp_path, _lock_for(b"good"))

    with pytest.raises(ValueError, match="timeout"):
        installer.install_locked_voice(lock_path, tmp_path / "models", timeout=timeout)
