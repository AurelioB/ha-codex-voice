# ThirdReality full-duplex AEC deployment assets

These assets are an opt-in preparation step for the pinned ThirdReality
`1.01.07`/upstream `v1.1.7` image. They do not deploy themselves. The helper
never restarts PulseAudio or the voice service, never changes the running
speaker volume, and never changes or disables TCP ADB on port 5555. It does
write the explicitly selected AEC sink volume into the managed PulseAudio
startup block so the reviewed value is applied when the AEC sink is created.
On the stock device, the vendor voice process later reapplies the separate
media-player preference from `/data/conf/sound.json`; that authoritative
preference must be set to the same value through Home Assistant and verified
after every restart or reboot.

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
`--aec-sink-volume-percent` selects a startup value from 1 through 60 and
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
python3 prepare_pulseaudio_aec.py check --aec-method adrian --aec-sink-volume-percent 60
python3 prepare_pulseaudio_aec.py install --aec-method adrian --aec-sink-volume-percent 60
python3 prepare_pulseaudio_aec.py install --aec-method adrian --aec-sink-volume-percent 60 --apply
```

Use 60 only for an installation that will be physically qualified at 60;
otherwise omit the option for the safe 25% default or pass the lower reviewed
value explicitly. A released legacy managed block without a startup volume is
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
managed startup value and the device's Home Assistant media-player volume. At
60%, the media-player entity must report `volume_level: 0.6`, its vendor
preference must report `"volume": 60`, and `pactl get-sink-volume` must report
raw `39321` for every channel; 25% is raw `16384`. The realtime client repeats
these checks before opening the bridge socket and refuses a mismatched or
unsupported engine instead of falling back. It additionally requires an uncorked
`protocol-native.c` capture stream owned by its exact process PID (the vendor
recorder that was opened before wake) to reference the AEC source index, and
requires every reported AEC sink channel to be no louder than
`aec_sink_volume_ceiling_percent`. It fails closed if any route or channel is
wrong. Playback is additionally pinned to the AEC sink. Once per direct
session, after the preflight and before requesting the SDP offer or opening the
bridge socket, a fixed-argv `pactl` controller sets and verifies the dedicated
sink itself to the exact raw `playback_volume_percent`. Direct v3 `paplay`
targets that sink with raw stream volume 65536 (100% relative),
`--latency-msec=60`, and `--process-time-msec=20`; the client never enumerates
or mutates a sink-input. The retained v2 `paplay` path instead derives its
stream volume from the configured playback percentage. The response,
playback-begin/resume, and interruption paths intentionally perform no live
volume subprocess work. The preflight and preparation compare PulseAudio's raw
channel units with the exact linear value (`65536 × percent // 100`), not the
rounded percentage printed beside them. Admission is not a continuing monitor:
operators and other software must not mutate the qualified sink during a live
direct session or raise a live sink or stream during the canary.

Loading Adrian and observing its 16 kHz mono endpoints verifies topology, not
acoustic echo cancellation. For the first physical double-talk canary, read
`aec_sink_volume_ceiling_percent` from the reviewed root-only realtime config
and set the AEC sink to it. V3 and the v2 rollback both read
`playback_volume_percent`; configuration rejects that value above the sink
ceiling. Both settings default to 25 and have an absolute 1–60 range. Never
increase the active value above a previously qualified level without repeating
AEC qualification at the new value. Lower it for a quiet room or near-field test. Record the
pre-test volume separately and restore it only after echo-rejection,
early/middle/late barge-in, wake, normal Assist, and repeated-turn tests pass.
The helper intentionally does not change the running sink. Its static startup
setpoint takes effect on the next controlled PulseAudio/service start; use the
following runtime command only for the immediate canary.

For the default canary value, the explicit device-side volume command is:

```sh
pactl set-sink-volume codex_echo_cancel_sink 25%
```

Use the exact configured ceiling. A 60% deployment must explicitly configure
60 in the installer and root-only realtime configuration, set the official
Home Assistant media-player entity to 60%, run the sink command with 60% for an
immediate canary before restart, and pass the complete no-user echo plus
early/middle/late double-talk canaries before normal use. The static command
provides the initial setpoint; the vendor media-player preference is the later
writer and reboot-persistence authority. They must agree. PulseAudio's deferred
restore database is not sufficient evidence.

Only after the static topology and acoustic canary pass should the root-only
realtime configuration enable:

```json
{
  "media_transport": "device_webrtc",
  "full_duplex": true,
  "pulse_aec_source": "codex_echo_cancel_source",
  "pulse_aec_sink": "codex_echo_cancel_sink",
  "pulse_aec_method": "adrian",
  "aec_sink_volume_ceiling_percent": 25,
  "playback_volume_percent": 25
}
```

These checks qualify only the AEC topology and acoustics. The earlier 25%
reference measurements exercised v2 bridge PCM; they are not themselves proof
of the device-owned WebRTC v3 path. A separate reference-device v3 hardware
double-interruption canary, run at that installation's qualified 60% setting,
passed twice with the exact artifact: four local cuts were 208–211 ms and four
rollovers were 1.29–1.57 s. Each run recycled its same two worker PIDs without
a cold replacement and retained context twice. The 25%-default example above
remains conservative, and 60% qualification is not transferable. Each
installation still requires the complete acceptance matrix.

Transport-only rollback sets `media_transport` to `bridge_pcm` and
`full_duplex` to `false`, then removes the AEC route keys. Full AEC rollback
uses a dry run followed by the explicit removal:

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
