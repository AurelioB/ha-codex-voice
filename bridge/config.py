"""Runtime configuration for the bridge."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_PERMISSION_PROFILE = "ha-voice-minimal"
DEFAULT_REALTIME_MODEL = "gpt-live-1-codex"
DEFAULT_CODEX_COMMAND = (
    "codex",
    "app-server",
    "--enable",
    "realtime_conversation",
    "--disable",
    "shell_tool",
    "-c",
    f'default_permissions="{DEFAULT_PERMISSION_PROFILE}"',
    "-c",
    (
        f"permissions.{DEFAULT_PERMISSION_PROFILE}.description="
        '"Least-privilege Home Assistant voice profile."'
    ),
    "-c",
    f'permissions.{DEFAULT_PERMISSION_PROFILE}.extends=":read-only"',
    "-c",
    (
        f"permissions.{DEFAULT_PERMISSION_PROFILE}.filesystem="
        '{":root"="deny",":minimal"="read",":tmpdir"="deny",'
        '":slash_tmp"="deny"}'
    ),
    "-c",
    f"permissions.{DEFAULT_PERMISSION_PROFILE}.network.enabled=false",
    "-c",
    'shell_environment_policy.inherit="none"',
    "-c",
    'history.persistence="none"',
    "-c",
    'cli_auth_credentials_store="file"',
    "-c",
    'web_search="disabled"',
    "-c",
    "tools.web_search=false",
    "-c",
    "tools.view_image=false",
    "-c",
    "features.remote_plugin=false",
    "-c",
    "features.skill_mcp_dependency_install=false",
    "-c",
    "mcp_servers={}",
    "-c",
    "plugins={}",
    "-c",
    "apps={}",
    "-c",
    "hooks={}",
    "-c",
    "project_doc_max_bytes=0",
    "--stdio",
)


def _parse_boolean_environment(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of 0/1, false/true, no/yes, off/on")


@dataclass(slots=True)
class BridgeConfig:
    """Configuration shared by the HTTP service and Codex child process."""

    bearer_token: str
    realtime_device_token: str | None = None
    host: str = "127.0.0.1"
    port: int = 8787
    codex_command: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_CODEX_COMMAND
    )
    codex_cwd: str | None = None
    codex_auth_file: str | None = None
    permission_profile: str = DEFAULT_PERMISSION_PROFILE
    request_timeout: float = 90.0
    transcript_timeout: float = 90.0
    synthesis_timeout: float = 90.0
    realtime_version: str = "v3"
    realtime_model: str = DEFAULT_REALTIME_MODEL
    silence_ms: int = 0
    live_fragment_quiet_seconds: float = 2.0
    realtime_log_transcripts: bool = False
    agent_url: str | None = None
    agent_token: str | None = field(default=None, repr=False)
    agent_announce_token: str | None = field(default=None, repr=False)
    agent_room: str = "home"
    agent_recall_timeout: float = 8.0
    agent_task_timeout: float = 35.0

    def __post_init__(self) -> None:
        if not self.bearer_token:
            raise ValueError("bearer_token must not be empty")
        if self.realtime_device_token is not None:
            if not self.realtime_device_token:
                raise ValueError("realtime_device_token must not be empty")
            if self.realtime_device_token == self.bearer_token:
                raise ValueError("realtime_device_token must differ from bearer_token")
        if not 0 < self.port < 65_536:
            raise ValueError("port must be between 1 and 65535")
        if self.realtime_version not in {"v1", "v3"}:
            raise ValueError("realtime_version must be v1 or v3 for WebRTC")
        if not self.realtime_model or len(self.realtime_model) > 128:
            raise ValueError("realtime_model must be a non-empty bounded model name")
        if not self.codex_command:
            raise ValueError("codex_command must not be empty")
        if not self.permission_profile:
            raise ValueError("permission_profile must not be empty")
        if not 0.5 <= self.live_fragment_quiet_seconds <= 2.0:
            raise ValueError("live_fragment_quiet_seconds must be between 0.5 and 2.0")
        if self.agent_url is not None:
            parsed_agent_url = urlsplit(self.agent_url)
            if (
                parsed_agent_url.scheme not in {"http", "https"}
                or not parsed_agent_url.netloc
                or parsed_agent_url.username is not None
                or parsed_agent_url.password is not None
                or len(self.agent_url) > 2_048
            ):
                raise ValueError("agent_url must be a bounded HTTP(S) URL")
        if self.agent_announce_token is not None:
            if not self.agent_announce_token:
                raise ValueError("agent_announce_token must not be empty")
            if self.agent_announce_token in {
                self.bearer_token,
                self.realtime_device_token,
            }:
                raise ValueError(
                    "agent_announce_token must differ from bridge and device tokens"
                )
        if not self.agent_room or len(self.agent_room) > 128:
            raise ValueError("agent_room must be a non-empty bounded value")
        if not 0.5 <= self.agent_recall_timeout <= 40.0:
            raise ValueError("agent_recall_timeout must be between 0.5 and 40 seconds")
        if not 0.5 <= self.agent_task_timeout <= 40.0:
            raise ValueError("agent_task_timeout must be between 0.5 and 40 seconds")

    @classmethod
    def from_env(cls) -> BridgeConfig:
        """Load configuration from environment variables.

        ``HA_CODEX_BRIDGE_TOKEN`` is deliberately mandatory: binding an
        unauthenticated process that can use a ChatGPT session is unsafe.
        """

        token = os.environ.get("HA_CODEX_BRIDGE_TOKEN", "")
        command_text = os.environ.get("CODEX_APP_SERVER_COMMAND")
        codex_binary = os.environ.get("HA_CODEX_BINARY")
        command = (
            tuple(shlex.split(command_text))
            if command_text
            else (
                (codex_binary, *DEFAULT_CODEX_COMMAND[1:])
                if codex_binary
                else DEFAULT_CODEX_COMMAND
            )
        )
        return cls(
            bearer_token=token,
            realtime_device_token=(
                os.environ.get("HA_CODEX_REALTIME_DEVICE_TOKEN") or None
            ),
            host=os.environ.get("HA_CODEX_BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("HA_CODEX_BRIDGE_PORT", "8787")),
            codex_command=command,
            codex_cwd=os.environ.get("HA_CODEX_BRIDGE_CWD") or None,
            codex_auth_file=os.environ.get("HA_CODEX_AUTH_FILE") or None,
            permission_profile=os.environ.get(
                "HA_CODEX_PERMISSION_PROFILE", DEFAULT_PERMISSION_PROFILE
            ),
            request_timeout=float(os.environ.get("HA_CODEX_BRIDGE_TIMEOUT", "90")),
            transcript_timeout=float(
                os.environ.get("HA_CODEX_BRIDGE_TRANSCRIPT_TIMEOUT", "90")
            ),
            synthesis_timeout=float(
                os.environ.get("HA_CODEX_BRIDGE_SYNTHESIS_TIMEOUT", "90")
            ),
            realtime_version=os.environ.get("HA_CODEX_REALTIME_VERSION", "v3"),
            realtime_model=os.environ.get(
                "HA_CODEX_REALTIME_MODEL", DEFAULT_REALTIME_MODEL
            ),
            silence_ms=int(os.environ.get("HA_CODEX_TRANSCRIBE_SILENCE_MS", "0")),
            live_fragment_quiet_seconds=float(
                os.environ.get(
                    "HA_CODEX_TRANSCRIBE_LIVE_FRAGMENT_QUIET_SECONDS",
                    "2.0",
                )
            ),
            realtime_log_transcripts=_parse_boolean_environment(
                "HA_CODEX_REALTIME_LOG_TRANSCRIPTS"
            ),
            agent_url=os.environ.get("HA_CODEX_AGENT_URL") or None,
            agent_token=os.environ.get("HA_CODEX_AGENT_TOKEN") or None,
            agent_announce_token=(
                os.environ.get("HA_CODEX_AGENT_ANNOUNCE_TOKEN") or None
            ),
            agent_room=os.environ.get("HA_CODEX_AGENT_ROOM", "home"),
            agent_recall_timeout=float(
                os.environ.get("HA_CODEX_AGENT_RECALL_TIMEOUT", "8")
            ),
            agent_task_timeout=float(
                os.environ.get("HA_CODEX_AGENT_TASK_TIMEOUT", "35")
            ),
        )
