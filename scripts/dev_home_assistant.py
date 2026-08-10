"""Run a pinned, disposable Home Assistant container for component development."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

IMAGE = "ghcr.io/home-assistant/home-assistant:2026.8.1"
CONTAINER_NAME = "ha-codex-voice-dev"
HOST = "127.0.0.1"
HOST_PORT = 18123
CONTAINER_PORT = 8123

_DOCKER = "docker"
_STATE_DIRECTORY = ".ha-dev"
_COMPONENT_RELATIVE_PATH = Path("custom_components/codex_voice")
_CONTAINER_COMPONENT_PATH = "/config/custom_components/codex_voice"
_OWNER_LABEL = "io.github.aureliob.ha-codex-voice.dev"
_ROOT_LABEL = "io.github.aureliob.ha-codex-voice.dev-root"
_OWNER_LABEL_VALUE = "1"
_DEFAULT_STARTUP_TIMEOUT = 60.0
_MAX_STARTUP_TIMEOUT = 300.0
_HEALTH_POLL_INTERVAL = 0.5
_HEALTH_REQUEST_TIMEOUT = 2.0
_HEALTH_URL = f"http://{HOST}:{HOST_PORT}/manifest.json"
_STOP_GRACE_SECONDS = 10
_STOP_COMMAND_TIMEOUT = _STOP_GRACE_SECONDS + 5
_CONTAINER_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
_CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MISSING_CONTAINER_MESSAGES = ("no such container", "no such object")
_STOPPED_CONTAINER_STATES = {"created", "dead", "exited"}
_KNOWN_CONTAINER_STATES = _STOPPED_CONTAINER_STATES | {
    "paused",
    "removing",
    "restarting",
    "running",
}
_CHECK_SCRIPT = """\
import importlib
import pathlib
import sys

component = pathlib.Path("/config/custom_components/codex_voice")
sources = sorted(component.rglob("*.py"))
if not sources:
    raise SystemExit("no Python sources found in mounted component")
for source in sources:
    compile(source.read_bytes(), str(source), "exec", dont_inherit=True)
sys.path.insert(0, "/config")
importlib.import_module("custom_components.codex_voice")
print(f"compiled {len(sources)} Python files and imported custom_components.codex_voice")
"""


class DevLoopError(RuntimeError):
    """Raised when the local development environment is unsafe or unavailable."""


class ContainerDriftError(DevLoopError):
    """Raised when an owned container is unsafe to reuse."""


@dataclass(frozen=True, slots=True)
class DevPaths:
    """Validated host paths used by the development container."""

    repo_root: Path
    component: Path
    state: Path


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """The state of an owned development container."""

    identifier: str
    status: str
    configuration_error: str | None = None


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _validate_mount_path(path: Path, *, description: str) -> None:
    raw_path = str(path)
    if not path.is_absolute():
        raise DevLoopError(f"{description} must be an absolute path")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw_path):
        raise DevLoopError(f"{description} contains a control character")
    if "," in raw_path:
        raise DevLoopError(
            f"{description} contains ',' and cannot be represented safely as a mount"
        )


def _validate_existing_directory(path: Path, *, description: str) -> None:
    _validate_mount_path(path, description=description)
    if path.is_symlink() or not path.is_dir():
        raise DevLoopError(f"{description} must be a non-symlink directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DevLoopError(f"cannot resolve {description}: {error}") from error
    if resolved != path:
        raise DevLoopError(f"{description} must not traverse symlinks or '..'")


def _dev_paths(repo_root: Path, *, require_component: bool) -> DevPaths:
    _validate_existing_directory(repo_root, description="repository root")
    if repo_root == Path(repo_root.anchor):
        raise DevLoopError("refusing to use a filesystem root as the repository root")

    component = repo_root / _COMPONENT_RELATIVE_PATH
    state = repo_root / _STATE_DIRECTORY
    _validate_mount_path(component, description="component path")
    _validate_mount_path(state, description="development state path")
    if component.parent.parent != repo_root or state.parent != repo_root:
        raise DevLoopError("development paths escaped the repository root")
    if require_component:
        _validate_existing_directory(component, description="component path")
        if not (component / "manifest.json").is_file():
            raise DevLoopError("component path does not contain manifest.json")
    return DevPaths(repo_root=repo_root, component=component, state=state)


def _validate_state_directory(paths: DevPaths) -> None:
    state = paths.state
    if state.is_symlink():
        raise DevLoopError("development state path must not be a symlink")
    _validate_existing_directory(state, description="development state path")
    if state.parent != paths.repo_root:
        raise DevLoopError("development state path escaped the repository root")
    try:
        metadata = state.lstat()
    except OSError as error:
        raise DevLoopError(f"cannot inspect development state: {error}") from error
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o700:
        raise DevLoopError(
            f"development state directory must have mode 0700, not {mode:04o}"
        )
    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is not None and metadata.st_uid != get_effective_uid():
        raise DevLoopError(
            "development state directory must be owned by the current user"
        )


def _ensure_state_directory(paths: DevPaths) -> None:
    state = paths.state
    if state.is_symlink():
        raise DevLoopError("development state path must not be a symlink")
    try:
        state.mkdir(mode=0o700, exist_ok=True)
    except OSError as error:
        raise DevLoopError(
            f"cannot create development state directory: {error}"
        ) from error
    _validate_state_directory(paths)


def _container_name() -> str:
    if _CONTAINER_NAME_PATTERN.fullmatch(CONTAINER_NAME) is None:
        raise DevLoopError("refusing unsafe development container name")
    return CONTAINER_NAME


def _docker_error(result: subprocess.CompletedProcess[str]) -> DevLoopError:
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        detail = detail.splitlines()[-1][:500]
        return DevLoopError(f"docker command failed ({result.returncode}): {detail}")
    return DevLoopError(f"docker command failed with exit code {result.returncode}")


def _run_docker(
    arguments: Sequence[str],
    *,
    capture_output: bool = False,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [_DOCKER, *arguments]
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=capture_output,
            shell=False,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise DevLoopError("docker is not installed or is not on PATH") from error
    except OSError as error:
        raise DevLoopError(f"could not execute docker: {error}") from error
    except subprocess.TimeoutExpired as error:
        timeout_description = f"{timeout:g}" if timeout is not None else "configured"
        raise DevLoopError(
            f"docker command did not finish within {timeout_description} seconds"
        ) from error
    if check and result.returncode != 0:
        raise _docker_error(result)
    return result


def _mapping(value: object, *, description: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DevLoopError(f"docker returned malformed {description}")
    return value


def _validate_container_configuration(
    document: dict[str, object],
    config: dict[str, object],
    paths: DevPaths,
    *,
    status: str,
) -> None:
    if config.get("Image") != IMAGE:
        raise ContainerDriftError(
            "image does not match the pinned Home Assistant image"
        )

    try:
        host_config = _mapping(
            document.get("HostConfig"), description="container host configuration"
        )
        restart_policy = _mapping(
            host_config.get("RestartPolicy"), description="container restart policy"
        )
    except DevLoopError as error:
        raise ContainerDriftError(str(error)) from error

    expected_port_bindings = {
        f"{CONTAINER_PORT}/tcp": [
            {"HostIp": HOST, "HostPort": str(HOST_PORT)},
        ]
    }
    if host_config.get("PortBindings") != expected_port_bindings:
        raise ContainerDriftError(
            "port publishing is not exactly 127.0.0.1:18123->8123/tcp"
        )
    if host_config.get("PublishAllPorts") is not False:
        raise ContainerDriftError("Docker publish-all-ports must be disabled")
    if host_config.get("NetworkMode") != "bridge":
        raise ContainerDriftError("container network mode is not the isolated bridge")
    if (
        restart_policy.get("Name") != "no"
        or restart_policy.get("MaximumRetryCount") != 0
    ):
        raise ContainerDriftError("restart policy is not 'no' with zero retries")

    mounts = document.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) != 2:
        raise ContainerDriftError("container must have exactly two bind mounts")
    observed_mounts: set[tuple[str, str, bool]] = set()
    for mount_value in mounts:
        try:
            mount = _mapping(mount_value, description="container mount")
        except DevLoopError as error:
            raise ContainerDriftError(str(error)) from error
        if mount.get("Type") != "bind":
            raise ContainerDriftError("container mounts must both be bind mounts")
        source = mount.get("Source")
        destination = mount.get("Destination")
        read_write = mount.get("RW")
        if (
            not isinstance(source, str)
            or not isinstance(destination, str)
            or type(read_write) is not bool
        ):
            raise ContainerDriftError("container bind-mount metadata is malformed")
        observed_mounts.add((source, destination, read_write))

    expected_mounts = {
        (str(paths.state), "/config", True),
        (str(paths.component), _CONTAINER_COMPONENT_PATH, False),
    }
    if observed_mounts != expected_mounts or len(observed_mounts) != len(mounts):
        raise ContainerDriftError(
            "bind mounts do not match the writable state and read-only component paths"
        )

    # A second network attachment could expose port 8123 without going through
    # the loopback-only published port, even when PortBindings itself is safe.
    try:
        network_settings = _mapping(
            document.get("NetworkSettings"),
            description="container network settings",
        )
        networks = _mapping(
            network_settings.get("Networks"),
            description="container network attachments",
        )
    except DevLoopError as error:
        raise ContainerDriftError(str(error)) from error
    if set(networks) != {"bridge"}:
        raise ContainerDriftError("container has an unexpected network attachment")

    if status in {"paused", "restarting", "running"}:
        if network_settings.get("Ports") != expected_port_bindings:
            raise ContainerDriftError(
                "active container port binding is not loopback-only"
            )


def _inspect_container(paths: DevPaths) -> ContainerInfo | None:
    name = _container_name()
    result = _run_docker(
        ["container", "inspect", name],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        error_text = (result.stderr or "").lower()
        if any(message in error_text for message in _MISSING_CONTAINER_MESSAGES):
            return None
        raise _docker_error(result)

    try:
        documents = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise DevLoopError("docker returned malformed container metadata") from error
    if not isinstance(documents, list) or len(documents) != 1:
        raise DevLoopError("docker returned an unexpected number of containers")
    document = _mapping(documents[0], description="container metadata")
    config = _mapping(document.get("Config"), description="container configuration")
    labels = _mapping(config.get("Labels"), description="container labels")
    state = _mapping(document.get("State"), description="container state")

    if labels.get(_OWNER_LABEL) != _OWNER_LABEL_VALUE or labels.get(_ROOT_LABEL) != str(
        paths.repo_root
    ):
        raise DevLoopError(
            f"refusing to operate on unowned container {name!r}; "
            "remove or rename it explicitly with docker"
        )
    identifier = document.get("Id")
    if (
        not isinstance(identifier, str)
        or _CONTAINER_ID_PATTERN.fullmatch(identifier) is None
    ):
        raise DevLoopError("docker returned an unsafe container identifier")
    status = state.get("Status")
    if not isinstance(status, str) or status not in _KNOWN_CONTAINER_STATES:
        raise DevLoopError("docker returned an unsafe container status")

    configuration_error: str | None = None
    try:
        _validate_container_configuration(
            document,
            config,
            paths,
            status=status,
        )
    except ContainerDriftError as error:
        configuration_error = str(error)
    return ContainerInfo(
        identifier=identifier,
        status=status,
        configuration_error=configuration_error,
    )


def _mount(source: Path, destination: str, *, read_only: bool = False) -> str:
    _validate_mount_path(source, description="bind-mount source")
    option = f"type=bind,source={source},target={destination}"
    if read_only:
        option += ",readonly"
    return option


def _startup_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if not 0 < timeout <= _MAX_STARTUP_TIMEOUT:
        raise argparse.ArgumentTypeError(
            f"timeout must be greater than 0 and at most {_MAX_STARTUP_TIMEOUT:g}"
        )
    return timeout


def _log_tail(value: str) -> int:
    try:
        lines = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("tail must be an integer") from error
    if not 1 <= lines <= 10_000:
        raise argparse.ArgumentTypeError("tail must be between 1 and 10000")
    return lines


def wait_for_home_assistant(timeout: float) -> None:
    """Wait up to ``timeout`` seconds for Home Assistant's frontend manifest."""
    if not 0 < timeout <= _MAX_STARTUP_TIMEOUT:
        raise DevLoopError(
            f"startup timeout must be greater than 0 and at most "
            f"{_MAX_STARTUP_TIMEOUT:g} seconds"
        )

    deadline = time.monotonic() + timeout
    last_error = "Home Assistant did not answer"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DevLoopError(
                f"Home Assistant did not become ready within {timeout:g} seconds "
                f"({last_error}); inspect it with the logs command"
            )
        request_timeout = min(_HEALTH_REQUEST_TIMEOUT, remaining)
        try:
            response = urlopen(
                _HEALTH_URL,
                timeout=request_timeout,
            )
            try:
                status = response.getcode()
            finally:
                response.close()
            if status == 200:
                return
            last_error = f"HTTP {status}"
        except HTTPError as error:
            if error.code == 200:
                error.close()
                return
            last_error = f"HTTP {error.code}"
            error.close()
        except (TimeoutError, URLError, OSError) as error:
            last_error = str(error) or type(error).__name__

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        time.sleep(min(_HEALTH_POLL_INTERVAL, remaining))


def _create_container(paths: DevPaths) -> None:
    _run_docker(
        [
            "run",
            "--detach",
            "--name",
            _container_name(),
            "--label",
            f"{_OWNER_LABEL}={_OWNER_LABEL_VALUE}",
            "--label",
            f"{_ROOT_LABEL}={paths.repo_root}",
            "--restart",
            "no",
            "--network",
            "bridge",
            "--publish",
            f"{HOST}:{HOST_PORT}:{CONTAINER_PORT}/tcp",
            "--mount",
            _mount(paths.state, "/config"),
            "--mount",
            _mount(paths.component, _CONTAINER_COMPONENT_PATH, read_only=True),
            IMAGE,
        ]
    )


def _configuration_error(info: ContainerInfo) -> str | None:
    return info.configuration_error


def _require_reusable_container(paths: DevPaths, info: ContainerInfo) -> None:
    if configuration_error := _configuration_error(info):
        raise DevLoopError(
            f"refusing to reuse drifted container {_container_name()!r}: "
            f"{configuration_error}"
        )
    _validate_state_directory(paths)


def _remove_container(info: ContainerInfo) -> None:
    identifier = info.identifier
    if info.status == "paused":
        _run_docker(
            ["container", "unpause", identifier],
            timeout=_STOP_COMMAND_TIMEOUT,
        )
    if info.status not in _STOPPED_CONTAINER_STATES:
        _run_docker(
            [
                "container",
                "stop",
                "--timeout",
                str(_STOP_GRACE_SECONDS),
                identifier,
            ],
            timeout=_STOP_COMMAND_TIMEOUT,
        )
    _run_docker(["container", "rm", identifier])


def _recreate_container(paths: DevPaths, info: ContainerInfo) -> None:
    print(
        f"Recreating drifted container {_container_name()!r}: "
        f"{info.configuration_error}",
        file=sys.stderr,
    )
    _remove_container(info)
    _create_container(paths)


def _up(paths: DevPaths, *, timeout: float) -> None:
    _ensure_state_directory(paths)
    info = _inspect_container(paths)
    if info is None:
        _create_container(paths)
    elif info.configuration_error is not None:
        _recreate_container(paths, info)
    elif info.status in {"created", "exited"}:
        _run_docker(["container", "start", info.identifier])
    elif info.status not in {"running", "restarting"}:
        raise DevLoopError(
            f"container is {info.status!r}; run down and then up to recreate it"
        )
    wait_for_home_assistant(timeout)
    print(f"Home Assistant is ready at http://{HOST}:{HOST_PORT}")


def _restart(paths: DevPaths, *, timeout: float) -> None:
    _ensure_state_directory(paths)
    info = _inspect_container(paths)
    if info is None:
        raise DevLoopError("development container does not exist; run up first")
    if info.configuration_error is not None:
        _recreate_container(paths, info)
    else:
        _run_docker(
            [
                "container",
                "restart",
                "--timeout",
                str(_STOP_GRACE_SECONDS),
                info.identifier,
            ],
            timeout=_STOP_COMMAND_TIMEOUT,
        )
    wait_for_home_assistant(timeout)
    print(f"Home Assistant restarted at http://{HOST}:{HOST_PORT}")


def _check(paths: DevPaths) -> None:
    _run_docker(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            _mount(paths.component, _CONTAINER_COMPONENT_PATH, read_only=True),
            "--entrypoint",
            "python3",
            IMAGE,
            "-c",
            _CHECK_SCRIPT,
        ]
    )


def _status(paths: DevPaths) -> int:
    info = _inspect_container(paths)
    if info is None:
        print("Home Assistant development container is not created")
        return 1
    _require_reusable_container(paths, info)
    suffix = f" at http://{HOST}:{HOST_PORT}" if info.status == "running" else ""
    print(f"Home Assistant development container: {info.status}{suffix}")
    return 0


def _logs(paths: DevPaths, *, follow: bool, tail: int) -> None:
    info = _inspect_container(paths)
    if info is None:
        raise DevLoopError("development container does not exist; run up first")
    _require_reusable_container(paths, info)
    arguments = ["container", "logs", "--tail", str(tail)]
    if follow:
        arguments.append("--follow")
    arguments.append(info.identifier)
    _run_docker(arguments)


def _down(paths: DevPaths) -> None:
    info = _inspect_container(paths)
    if info is None:
        print("Home Assistant development container is already absent")
        return
    _remove_container(info)
    print(f"Removed the development container; state remains in {paths.state}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    up = commands.add_parser("up", help="create or start Home Assistant")
    up.add_argument(
        "--timeout",
        type=_startup_timeout,
        default=_DEFAULT_STARTUP_TIMEOUT,
        help="maximum startup wait in seconds (default: %(default)s)",
    )
    restart = commands.add_parser("restart", help="restart Home Assistant")
    restart.add_argument(
        "--timeout",
        type=_startup_timeout,
        default=_DEFAULT_STARTUP_TIMEOUT,
        help="maximum startup wait in seconds (default: %(default)s)",
    )
    commands.add_parser("check", help="compile and import the mounted component")
    commands.add_parser("status", help="show the container state")
    logs = commands.add_parser("logs", help="show container logs")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("--tail", type=_log_tail, default=200)
    commands.add_parser("down", help="remove the container and preserve local state")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path | None = None,
) -> int:
    """Run one local Home Assistant development command."""
    parser = _parser()
    args = parser.parse_args(argv)
    selected_root = repo_root if repo_root is not None else _default_repo_root()
    require_component = args.command in {"up", "restart", "check"}
    try:
        paths = _dev_paths(selected_root, require_component=require_component)
        if args.command == "up":
            _up(paths, timeout=args.timeout)
        elif args.command == "restart":
            _restart(paths, timeout=args.timeout)
        elif args.command == "check":
            _check(paths)
        elif args.command == "status":
            return _status(paths)
        elif args.command == "logs":
            _logs(paths, follow=args.follow, tail=args.tail)
        elif args.command == "down":
            _down(paths)
    except DevLoopError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
