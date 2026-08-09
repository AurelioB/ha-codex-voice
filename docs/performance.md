# Performance and ThirdReality tuning

Codex Voice removes avoidable local waits, but most turn latency still comes
from an experimental remote realtime session. Treat the figures below as
diagnostic reference points, not service-level guarantees. Network path,
ChatGPT load and quota, Codex CLI version, Home Assistant pipeline choices,
utterance length, and media-player buffering all affect a turn.

## What is optimized

The standard Assist path remains turn-based:

```text
wake cue -> capture -> STT -> Conversation -> TTS -> speaker playback
```

Several enabled optimizations shorten different parts of that path:

1. Streaming STT sends microphone frames to the bridge while Home Assistant is
   still capturing. The bridge starts the Codex thread and WebRTC handshake as
   soon as it receives the stream's start frame. After bounded calibration on
   sustained speech, it feeds normalized audio while capture continues. Quiet
   or ambiguous audio stays buffered until EOF, and a complete raw copy remains
   available for a fresh normalized retry.
2. Streaming TTS returns an EOF-terminated PCM16 WAV stream as realtime speech
   frames arrive. Home Assistant can begin serving audio without waiting for
   the complete rendered response and remote cleanup.
3. Speech peers use an explicit empty ICE-server list for this local
   subscription-backed path, avoiding the default public STUN probe. Voice
   conversation turns default to low reasoning effort to favor response
   latency.

Automatic STT-to-TTS session reuse is not enabled. Live validation found that
the realtime v3 session can start genuine assistant output before the user
transcript completes, and the supported tagged Frameless Bidi outbound protocol
does not define a response-cancel message.

These paths preserve the finite Home Assistant provider contracts. They do not
turn the standard Assist pipeline into a full-duplex or barge-in session.

## Live measurements

All figures in this section are **live measurements from 2026-08-08**, not
simulated test results. Physical pipeline measurements used one ThirdReality
3RSPK whose Home Assistant firmware display reported `1.01.07`; the direct
bridge/session probes use narrower timing boundaries and are labeled
separately. Sample sizes are deliberately shown because these small probes are
not population benchmarks.

### Physical pipeline reference

Six physical voice commands produced these Home Assistant event intervals:

| Interval | Median | Observed range |
|---|---:|---:|
| intent start to TTS start | 2.093 s | 1.488–5.421 s |
| TTS start to announcement finished | 17.493 s | 13.970–20.436 s |
| intent start to announcement finished | 20.887 s | — |

The end-to-end median is the median of the six complete turns; it is not the
sum of the two independently calculated sub-interval medians. “Announcement
finished” includes generation, Home Assistant serving, device buffering, and
physical playback, so it should not be compared directly with a bridge
first-byte measurement.

### Controlled acoustic wake canary

Two additional 2026-08-08 canaries exercised the actual speaker, microphone,
wake detector, shortened confirmation cue, Home Assistant pipeline, Codex STT,
local intent routing, TTS, and speaker playback. Each WAV used the repository's
two-second “Okay Nabu” test sample, then a fixed non-sensitive “What time is
it?” command. The second run reduced the silence between those source clips
from 700 ms to 300 ms.

All intervals below begin at Home Assistant's pipeline `run-start` event, not
at the start of WAV playback.

| Source gap | Run to VAD start | Run to VAD end | Run to STT end | Run to pipeline end | Run to satellite idle |
|---:|---:|---:|---:|---:|---:|
| 700 ms | 1.852 s | 2.892 s | 9.655 s | 9.669 s | 23.082 s |
| 300 ms | 1.613 s | 2.523 s | 9.422 s | 9.423 s | 22.580 s |

Both runs returned the local `action_done` response type, emitted no pipeline
error, played the answer, and returned the Assist satellite to `idle`. The
300 ms source gap therefore did not lose enough initial speech to prevent
correct routing. This is stronger than a software-only injection because the
audio traversed the device's physical output/input path, but it is still a
controlled self-acoustic test rather than a substitute for near-, normal-, and
far-field human trials in the deployment room.

### Streaming TTS probe

One v0.1.7 live bridge probe of the same synthesis request measured:

| Observation | Time |
|---|---:|
| finite endpoint response ready | 10.717 s |
| streaming endpoint first PCM | 6.495 s |
| streaming endpoint complete | 10.457 s |

Streaming exposed the first PCM **4.222 s earlier** than the finite response.
It did not shorten the remote model's complete rendering by that amount, and
the figure excludes downstream speaker buffering and playback.

### Streaming STT capture overlap

A reference paced STT run with a 2.0 s clip reached STT completion in
11.709 s. Three direct runs through the capture-overlap path measured:

| Run | Total | After capture | Handshake overlapping capture |
|---:|---:|---:|---:|
| 1 | 9.832 s | 7.832 s | 2.005 s |
| 2 | 9.473 s | 7.473 s | 2.005 s |
| 3 | 9.680 s | 7.680 s | 2.005 s |

The three-run median was 9.680 s, 2.029 s below the reference run. This is a
small live comparison, not proof of a fixed two-second saving. The overlap log
is the useful verification signal: setup should advance while microphone
capture is active instead of beginning only after the final audio chunk.

### Calibrated live STT feed

A later paired physical-speaker canary used the same ThirdReality device,
command audio, runtime microphone gain, and aggressive finished-speaking
detection. The second run enabled calibrated feeding during capture:

| Path | Capture | After capture | Total STT |
|---|---:|---:|---:|
| finite feed at EOF | 3.858 s | 5.661 s | 9.519 s |
| calibrated live feed | 3.984 s | 2.153 s | 6.138 s |

The live-feed run had 0.905 s of queued audio when the remote handshake became
ready and saved 3.381 s overall. It completed without a retry or pipeline
error. This is one controlled self-acoustic A/B, not a latency guarantee or a
replacement for human near-, normal-, and far-field trials.

### Native TTS output format

The component advertises mono 16-bit WAV at 16 and 24 kHz and forwards Home
Assistant's selected native tuple to the bridge. The bridge incrementally
resamples its 24 kHz realtime output when 16 kHz is requested, without waiting
for the full response. This removes a format mismatch at the provider boundary;
downstream Home Assistant or media-player conversion and buffering can still
add latency.

### Retained-session exploratory probe

An exploratory direct realtime v3 run measured a 5.998 s initial handshake and
3.844 s to obtain a transcript. After the client later sent `appendSpeech` on
that connection, it received a non-empty queued audio frame 0.018 s later. The
probe did not continuously drain and verify a quiet realtime stream before
`appendSpeech`, so that frame cannot be causally attributed to the speech
request and is not a warm-TTS latency measurement.

A later causal probe observed non-empty assistant transcript and output events
before the user transcript completed. The bridge rejected that session for
reuse while still returning the valid STT result. Codex 0.146.0's tagged
[Frameless Bidi outbound message
definitions](https://github.com/openai/codex/blob/rust-v0.146.0/codex-rs/codex-api/src/endpoint/realtime_websocket/protocol.rs#L42-L84)
include session close and context/delegation appends but no response-cancel
control. There is therefore no retained-session latency claim, and automatic
handoff remains disabled. A future implementation must first expose a supported
cancel-or-transcription-only protocol and prove a quiet causal boundary before
running a physical A/B.

## One-time STT-to-TTS session handoff

Handoff is a dormant diagnostic wire path in the bridge. The bundled Home
Assistant component never prepares or requests it, and the released bridge
never retains STT or issues a ticket. Standard Assist STT and TTS therefore
always use separate realtime threads and sessions. The parser and ownership
machinery remain covered for future protocol work.

If a future supported implementation enables the path, Home Assistant reads an
entity's default options before it merges an Assist pipeline's voice override.
Preparation therefore reserves the TTS subentry's
default voice. If a pipeline overrides it with another supported voice, the
later exact-voice check safely rejects reuse and takes the cold path; warm reuse
for per-pipeline voice overrides is not currently supported.

A ticket is usable only when all of these conditions hold:

- it has not already been claimed or revoked;
- fewer than 30 seconds have elapsed since the bridge offered it;
- the TTS voice exactly matches the voice reserved during STT;
- the normalized TTS language matches the language reserved during STT;
- the TTS request has no custom voice instructions; and
- the request is still in the same Home Assistant chat-session and prepared
  TTS context.

There is at most one outstanding handoff offer per bridge. A replacement or
unrelated incompatible speech request cleans the old resource before it can
start or publish another session.

The bridge stores only a SHA-256 digest of the ticket, compares it in constant
time, and never logs it. Outside the bearer-authenticated request/response
transport, the component keeps the raw ticket only in private in-memory task
context. Tickets must still be treated as bearer secrets: do not put them in
logs, diagnostics, automations, URLs, or user-visible state.

Before offering or claiming a session, the bridge discards unsent STT input,
drains benign late STT events, and rejects any unexpected assistant audio,
assistant output, remote close, or App Server failure. A watchdog invalidates
the offer if such activity appears while it is waiting. Expiry, cancellation,
replacement, component release, bridge shutdown, and successful use all stop
the realtime session and delete its thread exactly once.

No session is inferred from “the next request.” A direct `tts.speak` (including
one invoked from the same chat session), a different Home Assistant chat
session, another bridge client, a mismatched voice or language, or a request
with custom instructions does not inherit the retained session. An
incompatible speech request closes the offer and cold-starts its own session.
If reuse fails before any PCM has been returned, synthesis may cold-start once
within the original deadline; after the first PCM, it fails instead of risking
duplicate speech.

The released component omits the handoff request, so every STT and TTS operation
has a new remote context. Direct calls do the same.

## Why remote prewarming is not enabled

The bridge does not keep an always-on remote speech session waiting for a
future wake word. Home Assistant exposes no custom STT-provider callback before
wake detection; the earliest reliable component hook is the STT stream itself,
which already overlaps setup with capture.

Always-on remote prewarming would also:

- continuously send paced silent RTP to keep the WebRTC media path active;
- occupy the account's single admitted speech-session lane;
- need speculative ownership across satellites and chat sessions;
- add cancellation and cleanup work when the next request is incompatible; and
- consume an amount of ChatGPT subscription availability that App Server does
  not document as free or quota-neutral.

The official [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
provides usage and rate-limit observability, but no idle realtime-session cost
or lifetime guarantee. Any future prewarm experiment must therefore be
explicit, one-shot, short-lived, owner/profile-bound, and measured against a
no-prewarm control. It must not silently replenish itself. Preparing only
local SDP/ICE state is a lower-risk future experiment, but is not currently
implemented.

## ThirdReality safe performance settings

These settings affect device and Home Assistant satellite behavior, not the
bridge. Change one variable at a time, keep the original value, and repeat a
fixed phrase set before accepting it.

### Short wake cue

Firmware `1.01.07` waits for the entire wake confirmation file to finish before
it begins forwarding microphone audio. The measured stock cue was 0.946979 s;
a patched older cue was 0.399592 s, removing about 0.547 s from this gate.

Replacing the cue is a firmware modification. Back up the original asset and
full recoverable firmware first. Keep an audible confirmation, preserve the
audio format expected by the player, and test immediate and delayed speech at
several distances. Reject the change if initial phonemes disappear, the cue is
not reliably audible, playback blocks, or wake cycles become unstable.

### Finished speaking detection

The ThirdReality Home Assistant entity's **Finished speaking detection** select
was measured at approximately 700 ms of trailing silence in its default mode
and 250 ms in `aggressive` mode, a nominal 450 ms reduction. This is
device/Home Assistant satellite endpointing; Codex Voice does not set it.

`aggressive` is appropriate only after testing natural pauses, short commands,
numbers, names, and slow speech. Revert to the default if final words are
truncated or mid-sentence pauses end capture. A faster but incomplete
transcript is not a performance improvement.

### Microphone gain

On the measured device, a ThirdReality microphone-gain setting of 50 mapped to
an ALSA PDM level of 24/48; the factory 30% setting mapped to 14/48. The higher
setting is an acceptance candidate, not a universal recommendation. Room
acoustics and individual hardware vary.

Test near, typical, and far speech plus a loud voice. Accept the higher gain
only when recognition improves without clipped peaks, elevated background
activation, echo-related failures, or degraded wake detection. Hardware
clipping cannot be repaired by the bridge's bounded adaptive normalization.
Use the bridge's privacy-safe numeric peak/RMS and adaptive-gain logs for
comparison; they never include speech or transcripts.

## Official v1.2 C++ firmware canary evaluation

Home Assistant reported `1.01.07` installed and `1.02.01` available on the
measured unit. Those display versions correspond to upstream firmware tags
v1.1.7 and v1.2.1.

The official [v1.2.0 release](https://github.com/thirdreality/voice-music-assistant/releases/tag/v1.2.0)
replaces the Python assistant with a native C++ binary and adds hardware-loopback
WebRTC AEC3. Its notes also describe a default 0.5 s continued-conversation
settle delay, unified voice/music volume behavior, and playback and memory
stability fixes. The [v1.2.1 release](https://github.com/thirdreality/voice-music-assistant/releases/tag/v1.2.1),
dated 2026-07-30, updates Sendspin, fixes DNS handling, and enables ADB over USB
and TCP.

### Production-device decision: no-go

The measured speaker must remain on its known-good 1.01.07 image. The vendor
images and tagged board source establish a single boot/system/recovery layout,
not two independently bootable slots: the board configuration leaves
[`CONFIG_AB_SYSTEM`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/sources/uboot/board/amlogic/configs/axg_s420_v1_trspk.h)
disabled, and the UBI configuration defines one dynamic
[`rootfs`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/fs/ubi/ubinize.cfg)
volume. The tree contains generic A/B support, but this board does not select
it. A flash of the only speaker is therefore not an A/B experiment.

The cached 1.1.7 file is a stock full-burn image, not a bit-for-bit read-back of
this device, and the configuration backup does not contain bootloader, boot,
recovery, rootfs/UBI, U-Boot environment, or device security state. Recovery
exposes neither SSH nor ADB, so TCP/5555 cannot rescue a system that fails
before the normal root filesystem boots. Until physical burn-mode entry and a
full 1.2.1-to-stock-1.1.7 downgrade have been rehearsed on identical spare
hardware, the production upgrade decision is **no-go**. Even a successful
stock-image downgrade is not exact restoration without this speaker's own
complete pre-flash read-backs.

Native C++ and lower memory use do not by themselves guarantee a faster Assist
turn. The v1.2.1
[`Satellite.cpp`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/package/thirdreality/linux-voice-assistant-cpp/src/satellite/Satellite.cpp)
still starts microphone streaming only after the wake cue callback. The
tagged v1.2.1 source vendors and installs the exact same cue blob as v1.1.7;
that asset measures 0.946979 s, so the upgrade alone does not remove that
gate. Its short-sound-safe
[`LibMpvPlayer.cpp`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/package/thirdreality/linux-voice-assistant-cpp/src/audio/LibMpvPlayer.cpp)
sets mpv's `audio-buffer` to `0.8`. That setting must be measured; it should not
be treated as an additive 0.8 s delay without evidence.

Only after those gates pass, run a paired comparison using a physically
separate canary while the known-good speaker remains untouched. Use the same
room, wake word, pipeline, voice, network path, command set, and playback
volume. Record at least:

- wake detection to microphone-stream start and the cue duration;
- utterance length and trailing-silence endpoint;
- STT completion and transcription accuracy/truncation;
- intent start to first TTS PCM and to first audible playback;
- TTS start to announcement finished;
- continued-conversation echo and settle behavior; and
- CPU, memory, audio underruns, disconnects, and recovery after restart.

Keep each raw run rather than reporting only the best result. Compare medians
and ranges, and return to the old image if accuracy, echo control, stability,
or tail latency regresses even when one median improves.

### Backup and rollback before a canary flash

Firmware flashing can erase configuration or make a device temporarily
unbootable. Before a v1.2 canary flash:

1. Require independently authenticated image provenance. After provenance is
   established, record SHA-256 hashes, byte sizes, source URLs, and acquisition
   dates in a private manifest. A locally calculated hash is only an identifier
   and later-corruption check; it does not authenticate the publisher. The
   v1.2.1
   [`UpdateEntity_Ota.cpp`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/package/thirdreality/linux-voice-assistant-cpp/src/entities/UpdateEntity_Ota.cpp)
   and
   [`OtaClient.cpp`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/package/thirdreality/linux-voice-assistant-cpp/src/tr/OtaClient.cpp)
   disable TLS peer and hostname verification for update metadata and image
   downloads.
2. Capture raw, verified read-backs of this exact device's bootloader, DTB,
   boot, recovery, rootfs/UBI, U-Boot environment, and complete `/data` before
   flashing. Record read-only secure-boot, lock, fuse, and anti-rollback state.
   Include `/data/conf`, every locally modified init script, and every modified
   audio asset. Treat Wi-Fi and service configuration as secrets; do not
   publish the archive or its host path.
3. On identical spare hardware, physically enter USB/burn recovery without
   Linux, ADB, or SSH; flash the exact candidate; then perform and verify the
   complete stock-image downgrade and restoration procedure. This rehearsal is
   mandatory, not optional.
4. Use stable power during flashing. After boot, verify the reported firmware,
   Home Assistant entities, wake/stop words, microphone, TTS, volume, network,
   and restart recovery before performance testing. The v1.2.0 release notes
   warn that the first v1.1-to-v1.2 OTA can leave a stale Home Assistant entity.
5. Do not flash the production speaker unless exact restoration from its own
   read-backs is proven. A stock 1.1.7 reflash proves only a downgrade, not
   recovery of the device's prior state. Restore version-specific configuration
   only to matching firmware; never blindly overlay one version's rootfs onto
   another.

### Harden root access after v1.2.1

The official v1.2.1
[`S55adbd`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/board/thirdreality/trspk/rootfs/etc/init.d/S55adbd)
defaults `ADB_TCP_PORT` to `5555`. Its own warning states that the socket binds
to all interfaces while `adbd` runs as root with `ro.adb.secure=0`. Anyone who
can reach that port can therefore obtain an unauthenticated root shell.

Put the device on an isolated network while testing. If TCP ADB is required,
keep port 5555 enabled but restrict it at the network boundary to the specific
administration host or management network that needs it. Verify that the
approved host can reconnect after reboot and that other LAN/VLAN hosts cannot;
never expose the port to the internet. Because the daemon itself provides no
authentication and runs as root, a broad trusted-LAN rule is not an adequate
substitute for source restriction. Repeat both the allowed- and denied-source
checks after every firmware update.

For the measured device in this project, TCP ADB is an explicit normal-OS
remote-administration requirement: canary, reboot, and rollback procedures must
leave port 5555 enabled and verify that the administration path can reconnect.

Only deployments that do not require TCP ADB should turn it off. Use a local
or serial console—not the TCP session being disabled—and create or edit
`/etc/default/adbd`. Preserve any existing settings in that file, especially a
custom `UDC_NAME`, and set:

```sh
ADB_TCP_PORT=
```

Reboot, or restart `S55adbd` from a console that does not depend on that daemon;
restarting it can terminate both TCP and USB ADB sessions. Then verify from the
device and from a different LAN host that nothing listens on TCP port 5555 and
that `adb connect DEVICE_IP:5555` fails. Reboot and verify again. Firmware
updates may replace or ignore local overrides, so repeat this check after every
update. Also block 5555 at the network boundary. If neither USB nor TCP ADB is
required, disable the entire `S55adbd` boot service using a recoverable,
firmware-appropriate mechanism and verify it remains disabled after reboot.

The tagged board configuration also enables Dropbear SSH, and the official
v1.2.1 instructions publish a fixed root password. Do not leave that default
reachable from the LAN. From a trusted local or serial console, install a
unique credential and disable root password authentication if the shipped
Dropbear build and recovery design support it; otherwise disable the SSH boot
service or block TCP port 22 at both the device and network boundary. Verify
from another LAN host that the old credential no longer works and that any
intentionally closed port remains closed after reboot and every firmware
update. Preserve a tested local/serial recovery route before disabling both
remote administration services.
