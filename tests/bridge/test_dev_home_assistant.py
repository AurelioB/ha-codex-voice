from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from scripts import dev_home_assistant as dev


def _repository(tmp_path: Path) -> Path:
    component = tmp_path / "custom_components" / "codex_voice"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text("{}\n", encoding="utf-8")
    (component / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def _completed(
    command: list[str],
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


_CONTAINER_ID = "a" * 64


@pytest.fixture(autouse=True)
def _clear_dev_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(dev._DEV_TOKEN_ENV, raising=False)


def _inspect_document(
    repo_root: Path,
    status: str = "running",
) -> dict[str, Any]:
    port_bindings = {"8123/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18123"}]}
    return {
        "Id": _CONTAINER_ID,
        "Config": {
            "Image": dev.IMAGE,
            "Entrypoint": ["/init"],
            "Cmd": None,
            "Labels": {
                dev._OWNER_LABEL: dev._OWNER_LABEL_VALUE,
                dev._ROOT_LABEL: str(repo_root),
                "unrelated.image.label": "allowed",
            },
        },
        "State": {"Status": status},
        "HostConfig": {
            "NetworkMode": "bridge",
            "ExtraHosts": [dev._BRIDGE_HOST_MAPPING],
            "PortBindings": port_bindings,
            "PublishAllPorts": False,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(repo_root / ".ha-dev"),
                "Destination": "/config",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": str(repo_root / "custom_components" / "codex_voice"),
                "Destination": "/config/custom_components/codex_voice",
                "RW": False,
            },
        ],
        "NetworkSettings": {
            "Ports": port_bindings
            if status in {"paused", "restarting", "running"}
            else {},
            "Networks": {"bridge": {"Gateway": "172.17.0.1"}},
        },
    }


def _owned_inspect_output(repo_root: Path, status: str = "running") -> str:
    return json.dumps([_inspect_document(repo_root, status)])


def _private_state(repo_root: Path) -> Path:
    state = repo_root / ".ha-dev"
    state.mkdir(mode=0o700)
    return state


def _apply_drift(
    document: dict[str, Any],
    drift: str,
    repo_root: Path,
) -> None:
    if drift == "image":
        document["Config"]["Image"] = f"{dev.IMAGE}-stale"
    elif drift == "entrypoint":
        document["Config"]["Entrypoint"] = ["python3"]
    elif drift == "command":
        document["Config"]["Cmd"] = ["--debug"]
    elif drift == "host_ip":
        document["HostConfig"]["PortBindings"]["8123/tcp"][0]["HostIp"] = "0.0.0.0"
    elif drift == "host_port":
        document["HostConfig"]["PortBindings"]["8123/tcp"][0]["HostPort"] = "8123"
    elif drift == "container_port":
        bindings = document["HostConfig"]["PortBindings"]
        bindings["9000/tcp"] = bindings.pop("8123/tcp")
    elif drift == "publish_all":
        document["HostConfig"]["PublishAllPorts"] = True
    elif drift == "restart_policy":
        document["HostConfig"]["RestartPolicy"]["Name"] = "always"
    elif drift == "restart_retries":
        document["HostConfig"]["RestartPolicy"]["MaximumRetryCount"] = 1
    elif drift == "state_source":
        document["Mounts"][0]["Source"] = str(repo_root / "other-state")
    elif drift == "state_read_only":
        document["Mounts"][0]["RW"] = False
    elif drift == "component_source":
        document["Mounts"][1]["Source"] = str(repo_root / "other-component")
    elif drift == "component_writable":
        document["Mounts"][1]["RW"] = True
    elif drift == "extra_mount":
        document["Mounts"].append(
            {
                "Type": "bind",
                "Source": str(repo_root / "extra-mount"),
                "Destination": "/extra",
                "RW": False,
            }
        )
    elif drift == "host_network":
        document["HostConfig"]["NetworkMode"] = "host"
    elif drift == "missing_extra_host":
        document["HostConfig"]["ExtraHosts"] = None
    elif drift == "wrong_extra_host":
        document["HostConfig"]["ExtraHosts"] = ["host.docker.internal:192.0.2.20"]
    elif drift == "extra_host":
        document["HostConfig"]["ExtraHosts"].append("example.test:192.0.2.21")
    elif drift == "extra_network":
        document["NetworkSettings"]["Networks"]["lan"] = {}
    elif drift == "runtime_host_ip":
        document["NetworkSettings"]["Ports"]["8123/tcp"][0]["HostIp"] = "::"
    else:
        raise AssertionError(f"unknown test drift: {drift}")


def test_runtime_is_fixed_to_pinned_image_and_loopback_port() -> None:
    assert dev.IMAGE == "ghcr.io/home-assistant/home-assistant:2026.8.1"
    assert dev.CONTAINER_NAME == "ha-codex-voice-dev"
    assert (dev.HOST, dev.HOST_PORT, dev.CONTAINER_PORT) == (
        "127.0.0.1",
        18123,
        8123,
    )
    assert dev.BRIDGE_HOST_PORT == 18787


def test_up_creates_private_state_and_safe_bind_mounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []
    waits: list[tuple[float, str | None]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[1:3] == ["container", "inspect"]:
            return _completed(
                command,
                1,
                stderr=f"Error: No such container: {dev.CONTAINER_NAME}\n",
            )
        return _completed(command, stdout="container-id\n")

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setattr(
        dev,
        "wait_for_home_assistant",
        lambda timeout, *, token: waits.append((timeout, token)) or False,
    )

    assert dev.main(["up", "--timeout", "12"], repo_root=repo_root) == 0
    output = capsys.readouterr().out
    assert "frontend is available" in output
    assert "complete onboarding" in output
    assert "is ready" not in output

    state = repo_root / ".ha-dev"
    assert state.is_dir()
    assert waits == [(12.0, None)]
    assert len(calls) == 2
    for _command, kwargs in calls:
        assert kwargs == {
            "check": False,
            "text": True,
            "capture_output": _command[1:3] == ["container", "inspect"],
            "shell": False,
            "timeout": None,
        }

    run_command = calls[1][0]
    assert run_command[:2] == ["docker", "run"]
    assert run_command[run_command.index("--add-host") + 1] == (
        "host.docker.internal:host-gateway"
    )
    assert run_command[
        run_command.index("--publish") : run_command.index("--publish") + 2
    ] == ["--publish", "127.0.0.1:18123:8123/tcp"]
    assert run_command[
        run_command.index("--network") : run_command.index("--network") + 2
    ] == ["--network", "bridge"]
    mounts = [
        run_command[index + 1]
        for index, argument in enumerate(run_command)
        if argument == "--mount"
    ]
    assert mounts == [
        f"type=bind,source={state},target=/config",
        (
            f"type=bind,source={repo_root}/custom_components/codex_voice,"
            "target=/config/custom_components/codex_voice,readonly"
        ),
    ]
    assert f"{dev._OWNER_LABEL}={dev._OWNER_LABEL_VALUE}" in run_command
    assert f"{dev._ROOT_LABEL}={repo_root}" in run_command
    assert "--entrypoint" not in run_command
    assert run_command[-1] == dev.IMAGE


def test_up_starts_an_owned_stopped_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = _repository(tmp_path)
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["container", "inspect"]:
            return _completed(
                command, stdout=_owned_inspect_output(repo_root, "exited")
            )
        return _completed(command)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setattr(
        dev, "wait_for_home_assistant", lambda _timeout, *, token: False
    )

    assert dev.main(["up"], repo_root=repo_root) == 0
    assert commands == [
        [
            "docker",
            "container",
            "inspect",
            dev.CONTAINER_NAME,
        ],
        ["docker", "container", "start", _CONTAINER_ID],
    ]


def test_restart_requires_owned_container_then_waits_for_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = _repository(tmp_path)
    _private_state(repo_root)
    commands: list[list[str]] = []
    waits: list[tuple[float, str | None]] = []
    preflights: list[str] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["container", "inspect"]:
            return _completed(command, stdout=_owned_inspect_output(repo_root))
        return _completed(command)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setenv(dev._DEV_TOKEN_ENV, "dev-token")
    monkeypatch.setattr(
        dev,
        "_require_running_preflight",
        lambda token: preflights.append(token),
    )
    monkeypatch.setattr(
        dev,
        "wait_for_home_assistant",
        lambda timeout, *, token: waits.append((timeout, token)) or True,
    )

    assert dev.main(["restart", "--timeout", "7.5"], repo_root=repo_root) == 0
    assert commands[-1] == [
        "docker",
        "container",
        "restart",
        "--timeout",
        str(dev._STOP_GRACE_SECONDS),
        _CONTAINER_ID,
    ]
    assert waits == [(7.5, "dev-token")]
    assert preflights == ["dev-token"]


def test_restart_requires_dev_token_before_docker_or_state_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("restart must fail before Docker"),
    )

    assert dev.main(["restart"], repo_root=repo_root) == 1
    output = capsys.readouterr()
    assert "requires HA_CODEX_DEV_HASS_TOKEN" in output.err
    assert output.out == ""
    assert not (repo_root / ".ha-dev").exists()


def test_check_is_isolated_read_only_and_does_not_create_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = _repository(tmp_path)
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _completed(command)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    assert dev.main(["check"], repo_root=repo_root) == 0
    assert not (repo_root / ".ha-dev").exists()
    assert len(calls) == 1
    command = calls[0]
    assert command[:4] == ["docker", "run", "--rm", "--network"]
    assert "none" in command
    assert "--read-only" in command
    assert command[command.index("--cap-drop") : command.index("--cap-drop") + 2] == [
        "--cap-drop",
        "ALL",
    ]
    assert "no-new-privileges:true" in command
    assert (
        command[command.index("--user") + 1] == f"{dev.os.getuid()}:{dev.os.getgid()}"
    )
    mount = command[command.index("--mount") + 1]
    assert mount.endswith("target=/config/custom_components/codex_voice,readonly")
    assert command[command.index("--entrypoint") + 1] == "python3"
    image_index = command.index(dev.IMAGE)
    assert command[image_index + 1] == "-c"
    check_script = command[image_index + 2]
    assert "compile(source.read_bytes()" in check_script
    assert 'importlib.import_module("custom_components.codex_voice")' in check_script


def test_status_logs_and_down_use_only_owned_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    _private_state(repo_root)
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["container", "inspect"]:
            return _completed(command, stdout=_owned_inspect_output(repo_root))
        return _completed(command)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    assert dev.main(["status"], repo_root=repo_root) == 0
    assert "running at http://127.0.0.1:18123" in capsys.readouterr().out
    assert dev.main(["logs", "--tail", "25", "--follow"], repo_root=repo_root) == 0
    assert commands[-1] == [
        "docker",
        "container",
        "logs",
        "--tail",
        "25",
        "--follow",
        _CONTAINER_ID,
    ]
    assert dev.main(["down"], repo_root=repo_root) == 0
    assert commands[-2:] == [
        [
            "docker",
            "container",
            "stop",
            "--timeout",
            str(dev._STOP_GRACE_SECONDS),
            _CONTAINER_ID,
        ],
        ["docker", "container", "rm", _CONTAINER_ID],
    ]
    assert not any("--force" in command for command in commands)
    assert (repo_root / ".ha-dev").is_dir()
    assert commands[-1] == [
        "docker",
        "container",
        "rm",
        _CONTAINER_ID,
    ]


class _BindableSocket:
    def __init__(self, *, error: OSError | None = None) -> None:
        self.error = error
        self.bound: list[tuple[str, int]] = []

    def __enter__(self) -> _BindableSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def bind(self, address: tuple[str, int]) -> None:
        self.bound.append(address)
        if self.error is not None:
            raise self.error


def test_bridge_host_prints_only_validated_bindable_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    _private_state(repo_root)
    calls: list[tuple[list[str], dict[str, object]]] = []
    bindable = _BindableSocket()

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[1:3] == ["container", "inspect"]:
            return _completed(command, stdout=_owned_inspect_output(repo_root))
        if command[1:3] == ["container", "exec"]:
            return _completed(command, stdout="172.17.0.1\n")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setattr(dev.socket, "socket", lambda _family, _kind: bindable)

    assert dev.main(["bridge-host"], repo_root=repo_root) == 0
    assert capsys.readouterr() == ("172.17.0.1\n", "")
    assert bindable.bound == [("172.17.0.1", dev.BRIDGE_HOST_PORT)]
    assert calls[1] == (
        [
            "docker",
            "container",
            "exec",
            _CONTAINER_ID,
            "python3",
            "-c",
            dev._BRIDGE_RESOLVER,
        ],
        {
            "check": False,
            "text": True,
            "capture_output": True,
            "shell": False,
            "timeout": dev._HEALTH_REQUEST_TIMEOUT,
        },
    )


@pytest.mark.parametrize("status", ["created", "exited", "restarting", "paused"])
def test_bridge_host_requires_running_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    repo_root = _repository(tmp_path)
    _private_state(repo_root)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda command, **_kwargs: _completed(
            command, stdout=_owned_inspect_output(repo_root, status)
        ),
    )

    assert dev.main(["bridge-host"], repo_root=repo_root) == 1
    assert "must be running" in capsys.readouterr().err


def test_bridge_host_rejects_missing_and_drifted_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    _private_state(repo_root)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda command, **_kwargs: _completed(
            command, 1, stderr="No such container: local-dev\n"
        ),
    )
    assert dev.main(["bridge-host"], repo_root=repo_root) == 1
    assert "does not exist" in capsys.readouterr().err

    document = _inspect_document(repo_root)
    _apply_drift(document, "wrong_extra_host", repo_root)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda command, **_kwargs: _completed(command, stdout=json.dumps([document])),
    )
    assert dev.main(["bridge-host"], repo_root=repo_root) == 1
    assert "refusing to reuse drifted" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("resolver_output", "message"),
    [
        ("", "malformed output"),
        ("172.17.0.1 172.17.0.2\n", "malformed output"),
        ("not-an-ip\n", "not an IPv4 address"),
        ("127.0.0.1\n", "not a safe host address"),
        ("0.0.0.0\n", "not a safe host address"),
        ("224.0.0.1\n", "not a safe host address"),
        ("x" * (dev._MAX_RESOLVER_OUTPUT_BYTES + 1), "oversized output"),
    ],
)
def test_bridge_host_rejects_malformed_or_unsafe_resolution(
    monkeypatch: pytest.MonkeyPatch,
    resolver_output: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda command, **_kwargs: _completed(command, stdout=resolver_output),
    )

    with pytest.raises(dev.DevLoopError, match=message):
        dev._resolve_bridge_host(_CONTAINER_ID)


def test_bridge_host_rejects_gateway_mismatch_and_unbindable_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    _private_state(repo_root)
    resolver_output = "172.17.0.2\n"

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["container", "inspect"]:
            return _completed(command, stdout=_owned_inspect_output(repo_root))
        return _completed(command, stdout=resolver_output)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setattr(
        dev.socket,
        "socket",
        lambda *_args: pytest.fail("mismatch must fail before binding"),
    )
    assert dev.main(["bridge-host"], repo_root=repo_root) == 1
    assert "does not match" in capsys.readouterr().err

    resolver_output = "172.17.0.1\n"
    unbindable = _BindableSocket(error=OSError("address unavailable"))
    monkeypatch.setattr(dev.socket, "socket", lambda _family, _kind: unbindable)
    assert dev.main(["bridge-host"], repo_root=repo_root) == 1
    assert "host cannot bind" in capsys.readouterr().err


def test_missing_container_status_and_down_are_non_destructive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    commands: list[list[str]] = []

    def missing(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _completed(command, 1, stderr="No such object: local-dev\n")

    monkeypatch.setattr(dev.subprocess, "run", missing)

    assert dev.main(["status"], repo_root=repo_root) == 1
    assert "not created" in capsys.readouterr().out
    assert dev.main(["down"], repo_root=repo_root) == 0
    assert "already absent" in capsys.readouterr().out
    assert all(command[1:3] == ["container", "inspect"] for command in commands)


def test_refuses_same_named_container_from_another_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    commands: list[list[str]] = []

    def unowned(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        document = _inspect_document(repo_root)
        document["Config"]["Labels"][dev._ROOT_LABEL] = "/another/repo"
        return _completed(command, stdout=json.dumps([document]))

    monkeypatch.setattr(dev.subprocess, "run", unowned)

    assert dev.main(["down"], repo_root=repo_root) == 1
    assert "refusing to operate on unowned container" in capsys.readouterr().err
    assert len(commands) == 1


@pytest.mark.parametrize(
    "drift",
    [
        "image",
        "entrypoint",
        "command",
        "host_ip",
        "host_port",
        "container_port",
        "publish_all",
        "restart_policy",
        "restart_retries",
        "state_source",
        "state_read_only",
        "component_source",
        "component_writable",
        "extra_mount",
        "host_network",
        "missing_extra_host",
        "wrong_extra_host",
        "extra_host",
        "extra_network",
        "runtime_host_ip",
    ],
)
def test_status_refuses_every_reuse_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    drift: str,
) -> None:
    repo_root = _repository(tmp_path)
    _private_state(repo_root)
    document = _inspect_document(repo_root)
    _apply_drift(document, drift, repo_root)
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _completed(command, stdout=json.dumps([document]))

    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    assert dev.main(["status"], repo_root=repo_root) == 1
    output = capsys.readouterr()
    assert "refusing to reuse drifted container" in output.err
    assert "http://127.0.0.1:18123" not in output.out
    assert commands == [["docker", "container", "inspect", dev.CONTAINER_NAME]]


def test_reuse_accepts_reversed_mount_order_and_extra_labels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = _repository(tmp_path)
    _private_state(repo_root)
    document = _inspect_document(repo_root)
    document["Mounts"].reverse()
    document["Config"]["Labels"]["another.unrelated.label"] = "value"
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda command, **_kwargs: _completed(command, stdout=json.dumps([document])),
    )

    assert dev.main(["status"], repo_root=repo_root) == 0


def test_up_safely_recreates_owned_drift_and_preserves_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    state = _private_state(repo_root)
    marker = state / "state-marker"
    marker.write_text("preserve", encoding="utf-8")
    document = _inspect_document(repo_root)
    _apply_drift(document, "host_ip", repo_root)
    calls: list[tuple[list[str], object]] = []
    waits: list[tuple[float, str | None]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs.get("timeout")))
        if command[1:3] == ["container", "inspect"]:
            return _completed(command, stdout=json.dumps([document]))
        return _completed(command)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setattr(
        dev,
        "wait_for_home_assistant",
        lambda timeout, *, token: waits.append((timeout, token)) or False,
    )

    assert dev.main(["up"], repo_root=repo_root) == 0

    assert [command for command, _timeout in calls[:3]] == [
        ["docker", "container", "inspect", dev.CONTAINER_NAME],
        [
            "docker",
            "container",
            "stop",
            "--timeout",
            str(dev._STOP_GRACE_SECONDS),
            _CONTAINER_ID,
        ],
        ["docker", "container", "rm", _CONTAINER_ID],
    ]
    assert calls[1][1] == dev._STOP_COMMAND_TIMEOUT
    assert calls[-1][0][:2] == ["docker", "run"]
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert waits == [(dev._DEFAULT_STARTUP_TIMEOUT, None)]
    assert "Recreating drifted container" in capsys.readouterr().err


def test_logs_refuses_drift_but_down_can_remove_owned_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    _private_state(repo_root)
    document = _inspect_document(repo_root)
    _apply_drift(document, "image", repo_root)
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["container", "inspect"]:
            return _completed(command, stdout=json.dumps([document]))
        return _completed(command)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    assert dev.main(["logs"], repo_root=repo_root) == 1
    assert "refusing to reuse drifted" in capsys.readouterr().err
    assert not any("logs" in command for command in commands)

    assert dev.main(["down"], repo_root=repo_root) == 0
    assert commands[-2:] == [
        [
            "docker",
            "container",
            "stop",
            "--timeout",
            str(dev._STOP_GRACE_SECONDS),
            _CONTAINER_ID,
        ],
        ["docker", "container", "rm", _CONTAINER_ID],
    ]


@pytest.mark.parametrize("stdout", ["not json", "[]", "[{}, {}]"])
def test_inspect_metadata_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stdout: str,
) -> None:
    repo_root = _repository(tmp_path)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda command, **_kwargs: _completed(command, stdout=stdout),
    )

    assert dev.main(["status"], repo_root=repo_root) == 1
    assert "docker returned" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["--force", "bad/name", "bad name", "", "a" * 64])
def test_refuses_unsafe_container_names(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setattr(dev, "CONTAINER_NAME", name)

    with pytest.raises(dev.DevLoopError, match="unsafe development container name"):
        dev._container_name()


def test_refuses_relative_comma_and_symlink_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unsafe paths must fail before docker"),
    )

    assert dev.main(["check"], repo_root=Path("relative")) == 1
    assert "absolute path" in capsys.readouterr().err

    comma_root = tmp_path / "comma,repo"
    comma_root.mkdir()
    _repository(comma_root)
    assert dev.main(["check"], repo_root=comma_root) == 1
    assert "contains ','" in capsys.readouterr().err

    real_component = tmp_path / "real-component"
    real_component.mkdir()
    (real_component / "manifest.json").write_text("{}", encoding="utf-8")
    symlink_root = tmp_path / "symlink-repo"
    (symlink_root / "custom_components").mkdir(parents=True)
    (symlink_root / "custom_components" / "codex_voice").symlink_to(real_component)
    assert dev.main(["check"], repo_root=symlink_root) == 1
    assert "non-symlink directory" in capsys.readouterr().err


def test_refuses_symlink_state_before_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    (repo_root / ".ha-dev").symlink_to(real_state, target_is_directory=True)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unsafe state must fail before docker"),
    )

    assert dev.main(["up"], repo_root=repo_root) == 1
    assert "must not be a symlink" in capsys.readouterr().err


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o710, 0o701])
def test_refuses_state_permissions_for_group_or_other_users(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mode: int,
) -> None:
    repo_root = _repository(tmp_path)
    state = _private_state(repo_root)
    state.chmod(mode)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("insecure state must fail before docker"),
    )

    assert dev.main(["up"], repo_root=repo_root) == 1
    assert "must have mode 0700" in capsys.readouterr().err


def test_refuses_state_not_owned_by_current_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    state = _private_state(repo_root)
    monkeypatch.setattr(dev.os, "geteuid", lambda: state.stat().st_uid + 1)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unowned state must fail before docker"),
    )

    assert dev.main(["up"], repo_root=repo_root) == 1
    assert "must be owned by the current user" in capsys.readouterr().err


def test_down_timeout_does_not_force_or_remove_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["container", "inspect"]:
            return _completed(command, stdout=_owned_inspect_output(repo_root))
        if command[1:3] == ["container", "stop"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return _completed(command)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    assert dev.main(["down"], repo_root=repo_root) == 1
    assert "did not finish within 15 seconds" in capsys.readouterr().err
    assert not any(command[1:3] == ["container", "rm"] for command in commands)
    assert not any("--force" in command for command in commands)


def test_down_skips_stop_for_exited_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = _repository(tmp_path)
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["container", "inspect"]:
            return _completed(
                command,
                stdout=_owned_inspect_output(repo_root, "exited"),
            )
        return _completed(command)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    assert dev.main(["down"], repo_root=repo_root) == 0
    assert commands[-1] == ["docker", "container", "rm", _CONTAINER_ID]
    assert not any(command[1:3] == ["container", "stop"] for command in commands)


def test_state_creation_error_is_reported_from_mocked_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = _repository(tmp_path)
    paths = dev._dev_paths(repo_root, require_component=True)
    original_mkdir = Path.mkdir

    def denied_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == paths.state:
            raise PermissionError("read-only test filesystem")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied_mkdir)

    with pytest.raises(dev.DevLoopError, match=r"cannot create.*read-only"):
        dev._ensure_state_directory(paths)


def test_docker_failure_and_missing_binary_are_concise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda command, **_kwargs: _completed(
            command, 125, stderr="daemon detail\nfinal failure\n"
        ),
    )
    assert dev.main(["check"], repo_root=repo_root) == 1
    error = capsys.readouterr().err
    assert "docker command failed (125): final failure" in error
    assert "daemon detail" not in error

    def missing_binary(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(dev.subprocess, "run", missing_binary)
    assert dev.main(["check"], repo_root=repo_root) == 1
    assert "docker is not installed" in capsys.readouterr().err


class _Response:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self.body = body
        self.closed = False
        self.read_limits: list[int] = []

    def getcode(self) -> int:
        return self.status

    def read(self, amount: int) -> bytes:
        self.read_limits.append(amount)
        return self.body

    def close(self) -> None:
        self.closed = True


def test_restart_bad_token_fails_preflight_before_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    secret = "bad-local-dev-token"
    error = HTTPError(dev._CONFIG_URL, 401, "Unauthorized", {}, None)
    monkeypatch.setenv(dev._DEV_TOKEN_ENV, secret)
    monkeypatch.setattr(
        dev, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    )
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("preflight must fail before Docker"),
    )

    assert dev.main(["restart"], repo_root=repo_root) == 1
    output = capsys.readouterr()
    assert "was rejected" in output.err
    assert secret not in output.err
    assert output.out == ""
    assert not (repo_root / ".ha-dev").exists()
    assert error.closed is True


def test_restart_non_running_preflight_does_not_restart_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _repository(tmp_path)
    response = _Response(200, b'{"state":"NOT_RUNNING"}')
    monkeypatch.setenv(dev._DEV_TOKEN_ENV, "local-dev-token")
    monkeypatch.setattr(dev, "urlopen", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("preflight must fail before Docker"),
    )

    assert dev.main(["restart"], repo_root=repo_root) == 1
    output = capsys.readouterr()
    assert "preflight Core state is NOT_RUNNING" in output.err
    assert output.out == ""
    assert not (repo_root / ".ha-dev").exists()
    assert response.closed is True


def test_health_probe_uses_fixed_loopback_url_and_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []
    response = _Response(200)

    def healthy(url: str, *, timeout: float) -> _Response:
        calls.append((url, timeout))
        return response

    monkeypatch.setattr(dev, "urlopen", healthy)

    assert dev.wait_for_home_assistant(5, token=None) is False

    assert calls == [("http://127.0.0.1:18123/manifest.json", 2.0)]
    assert response.closed is True


def test_health_probe_waits_for_not_running_to_become_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        (
            _Response(200, b'{"state":"NOT_RUNNING"}'),
            _Response(200, b'{"state":"RUNNING"}'),
        )
    )
    requests: list[Request] = []

    def config(request: Request, *, timeout: float) -> _Response:
        assert 0 < timeout <= dev._HEALTH_REQUEST_TIMEOUT
        requests.append(request)
        return next(responses)

    monkeypatch.setattr(dev, "urlopen", config)
    monkeypatch.setattr(dev.time, "sleep", lambda _duration: None)

    assert dev.wait_for_home_assistant(1, token="local-dev-token") is True

    assert len(requests) == 2
    assert all(request.full_url == dev._CONFIG_URL for request in requests)
    assert all(
        request.get_header("Authorization") == "Bearer local-dev-token"
        for request in requests
    )


def test_health_probe_rejects_unauthorized_token_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = HTTPError(dev._CONFIG_URL, 401, "Unauthorized", {}, None)
    secret = "secret-local-dev-token"

    def probe(*_args: object, **_kwargs: object) -> _Response:
        raise error

    monkeypatch.setattr(dev, "urlopen", probe)

    with pytest.raises(dev.DevLoopError, match="was rejected") as raised:
        dev.wait_for_home_assistant(1, token=secret)

    assert error.closed is True
    assert secret not in str(raised.value)


def test_health_wait_is_bounded_when_network_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    attempts: list[float] = []
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(duration: float) -> None:
        nonlocal now
        sleeps.append(duration)
        now += duration

    def unavailable(_request: object, *, timeout: float) -> _Response:
        attempts.append(timeout)
        raise URLError("not ready")

    monkeypatch.setattr(dev.time, "monotonic", monotonic)
    monkeypatch.setattr(dev.time, "sleep", sleep)
    monkeypatch.setattr(dev, "urlopen", unavailable)

    with pytest.raises(dev.DevLoopError, match=r"within 1.2 seconds"):
        dev.wait_for_home_assistant(1.2, token="local-dev-token")

    assert len(attempts) == 3
    assert all(0 < timeout <= dev._HEALTH_REQUEST_TIMEOUT for timeout in attempts)
    assert attempts[-1] < attempts[0]
    assert sum(sleeps) == pytest.approx(1.2)


def test_config_probe_uses_bounded_json_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(200, b'{"state":"RUNNING","other":"allowed"}')
    calls: list[tuple[Request, float]] = []

    def probe(request: Request, *, timeout: float) -> _Response:
        calls.append((request, timeout))
        return response

    monkeypatch.setattr(dev, "urlopen", probe)

    assert dev._config_state("local-dev-token", timeout=1.25) == "RUNNING"
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "http://127.0.0.1:18123/api/config"
    assert request.get_header("Authorization") == "Bearer local-dev-token"
    assert timeout == 1.25
    assert response.read_limits == [dev._MAX_CONFIG_RESPONSE_BYTES + 1]
    assert response.closed is True


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not json", "malformed JSON"),
        (b"[]", "malformed state"),
        (b'{"state":1}', "malformed state"),
        (b"x" * (dev._MAX_CONFIG_RESPONSE_BYTES + 1), "oversized JSON"),
    ],
)
def test_config_probe_rejects_malformed_or_oversized_output(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    message: str,
) -> None:
    response = _Response(200, body)
    monkeypatch.setattr(dev, "urlopen", lambda _request, **_kwargs: response)

    with pytest.raises(dev.DevLoopError, match=message):
        dev._config_state("local-dev-token", timeout=1)
    assert response.closed is True


def test_config_probe_rejects_non_200_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(503, b'{"state":"RUNNING"}')
    monkeypatch.setattr(dev, "urlopen", lambda _request, **_kwargs: response)
    with pytest.raises(dev.DevLoopError, match="HTTP 503"):
        dev._config_state("local-dev-token", timeout=1)
    assert response.closed is True


@pytest.mark.parametrize("value", ["", " token", "token ", "bad\nvalue", "x" * 4097])
def test_dev_token_rejects_empty_unbounded_or_malformed_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(dev._DEV_TOKEN_ENV, value)
    with pytest.raises(dev.DevLoopError, match="empty or malformed") as raised:
        dev._dev_token()
    assert str(raised.value) == "HA_CODEX_DEV_HASS_TOKEN is empty or malformed"


def test_dev_token_is_optional_and_validated_without_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(dev._DEV_TOKEN_ENV, raising=False)
    assert dev._dev_token() is None
    monkeypatch.setenv(dev._DEV_TOKEN_ENV, "local-dev-token")
    assert dev._dev_token() == "local-dev-token"


@pytest.mark.parametrize("value", ["0", "-1", "301", "nan", "inf"])
def test_cli_rejects_unbounded_startup_timeout(value: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        dev.main(["up", "--timeout", value])
