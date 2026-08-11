# ThirdReality v1.1.7 device overlay

This directory contains the guarded wake-latency patch and the Milestone 2
realtime client for the Python-based ThirdReality
`1.01.07`/upstream `v1.1.7` image. This image is a Python 3.11, aarch64
Buildroot Linux system, not Android. The root voice process keeps only the
standard-library session/overlay code; direct media runs `aiortc` in a separate
isolated child using the deterministic, hash-locked runtime described in
[`webrtc-runtime.md`](webrtc-runtime.md). The overlay does not replace
firmware, add a separately supervised daemon, or change Home Assistant.

The two wake phrases deliberately have different authority:

| Wake phrase | Route | Capability |
|---|---|---|
| Okay Nabu | Official Home Assistant Assist satellite flow | Local/remote STT, Conversation, TTS, and Home Assistant controls |
| Okay Computer | Explicit native [realtime wire v3](../../protocol/realtime-wire-v3.md) signaling to the Codex Voice bridge | Tool-free direct device WebRTC media/data channel; same-session barge-in when the AEC safety contract is enabled |

Okay Nabu can preempt an active direct session and immediately reclaim the
microphone for the normal Home Assistant path. Saying the configured stop word,
starting the normal Home Assistant path, mute/disconnect, or otherwise releasing
the vendor owner flushes local playback and tears down the remote session. A
later wake after teardown creates a fresh WebSocket, peer, and realtime thread.
In v3, bounded local AEC-filtered speech detection clears queued playback and
immediately SIGKILLs the active `paplay` child in the parent. The sidecar child
locally mutes decoded RTP while microphone upload continues. Only that trusted
AEC detector may send the zero-field local `response.interrupt` token; a manual
or non-speech preserving interrupt must use a fresh session. The direct
Frameless data channel supplies no provider interruption acknowledgement and
rejects public Realtime controls. The token is ordered immediately after the
second qualifying capture frame. Same-peer reuse is an explicitly empirical
WebRTC auto-truncation invariant: the child must consume through that watermark
and 250 ms beyond its token-time sender cursor, an overlapping 750 ms guard must
elapse, and the receiver must measure a fresh 500 ms decoded-RTP silence
interval after observing its barrier request. The fixed absolute five-second deadline never restarts;
timeout remains muted and requires a fresh peer and session.

V3 direct mode has no captured-audio handoff to Home Assistant. If the AEC
preflight, pinned runtime, sidecar, SDP exchange, WebRTC transport, data channel,
queue, playback, or bridge lifeline fails, the overlay clears its bounded
direct audio and returns to idle. The user must invoke Okay Nabu separately.
This prevents a private Okay Computer utterance from silently becoming an
Assist request with different authority.

## Realtime behavior and bounds

[`realtime_client`](realtime_client) selects one explicit media transport:

- `device_webrtc` uses protocol v3. The device sidecar owns the `aiortc`
  `RTCPeerConnection`, bidirectional audio transceiver, and ordered
  `oai-events` data channel. The authenticated bridge WebSocket carries the SDP
  offer/answer, `transport_ready`, `started`, ping, stop, and sanitized terminal
  errors only. It never carries PCM or raw provider events.
- `bridge_pcm` retains protocol v2 for rollback. The voice process sends 16 kHz
  mono PCM16 to the bridge and receives bridge-gated 24 kHz mono PCM16. See
  [the v2 contract](../../protocol/realtime-wire-v2.md).

Both routes hardcode `conversation_mode: "native"` and remain tool-free. The
bridge ignores any Home Assistant broker snapshot, creates one native App
Server realtime voice thread, and never inserts a completed-transcript wait,
executor thread, or `thread/realtime/appendSpeech` render. Okay Nabu remains a
separate vendor/Home Assistant path rather than an error fallback for v3.

The v3 child accepts timestamped 16 kHz mono PCM16 from the vendor capture
callback, sends it as WebRTC audio, decodes provider audio to 24 kHz mono PCM16,
and passes only bounded playback packets and sanitized lifecycle metadata back
over a Unix `SOCK_SEQPACKET` socket. Transcript/model text, tool data, arbitrary
provider payloads, credentials, and prompts never cross the child IPC
boundary. The parent uses a bounded direct player pinned to the configured AEC
sink; media never traverses the bridge WebSocket.

The default input and pre-ready fallback buffers are each 64 KiB: 2.048 seconds
at 16 kHz mono PCM16. Once a cold handshake completes, queued input is sent at
no more than 2× capture rate while more than one captured frame remains, then
returns to realtime pacing. This bounded catch-up preserves accepted audio and
removes a permanent startup offset; it does **not** remove the cold
App Server/WebRTC handshake or provider-generation latency. In v3, startup or
capacity failure clears both copies and returns idle; no captured prefix is
replayed to Home Assistant. The retained v2 `bridge_pcm` route alone keeps its
historical bounded pre-ready Assist replay behavior.

Wake activation occurs after the pinned recorder callback has already handled
the triggering frame. While the satellite is idle, connected, and unmuted, the
overlay therefore retains only the newest six 2,048-byte recorder frames in
RAM: 384 ms, or 12 KiB. An Okay Computer wake atomically takes that direct-only
pre-roll and seeds the bounded direct queues. An Okay Nabu wake discards it, so
the official Assist path still receives only post-wake audio. Stop, mute,
disconnect, teardown, and every v3 failure clear the buffer without forwarding,
persisting, or logging it.

Pre-roll never consumes the client's reserved 32 KiB (1.024 s) of live
post-wake capacity. It is trimmed from the oldest samples or omitted when a
smaller legal input or fallback queue needs that headroom; the default 64 KiB
queues retain all 12 KiB. This allowance is part of the existing queue bounds,
not additional unbounded storage.

The default device output queue is 48 KiB, about 1.024 seconds at 24 kHz mono
PCM16. V3 also bounds every child IPC packet and fails rather than dropping a
capture or playback packet under pressure. The bridge's 2,250 ms live-input cap
applies only to v2; v3 media never enters that bridge queue. These are safety
bounds, not a latency promise.

`device_webrtc` requires `full_duplex: true`, explicit safe
`pulse_aec_source` and `pulse_aec_sink` names, and one allowlisted
`pulse_aec_method`: `webrtc`, `speex`, or `adrian`. Omitting the method defaults
to WebRTC; the client never probes or falls back to another engine. Before it
creates the device peer or connects to the bridge, the client
requires those exact PulseAudio defaults and a loaded `module-echo-cancel` with
the expected raw hardware masters, endpoint names, configured method, and
master format. It also requires an uncorked native capture stream owned by the
current voice-process PID—including the already-open vendor recorder—to use
that AEC source, and requires every live AEC sink channel to be at or below
`aec_sink_volume_ceiling_percent`; v3 `paplay` playback is pinned to the same
sink. A failed
or mismatched check ends startup before any microphone audio leaves the device.
Once per direct session, after that preflight and before the SDP offer or bridge
connection, a fixed-argv `pactl` controller sets the dedicated AEC sink itself
to the exact raw `playback_volume_percent` value and verifies it. The direct
`paplay` child targets that sink with `--volume=65536` (100% relative) and never
enumerates or mutates a sink-input. Configuration rejects a playback value
above the 1–60% `aec_sink_volume_ceiling_percent`. The retained v2 path instead
converts the same playback value into its `paplay` stream volume. The live v3
response, playback begin/resume, and interruption paths run no `pactl`; an
operator must not mutate the qualified sink while the direct session is live.
The preflight and exact preparation compare raw PulseAudio units rather than
trusting rounded displayed percentages.

V3 owns at most one fixed-argv `paplay` child at a time. It accepts raw 24 kHz
mono signed-16-bit PCM, requests 60 ms latency and 20 ms process time, and uses
non-blocking stdin writes of at most 20 ms per network-loop service pass. A
receiver-quiescence media boundary can reuse that child so RTP tail is not
discarded. Interruption clears pending PCM, closes stdin, and issues immediate
SIGKILL without blocking the realtime loop; reap remains separately bounded.

Provider response/output lifecycle does not label, gate, or retire the normal
RTP lane. The decoded receiver is one continuous media lane: its first frame
emits transcript-free `media.started`, every frame resets the quiet timer, and
only about 120 ms of actual receiver silence emits `media.quiet`. A later frame
opens a new local media generation. This preserves prefixes received before
provider output-start and tails received after provider stopped events. This
normal-generation quiet boundary is independent of the longer interruption
fence below.

While verified full duplex is active, capture remains continuous during
playback. Two consecutive 64 ms AEC-filtered microphone frames that meet the
bounded peak and sustained-energy checks kill local playback in the parent and
drop queued playback IPC. The parent keeps capture paced, marks the second frame
as an exact watermark, and sends the zero-field local `response.interrupt` token
immediately after that packet and before any later capture. That trusted
detector is the only preserving entry to the fence. The child mutes stale
decoded RTP but continues consuming capture for upstream microphone RTP. The
device sends no cancel, clear, or interruption event to the provider. The direct Frameless data channel rejects public
Realtime `session.update` VAD configuration; live evidence produced only
`session.started`, with no `speech_started`, `turn.done`, or transcript event.
It therefore provides no acknowledgement or causal proof of remote
interruption. The public Realtime v2 WebRTC/client-event dialect is unsupported
on this subscription-backed route.

Same-peer continuation relies on an explicitly empirical WebRTC
auto-truncation invariant. From the trusted token, the child must consume
through the pre-token qualifying watermark and at least 4,000 samples beyond
its token-time sender cursor (250 ms at 16 kHz), at least 750 ms must elapse,
and the sole decoded-audio consumer must measure a fresh continuous 500 ms of
silence after observing its receiver-barrier request. Every queued, ready, or
later decoded frame is counted before resampling and resets that interval;
stalled event-loop wall time does not count. The hash-pinned aiortc 1.15 track
callback wraps its verified empty encoded-decoder and decoded-output queues
before the decoder starts. Queue/in-flight serials and producer-side silence are
rechecked under their locks; the retained jitter-buffer and resampler tails are
discarded through the no-await final commit and unmute. Decoder termination is
terminal, never silence.
These conditions overlap
rather than add. When all hold, an open normal media generation emits
`media.quiet` first; only successful `media.quiet` and `interrupt.fenced` IPC
writes permit unmute and let the parent return `READY`. Provider events cannot
forge those reserved internal names. One fixed absolute five-second deadline
is checked after receiver proof, after optional `media.quiet`, and immediately
before the final fenced commit; success and timeout are mutually exclusive. It
never restarts. Timeout emits fatal
`media_fence_capture_timeout` if either capture proof is unmet, otherwise
`media_fence_timeout`; lifecycle failure also stays muted. All failures require
a fresh peer/session. Manual or non-speech preserving
interruption cannot use this invariant and must take that fresh-session path.
Capture overflow never fabricates a watermark: if the two-frame trigger ends on
an unaccepted frame, the parent immediately takes the fresh-session path.
These behaviors have
automated coverage but still require a physical v3 canary under independent
RTP/SCTP delivery. See the
[wire-v3 interruption contract](../../protocol/realtime-wire-v3.md#barge-in-and-interruption).

The v2 path retains its older bridge-mediated interruption acknowledgements,
fresh-session fallback, and optional omitted-mode managed compatibility. Those
semantics are documented for rollback and do not apply to a v3 socket.

The observed stock `1.01.07`/v1.1.7 PulseAudio build rejects both WebRTC and
Speex because those engines are not compiled in. Adrian loads with the pinned
`hw:0,2`/`hw:0,1` masters, `use_master_format=1`, and creates the expected
16 kHz mono source and sink. The reference device previously passed a bounded
AEC/bridge-PCM canary at 25%: 5.531 seconds of assistant playback caused no
false interrupt across 86 microphone frames (maximum peak 2 and integer RMS 0),
while staged double-talk stopped local output in 141 ms and continued on the
same session. That result does not validate the new device-owned WebRTC v3
transport and does not qualify another installation. V3 currently has
automated protocol, sidecar, queue, barge-in, cleanup, and runtime-install
coverage but no claimed end-to-end physical acceptance. Install and qualify
the static PulseAudio AEC assets in [`deploy`](deploy) first. The v3 acoustic
canary must use the configured sink ceiling and exact pre-negotiation playback
value; the v2 rollback uses the same playback setting. Both settings default
to 25 and are hard-limited to 1–60. Do
not increase the active setting above a previously qualified level until echo
rejection and early, middle, and late double-talk barge-in pass at the new value
on that physical device. Speex is available
only on a different firmware build that actually compiles that engine.

## Wake-latency patch

The Python-based ThirdReality `1.01.07`/upstream `v1.1.7` client starts the wake
confirmation cue but waits for cue EOF before it asks Home Assistant to prepare
the Assist pipeline. After starting that asynchronous cue, its ThirdReality
wrapper performs a synchronous LED DBus call on the microphone thread with a
two-second timeout. In three 2026-08-09 human baselines, Home Assistant VAD
began 1.37, 2.46, and 3.27 seconds after pipeline start. Those end-to-end values
include the combined device and Home Assistant path; they do not isolate the
LED call by itself.

[`latency_sitecustomize/sitecustomize.py`](latency_sitecustomize/sitecustomize.py)
uses an LED-only acknowledgement and does not play wake audio; this cue-free
path remains independent of whether the optional full-duplex AEC topology is
enabled. On the pinned single microphone thread, it pre-arms forwarding, queues
the pipeline start request, and ducks music without playing the local wake cue.
For Okay Computer only, its bounded RAM pre-roll recovers recorder
frames handled before that direct owner exists; Okay Nabu explicitly discards
the same history. The
ThirdReality LED command is queued on one daemon worker, so the DBus subprocess
can no longer block microphone capture. Commands are serialized and bounded by
the vendor's two-second timeout. If the bounded queue fills, stale pending
animations are coalesced into the newest state; timed-out children are reaped.
Both the vendor base class and the pinned ThirdReality subclass are patched
directly. This prevents a later base-method rebinding during device startup from
restoring the cue gate while retaining the guarded LED behavior.

The override is applied atomically only when SHA-256 hashes of all four
installed vendor code objects match the tested build: the base wake and cue-EOF
methods plus the ThirdReality wake wrapper and LED helper. A mismatch logs a
warning and leaves both vendor modules intact.

Wake setup is transactional: a send or duck failure rolls local pipeline and
streaming flags back to idle and best-effort queues a cancellation if a start
request was attempted. Pre-arming occurs on the same microphone thread that
forwards audio, so it cannot leak a frame before wake setup returns. The overlay
rechecks the armed state after both external calls, and never sets it again;
pinned VAD/STT-end, mute, disconnect, and run-end teardown therefore wins any
startup race.

The overlay does not replace vendor modules, change Home Assistant, modify the
wake audio file, reboot the speaker, or enable/disable USB or TCP ADB.

## Deployment contract

Published releases attach `thirdreality-realtime.zip` with this layout:

```text
latency_sitecustomize/
  sitecustomize.py
realtime_client/
  __init__.py
  config.py
  playback.py
  session.py
  sidecar.py
  websocket.py
webrtc_sidecar/
  __init__.py
  __main__.py
  peer.py
  protocol.py
  runtime.py
deploy/
  README.md
  prepare_pulseaudio_aec.py
  pulse/
    codex-echo-cancel.pa
    codex-echo-cancel-speex.pa
    codex-echo-cancel-adrian.pa
codex-realtime.example.json
webrtc-runtime.in
webrtc-runtime.lock.txt
webrtc-runtime.md
install_thirdreality_webrtc_runtime.py
README.md
LICENSE
```

The example contains documentation-only addresses and a placeholder—not a
credential. Generate a distinct `HA_CODEX_REALTIME_DEVICE_TOKEN` on the bridge
host, configure that same value only in the root-only device file, and never
put the token in the archive, repository, shell history, or diagnostics. The
device token is accepted only by `/v1/realtime` after a valid v2 or v3
negotiation; it cannot enter legacy v1 or `/v1/home-assistant/tools`. Do not
reuse the Home Assistant bridge token, a Home Assistant access token, or Codex
`auth.json`. Both packaged transports hardcode `conversation_mode: "native"`.
The v2 rollback client also emits
`User-Agent: ha-codex-voice-thirdreality/2` for its compatibility interrupt
contract. V3 barge-in uses a local parent-to-child `response.interrupt` IPC
packet only after trusted AEC-filtered local speech. Microphone RTP continues,
but the direct Frameless channel sends no provider client control and supplies
no bridge or provider acknowledgement. Manual/non-speech preserving
interruption requires a fresh session.

The device never receives tool schemas, tool calls, results, or a Home
Assistant credential. Okay Computer is always native and tool-free; use Okay
Nabu for Home Assistant control. **Provide Home Assistant tools to realtime
voice** applies only to older strict-v2 clients that omit `conversation_mode`.
The integration may maintain that compatibility broker, but its presence does
not affect a reference-client session and adds nothing to device configuration
or v3 signaling.

Treat `PYTHONPATH` as root-process code execution. Copy only the reviewed file
and package from a pinned repository commit or release asset, verify SHA-256
after transfer, make the directory and source files root-owned, and deny
group/other writes. Use a mode-0755 directory and mode-0644 Python files. Never
put a user-writable directory on the root service's import path.

Copy `latency_sitecustomize/sitecustomize.py` from the archive to the root of a
dedicated import directory and copy both `realtime_client/` and
`webrtc_sidecar/` beside it. The installed layout should be:

```text
/data/conf/codex-python/
  sitecustomize.py
  realtime_client/
    __init__.py
    config.py
    playback.py
    session.py
    sidecar.py
    websocket.py
  webrtc_sidecar/
    __init__.py
    __main__.py
    peer.py
    protocol.py
    runtime.py
```

Add `/data/conf/codex-python` to `PYTHONPATH` only for the
`python3 -m linux_voice_assistant` process. Set
`PYTHONDONTWRITEBYTECODE=1` on the same launch line: this prevents a permissive
device umask from creating a group/world-writable `__pycache__` beneath a
root-process import path. Back up the exact init script before changing its
launch line.

Build the deterministic aarch64/Python 3.11 dependency archive on a trusted
host, verify its separately recorded SHA-256, and install it atomically on the
device before enabling `device_webrtc`. Follow
[`webrtc-runtime.md`](webrtc-runtime.md); do not run ambient `pip install` on
the speaker. The installer admits only the complete hash-locked wheel set,
verifies a bounded per-file manifest, smoke-tests the exact `aiortc`, `av`, and
`pylibsrtp` versions plus SDP offer creation under the device interpreter as
UID/GID 65534 on a real root install, and swaps the
`/data/conf/codex-webrtc` link only after every check succeeds. Runtime
directories are root-owned mode 0755 and files mode 0644 so that unprivileged
smoke/sidecar processes can read but not modify them. Previous release
directories remain available for explicit runtime rollback.

At voice-process startup the overlay prewarms one isolated sidecar with
`/usr/bin/python3 -I -S`; every executable, source, runtime, and dependency
path must resolve to a root-owned file or directory with no group/other write
access. Source/runtime directories are mode 0755 and files mode 0644 so the
child can traverse and read them without modifying them. The launcher assigns
UID/GID 65534, removes supplementary groups, supplies a minimal fixed
environment, and uses umask 077. The child receives one bounded Unix
sequenced-packet descriptor and no credential in argv, environment, or IPC; it
cannot read the root-owned mode-0600 realtime configuration or runtime archive.
This is privilege separation, not a general filesystem/syscall/network
sandbox, so treat reviewed sidecar/native dependencies as trusted device code.
Prewarm failure does not weaken the route: the next Okay Computer wake fails
closed unless a valid child can be launched.

Create `/data/conf/codex-realtime.json` as a root-owned regular file with mode
0600. Start from [`codex-realtime.example.json`](codex-realtime.example.json),
replace the documentation address and placeholder token, then explicitly set
`enabled` to `true`. A minimal active configuration is:

```json
{
  "enabled": true,
  "url": "ws://192.0.2.10:8787/v1/realtime",
  "connect_address": "192.0.2.10",
  "token": "REPLACE_WITH_DISTINCT_REALTIME_DEVICE_TOKEN",
  "wake_phrase": "okay computer",
  "voice": "cove",
  "prompt": "Responde en español latinoamericano de México salvo que el usuario pida explícitamente otro idioma. Usa un acento mexicano natural, estable y claro; mantén separados el idioma y el acento, y no cambies de idioma solo por el acento del usuario. Sé conciso.",
  "media_transport": "device_webrtc",
  "full_duplex": true,
  "pulse_aec_source": "codex_echo_cancel_source",
  "pulse_aec_sink": "codex_echo_cancel_sink",
  "pulse_aec_method": "adrian",
  "aec_sink_volume_ceiling_percent": 25,
  "playback_volume_percent": 25
}
```

This example is disabled in the repository because enabling v3 also asserts
that the pinned runtime and physically qualified AEC topology are installed.
On the observed stock v1.1.7 PulseAudio build, `adrian` is the available engine;
the AEC names and 25% sink ceiling must exactly match the reviewed static block
and the device's media-player preference. The 25% playback value is retained
for v2 compatibility and must not exceed that ceiling. Do not set only
`enabled: true` until the
runtime verification, static topology checks, and physical echo/double-talk
canaries have passed.

Use `wss` with normal certificate validation whenever the path is not a
source-restricted trusted LAN. `connect_address` must be the numeric address to
which the client connects; the URL supplies the HTTP Host/TLS server name.
Unknown keys, symlinks, non-root ownership, group/other access, unsafe URLs,
and invalid bounds fail closed. `max_message_bytes` ranges from 2,048 through
65,536 bytes (the example and default use 65,536) and bounds WebSocket payload
bytes, excluding framing overhead; its minimum therefore carries exactly one
fixed 2,048-byte recorder frame. An absent or explicitly disabled config
leaves the direct client inactive while retaining the guarded wake-latency
patch.

To qualify full duplex, first follow [`deploy/README.md`](deploy/README.md).
The pinned PulseAudio server starts with `--disallow-module-loading`, and its
`default.pa.d` include occurs before the raw hardware masters; dynamic module
loading or a naive drop-in cannot establish the required startup order. The
guarded helper dry-runs by default, backs up the root config, and appends an
exact fail-closed block after the `hw:0,2` capture and `hw:0,1` playback
masters. Its `--aec-method` allowlist is `webrtc`, `speex`, and `adrian`; an
omitted flag means WebRTC and never triggers a fallback. The stock v1.1.7 image
must use `--aec-method adrian`. The helper never restarts services, changes
the live volume, or changes ADB. It requires an explicit 1–60% startup sink
value (`--aec-sink-volume-percent`, default 25) and writes its exact raw
PulseAudio value into the managed block after sink creation. The stock vendor
voice process subsequently applies its Home Assistant media-player preference,
which must be set to the same value and verified after restart. Once its static
and physical canaries pass, select the v3 transport and add the complete AEC
settings together:

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

To run this device at 60%, install the static block with
`--aec-sink-volume-percent 60`, set the sink ceiling to `60`, keep the retained
v2 playback value at or below `60`, set the
live AEC sink to 60% for the immediate canary, and repeat the complete physical
qualification before normal use. Also set the device's official Home Assistant
media-player entity to `0.6`; its persisted `sound.json` value is the later
post-startup writer and must remain `60`. The startup line uses raw `39321` as
the initial setpoint, and both layers must agree across restart or reboot.
Existing released blocks without a startup-volume line must be explicitly
removed and then reinstalled; the helper will not silently rewrite a different
managed block.
The legacy `aec_test_volume_percent` key remains accepted as an alias that sets
both values, but it cannot be combined with either explicit key.

PulseAudio object names must start with an ASCII letter, contain only ASCII
letters, digits, `.` or `_`, and be at most 128 characters. Supplying an AEC
route or method while full duplex is disabled, omitting either route while it
is enabled, selecting a method outside the three-value allowlist, combining the
legacy and explicit volume keys, or setting either volume outside 1–60 fails
configuration loading. Omitting the method in
full duplex selects WebRTC rather than detecting or substituting an available
engine. The shipped disabled configuration example intentionally names the
complete v3 route so its requirements are reviewable, but `enabled: false`
keeps it inactive until the runtime and AEC qualification are complete.

Optional `voice` and `prompt` settings are sent in either native v2 or v3 start
message.
Voice names start with an ASCII letter, contain 1–64 ASCII letters, digits,
`_`, or `-`, and are normalized to lowercase. Prompts are printable, non-empty
text up to 1,024 characters. Their
actual compact, ASCII-escaped start message must fit `max_message_bytes`, so a
small custom message bound may require a shorter prompt. Prompt text and the
bearer token are omitted from the configuration object's diagnostic
representation. The example selects the existing `cove` realtime voice and
keeps language and accent as separate instructions: Spanish is the default
language unless explicitly changed, while the voice keeps a stable, natural
Mexican accent instead of switching language merely because of the user's
accent. Omitting both settings preserves the provider defaults.

### V2 transport rollback

To roll back only the new device-owned WebRTC transport, leave the guarded
overlay installed and change the root-only configuration to:

```json
{
  "media_transport": "bridge_pcm",
  "full_duplex": false
}
```

Remove `pulse_aec_source`, `pulse_aec_sink`, and `pulse_aec_method` when
disabling full duplex; otherwise strict configuration validation rejects the
mix. Restart the long-lived voice process and verify that its start/started
frames negotiate [wire v2](../../protocol/realtime-wire-v2.md), binary 16 kHz
input, and binary 24 kHz output. The installed `/data/conf/codex-webrtc`
runtime can remain in place because `bridge_pcm` never launches it.

V2 preserves its historical pre-ready bounded Assist fallback. That is a
deliberate rollback behavior and a privacy/authority difference from v3: a v2
startup failure may replay its retained Okay Computer prefix into the official
Home Assistant path, while v3 always clears captured direct audio and returns
idle. Complete removal of the overlay is documented in
[Acceptance and rollback](#acceptance-and-rollback).

Enable both installed detector identifiers—`okay_nabu` and
`okay_computer`—in the device's existing sound configuration without replacing
its other settings. Keep Okay Nabu as the normal Home Assistant wake phrase and
make `wake_phrase` match Okay Computer. Do not assign the same phrase to both
paths.

The v1.1.7 unified init script keeps its supervision functions in a long-lived
shell. Editing the script does not update the function already held by that
monitor: a voice-only restart can briefly launch the overlay and then be
replaced by the stale monitor without `PYTHONPATH`. Refresh the unified monitor
once through the device's normal service manager (or reboot during an approved
maintenance window), then start the voice child from the updated definition.
When invoking the service manager through ADB, ensure its background monitor is
detached from the controlling shell rather than relying on a short-lived remote
shell job.

Verify the *long-lived* process PID after at least one monitor interval, its
`PYTHONPATH`/`PYTHONDONTWRITEBYTECODE` environment, source and config
ownership/mode/hash, absence of `__pycache__` in the import directory, and TCP
ADB connection. A physical wake must also show no
`wake_word_triggered_old.wav` playback. Merely importing `sitecustomize` in a
separate probe process is not acceptance.

Python imports `sitecustomize` during process startup. The script validates all
compatibility guards before mutating the vendor class. An import failure or
unknown bytecode is non-destructive: Python reports the error or warning and
the original wake implementation remains installed.

## Acceptance and rollback

The repository tests cover immediate request/duck/stream ordering, the first
post-wake microphone frame, VAD/run-end/disconnect/mute flag races during setup,
startup guards and exceptions, bounded direct-only pre-roll, normal-wake
discard, 32 KiB live headroom, normal-wake preemption, v3 fail-closed startup,
retained v2 pre-ready fallback/replay, strict v2 and v3 framing, device-owned
SDP negotiation, absence of bridge PCM in v3, bounded sidecar IPC and media
queues, receiver-owned `media.started`/`media.quiet` boundaries,
PulseAudio AEC preflight and once-per-session pre-negotiation sink preparation,
continuous full-duplex capture, RTP-before-start and stopped-before-tail
ordering, continued capture during local RTP fences, trusted-AEC-only
`response.interrupt`, exact qualifying-capture watermark ordering, 250 ms of
post-token child capture consumption, the overlapping 750 ms guard, a
receiver-owned fresh 500 ms decoded-RTP silence interval, reserved internal
lifecycle names, lifecycle-write failure, and the fixed absolute five-second
fail-closed deadline, fixed-argv non-blocking `paplay`,
immediate abort SIGKILL, retained v2 native/managed interruption acknowledgements and
fresh-session fallback, deterministic hash-locked runtime
build/install/rollback validation, guarded static-AEC installation and
rollback, interrupt cleanup, timer interruption, serialized non-blocking LED
execution, newest-state overload coalescing, DBus timeout/nonzero handling,
explicit worker shutdown, and the atomic unknown-bytecode fail-closed path.

These automated checks are not physical v3 acceptance. The following is the
required device acceptance matrix for a deployment; no completed repository
run is claimed yet.

On the physical device, verify these independently:

1. Okay Nabu still completes Home Assistant commands, timers, announcements,
   reconnects, and repeated wakes.
2. Okay Computer starts direct voice, speaks a response, returns to listening,
   and responds to the stop word without replaying its own output.
3. Okay Computer cannot control Home Assistant even when a compatibility
   realtime authority is registered. Its start and `started` frames both carry
   `conversation_mode: "native"`. Okay Nabu can invoke only the reviewed
   entities exposed through its official Assist pipeline.
4. A normal wake can preempt direct mode and then control Home Assistant.
5. A forced v3 pre-ready and post-ready failure clears captured Okay Computer
   audio, returns idle, and does not start or replay into Home Assistant. A
   subsequent explicit Okay Nabu wake must still work normally.
6. Repeated direct and standard turns leave one stable voice process, no stale
   direct or v2 `paplay` child, no LED/duck state behind, and bounded memory.
7. At the configured and previously qualified sink/playback value, speak over early,
   middle, and late response audio from near and far positions. Playback must stop at
   speech detection, the command must be recognized once, and response audio
   must not appear as user speech. Repeat after an idle gap and reconnect.
   For each newly negotiated direct session, the fixed-argv controller must set
   and verify the exact raw `playback_volume_percent` on that dedicated sink
   before the SDP offer. A forced preflight or preparation mismatch must fail
   before negotiation or outbound capture. Trace child execution to confirm
   that `response.created`, playback begin/resume, and interruption never run
   `pactl`; do not mutate the sink while the session is live. Direct `paplay`
   must target only that sink, use raw stream volume 65536, accept non-blocking
   20 ms writes with the reviewed 60 ms/20 ms settings, and never enumerate or
   mutate a sink-input.
8. During native v3 speech, exercise early, middle, and late trusted local
   AEC barge-in plus RTP-before-output-start and
   stopped-before-audio-tail timing. Prefix and tail audio must remain intact
   during normal playback. Interruption must drop queued PCM and SIGKILL
   `paplay` immediately in the parent, locally mute decoded RTP in the child,
   and continue microphone upload. The device data channel must
   send no `response.cancel`, `output_audio_buffer.clear`, or borrowed public
   Realtime client control; public Realtime `session.update` VAD configuration
   must remain rejected. Verify the observed direct data channel is not treated
   as an acknowledgement source when it yields only `session.started` and no
   speech-start, turn-completion, or transcript event. Same-peer unmute and
   `READY` may use only the explicitly empirical auto-truncation fence: the
   token must follow the exact qualifying capture watermark; the child must
   consume through that watermark and 4,000 samples beyond its token-time
   sender cursor; an overlapping 750 ms guard and a receiver-owned fresh 500 ms
   decoded-RTP silence interval must follow. Successful
   lifecycle writes then emit `interrupt.fenced`. Late RTP must restart the
   500 ms interval only. The
   absolute five-second deadline must not restart; timeout must stay muted and
   require a fresh peer/session. Verify that manual/non-speech preserving
   interruption cannot enter this fence. Repeat under adverse timing because
   automated fence tests do not physically validate the invariant.
9. A deliberately missing AEC module or mismatched default source/sink must fail
   direct startup before sending microphone audio to the bridge or provider,
   must not hand captured audio to Home Assistant, and must leave a later Okay
   Nabu wake usable.
10. TCP ADB remains reachable on port 5555 before deployment, after the service
   restart, and after any reboot. The overlay and its procedures must never
   stop `adbd`, blank its TCP-port setting, or remove that recovery path.

Before v3 was introduced, one v2 bridge-PCM post-deployment physical regression
canary replayed the exact previously
failing sample with 308 ms of silence between wake phrase and command. The
device-local input playback ran from 21:52:39 through 21:52:42, the bridge
completed its handshake in 1.308 s, and device-local answer playback began at
21:52:45. The command was captured and answered end to end. This single
controlled run validates only that historical v2 clipping regression; it is
not a latency distribution, benchmark, or v3 validation.

Reject the overlay if it clips initial phonemes, plays or forwards cue audio,
leaks microphone audio after session teardown, echoes its own response, fails to
return to idle, leaves stale player/LED/duck state, destabilizes wake detection,
or breaks the standard Home Assistant path.

To disable only direct mode, set `enabled` to `false` in the root-only config
and restart `voice-assistant`; the latency patch remains active. To keep direct
voice while rolling back v3, use the explicit `bridge_pcm` procedure above.
Full overlay rollback first sets `enabled` and `full_duplex` to `false`, removes
the exact managed PulseAudio tail with the guarded deploy helper, and performs
the controlled PulseAudio/service restart. Removing the process-specific
`PYTHONPATH` assignment and restarting only `voice-assistant` then allows
untouched vendor bytecode to supply the original behavior. The inactive
hash-addressed WebRTC runtime may be retained for diagnosis or removed only
after its exact `/data/conf/codex-webrtc` link and release directory have been
resolved and backed up. Preserve the PulseAudio, init-script, runtime, and
configuration backups until all physical tests pass. Verify TCP ADB on port
5555 before and after every operation.

## Rejected global playback-cache experiment

An earlier candidate reduced module-level mpv network-cache constants. One
uncached `tts.speak` comparison observed about 3.21 seconds of stock player
state overhead versus 3.07 seconds with that candidate, but the responses were
not identical and the difference was noisy. Those globals also configure the
music player, so the change could trade a small unproven announcement gain for
internet-radio underruns. The shipped overlay preserves the vendor playback
cache values.
