# ThirdReality microphone and full-duplex AEC deployment assets

These assets are an opt-in preparation step for the pinned ThirdReality
`1.01.07`/upstream `v1.1.7` image. They do not deploy themselves. The helper
scripts never restart PulseAudio or the voice service, never change the running
speaker volume, and never change or disable TCP ADB on port 5555.

The active route is strict-v2 `bridge_pcm` with full duplex, native AEC3,
a 10 dB native baseline with noise-limited adaptive gain, a limiter, moderate
noise suppression, 0 dB transport gain, and a fixed 100% sink/playback anchor.
`paplay` uses 100% relative stream volume and one non-amplifying software stage
gives the physical buttons their full 0–100% range. The dormant `device_webrtc`
sidecar is not required for this deployment.

## Latch configured microphone gain before capture opens

The pinned firmware has a boot-order defect. `S50pulseaudio` opens the PDM
capture device at `hw:0,2`; only later does `S99ha-speaker` run
`setup_env.sh`, which reads `mic_gain` from `/data/conf/sound.json` and calls
`amixer`. Changing that mixer control while capture is open updates the value
reported by `amixer`, but does not update the samples from this codec. The new
gain takes effect only after ALSA capture is reopened. This can make a correctly
configured microphone appear almost silent after every reboot.

[`prepare_mic_gain_boot.py`](prepare_mic_gain_boot.py) installs exactly one
root-owned mode-0755 init script, `/etc/init.d/S49codex-mic-gain`. It reads the
same preference and applies ALSA card 0 control `numid=7` before
`S50pulseaudio`. It does not edit or replace either vendor script. Installation
fails closed unless the init names, sibling ordering, pinned `rcS`, and pinned
`S50pulseaudio` match the reviewed firmware. It also refuses to overwrite any
pre-existing or modified hook.

The preference must be a JSON number that is an integer from 0 through 100.
The hook does not silently clamp an invalid value upward or downward. An
absent, malformed, non-integer, or out-of-range value uses the vendor's safe
30% default and writes a warning to the boot log. On the observed control,
ALSA's standard percentage mapping gives 30% = 14/48, 70% = 34/48, and 100% =
48/48.

After copying the reviewed helper from a pinned release, install it with the
dry-run/apply/check sequence:

```sh
python3 prepare_mic_gain_boot.py check
python3 prepare_mic_gain_boot.py install
python3 prepare_mic_gain_boot.py install --apply
python3 prepare_mic_gain_boot.py check
```

The command intentionally does not touch the live mixer. Review the installed
file, root ownership, mode 0755, `sound.json`, and TCP ADB reachability before
a controlled reboot. The hook's purpose is deterministic ordering on every
boot; a separately controlled PulseAudio stop/start can also latch a new mixer
value by reopening ALSA capture. A voice-only restart cannot. After reboot,
confirm the early-hook log precedes PulseAudio startup, verify
`amixer -c 0 cget numid=7`, then perform a raw microphone peak/RMS recording
and human near-/normal-/far-field canary. The reported mixer value alone does
not prove that capture latched it.

Changing `mic_gain` later updates the stored preference but does not affect
captured samples until ALSA capture reopens. The installed hook guarantees the
new value is applied on the next controlled reboot; an explicitly managed
PulseAudio capture reopen can apply it sooner. Rollback is symmetric:

```sh
python3 prepare_mic_gain_boot.py remove
python3 prepare_mic_gain_boot.py remove --apply
```

Removal is available even if the pinned vendor boot files later drift, but it
deletes only the byte-exact installer-owned hook. No backup restoration is
needed because installation never overwrites an existing file. Removal does
not restart services; the original late vendor behavior returns on the next
boot. Verify TCP ADB port 5555 before and after that reboot.

## Static PulseAudio echo cancellation

The AEC helper writes the explicitly selected AEC sink volume into the managed
PulseAudio startup block so the reviewed value is applied when the AEC sink is
created. On the stock device, the vendor voice process later reapplies the
separate media-player preference from `/data/conf/sound.json`; that
authoritative preference must be set to the same value through Home Assistant
and verified after every restart or reboot.

The device starts PulseAudio with `--disallow-module-loading`. Its
`/etc/pulse/default.pa` includes `default.pa.d` **before** the two raw ALSA
masters are created, so copying the fragment into `default.pa.d` is invalid.
The managed block must be appended after both of these pinned definitions:

```text
load-module module-alsa-source ... device=hw:0,2 ...
load-module module-alsa-sink ... device=hw:0,1 ...
```

[`prepare_pulseaudio_aec.py`](prepare_pulseaudio_aec.py) verifies those
definitions and appends one exact reviewed block selected by `--aec-method`:
[`pulse/codex-echo-cancel.pa`](pulse/codex-echo-cancel.pa) for WebRTC,
[`pulse/codex-echo-cancel-speex.pa`](pulse/codex-echo-cancel-speex.pa) for
Speex, or
[`pulse/codex-echo-cancel-adrian.pa`](pulse/codex-echo-cancel-adrian.pa) for
Adrian. Only `webrtc`, `speex`, and `adrian` are accepted. Omitting the flag
selects WebRTC; the helper does not probe engines or fall back automatically.
`--aec-sink-volume-percent` selects a startup value from 1 through 100 and
defaults to 25. The managed block renders the exact PulseAudio raw value
(`65536 × percent // 100`) immediately after the AEC sink is created.
`check`, `install`, and `remove` are content-safe and idempotent;
install/remove remain dry runs without `--apply`. Installation requires an
existing backup parent, creates a mode-0600 backup without overwriting a
different file, and atomically replaces only a root-owned,
non-group/world-writable regular `default.pa`.

The observed stock `1.01.07`/v1.1.7 image rejects WebRTC and Speex because its
PulseAudio module was built without those engines. Adrian loads with the exact
existing raw masters and `use_master_format=1`, and creates 16 kHz mono AEC
endpoints. Therefore active commands for that stock image must explicitly use
Adrian. Speex applies only to a firmware build that actually compiles it.

Example device-side sequence after copying the reviewed assets from a pinned
release:

```sh
python3 prepare_pulseaudio_aec.py check --aec-method adrian --aec-sink-volume-percent 100
python3 prepare_pulseaudio_aec.py install --aec-method adrian --aec-sink-volume-percent 100
python3 prepare_pulseaudio_aec.py install --aec-method adrian --aec-sink-volume-percent 100 --apply
```

The active full-range reference uses 100 and must pass its worst-case acoustic
qualification at that anchor. A released legacy managed block without a startup volume is
recognized for removal, but is not silently upgraded. Remove it with the dry
run/apply pair documented below, then reinstall with the chosen method and
volume so the change remains auditable.

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
`codex_echo_cancel_sink`, and on the stock image the static
`module-echo-cancel` instance must use `aec_method=adrian` and
`use_master_format=1` with the reviewed raw masters. The root-only realtime
configuration must name the same method and its sink ceiling must match the
managed startup anchor. At 100%, `pactl get-sink-volume` must report raw
`65536` for every channel. A saved 80% user level is separately reported as
`volume_level: 0.8`/`"volume": 80` and is implemented by the runtime
attenuator; 80% is raw `52428` only when no direct session owns the fixed
anchor. The realtime client repeats
these checks before opening the bridge socket and refuses a mismatched or
unsupported engine instead of falling back. It additionally requires an uncorked
`protocol-native.c` capture stream owned by its exact process PID (the vendor
recorder that was opened before wake) to reference the AEC source index, and
requires every reported AEC sink channel to be no louder than
`aec_sink_volume_ceiling_percent`. It fails closed if any route or channel is
wrong. Playback is additionally pinned to the AEC sink. Once per direct
session, after the preflight and before opening the bridge socket, a fixed-argv
`pactl` controller sets and verifies the dedicated sink itself to the exact raw
`playback_volume_percent`. Active v2 `paplay`
targets that sink with raw stream volume 65536 (100% relative),
`--latency-msec=60`, and `--process-time-msec=20`; the client never enumerates
or mutates a sink-input. One non-amplifying software attenuator implements
dynamic user volume, eliminating the old double-attenuation path. The response,
playback-begin/resume, and interruption paths intentionally perform no live
volume subprocess work. The preflight and preparation compare PulseAudio's raw
channel units with the exact linear value (`65536 × percent // 100`), not the
rounded percentage printed beside them. Admission is not a continuing monitor:
operators and other software must not mutate the qualified sink during a live
direct session or raise a live sink or stream during the canary.

Loading Adrian and observing its 16 kHz mono endpoints verifies topology, not
acoustic echo cancellation. For the first physical double-talk canary, read
`aec_sink_volume_ceiling_percent` from the reviewed root-only realtime config
and set the AEC sink to it. Active v2 reads
`playback_volume_percent`; configuration rejects that value above the sink
ceiling. The active full-range reference explicitly sets both to 100; the
schema range is 1–100. A saved level such as 80 is applied later by the single
non-amplifying runtime attenuator and does not lower this physical anchor.
Record the pre-test volume separately and restore it only after echo-rejection,
early/middle/late barge-in, wake, normal Assist, and repeated-turn tests pass.
The helper intentionally does not change the running sink. Its static startup
setpoint takes effect on the next controlled PulseAudio/service start; use the
following runtime command only for the immediate canary.

For the active reference canary value, the explicit device-side volume command is:

```sh
pactl set-sink-volume codex_echo_cancel_sink 100%
```

Use the exact configured anchor. The full-range reference must explicitly
configure 100 in the installer and root-only realtime configuration, run the
sink command with 100% for an immediate worst-case canary before restart, and
pass the complete no-user echo plus
early/middle/late double-talk canaries before normal use. The static command
provides the initial setpoint; the vendor media-player preference is the later
writer and reboot-persistence authority. They must agree. PulseAudio's deferred
restore database is not sufficient evidence.

Only after the static topology and acoustic canary pass should the root-only
realtime configuration enable the server-offloaded route:

```json
{
  "media_transport": "bridge_pcm",
  "capture_backend": "native_aec3",
  "full_duplex": true,
  "pulse_aec_source": "codex_echo_cancel_source",
  "pulse_aec_sink": "codex_echo_cancel_sink",
  "pulse_aec_method": "adrian",
  "aec_sink_volume_ceiling_percent": 100,
  "playback_volume_percent": 100,
  "direct_capture_gain_db": 0
}
```

These checks qualify only the AEC topology and acoustics. The earlier 25%
reference measurements exercised an older v2 bridge-PCM configuration; they do
not qualify active native-AEC3 v2 at 100%. The dormant v3 build kept one
isolated sidecar OS process; its active and offer-warm standby peers are logical
slots within that worker, and the first standby is gated until readiness, cue
completion, and capture-open. A historical two-worker v3 build separately
passed a reference-device hardware double-interruption canary twice at that
installation's qualified 60% setting with the exact artifact: four local cuts
were 208–211 ms and four rollovers were 1.29–1.57 s. Each run recycled its same
two worker PIDs without a cold replacement and retained context twice. Those
measurements do not physically validate the dormant single-worker build. The
60% qualification is not transferable. Each
installation still requires the complete acceptance matrix.

The dormant device-owned WebRTC experiment can be re-enabled only by selecting
`media_transport: "device_webrtc"` and reinstalling/validating its pinned
runtime. That is not an automatic fallback. Complete AEC removal first disables
realtime, then uses a dry run followed by the explicit removal:

```sh
python3 prepare_pulseaudio_aec.py remove
python3 prepare_pulseaudio_aec.py remove --apply
```

The remover deletes only an exact installer-owned tail and refuses partial,
modified, or duplicated markers. It recognizes both current method/volume
blocks and the released method-only legacy blocks. It does not restore the
backup automatically or restart services. Keep the backup until physical
acceptance is complete, and verify TCP ADB port 5555 before and after the
controlled restart.
