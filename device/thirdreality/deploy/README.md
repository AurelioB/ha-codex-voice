# ThirdReality full-duplex AEC deployment assets

These assets are an opt-in preparation step for the pinned ThirdReality
`1.01.07`/upstream `v1.1.7` image. They do not deploy themselves. The helper
never restarts PulseAudio or the voice service, never changes speaker volume,
and never changes or disables TCP ADB on port 5555.

The device starts PulseAudio with `--disallow-module-loading`. Its
`/etc/pulse/default.pa` includes `default.pa.d` **before** the two raw ALSA
masters are created, so copying the fragment into `default.pa.d` is invalid.
The managed block must be appended after both of these pinned definitions:

```text
load-module module-alsa-source ... device=hw:0,2 ...
load-module module-alsa-sink ... device=hw:0,1 ...
```

[`prepare_pulseaudio_aec.py`](prepare_pulseaudio_aec.py) verifies those
definitions and appends the exact
[`pulse/codex-echo-cancel.pa`](pulse/codex-echo-cancel.pa) block. `check`,
`install`, and `remove` are content-safe and idempotent; install/remove remain
dry runs without `--apply`. Installation requires an existing backup parent,
creates a mode-0600 backup without overwriting a different file, and atomically
replaces only a root-owned, non-group/world-writable regular `default.pa`.

Example device-side sequence after copying the reviewed assets from a pinned
release:

```sh
python3 prepare_pulseaudio_aec.py check
python3 prepare_pulseaudio_aec.py install
python3 prepare_pulseaudio_aec.py install --apply
```

Do not restart anything until the exact resulting tail, backup, ownership, and
modes have been reviewed and TCP ADB port 5555 has been verified reachable.
Then use the device's normal service manager during an approved maintenance
window; do not use `pactl load-module`, because module loading is disabled for
the running server. After restart, verify exact defaults and the static module:

```sh
pactl get-default-source
pactl get-default-sink
pactl list short modules
pactl list short sources
pactl --format=json list source-outputs
pactl get-sink-volume codex_echo_cancel_sink
```

The source and sink must be `codex_echo_cancel_source` and
`codex_echo_cancel_sink`, and the static `module-echo-cancel` instance must use
`aec_method=webrtc` and `use_master_format=1` with the reviewed raw masters.
The realtime client repeats these checks before opening the bridge socket. It
additionally requires an uncorked `protocol-native.c` capture stream owned by
its exact process PID (the vendor recorder that was opened before wake) to
reference the AEC source index, and requires every reported AEC sink channel
to be no louder than `aec_test_volume_percent`. It fails closed if any route or
channel is wrong. Playback is additionally pinned to the AEC sink, and each full-duplex
`paplay` stream starts with a fixed linear volume no greater than that
configured percentage. Before every `speaking.started`, the client re-reads
all AEC sink channels and fails the response closed if any exceeds the same
ceiling; the startup preflight alone is not treated as a lasting volume claim.
It compares PulseAudio's raw channel units with the exact linear ceiling
(`65536 × percent // 100`), not the rounded percentage printed beside them.
Operators must not raise a live sink or stream during the canary.

For the first acoustic canary, read `aec_test_volume_percent` from the reviewed
root-only realtime config and set the AEC sink to that percentage. The setting
defaults to 25 and configuration validation enforces an absolute range of
1–25; never exceed 25% during AEC qualification. Lower it for a quiet room or
near-field test. Record the pre-test volume separately and restore it only
after echo-rejection, barge-in, wake, normal Assist, and repeated-turn tests
pass. The helper intentionally does not automate volume changes.

For the default canary value, the explicit device-side volume command is:

```sh
pactl set-sink-volume codex_echo_cancel_sink 25%
```

Use a lower validated percentage when configured; never substitute a value
above 25 during qualification.

Only after the static topology and acoustic canary pass should the root-only
realtime configuration enable:

```json
{
  "full_duplex": true,
  "pulse_aec_source": "codex_echo_cancel_source",
  "pulse_aec_sink": "codex_echo_cancel_sink",
  "aec_test_volume_percent": 25
}
```

Rollback first sets `full_duplex` to `false`, then uses a dry run followed by
the explicit removal:

```sh
python3 prepare_pulseaudio_aec.py remove
python3 prepare_pulseaudio_aec.py remove --apply
```

The remover deletes only an exact installer-owned tail and refuses partial,
modified, or duplicated markers. It does not restore the backup automatically
or restart services. Keep the backup until physical acceptance is complete,
and verify TCP ADB port 5555 before and after the controlled restart.
