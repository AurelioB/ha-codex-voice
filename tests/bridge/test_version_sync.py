import json
import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).parents[2]


def test_project_versions_stay_synchronized() -> None:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    version = pyproject["project"]["version"]
    manifest = json.loads(
        (_ROOT / "custom_components" / "codex_voice" / "manifest.json").read_text()
    )
    lock = tomllib.loads((_ROOT / "uv.lock").read_text())
    project_package = next(
        package
        for package in lock["package"]
        if package["name"] == "ha-codex-voice-development"
    )

    assert manifest["version"] == version
    assert project_package["version"] == version
    for relative_path in ("bridge/app_server.py", "scripts/probe_webrtc.py"):
        source = (_ROOT / relative_path).read_text()
        client_versions = re.findall(r'"version": "([0-9]+\.[0-9]+\.[0-9]+)"', source)
        assert client_versions == [version]
