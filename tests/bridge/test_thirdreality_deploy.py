from __future__ import annotations

from pathlib import Path

import pytest

from device.thirdreality.deploy.prepare_pulseaudio_aec import (
    AEC_BLOCK,
    AEC_SINK_NAME,
    AEC_SOURCE_NAME,
    BEGIN_MARKER,
    DeploymentError,
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


def test_aec_remover_refuses_modified_or_non_tail_managed_block() -> None:
    installed, _changed = render_install(_PINNED_DEFAULT_PA)

    with pytest.raises(DeploymentError, match="modified"):
        render_remove(installed.replace(AEC_SINK_NAME, "other_sink", 1))
    with pytest.raises(DeploymentError, match="modified"):
        render_remove(f"{installed}# later local change\n")


def test_reviewable_pulse_fragment_exactly_matches_installer_block() -> None:
    fragment = (_DEPLOY / "pulse" / "codex-echo-cancel.pa").read_text()

    assert fragment == f"{AEC_BLOCK}\n"
    assert ".fail\nload-module module-echo-cancel " in fragment
    assert f"set-default-source {AEC_SOURCE_NAME}" in fragment
    assert f"set-default-sink {AEC_SINK_NAME}" in fragment


def test_deployment_helper_cannot_restart_services_change_volume_or_adb() -> None:
    helper = (_DEPLOY / "prepare_pulseaudio_aec.py").read_text().lower()

    assert "subprocess" not in helper
    for forbidden in (
        "adb tcpip",
        "stop adbd",
        "service.adb.tcp.port",
        "set-sink-volume",
        "systemctl",
        "reboot",
    ):
        assert forbidden not in helper
