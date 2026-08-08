"""Runtime configuration for the bridge."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

DEFAULT_PERMISSION_PROFILE = "ha-voice-minimal"
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


@dataclass(slots=True)
class BridgeConfig:
    """Configuration shared by the HTTP service and Codex child process."""

    bearer_token: str
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
    silence_ms: int = 1_000

    def __post_init__(self) -> None:
        if not self.bearer_token:
            raise ValueError("bearer_token must not be empty")
        if not 0 < self.port < 65_536:
            raise ValueError("port must be between 1 and 65535")
        if self.realtime_version not in {"v1", "v3"}:
            raise ValueError("realtime_version must be v1 or v3 for WebRTC")
        if not self.codex_command:
            raise ValueError("codex_command must not be empty")
        if not self.permission_profile:
            raise ValueError("permission_profile must not be empty")

    @classmethod
    def from_env(cls) -> BridgeConfig:
        """Load configuration from environment variables.

        ``HA_CODEX_BRIDGE_TOKEN`` is deliberately mandatory: binding an
        unauthenticated process that can use a ChatGPT session is unsafe.
        """

        token = os.environ.get("HA_CODEX_BRIDGE_TOKEN", "")
        command_text = os.environ.get("CODEX_APP_SERVER_COMMAND")
        command = (
            tuple(shlex.split(command_text)) if command_text else DEFAULT_CODEX_COMMAND
        )
        return cls(
            bearer_token=token,
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
            silence_ms=int(os.environ.get("HA_CODEX_TRANSCRIBE_SILENCE_MS", "1000")),
        )
