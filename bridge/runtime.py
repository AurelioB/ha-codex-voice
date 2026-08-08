"""Private runtime isolation for the managed Codex App Server process."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

_CODEX_ENV_ALLOWLIST = frozenset(
    {
        "CURL_CA_BUNDLE",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TZ",
    }
)


def codex_child_environment() -> dict[str, str]:
    """Keep bridge, Home Assistant, and developer credentials out of Codex."""
    return {
        name: value
        for name, value in os.environ.items()
        if name in _CODEX_ENV_ALLOWLIST
    }


def _default_auth_file() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    if configured_home:
        codex_home = Path(configured_home).expanduser()
    else:
        configured_user_home = os.environ.get("HOME")
        if not configured_user_home:
            raise ValueError(
                "HOME or CODEX_HOME must be set so the managed Codex login can be found"
            )
        codex_home = Path(configured_user_home).expanduser() / ".codex"
    return codex_home / "auth.json"


def _validated_auth_file(configured: str | None) -> Path:
    auth_file = Path(configured).expanduser() if configured else _default_auth_file()
    if not auth_file.is_absolute():
        raise ValueError("HA_CODEX_AUTH_FILE must be an absolute path")
    try:
        resolved = auth_file.resolve(strict=True)
        metadata = resolved.stat()
    except FileNotFoundError as exc:
        raise ValueError(
            "A file-backed managed Codex login is required; expected auth.json at "
            f'{auth_file}. Set cli_auth_credentials_store to "file", run '
            "codex login, or set HA_CODEX_AUTH_FILE."
        ) from exc
    if not resolved.is_file():
        raise ValueError(f"Codex auth path is not a regular file: {resolved}")
    if os.name == "posix":
        effective_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
        if metadata.st_uid != effective_uid:
            raise ValueError("Codex auth.json must be owned by the bridge user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError(
                "Codex auth.json must not be accessible by group or others"
            )
        if not metadata.st_mode & stat.S_IWUSR or not os.access(resolved, os.W_OK):
            raise ValueError(
                "Codex auth.json must be writable for managed token refresh"
            )
    return resolved


class IsolatedCodexRuntime:
    """Own a temporary Codex home containing only a link to managed OAuth state."""

    def __init__(self, auth_file: str | None = None) -> None:
        source_auth = _validated_auth_file(auth_file)
        self._temporary = tempfile.TemporaryDirectory(prefix="ha-codex-voice-home-")
        try:
            self.root = Path(self._temporary.name)
            private_home = self.root / "home"
            private_codex_home = self.root / "codex"
            private_cache = self.root / "cache"
            private_config = self.root / "config"
            private_data = self.root / "data"
            private_tmp = self.root / "tmp"
            for directory in (
                private_home,
                private_codex_home,
                private_cache,
                private_config,
                private_data,
                private_tmp,
            ):
                directory.mkdir(mode=0o700)
            (private_codex_home / "auth.json").symlink_to(source_auth)
            self.environment = codex_child_environment()
            self.environment.update(
                {
                    "CODEX_HOME": str(private_codex_home),
                    "HOME": str(private_home),
                    "TMPDIR": str(private_tmp),
                    "XDG_CACHE_HOME": str(private_cache),
                    "XDG_CONFIG_HOME": str(private_config),
                    "XDG_DATA_HOME": str(private_data),
                }
            )
        except BaseException:
            self._temporary.cleanup()
            raise

    def cleanup(self) -> None:
        """Remove private state and the auth link without touching its target."""
        self._temporary.cleanup()
