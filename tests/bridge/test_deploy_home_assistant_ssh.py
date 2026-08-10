from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from urllib import error

import pytest

from scripts import deploy_home_assistant_ssh as deploy


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "custom_components" / "codex_voice"
    source.mkdir(parents=True)
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "domain": "codex_voice",
                "name": "Codex Voice",
                "version": "1.2.3",
            }
        )
    )
    (source / "__init__.py").write_text("VALUE = 1\n")
    return tmp_path, source


def _identity_file(tmp_path: Path) -> Path:
    identity = tmp_path / "deploy-key"
    identity.write_text("not-a-real-private-key\n")
    identity.chmod(0o600)
    return identity


def test_archive_contains_only_safe_integration_files(tmp_path: Path) -> None:
    repository, source = _repository(tmp_path)
    (source / "translations").mkdir()
    (source / "translations" / "en.json").write_text('{"title": "Codex"}\n')
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "module.pyc").write_bytes(b"bytecode")
    (source / "ignored.pyc").write_bytes(b"bytecode")
    (source / ".env").write_text("HASS_TOKEN=do-not-package\n")
    (source / "secrets.yaml").write_text("api_key: do-not-package\n")
    archive_path = tmp_path / "deployment.tar.gz"

    summary = deploy.build_archive(repository, archive_path)

    assert archive_path.stat().st_mode & 0o777 == 0o600
    assert summary.file_count == 3
    assert summary.archive_size == archive_path.stat().st_size
    assert len(summary.sha256) == 64
    with tarfile.open(archive_path, "r:gz") as archive:
        assert archive.getnames() == [
            "codex_voice",
            "codex_voice/__init__.py",
            "codex_voice/manifest.json",
            "codex_voice/translations",
            "codex_voice/translations/en.json",
        ]
        assert all(member.isfile() or member.isdir() for member in archive.getmembers())
        assert all(
            not member.issym() and not member.islnk() for member in archive.getmembers()
        )
        combined = b"".join(
            archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile() and archive.extractfile(member) is not None
        )
    assert b"do-not-package" not in combined


def test_archive_refuses_symlinks_and_non_portable_paths(tmp_path: Path) -> None:
    repository, source = _repository(tmp_path)
    (source / "linked.py").symlink_to(source / "__init__.py")

    with pytest.raises(deploy.DeploymentError, match="symbolic link"):
        deploy.build_archive(repository, tmp_path / "linked.tar.gz")

    (source / "linked.py").unlink()
    (source / "not portable.py").write_text("value = 2\n")
    with pytest.raises(deploy.DeploymentError, match="non-portable"):
        deploy.build_archive(repository, tmp_path / "non-portable.tar.gz")


@pytest.mark.parametrize(
    ("manifest", "error_match"),
    [
        ({"domain": "wrong", "name": "Codex", "version": "1"}, "domain"),
        ({"domain": "codex_voice", "version": "1"}, "name"),
        ({"domain": "codex_voice", "name": "Codex"}, "version"),
    ],
)
def test_archive_validates_manifest(
    tmp_path: Path,
    manifest: dict[str, str],
    error_match: str,
) -> None:
    repository, source = _repository(tmp_path)
    (source / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(deploy.DeploymentError, match=error_match):
        deploy.build_archive(repository, tmp_path / "invalid.tar.gz")


def test_archive_enforces_source_and_archive_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, source = _repository(tmp_path)
    (source / "large.txt").write_bytes(b"12345")
    monkeypatch.setattr(deploy, "MAX_FILE_BYTES", 4)

    with pytest.raises(deploy.DeploymentError, match="file exceeds"):
        deploy.build_archive(repository, tmp_path / "large.tar.gz")


def test_validate_archive_rejects_links_and_path_traversal(tmp_path: Path) -> None:
    for member_name, member_type in (
        ("codex_voice/link", tarfile.SYMTYPE),
        ("codex_voice/../outside", tarfile.REGTYPE),
    ):
        archive_path = tmp_path / f"bad-{len(list(tmp_path.iterdir()))}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            root = tarfile.TarInfo("codex_voice")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            manifest_data = json.dumps(
                {
                    "domain": "codex_voice",
                    "name": "Codex Voice",
                    "version": "1",
                }
            ).encode()
            manifest = tarfile.TarInfo("codex_voice/manifest.json")
            manifest.size = len(manifest_data)
            archive.addfile(manifest, io.BytesIO(manifest_data))
            unsafe = tarfile.TarInfo(member_name)
            unsafe.type = member_type
            if member_type == tarfile.SYMTYPE:
                unsafe.linkname = "/etc/passwd"
            else:
                unsafe.size = 1
            archive.addfile(unsafe, io.BytesIO(b"x"))

        with pytest.raises(deploy.DeploymentError, match=r"unsafe path|non-regular"):
            deploy.validate_archive(archive_path)


def test_scp_and_ssh_use_fixed_secure_argument_vectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _source = _repository(tmp_path)
    identity = _identity_file(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)

    result = deploy.main(
        [
            "--repo-root",
            str(repository),
            "--host",
            "192.168.8.14",
            "--identity-file",
            str(identity),
        ]
    )

    assert result == 0
    assert len(calls) == 2
    scp_argv, scp_options = calls[0]
    ssh_argv, ssh_options = calls[1]
    assert scp_argv[:3] == ["scp", "-P", "22"]
    remote_archive = scp_argv[-1]
    assert remote_archive.startswith("root@192.168.8.14:/config/.codex_voice-deploy-")
    assert remote_archive.endswith(".tar.gz")
    assert ssh_argv[:3] == ["ssh", "-p", "22"]
    assert ssh_argv[-7] == "root@192.168.8.14"
    assert ssh_argv[-6:-3] == ["sh", "-s", "--"]
    assert len(ssh_argv[-3]) == 32
    assert remote_archive.endswith(f"{ssh_argv[-3]}.tar.gz")
    assert len(ssh_argv[-2]) == 64
    assert ssh_argv[-1].isdigit()
    for argv in (scp_argv, ssh_argv):
        for required_option in (
            "BatchMode=yes",
            "IdentitiesOnly=yes",
            "StrictHostKeyChecking=yes",
        ):
            assert required_option in argv
        assert argv[argv.index("-i") + 1] == str(identity)
    assert scp_options["check"] is True
    assert scp_options["stdin"] == subprocess.DEVNULL
    assert "shell" not in scp_options
    assert ssh_options["check"] is True
    assert ssh_options["input"] == deploy.REMOTE_DEPLOY_SCRIPT
    assert "shell" not in ssh_options


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "homeassistant;touch-pwned"),
        ("host", "-oProxyCommand=bad"),
        ("user", "root attacker"),
        ("port", 0),
        ("deployment_id", "../shared"),
    ],
)
def test_argv_builders_reject_injectable_target_values(
    tmp_path: Path,
    field: str,
    value: str | int,
) -> None:
    values: dict[str, object] = {
        "host": "192.168.8.14",
        "user": "root",
        "port": 22,
        "identity_file": tmp_path / "key",
        "archive_path": tmp_path / "archive.tar.gz",
        "deployment_id": "a" * 32,
    }
    values[field] = value

    with pytest.raises(deploy.DeploymentError):
        deploy.build_scp_argv(**values)  # type: ignore[arg-type]


def test_remote_script_validates_manifest_and_keeps_one_backup() -> None:
    script = deploy.REMOTE_DEPLOY_SCRIPT

    assert "sha256sum" in script
    assert "EXPECTED_SIZE" in script
    assert "manifest.json" in script
    assert '"domain"' in script
    assert "/config/.codex_voice-deploy-" in script
    assert "/config/.codex_voice-deploy-lock" in script
    assert "/config/.codex_voice-deploy-previous" in script
    assert "/config/custom_components/codex_voice.previous" not in script
    assert 'mv -- "$TARGET" "$BACKUP"' in script
    assert 'mv -- "$BACKUP" "$TARGET"' in script
    assert "ROLLBACK_REQUIRED=1" in script
    assert "ROLLBACK_REQUIRED=0" in script
    assert "LOCK_ACQUIRED=1" in script


def test_dry_run_prints_sanitized_operations_and_never_runs_network_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, _source = _repository(tmp_path)
    token = "very-sensitive-home-assistant-token"
    monkeypatch.setenv("HASS_URL", "http://homeassistant.local:8123")
    monkeypatch.setenv("HASS_TOKEN", token)

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("dry-run must not invoke subprocesses")

    monkeypatch.setattr(deploy.subprocess, "run", unexpected_run)

    result = deploy.main(
        [
            "--repo-root",
            str(repository),
            "--host",
            "192.168.8.14",
            "--dry-run",
            "--restart",
        ]
    )
    captured = capsys.readouterr()

    assert result == 0
    assert token not in captured.out
    assert token not in captured.err
    assert "<redacted>" in captured.out
    assert "StrictHostKeyChecking=yes" in captured.out
    assert "<validated-archive>" in captured.out


def test_restart_uses_supported_rest_endpoint_and_waits_for_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "rest-api-secret-token"
    requests: list[object] = []

    def request_status(home_assistant_request: object, _timeout: float) -> int:
        requests.append(home_assistant_request)
        if len(requests) == 1:
            return 200
        if len(requests) == 2:
            raise error.URLError("restarting")
        return 200

    monkeypatch.setattr(deploy, "_request_status", request_status)
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)

    deploy.restart_home_assistant(
        "http://homeassistant.local:8123",
        token,
        wait_timeout=2,
        poll_interval=0.1,
    )

    restart_request = requests[0]
    assert restart_request.full_url.endswith("/api/services/homeassistant/restart")
    assert restart_request.method == "POST"
    assert restart_request.data == b"{}"
    assert restart_request.headers["Authorization"] == f"Bearer {token}"
    assert requests[-1].full_url.endswith("/api/")


def test_restart_failure_does_not_expose_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "never-print-this-token"

    def unauthorized(_request: object, _timeout: float) -> int:
        raise error.HTTPError(
            "http://homeassistant.local:8123/api/services/homeassistant/restart",
            401,
            f"unauthorized {token}",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(deploy, "_request_status", unauthorized)

    with pytest.raises(deploy.DeploymentError) as raised:
        deploy.restart_home_assistant("http://homeassistant.local:8123", token)

    assert token not in str(raised.value)


def test_subprocess_failure_is_sanitized_and_stops_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, _source = _repository(tmp_path)
    identity = _identity_file(tmp_path)
    call_count = 0

    def failed_upload(argv: list[str], **_kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        raise subprocess.CalledProcessError(17, argv, stderr="remote detail")

    monkeypatch.setattr(deploy.subprocess, "run", failed_upload)

    result = deploy.main(
        [
            "--repo-root",
            str(repository),
            "--host",
            "192.168.8.14",
            "--identity-file",
            str(identity),
        ]
    )
    captured = capsys.readouterr()

    assert result == 1
    assert call_count == 1
    assert "scp operation failed with exit status 17" in captured.err
    assert "remote detail" not in captured.err


def test_identity_file_must_be_private_and_not_a_symlink(tmp_path: Path) -> None:
    identity = _identity_file(tmp_path)
    identity.chmod(0o644)
    with pytest.raises(deploy.DeploymentError, match="permissions"):
        deploy._validate_identity_file(identity, required=True)

    identity.chmod(0o600)
    linked_identity = tmp_path / "linked-key"
    linked_identity.symlink_to(identity)
    with pytest.raises(deploy.DeploymentError, match="regular file"):
        deploy._validate_identity_file(linked_identity, required=True)
