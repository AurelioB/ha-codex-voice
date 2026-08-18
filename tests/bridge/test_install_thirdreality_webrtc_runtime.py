import hashlib
import json
import stat
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import install_thirdreality_webrtc_runtime as installer

_ROOT = Path(__file__).parents[2]
_PRODUCTION_LOCK = _ROOT / "device" / "thirdreality" / "webrtc-runtime.lock.txt"


def _fake_uv(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    assert command[:3] == ["uv", "pip", "install"]
    assert command[command.index("--python-version") + 1] == "3.11"
    assert command[command.index("--python-platform") + 1] == ("aarch64-manylinux_2_28")
    for option in (
        "--require-hashes",
        "--only-binary",
        "--strict",
        "--no-config",
    ):
        assert option in command
    assert command[command.index("--default-index") + 1] == "https://pypi.org/simple"
    assert kwargs["check"] is True
    assert kwargs["shell"] is False
    assert {"LANG", "LC_ALL", "PATH"} <= set(kwargs["env"])
    assert set(kwargs["env"]) <= {
        "LANG",
        "LC_ALL",
        "PATH",
        *installer._BUILD_ENVIRONMENT_KEYS,
    }
    assert "OPENAI_TOKEN" not in kwargs["env"]

    target = Path(command[command.index("--target") + 1])
    for name, version in installer._EXPECTED_PACKAGES.items():
        distribution = target / f"{name.replace('-', '_')}-{version}.dist-info"
        distribution.mkdir()
        (distribution / "METADATA").write_text(
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )
        (distribution / "RECORD").write_text("fixed runtime record\n", encoding="utf-8")
    (target / "runtime_payload.py").write_text("VALUE = 1\n", encoding="utf-8")
    return subprocess.CompletedProcess(command, 0)


def _build_bundle(tmp_path: Path, name: str = "runtime.tar.gz") -> tuple[Path, str]:
    archive = tmp_path / name
    digest = installer.build_runtime(_PRODUCTION_LOCK, archive, runner=_fake_uv)
    return archive, digest


def _fake_python(tmp_path: Path) -> Path:
    python = tmp_path / "python3"
    python.write_bytes(b"test interpreter placeholder")
    python.chmod(0o700)
    return python


def _smoke_success(
    calls: list[tuple[list[str], dict[str, Any]]],
    *,
    unprivileged: bool = False,
):
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        assert command[1:3] == ["-I", "-S"]
        assert command[3] == "-c"
        assert "RTCPeerConnection" in command[4]
        assert command[-1].endswith("/site-packages")
        assert kwargs["check"] is True
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == "/"
        assert kwargs["env"] == {
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        assert kwargs["timeout"] == 30.0
        if unprivileged:
            assert kwargs["user"] == 65_534
            assert kwargs["group"] == 65_534
            assert kwargs["extra_groups"] == ()
            assert kwargs["umask"] == 0o077
        else:
            assert "user" not in kwargs
            assert "group" not in kwargs
            assert "extra_groups" not in kwargs
        return subprocess.CompletedProcess(command, 0)

    return run


def test_production_lock_pins_complete_reviewed_aarch64_runtime() -> None:
    pins = installer.load_requirements_lock(_PRODUCTION_LOCK)

    assert pins == {
        "aioice": "0.10.2",
        "aiortc": "1.15.0",
        "av": "17.1.0",
        "cffi": "2.1.1",
        "cryptography": "50.0.0",
        "dnspython": "2.8.0",
        "google-crc32c": "1.8.0",
        "ifaddr": "0.2.0",
        "pycparser": "3.0",
        "pyee": "13.0.1",
        "pylibsrtp": "1.0.0",
        "pyopenssl": "26.4.0",
        "typing-extensions": "4.16.0",
    }
    lock = _PRODUCTION_LOCK.read_text(encoding="utf-8")
    assert "aiortc==1.15.0" in lock
    assert "av==17.1.0" in lock
    assert lock.count("--hash=sha256:") > len(pins)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda lines: [*lines[:-1], "typing-extensions==4.16.0"],
        lambda lines: [*lines, lines[0]],
        lambda lines: [*lines[:-1], f"extra==1 --hash=sha256:{'0' * 64}"],
    ],
)
def test_lock_rejects_unhashed_duplicate_or_unreviewed_entries(
    tmp_path: Path,
    mutation: Any,
) -> None:
    lines = [
        f"{name}=={version} --hash=sha256:{index:064x}"
        for index, (name, version) in enumerate(
            installer._EXPECTED_PACKAGES.items(), start=1
        )
    ]
    lock = tmp_path / "runtime.lock.txt"
    lock.write_text("\n".join(mutation(lines)) + "\n", encoding="utf-8")

    with pytest.raises(installer.RuntimeValidationError):
        installer.load_requirements_lock(lock)


def test_builder_is_hash_locked_secure_and_byte_deterministic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_TOKEN", "must-not-enter-build-environment")
    first, first_digest = _build_bundle(tmp_path, "first.tar.gz")
    second, second_digest = _build_bundle(tmp_path, "second.tar.gz")

    assert first.read_bytes() == second.read_bytes()
    assert (
        first_digest == second_digest == hashlib.sha256(first.read_bytes()).hexdigest()
    )
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == sorted(
            member.name for member in members
        )
        assert all(member.uid == member.gid == 0 for member in members)
        assert all(member.mtime == 0 for member in members)
        assert all(
            member.mode == (0o755 if member.isdir() else 0o644) for member in members
        )
        manifest_file = archive.extractfile(installer.MANIFEST_NAME)
        assert manifest_file is not None
        manifest = json.load(manifest_file)
    assert manifest["packages"] == installer._EXPECTED_PACKAGES
    assert manifest["python_version"] == "3.11"
    assert manifest["python_platform"] == "aarch64-manylinux_2_28"
    assert "must-not-enter-build-environment" not in first.read_bytes().decode(
        "latin-1"
    )


def test_failed_build_preserves_existing_archive(tmp_path: Path) -> None:
    output = tmp_path / "runtime.tar.gz"
    output.write_bytes(b"existing reviewed archive")

    def fail(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(1, ["uv"])

    with pytest.raises(installer.RuntimeBuildError, match="uv failed"):
        installer.build_runtime(_PRODUCTION_LOCK, output, runner=fail)

    assert output.read_bytes() == b"existing reviewed archive"
    assert not list(tmp_path.glob(".runtime.tar.gz.*.tmp"))


def test_installer_verifies_smokes_and_atomically_selects_release(
    tmp_path: Path,
) -> None:
    archive, digest = _build_bundle(tmp_path)
    device = tmp_path / "device"
    device.mkdir(mode=0o700)
    target = device / "codex-webrtc"
    python = _fake_python(tmp_path)
    smoke_calls: list[tuple[list[str], dict[str, Any]]] = []

    release = installer.install_runtime(
        archive,
        digest,
        target,
        python_executable=python,
        runner=_smoke_success(smoke_calls),
        require_root=False,
    )

    assert release == device / ".codex-webrtc-releases" / digest
    assert target.is_symlink()
    assert target.readlink() == Path(f".codex-webrtc-releases/{digest}")
    assert target.resolve() == release
    assert len(smoke_calls) == 2
    assert stat.S_IMODE(release.stat().st_mode) == 0o755
    assert stat.S_IMODE((release / installer.MANIFEST_NAME).stat().st_mode) == 0o644
    for entry in release.rglob("*"):
        expected_mode = 0o755 if entry.is_dir() else 0o644
        assert stat.S_IMODE(entry.stat().st_mode) == expected_mode
    assert not list((device / ".codex-webrtc-releases").glob(".*-staging-*"))

    second_calls: list[tuple[list[str], dict[str, Any]]] = []
    assert (
        installer.install_runtime(
            archive,
            digest,
            target,
            python_executable=python,
            runner=_smoke_success(second_calls),
            require_root=False,
        )
        == release
    )
    assert len(second_calls) == 1


def test_runtime_smoke_can_run_as_the_unprivileged_sidecar_identity(
    tmp_path: Path,
) -> None:
    python = _fake_python(tmp_path)
    python.chmod(0o755)
    runtime = tmp_path / "runtime"
    (runtime / installer.DEPENDENCY_DIRECTORY).mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    installer._smoke_runtime(
        python,
        runtime,
        owner_uid=python.stat().st_uid,
        runner=_smoke_success(calls, unprivileged=True),
        unprivileged_uid=65_534,
        unprivileged_gid=65_534,
    )

    assert len(calls) == 1


def test_digest_mismatch_fails_before_installation_changes(tmp_path: Path) -> None:
    archive, _digest = _build_bundle(tmp_path)
    device = tmp_path / "device"
    device.mkdir(mode=0o700)

    with pytest.raises(installer.RuntimeValidationError, match="does not match"):
        installer.install_runtime(
            archive,
            "0" * 64,
            device / "codex-webrtc",
            python_executable=_fake_python(tmp_path),
            require_root=False,
        )

    assert list(device.iterdir()) == []


def test_installer_rejects_group_writable_archive(tmp_path: Path) -> None:
    archive, digest = _build_bundle(tmp_path)
    archive.chmod(0o620)
    device = tmp_path / "device"
    device.mkdir(mode=0o700)

    with pytest.raises(installer.RuntimeValidationError, match="owner-controlled"):
        installer.install_runtime(
            archive,
            digest,
            device / "codex-webrtc",
            python_executable=_fake_python(tmp_path),
            require_root=False,
        )

    assert list(device.iterdir()) == []


def test_smoke_failure_preserves_target_and_removes_staging(tmp_path: Path) -> None:
    archive, digest = _build_bundle(tmp_path)
    device = tmp_path / "device"
    device.mkdir(mode=0o700)

    def fail_smoke(
        command: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(1, command)

    with pytest.raises(installer.RuntimeBuildError, match="smoke check failed"):
        installer.install_runtime(
            archive,
            digest,
            device / "codex-webrtc",
            python_executable=_fake_python(tmp_path),
            runner=fail_smoke,
            require_root=False,
        )

    assert not (device / "codex-webrtc").exists()
    releases = device / ".codex-webrtc-releases"
    assert releases.is_dir()
    assert list(releases.iterdir()) == []


def test_archive_traversal_is_rejected_without_writing_outside_staging(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../escaped")
        member.size = 0
        output.addfile(member)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    device = tmp_path / "device"
    device.mkdir(mode=0o700)

    with pytest.raises(installer.RuntimeValidationError, match="safe relative"):
        installer.install_runtime(
            archive,
            digest,
            device / "codex-webrtc",
            python_executable=_fake_python(tmp_path),
            require_root=False,
        )

    assert not (tmp_path / "escaped").exists()
    assert not (device / "codex-webrtc").exists()


def test_installation_requires_root_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive, digest = _build_bundle(tmp_path)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)

    with pytest.raises(PermissionError, match="must run as root"):
        installer.install_runtime(
            archive,
            digest,
            Path("/data/conf/codex-webrtc"),
        )


def test_insufficient_disk_space_fails_before_extracting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive, digest = _build_bundle(tmp_path)
    device = tmp_path / "device"
    device.mkdir(mode=0o700)
    monkeypatch.setattr(
        installer.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10, used=9, free=1),
    )

    with pytest.raises(installer.RuntimeBuildError, match="insufficient free space"):
        installer.install_runtime(
            archive,
            digest,
            device / "codex-webrtc",
            python_executable=_fake_python(tmp_path),
            require_root=False,
        )

    releases = device / ".codex-webrtc-releases"
    assert releases.is_dir()
    assert list(releases.iterdir()) == []
