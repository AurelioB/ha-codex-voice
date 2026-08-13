# Native AEC3 capture canary for ThirdReality v1.1.7

This directory contains an experimental, **disabled-by-default** capture path
for the Python-based ThirdReality `1.01.07`/upstream `v1.1.7` image. It does not
replace firmware, reconfigure PulseAudio, or add another supervised process.

The implementation consumes the speaker codec's synchronized ALSA loopback
capture:

| Property | Required value |
|---|---|
| ALSA device | `hw:0,4` (`LOOPBACK-A`) |
| Wire format | interleaved `S16_LE` |
| Rate/layout | 16 kHz, 4 channels |
| AEC frame | 160 frames / 10 ms |
| Microphone | zero-based channel 0 |
| Render reference | mono average of channels 2 and 3 |
| AEC ordering | reverse reference, delay 0 ms, capture |
| Output | 16 kHz mono PCM16, aggregated to the vendor's requested block |

Because microphone and codec playback reference arrive in the same hardware
period, they share a clock and AEC3 uses a zero stream-delay hint. Bytes merely
accepted by `paplay` are not an equivalent reference: they precede PulseAudio
buffering, resampling, and the physical sink by a variable interval.

## Safety state

Nothing in this package runs merely because it is installed. The Python
facade is normally selected by `capture_backend: "native_aec3"` in the enabled,
root-owned mode-0600 `/data/conf/codex-realtime.json`; the active configuration
selects full-duplex `media_transport: "bridge_pcm"`, while dormant v3 also
supports the same capture backend. The latency overlay reads and
validates this configuration before vendor microphone selection. Installing the
package alone does not activate it.

After the secure configuration selects native capture, the overlay makes the
facade's low-level opt-in explicit in a private environment mapping rather than
requiring a service-environment edit:

```python
import os

from aec3_capture import install_from_environment

aec3_environment = dict(os.environ)
aec3_environment["CODEX_AEC3_CAPTURE"] = "1"
_AEC3_PATCH = install_from_environment(environ=aec3_environment)
```

Only after that patch succeeds does the overlay publish
`CODEX_AEC3_ACTIVE=1` internally as proof for realtime session preflight;
operators must not set this proof variable. Supplying `CODEX_AEC3_CAPTURE=1`
in the service environment is an explicit diagnostic override of early
selection, not the normal activation path and not a replacement for the
persistent `capture_backend` session contract. The override still requires a
valid enabled native-AEC3 configuration.

An enabled native load, ABI, device, startup, ring, or processing failure fails
closed instead of returning raw microphone audio. The vendor command must also
omit `--audio-input-device`, because the facade replaces only
`soundcard.default_microphone()`.

## Exact cross-build inputs

The device has no compiler. Build on a host from a recursive checkout of
ThirdReality `voice-music-assistant` tag `v1.1.7`. Its pinned toolchain submodule
commit is `1fc3867c34095647024a80741540ca6b1cd5d053`, containing
`gcc-arm-10.2-2020.11-x86_64-aarch64-none-linux-gnu`. The firmware uses glibc
2.31 and `libstdc++.so.6.0.28`.

The stock defconfig does not select WebRTC APM. Populate a separate Buildroot
output/staging tree without creating or installing a firmware image:

```console
cd /path/to/voice-music-assistant/buildroot
make O=/path/to/output-aec3 3reality_trspk_defconfig
utils/config --file /path/to/output-aec3/.config \
  --enable BR2_PACKAGE_WEBRTC_AUDIO_PROCESSING
make O=/path/to/output-aec3 olddefconfig
make O=/path/to/output-aec3 webrtc-audio-processing alsa-lib
```

This resolves the exact Buildroot packages `webrtc-audio-processing` 1.3 and
`libabseil-cpp` 20230802.1. Then build and bundle this module:

```console
python3 device/thirdreality/aec3_capture/build_aarch64.py \
  --toolchain-root \
  /path/to/voice-music-assistant/sources/toolchain/gcc-arm-10.2-2020.11-x86_64-aarch64-none-linux-gnu \
  --buildroot-output /path/to/output-aec3
```

The builder refuses a non-GCC-10.2 compiler, requires the APM/ALSA staging
metadata, recursively bundles APM and Abseil shared libraries, copies their
licenses, and fails if any artifact exceeds these device ABI ceilings:

- `GLIBC_2.31`
- `GLIBCXX_3.4.28`
- `CXXABI_1.3.12`

Its deterministic archive contains `bin/codex_aec3_canary`,
`lib/libcodex_aec3_capture.so`, private dependencies, the Python facade,
licenses, and a SHA-256 manifest. ALSA and the C/C++ runtimes remain dynamic
dependencies on libraries already present in v1.1.7.

## Canary without restarting PulseAudio

The Amlogic loopback capture DAI requires playback DMA to be running before
`hw:0,4` is opened. On the current v1.1.7 deployment, the statically loaded
Adrian module normally keeps the raw sink running. Do not unload that module,
kill PulseAudio, or use direct `aplay` against the hardware device PulseAudio
owns.

Deploy the archive under the existing writable data partition:

```console
adb push aec3_capture /data/conf/codex-python/
adb shell chmod 0755 \
  /data/conf/codex-python/aec3_capture/bin/codex_aec3_canary
```

Before opening capture, start a 20–30 second speech/noise stimulus through the
existing raw PulseAudio sink, wait at least 200 ms, then run the canary. A WAV
input lets `paplay` derive its format:

```console
paplay --device=alsa_output.hw_0_1 /data/conf/aec-stimulus.wav &
sleep 0.2
LD_LIBRARY_PATH=/data/conf/codex-python/aec3_capture/lib \
  /data/conf/codex-python/aec3_capture/bin/codex_aec3_canary \
  --duration 15 --out /tmp/codex-aec3-canary
```

The canary never invokes `pactl`, `paplay`, or a service command itself. It
writes `raw_mic.wav`, `reference.wav`, and `aec3_output.wav`, plus one
content-free RMS/rough-ERLE line per second. Rough ERLE is meaningful only in a
playback-only interval; near-end speech contributes to both raw and processed
energy.

Qualification should cover:

1. playback-only at 25%, 40%, and 60% sink volume;
2. near-end-only speech at close, normal, and far distance;
3. double-talk during continuous playback;
4. 30 minutes of capture with zero drops and processing failures;
5. repeated canary starts while the PulseAudio PID remains unchanged.

Reject the path if channel 2/3 reference energy does not track physical
playback, near-end speech is materially suppressed, ALSA recovery repeats, the
ring overflows, memory pressure approaches OOM, or playback-only residual still
triggers wake/barge detectors.

## Recorder boundary

The native thread owns ALSA and APM. It rejects any rate/period/layout drift,
processes each synchronized 10 ms frame, resets AEC after an xrun or short read,
and never zero-pads missing audio. A bounded 4,096-frame ring bridges into the
vendor's `record(1024)` request. The Python facade returns float32 shape
`(1024, 1)`, preserving the existing 64 ms callback, wake-word processing,
384 ms pre-roll, official Home Assistant path, and direct WebRTC reframer.

The production profile mirrors ThirdReality's newer native audio processor:
AEC3 and its required high-pass filter, 10 dB fixed-digital gain with a -3 dBFS
target and limiter, and moderate WebRTC noise suppression. Conditioning happens
inside APM, so the vendor wake detector and realtime path receive the same
cleaned samples. The active deployment keeps only 2 dB of additional bounded
transport gain, preserving the previous nominal total while avoiding a second
12 dB amplification of residual noise.

## What needs a reboot

Private binaries and the opt-in facade can be installed under `/data/conf` and
tested by restarting **only** `voice-assistant`; PulseAudio must stay alive.
A firmware/rootfs rebuild, removal of the static Adrian topology, or a new boot
playback-DMA keepalive requires a later reboot-qualified change. Cold-boot mic
gain ordering also remains a separate boot concern.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for provenance and bundled
library licenses.
