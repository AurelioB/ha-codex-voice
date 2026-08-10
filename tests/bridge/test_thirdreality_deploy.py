from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from device.thirdreality.deploy import prepare_pulseaudio_aec
from device.thirdreality.deploy.prepare_pulseaudio_aec import (
    ADRIAN_AEC_BLOCK,
    AEC_BLOCK,
    AEC_SINK_NAME,
    AEC_SOURCE_NAME,
    BEGIN_MARKER,
    DEFAULT_AEC_METHOD,
    DEFAULT_AEC_SINK_VOLUME_PERCENT,
    MAX_AEC_SINK_VOLUME_PERCENT,
    MIN_AEC_SINK_VOLUME_PERCENT,
    SPEEX_AEC_BLOCK,
    SUPPORTED_AEC_METHODS,
    DeploymentError,
    aec_block,
    render_install,
    render_remove,
)

_ROOT = Path(__file__).parents[2]
_DEPLOY = _ROOT / "device" / "thirdreality" / "deploy"
_PINNED_DEFAULT_PA = """#!/usr/bin/pulseaudio -nF
.include /etc/pulse/default.pa.d
load-module module-alsa-source device=hw:0,2 channels=2 rate=16000
load-module module-alsa-sink device=hw:0,1 channels=2 rate=48000
"""


def _legacy_aec_block(aec_method: str) -> str:
    volume_line = f"\nset-sink-volume {AEC_SINK_NAME} 16384"
    block = aec_block(aec_method)
    assert block.count(volume_line) == 1
    return block.replace(volume_line, "", 1)


def test_aec_installer_appends_after_pinned_masters_and_round_trips() -> None:
    installed, changed = render_install(_PINNED_DEFAULT_PA)

    assert changed
    assert installed.index(BEGIN_MARKER) > installed.index("module-alsa-sink")
    assert installed.endswith(f"{AEC_BLOCK}\n")
    assert render_install(installed) == (installed, False)
    assert render_remove(installed) == (_PINNED_DEFAULT_PA, True)
    assert render_remove(_PINNED_DEFAULT_PA) == (_PINNED_DEFAULT_PA, False)


def test_aec_installer_preserves_existing_trailing_newlines_exactly() -> None:
    original = f"{_PINNED_DEFAULT_PA}\n\n"

    installed, changed = render_install(original)

    assert changed
    assert render_remove(installed) == (original, True)


@pytest.mark.parametrize("aec_method", SUPPORTED_AEC_METHODS)
def test_aec_installer_round_trips_pinned_file_without_final_newline(
    aec_method: str,
) -> None:
    original = _PINNED_DEFAULT_PA.rstrip("\n")

    installed, changed = render_install(original, aec_method)

    assert changed
    assert installed.endswith(f"{aec_block(aec_method)}\n")
    assert render_remove(installed) == (original, True)


@pytest.mark.parametrize("aec_method", SUPPORTED_AEC_METHODS)
def test_aec_installer_round_trips_each_supported_method(aec_method: str) -> None:
    installed, changed = render_install(_PINNED_DEFAULT_PA, aec_method)

    assert changed
    assert installed.endswith(f"{aec_block(aec_method)}\n")
    assert render_install(installed, aec_method) == (installed, False)
    assert render_remove(installed) == (_PINNED_DEFAULT_PA, True)


def test_aec_blocks_cover_every_supported_method_and_volume() -> None:
    blocks: set[str] = set()

    for aec_method in SUPPORTED_AEC_METHODS:
        for volume_percent in range(
            MIN_AEC_SINK_VOLUME_PERCENT, MAX_AEC_SINK_VOLUME_PERCENT + 1
        ):
            block = aec_block(aec_method, volume_percent)
            installed = f"{_PINNED_DEFAULT_PA}\n{block}\n"
            blocks.add(block)

            assert render_install(installed, aec_method, volume_percent) == (
                installed,
                False,
            )
            assert render_remove(installed) == (_PINNED_DEFAULT_PA, True)

    assert len(blocks) == len(SUPPORTED_AEC_METHODS) * 60


@pytest.mark.parametrize(
    ("volume_percent", "expected_raw"),
    [(1, 655), (25, 16_384), (60, 39_321)],
)
def test_aec_sink_startup_volume_uses_exact_pulse_raw_floor(
    volume_percent: int,
    expected_raw: int,
) -> None:
    block_lines = aec_block(DEFAULT_AEC_METHOD, volume_percent).splitlines()
    load_index = next(
        index
        for index, line in enumerate(block_lines)
        if line.startswith("load-module module-echo-cancel ")
    )

    assert block_lines[load_index - 1] == ".fail"
    assert block_lines[load_index + 1] == (
        f"set-sink-volume {AEC_SINK_NAME} {expected_raw}"
    )
    assert expected_raw == 65_536 * volume_percent // 100


@pytest.mark.parametrize("aec_method", ["speex", "adrian"])
def test_aec_installer_requires_explicit_matching_method(aec_method: str) -> None:
    installed, _changed = render_install(_PINNED_DEFAULT_PA, aec_method)

    with pytest.raises(DeploymentError, match=rf"{aec_method}, not requested webrtc"):
        render_install(installed)
    with pytest.raises(DeploymentError, match=rf"webrtc, not requested {aec_method}"):
        render_install(render_install(_PINNED_DEFAULT_PA)[0], aec_method)


def test_aec_installer_requires_explicit_matching_volume() -> None:
    installed, _changed = render_install(_PINNED_DEFAULT_PA, "webrtc", 24)

    with pytest.raises(DeploymentError, match=r"volume 24%, not requested 25%"):
        render_install(installed, "webrtc", 25)
    with pytest.raises(DeploymentError, match=r"volume 25%, not requested 24%"):
        render_install(render_install(_PINNED_DEFAULT_PA)[0], "webrtc", 24)


def test_aec_method_allowlist_keeps_webrtc_as_default() -> None:
    assert DEFAULT_AEC_METHOD == "webrtc"
    assert SUPPORTED_AEC_METHODS == ("webrtc", "speex", "adrian")
    assert DEFAULT_AEC_SINK_VOLUME_PERCENT == 25
    assert MIN_AEC_SINK_VOLUME_PERCENT == 1
    assert MAX_AEC_SINK_VOLUME_PERCENT == 60
    assert aec_block() == AEC_BLOCK
    assert aec_block("speex") == SPEEX_AEC_BLOCK
    assert aec_block("adrian") == ADRIAN_AEC_BLOCK

    with pytest.raises(DeploymentError, match="unsupported AEC method"):
        aec_block("null")
    with pytest.raises(DeploymentError, match="unsupported AEC method"):
        render_install(_PINNED_DEFAULT_PA, "unknown")


@pytest.mark.parametrize("volume_percent", [0, 61, -1, True, False, 25.0, "25", None])
def test_aec_sink_volume_api_rejects_invalid_bounds_and_types(
    volume_percent: Any,
) -> None:
    with pytest.raises(
        DeploymentError,
        match=r"must be an integer from 1 through 60",
    ):
        aec_block(DEFAULT_AEC_METHOD, volume_percent)
    with pytest.raises(
        DeploymentError,
        match=r"must be an integer from 1 through 60",
    ):
        render_install(_PINNED_DEFAULT_PA, DEFAULT_AEC_METHOD, volume_percent)


@pytest.mark.parametrize("value", ["0", "61", "-1", "true", "25.0", "1_0"])
def test_aec_sink_volume_cli_rejects_invalid_bounds_and_types(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["prepare_pulseaudio_aec.py", "check", "--aec-sink-volume-percent", value],
    )

    with pytest.raises(SystemExit, match="2"):
        prepare_pulseaudio_aec.main()


@pytest.mark.parametrize("volume_percent", [1, 25, 60])
def test_aec_sink_volume_cli_accepts_and_propagates_supported_values(
    volume_percent: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed, _changed = render_install(
        _PINNED_DEFAULT_PA,
        DEFAULT_AEC_METHOD,
        volume_percent,
    )
    monkeypatch.setattr(
        prepare_pulseaudio_aec,
        "_read_root_config",
        lambda _path: (installed, None),
    )
    cli_arguments = ["prepare_pulseaudio_aec.py", "check"]
    if volume_percent != DEFAULT_AEC_SINK_VOLUME_PERCENT:
        cli_arguments.extend(
            ["--aec-sink-volume-percent", str(volume_percent)]
        )
    monkeypatch.setattr(sys, "argv", cli_arguments)

    assert prepare_pulseaudio_aec.main() == 0


@pytest.mark.parametrize(
    "contents",
    [
        "load-module module-alsa-source device=hw:0,2\n",
        "load-module module-alsa-sink device=hw:0,1\n",
        f"{_PINNED_DEFAULT_PA}{BEGIN_MARKER}\n",
        f"{_PINNED_DEFAULT_PA}set-default-source {AEC_SOURCE_NAME}\n",
    ],
)
def test_aec_installer_refuses_unknown_or_partial_topology(contents: str) -> None:
    with pytest.raises(DeploymentError):
        render_install(contents)


@pytest.mark.parametrize("aec_method", SUPPORTED_AEC_METHODS)
def test_aec_remover_refuses_modified_or_non_tail_managed_block(
    aec_method: str,
) -> None:
    installed, _changed = render_install(_PINNED_DEFAULT_PA, aec_method)

    with pytest.raises(DeploymentError, match="modified"):
        render_remove(installed.replace(AEC_SINK_NAME, "other_sink", 1))
    with pytest.raises(DeploymentError, match="modified"):
        render_remove(f"{installed}# later local change\n")


@pytest.mark.parametrize("aec_method", SUPPORTED_AEC_METHODS)
def test_aec_legacy_block_requires_remove_then_reinstall_migration(
    aec_method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_installed = f"{_PINNED_DEFAULT_PA}\n{_legacy_aec_block(aec_method)}\n"

    with pytest.raises(
        DeploymentError,
        match=r"legacy.*remove it, then reinstall it to migrate",
    ):
        render_install(legacy_installed, aec_method)
    assert render_remove(legacy_installed) == (_PINNED_DEFAULT_PA, True)

    monkeypatch.setattr(
        prepare_pulseaudio_aec,
        "_read_root_config",
        lambda _path: (legacy_installed, None),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["prepare_pulseaudio_aec.py", "check", "--aec-method", aec_method],
    )
    with pytest.raises(
        DeploymentError,
        match=r"legacy.*remove it, then reinstall it to migrate",
    ):
        prepare_pulseaudio_aec.main()


def test_reviewable_pulse_fragment_exactly_matches_installer_block() -> None:
    fragment = (_DEPLOY / "pulse" / "codex-echo-cancel.pa").read_text()
    speex_fragment = (_DEPLOY / "pulse" / "codex-echo-cancel-speex.pa").read_text()
    adrian_fragment = (_DEPLOY / "pulse" / "codex-echo-cancel-adrian.pa").read_text()

    assert fragment == f"{AEC_BLOCK}\n"
    assert speex_fragment == f"{SPEEX_AEC_BLOCK}\n"
    assert adrian_fragment == f"{ADRIAN_AEC_BLOCK}\n"
    assert ".fail\nload-module module-echo-cancel " in fragment
    assert (
        "aec_method=webrtc use_master_format=1\n"
        f"set-sink-volume {AEC_SINK_NAME} 16384\n"
    ) in fragment
    assert f"set-default-source {AEC_SOURCE_NAME}" in fragment
    assert f"set-default-sink {AEC_SINK_NAME}" in fragment


def test_deployment_helper_cannot_mutate_live_volume_services_or_adb() -> None:
    helper = (_DEPLOY / "prepare_pulseaudio_aec.py").read_text().lower()

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
