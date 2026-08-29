from __future__ import annotations

import sys
from pathlib import Path

import pytest

from device.thirdreality.deploy import prepare_mic_gain_boot as installer

_ROOT = Path(__file__).parents[2]
_DEPLOY = _ROOT / "device" / "thirdreality" / "deploy"


def test_mic_gain_hook_round_trips_without_overwriting_unknown_file() -> None:
    installed, changed = installer.render_install(None)

    assert changed
    assert installed == installer.MIC_GAIN_BOOT_SCRIPT
    assert installer.render_install(installed) == (installed, False)
    assert installer.render_remove(installed) == (None, True)
    assert installer.render_remove(None) == (None, False)

    with pytest.raises(installer.DeploymentError, match="unknown contents"):
        installer.render_install("#!/bin/sh\nexit 0\n")
    with pytest.raises(installer.DeploymentError, match="not installer-owned"):
        installer.render_remove("#!/bin/sh\nexit 0\n")


def test_reviewable_init_asset_exactly_matches_installer_payload() -> None:
    asset = (_DEPLOY / "init" / installer.HOOK_NAME).read_text()

    assert asset == installer.MIC_GAIN_BOOT_SCRIPT
    assert asset.startswith("#!/bin/sh\n")
    assert '/usr/bin/amixer -c 0 cset numid=7 "${MIC_GAIN}%"' in asset
    assert "/usr/bin/pulseaudio" not in asset


def test_hook_reads_bounded_integer_preference_with_vendor_default() -> None:
    hook = installer.MIC_GAIN_BOOT_SCRIPT

    assert "SOUND_CONF=/data/conf/sound.json" in hook
    assert "DEFAULT_MIC_GAIN=30" in hook
    assert 'type == "object" and has("mic_gain")' in hook
    assert ".mic_gain // 30" not in hook
    assert "/usr/bin/logger -t codex-mic-gain" in hook
    assert "select(. >= 0 and . <= 100)" in hook
    assert "select(. == floor)" in hook
    assert "if . == 0 then 0 else floor end" in hook
    assert '[ ! -L "$SOUND_CONF" ]' in hook
    assert hook.index("MIC_GAIN=$(read_mic_gain)") < hook.index("amixer -c 0 cset")
    assert hook.index("amixer -c 0 cset") < hook.rindex("/usr/bin/logger")


def test_installer_requires_exact_early_init_names_and_sibling_order(
    tmp_path: Path,
) -> None:
    init_dir = tmp_path / "init.d"

    installer._validate_paths(
        init_dir / installer.HOOK_NAME,
        init_dir / installer.PULSEAUDIO_INIT_NAME,
    )
    with pytest.raises(installer.DeploymentError, match="must end"):
        installer._validate_paths(
            init_dir / "S51codex-mic-gain",
            init_dir / installer.PULSEAUDIO_INIT_NAME,
        )
    with pytest.raises(installer.DeploymentError, match="siblings"):
        installer._validate_paths(
            init_dir / installer.HOOK_NAME,
            tmp_path / "other" / installer.PULSEAUDIO_INIT_NAME,
        )

    assert installer.HOOK_NAME < installer.PULSEAUDIO_INIT_NAME


def test_check_validates_pinned_boot_before_inspecting_hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def validate(*_args: object) -> None:
        calls.append("boot")

    def read(_path: Path) -> tuple[None, None]:
        calls.append("hook")
        return None, None

    monkeypatch.setattr(installer, "_validate_pinned_boot", validate)
    monkeypatch.setattr(installer, "_read_optional_hook", read)
    monkeypatch.setattr(sys, "argv", ["prepare_mic_gain_boot.py", "check"])

    assert installer.main() == 0
    assert calls == ["boot", "hook"]
    assert capsys.readouterr().out == "mic-gain boot hook is ready\n"


def test_remove_remains_available_when_firmware_boot_files_change(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    validated_paths: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        installer,
        "_validate_pinned_boot",
        lambda *_args: pytest.fail("remove must not depend on pinned boot files"),
    )
    monkeypatch.setattr(
        installer,
        "_validate_paths",
        lambda hook, pulse: validated_paths.append((hook, pulse)),
    )
    monkeypatch.setattr(
        installer,
        "_read_optional_hook",
        lambda _path: (installer.MIC_GAIN_BOOT_SCRIPT, None),
    )
    monkeypatch.setattr(sys, "argv", ["prepare_mic_gain_boot.py", "remove"])

    assert installer.main() == 0
    assert validated_paths == [
        (installer.DEFAULT_HOOK_PATH, installer.DEFAULT_PULSEAUDIO_INIT_PATH)
    ]
    assert capsys.readouterr().out == ("dry run: mic-gain boot hook would be removed\n")


def test_apply_installs_only_after_explicit_root_authorization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed: list[tuple[Path, str]] = []
    monkeypatch.setattr(installer, "_validate_pinned_boot", lambda *_args: None)
    monkeypatch.setattr(installer, "_read_optional_hook", lambda _path: (None, None))
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        installer,
        "_atomic_install",
        lambda path, contents: installed.append((path, contents)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["prepare_mic_gain_boot.py", "install", "--apply"],
    )

    assert installer.main() == 0
    assert installed == [(installer.DEFAULT_HOOK_PATH, installer.MIC_GAIN_BOOT_SCRIPT)]
    assert capsys.readouterr().out == (
        "mic-gain boot hook installed; no service was restarted\n"
    )


def test_apply_refuses_non_root_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "_validate_pinned_boot", lambda *_args: None)
    monkeypatch.setattr(installer, "_read_optional_hook", lambda _path: (None, None))
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prepare_mic_gain_boot.py", "install", "--apply"],
    )

    with pytest.raises(installer.DeploymentError, match="requires root"):
        installer.main()


def test_helper_cannot_change_live_audio_services_or_adb() -> None:
    helper = (_DEPLOY / "prepare_mic_gain_boot.py").read_text().lower()

    assert "subprocess" not in helper
    for forbidden in (
        "pactl",
        "adb tcpip",
        "stop adbd",
        "service.adb.tcp.port",
        "systemctl",
        "reboot",
    ):
        assert forbidden not in helper


def test_release_archive_packages_mic_gain_deployment_assets_executable() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "mkdir -p release/thirdreality-realtime/deploy/init" in workflow
    assert (
        "install -m 0755 device/thirdreality/deploy/prepare_mic_gain_boot.py \\\n"
        "            release/thirdreality-realtime/deploy/prepare_mic_gain_boot.py"
        in workflow
    )
    assert (
        "install -m 0755 device/thirdreality/deploy/init/S49codex-mic-gain \\\n"
        "            release/thirdreality-realtime/deploy/init/S49codex-mic-gain"
        in workflow
    )
