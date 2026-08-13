# ThirdReality v1.1.7 device overlay

This directory contains the guarded wake-latency patch and the Milestone 2
realtime client for the Python-based ThirdReality
`1.01.07`/upstream `v1.1.7` image. This image is a Python 3.11, aarch64
Buildroot Linux system, not Android. The active design keeps the speaker small:
the root voice process owns the standard-library session/overlay code, native
AEC3, capture, playback, LED, and cue. `aiortc`, SDP, App Server, OAuth, and
provider lifecycle run on the local server. The overlay does not replace
firmware, add a separately supervised daemon, or change Home Assistant.

The current controlled deployment enables one wake route:

| Wake phrase | Route | Capability |
|---|---|---|
| Okay Nabu | `realtime_only` native [realtime wire v2](../../protocol/realtime-wire-v2.md) PCM to the Codex Voice bridge | Server-owned WebRTC, continuous full-duplex audio, stable-socket provider rollover, and the sole terminal `end_conversation` tool |

Home Assistant Assist/Hermes and Home Assistant entity tools are deferred for
this trial. An optional split configuration can be restored later, but it is
not the active deployment. The legacy terminal
stop-word detector is suspended for the entire direct ownership window, so the
wake tail cannot cancel signaling and playback echo cannot tear down the live
conversation. The explicit `end_conversation` tool is the sole spoken terminal
control. Its exact prior detector membership is restored on release. Mute,
disconnect, idle expiry, tool-requested termination, or otherwise releasing the
vendor owner flushes local playback, tears down the remote session, and queues
the idle LED. A later accepted wake creates a fresh WebSocket, peer, and
realtime thread.

The active configuration is strict-v2 `bridge_pcm`, native mode, full duplex,
native AEC3, +12 dB bounded post-AEC capture gain, and a fixed 100%
sink/playback anchor. The stream itself stays at 100% relative volume and one
non-amplifying software stage gives the physical buttons their full 0–100%
range. During provider
speech, two-frame qualified near-end detection cuts local `paplay` immediately
and sends one exact `{"type":"provider_barge"}` boundary without stopping
capture or closing the device WebSocket. The server sends `response.cancel`
then `output_audio_buffer.clear` on the same provider peer while the complete
replacement utterance continues upstream.

An accepted wake claims the vendor owner and pulses the LED. Up to three
startup attempts share one absolute 12-second owner deadline. Capture stays
closed and wake-tail PCM is discarded until the bridge has fully established
the App Server/WebRTC session and returned exact strict-v2 `started`. The
speaker then plays the pinned acknowledgement cue once; cue EOF switches the
LED to listening and opens capture. Any failure returns to a known idle state,
with no Assist/Hermes fallback or pre-ready replay.

## Active realtime behavior and bounds

[`realtime_client`](realtime_client) sends binary 16 kHz mono PCM16 to the
bridge and receives 24 kHz mono PCM16 inside monotonic speaking epochs. It
keeps the microphone open during playback. The device WebSocket, owner, LED,
capture, and playback controller remain live through interruption; the bridge
strictly stops and replaces the provider peer behind them. A matching
`thread/realtime/closed` confirmation within the 100 ms reuse grace permits
same-thread context reuse, while an ambiguous close is isolated on a fresh
thread. No second wake is required, and the path does not depend on provider
VAD or a cancel acknowledgement.

Both the client and bridge fix `conversation_mode: "native"`. The bridge
ignores Home Assistant authority and exposes exactly one empty-input tool,
`end_conversation`. Clear Spanish or English requests to finish the call end
the socket and return the device to idle. No entity tool, transcript executor,
or `appendSpeech` handoff participates.

The 64 KiB input queue bounds only post-cue live scheduling pressure; it is not
a startup backlog. The 48 KiB playback queue is about 1.024 seconds of 24 kHz
mono PCM16. Stop, mute, disconnect, hard lifetime expiry, and every terminal
transport or queue error clear both without persisting audio. The server's
2,250 ms input-track bound is enforced when it consumes active v2 PCM.

Native AEC3 is selected early from the root-owned mode-0600 configuration so
the physical microphone/render-reference relationship remains local. +12 dB
capture gain is saturating and applied only after cancellation, immediately
before LAN transmission. Playback uses the dedicated AEC sink and `paplay`
stream at a fixed 100% anchor, plus a non-amplifying software attenuator for the
saved button level. The active path requires a worst-case physical echo,
normal-distance, and early/middle/late interruption canary at full output.

The dormant v3 runtime described below is not launched by `bridge_pcm`.

## Dormant device-owned WebRTC experiment (historical)

[`realtime_client`](realtime_client) selects one explicit media transport:

- `device_webrtc` uses protocol v3. The device sidecar owns the `aiortc`
  `RTCPeerConnection`, bidirectional audio transceiver, and ordered
  `oai-events` data channel. The authenticated bridge WebSocket carries the SDP
  offer/answer, `transport_ready`, `started`, ping, stop, and sanitized terminal
  errors only. It never carries PCM or raw provider events.
- `bridge_pcm` is now the active protocol v2 transport. The voice process sends
  16 kHz mono PCM16 to the bridge and receives bridge-gated 24 kHz mono PCM16.
  See [the v2 contract](../../protocol/realtime-wire-v2.md).

Both transports hardcode `conversation_mode: "native"`. The bridge ignores any
Home Assistant broker snapshot and never inserts a completed-transcript wait,
executor thread, or `thread/realtime/appendSpeech` render. Native v3 creates one
App Server realtime voice thread per peer epoch with exactly one dynamic tool,
`end_conversation`; it accepts only an empty-object call to that tool and
rejects every other provider tool request. An explicit spoken request to stop,
end, close, leave, or a clear goodbye invokes that terminal tool. The result
ends the socket and normal device cleanup returns the LED and microphone owner
to idle. No Home Assistant entity tool is declared or reachable. Native v2 now
uses that same sole-tool policy.

The v3 child accepts timestamped 16 kHz mono PCM16 from the vendor capture
callback. It continuously reframes those variable callback boundaries into
exact 20 ms / 320-sample source frames, repeats each sample into one 960-sample
48 kHz frame, and therefore gives pinned aiortc exactly one Opus payload and
one advancing RTP timestamp per `recv()`. It decodes provider audio to 24 kHz
mono PCM16. On the media/data path it passes only bounded playback packets and
sanitized lifecycle metadata back over a Unix `SOCK_SEQPACKET` socket; the
signaling path also carries offer/answer SDP. No long-lived application credential—the
Home Assistant token, route-scoped device bearer, or Codex OAuth credential—nor
prompt, transcript/model text, tool data, or raw provider data-channel payload
crosses the child IPC boundary. The SDP contains ephemeral ICE credentials and
DTLS negotiation material.
The parent uses a bounded direct player pinned to the configured AEC
sink; media never traverses the bridge WebSocket.

The configured input queue remains bounded at 64 KiB, or 2.048 seconds of
16 kHz mono PCM16, but direct v3 does not use it as a cold-start capture buffer.
An accepted v3 wake discards the triggering/wake-tail PCM, and every recorder
frame produced before the ready cue reaches EOF is dropped. Capture opens with
an empty initial queue. After the cue, the queue bounds live scheduling pressure
and rollover capture; paced catch-up may drain such an accepted live backlog at
no more than 2× capture rate. Older optional v2 split configurations retained
a pre-ready buffer and Assist replay; active native/realtime-only v2 discards
pre-ready PCM instead.

Do not hand a complete 64 ms recorder callback directly to aiortc's Opus
encoder. In pinned aiortc 1.15 that callback produces three or four payloads,
but `RTCRtpSender` assigns every payload from one `recv()` the same timestamp.
An exact-WAV regression reconstructed only 1.6 seconds from 5.184 seconds of
input before the 20 ms reframer was added. The production regression requires
one payload per frame and consecutive 960-sample RTP timestamps.

Wake activation occurs after the pinned recorder callback has already handled
the triggering frame. The overlay may retain the newest six 2,048-byte idle
frames in its generic compatibility ring, but a direct v3 wake atomically
discards all six: no part of that 384 ms / 12 KiB history seeds the initial
peer. It also drops every pre-ready callback, so speaking during negotiation or
the cue is intentionally not accepted. The 32 KiB startup-headroom calculation
and any initial pre-roll transfer apply only to older v2 compatibility. Stop,
mute, disconnect, teardown, and every v3 failure clear remaining capture
without forwarding, persisting, logging, or handing it to Home Assistant.

This initial startup rule is separate from interruption rollover. Once a v3
session is live, the local AEC detector deliberately freezes exactly 4 KiB /
128 ms of recent live capture around a committed interruption and delivers it
once to the replacement peer as described below. That rollover pre-roll is not
wake pre-roll and never exists before the first cue-gated live phase.

An accepted direct wake immediately queues the non-blocking thinking/pulsing
LED and claims the vendor owner. At most three session attempts share one
absolute 12-second owner deadline; each session retains its own configured
10-second signaling-handshake bound inside the remaining owner budget. Process
prewarm only proves that each isolated `Popen` child is still alive. It does
not create or validate an SDP offer; the selected child must do that during the
owned attempt, and construction, preflight, offer, bridge, or transport failure
can consume an attempt.

`RealtimeSession.ready` is set only after the SDP answer is applied, the peer
is connected, the `oai-events` data channel is ready, the device has sent
`transport_ready`, and the bridge has returned the exact accepted `started`
message. The vendor stop detector is suspended for the entire direct ownership
window so the wake tail cannot immediately cancel signaling and playback echo
cannot end a live session. The explicit `end_conversation` tool is the sole
terminal voice control. Readiness then starts exactly one pinned, root-owned cue:
`/usr/lib/python3.11/site-packages/sounds/wake_word_triggered_old.wav`, SHA-256
`6b25dd2abaf7537865222ca9fd6e14fbf723458526fb79bbe29d8261d1320724`, PCM16
mono at 22,050 Hz and about 0.400 seconds. Capture stays closed until cue EOF.
EOF switches the LED to listening and opens live capture. The cue has a
2-second completion timeout. Cue failure/timeout, a terminal session, the
absolute deadline, or attempt exhaustion fails closed and returns idle without
starting Home Assistant.

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
master format. `capture_backend` is either `pulseaudio_aec` (the compatibility
default) or `native_aec3`. The PulseAudio backend also requires an uncorked
capture stream owned by the current voice-process PID to use that AEC source.
The normal native selector is `capture_backend: "native_aec3"` in the enabled,
root-owned mode-0600 `/data/conf/codex-realtime.json` with
`media_transport: "device_webrtc"`. `CODEX_AEC3_CAPTURE=1` is only an explicit
service-environment override; it is not required when that secure configuration
selects the backend. The early overlay hook loads and patches the native ABI
before vendor microphone selection, then sets `CODEX_AEC3_ACTIVE=1` internally
as proof for the later session preflight. Operators must not set that proof
variable themselves. A wake additionally proves that the recorder delivers
frames. Both backends require every live rendered-PCM sink channel to be at or
below `aec_sink_volume_ceiling_percent`. V3 `paplay` playback remains pinned to
the configured virtual AEC sink. Active `bridge_pcm` with proven `native_aec3`
capture instead pins playback and volume to its raw
`alsa_output.hw_0_1` master: AEC3 receives the same rendered audio through the
codec's synchronized `hw:0,4` hardware-loopback channels, without sending it
through a redundant PulseAudio echo-cancel render path. The configured virtual
source, sink, default routes, and module remain required as startup topology
proof. Without the internal native-active proof, sink selection stays virtual
and preflight fails closed before any microphone audio leaves the device.
At direct-session startup, after that preflight and before the SDP offer or
bridge connection, a fixed-argv `pactl` controller sets the dedicated AEC sink
itself to the exact raw `playback_volume_percent` value and verifies it. Every
new direct response checks that exact raw anchor again before admitting audio;
an out-of-band mismatch is repaired and verified or output fails closed. The
per-attempt ten-second handshake deadline begins only after this local AEC and
player preparation, while the absolute 12-second owner deadline covers local
work and all retries. The maximum-session clock remains a separate hard
lifetime cap. The direct `paplay` child targets that sink with
`--volume=65536` (100% relative) and never
enumerates or mutates a sink-input. Both volume controls accept 1–100%, and
configuration rejects a playback value above the selected
`aec_sink_volume_ceiling_percent`. Active v2 instead uses its resolved playback
sink's fixed anchor and 100%-relative stream with software attenuation.
Matching Home
Assistant volume, mute, and unmute commands are intercepted before either
vendor media player or its system-volume callback can change PulseAudio. The
pinned physical-button path is guarded separately: its existing settings loop
runs every 50 ms instead of 500 ms, and a changed logical volume is reconciled
before the two-frame (128 ms) local-interruption boundary. Reconciliation
restores and verifies the exact AEC anchor, moves the requested level into the
software attenuator, and updates the vendor/Home Assistant state without an
MPV volume setter. Direct `pactl` mutations remain unsupported operator action;
the per-response exact check is the fail-closed backstop. The requested audible level is
capped at `playback_volume_percent` and applied as non-amplifying software
attenuation immediately before each bounded 20 ms write. The attenuation uses
PulseAudio's cubic volume curve, so a requested level retains the vendor
slider's perceived loudness below the fixed anchor. A 40 ms gain ramp prevents
clicks, while the dedicated sink and `paplay --volume=65536` remain at their
qualified fixed operating point. This keeps the active echo canceller's render
reference equal to the PCM that reaches the selected playback sink.

The parent also retains a bounded 4 kHz representation of only the transformed
PCM actually accepted by `paplay`. During the existing 512 ms first-playback
AEC convergence window, high-correlation capture frames calibrate a narrow
render-delay and residual-echo model. After that window, only capture matching
that previously learned model is rejected as local self-echo; missing, stale,
quiet, uncalibrated, or genuinely independent speech fails open. Ambiguous
evidence requires four consecutive 64 ms callbacks, while clear near-end
speech keeps the two-callback interruption path. Raw microphone bytes remain
unchanged in the bounded device queue and local detector. With active native
AEC3, the parent never zeroes or otherwise rewrites accepted bridge PCM based
on this render classification: every frame continues through the configured
outbound gain, and the server retains its own already-resampled rollover
pre-roll. The classifier only qualifies the local cut and fail-closed anchor
guard. The PulseAudio-AEC compatibility backend retains the narrower prior
behavior of sending affirmative echo/ambiguous evidence as equal-length silence
to the current provider peer. Sample indices, capture timestamps, cadence, and
packet lengths do not change. Software-only volume changes add no detector
pause or blanket microphone blackout and duplicate requests do not extend a ramp.
Repair of a real physical sink excursion keeps
trained FIR coefficients only as an untrusted seed, discards the prior delay,
render timing, and pending local evidence, then searches the complete 20–320 ms
range. Three consecutive render-backed signal frames with stable delay,
correlation, and bounded third-frame residual requalify the path. The first
128 ms suppresses stale local-transition evidence; decorrelated near-end audio
can still pass the current-peer filter, and clear near-end speech is locally
interruptible after that boundary. Eight unsuccessful correlated/ambiguous
evidence frames fence output, while quiet capture, muted software playback, and
`media.quiet` retain the pending repair without consuming that bound. The guard
changes no active-native-AEC3 wire PCM. With the PulseAudio-AEC compatibility
backend, it may change only current-provider wire PCM; after a committed cut,
subsequent capture again flows normally to the stable socket. The existing
outbound-gain stage remains unchanged. Only bounded content-free decision and
suppression counts, correlation, and delay are logged. Direct volume persistence shares
`/tmp/sound_config.lock` with the physical button script and atomically replaces
`sound.json`. Every persisted change arms one next-tick exact anchor check that
clears without another write, preventing either ordering of a concurrent
physical key transaction from concealing sink drift or creating a poll loop.
The bounded level is reflected and persisted through the normal Home Assistant
entity, and each new direct session starts at that saved level.
The preflight and exact preparation compare raw PulseAudio units rather than
trusting rounded displayed percentages.

`direct_capture_gain_db` is an explicit post-AEC outbound microphone gain from
0 through 12 dB. Active bridge PCM uses 12 dB; dormant v3 may use the same
bounded stage. Gain is applied after the 16 kHz AEC capture frame is assembled
and before LAN transmission or WebRTC resampling, using saturating PCM16 arithmetic; it
does not change the wake detector, Home Assistant Assist audio, or the local
barge-in detector. Keep it at 0 unless privacy-safe peak/RMS telemetry and a
physical speech canary demonstrate that provider input is missing low-level
speech. Start such a canary at 6 dB, test close/normal/far and loud speech plus
playback echo and interruption, and increase no further than the 12 dB hard
limit only when the 6 dB evidence still requires it. This is fixed gain, not
automatic gain control, so it cannot pump ambient noise or AEC residual between
utterances.

V3 owns at most one fixed-argv `paplay` child at a time. It accepts raw 24 kHz
mono signed-16-bit PCM, requests 60 ms latency and 20 ms process time, and uses
non-blocking stdin writes of at most 20 ms per network-loop service pass. A
receiver-quiescence media boundary can reuse that child so RTP tail is not
discarded. Interruption clears pending PCM, closes stdin, and issues immediate
SIGKILL without blocking the realtime loop; reap remains separately bounded.

The first audible playback on each fresh sidecar peer starts one 512 ms
physical-AEC convergence guard. The sidecar continues consuming and emitting
exact 20 ms capture frames, but zero-fills frames whose original capture
timestamps fall inside that window. Their PTS/RTP timeline, cadence, freshness
checks, and consumption accounting are preserved. At the same new `paplay`
onset, the parent ignores local two-frame barge-in evidence until the matching
deadline. A later `media.started` after receiver quiet that resumes the same
active child does not restart or extend either guard. This is onset-only
protection: normal full-duplex capture and local barge-in resume after 512 ms,
and the separate post-interruption rearm rule below is unchanged.

The physical before/after canary exposed the startup echo directly. Without
the guard, playback stopped after 22 20 ms packets (about 0.44 seconds) and the
response remained unfinished. With the guard, it delivered 626 packets (about
12.52 seconds), completed both turns, reported `session.started=1`, and did not
roll over.

Provider response/output lifecycle does not label, gate, or retire the normal
RTP lane. The decoded receiver is one continuous media lane: its first
audible-scale PCM frame emits transcript-free `media.started`, and only another
audible-scale frame resets the quiet timer. Exact digital silence and decoded
Opus residue below both the 64-sample peak and 8-sample RMS bounds are neither
played nor semantic; about 120 ms without qualifying PCM emits `media.quiet`.
A later qualifying frame opens a new local media generation. This preserves
audible prefixes received before provider output-start and tails received after
provider stopped events without letting sub-audible keepalives hold `paplay` or
the LED open. Every decoded RTP frame still advances the independent receiver
fence, so this normal-generation boundary is not an interruption
acknowledgement and does not authorize peer reuse.

While verified full duplex is active, capture remains continuous during
playback. Two consecutive 64 ms AEC-filtered microphone frames that meet the
bounded peak and sustained-energy checks kill local playback in the parent and
drop queued playback IPC. It retires the old PeerConnection epoch and sends no
later capture to that peer. The outer vendor owner, device session, logical player, bridge
WebSocket, and ready latch remain attached. Exactly one reusable sidecar
process holds the active peer plus at most one fresh, offer-warm standby peer.
The first standby is gated until initial readiness, cue completion, and
capture-open. An ordered promotion fences and stops the retired peer, promotes
the exact standby epoch, and routes later capture only to it; the same worker
then prepares the following standby. The hard process cap remains one. The parent
freezes exactly 4 KiB (two 64 ms frames, 128 ms) of recent AEC
pre-roll through the trigger and queues live speech, then delivers those samples
once and in capture order to the replacement peer.
The committed interruption also disarms the local detector across the peer
boundary. Continuing speech cannot retire the replacement; eight consecutive
detector-quiet 64 ms callbacks (512 ms) rearm it for a new speech edge, while
qualifying signal before the eighth resets the quiet count.

Capture timestamps are checked again when the replacement RTP track actually
consumes each packet. A packet older than 2.25 seconds is a content-free
terminal failure even if it passed queue admission. The logical standby is
validated during the active epoch and again immediately before use; failure or
an unexpected peer epoch disqualifies it and terminates the outer session
rather than launching a replacement worker.

Replacement lifecycle and PCM received during signaling share an ordered
buffer capped by configured `output_queue_bytes`. Nothing in that buffer is
audible or mutates the player before the exact epoch-matching
`rollover_started`; after that acknowledgement it is replayed once and in order
through the normal handlers. Overflow or invalid ordering fails closed.

The initial peer is implicit epoch 1. Rollover uses exact consecutive
`rollover`, `rollover_answer`, `rollover_transport_ready`, and
`rollover_started` controls without changing the initial v3 message shapes.
Deploy the bridge first because an old bridge cannot advertise or accept the
extension. Queue/age/timeout, sidecar, or invalid-epoch failure closes the
outer session. Protocol and epoch numbers require exact integers; floats and
booleans are rejected. Manual stop, mute, disconnect, and
non-speech interruption also end it; `stop` remains normal termination in
every rollover phase. No path hands direct audio to Home Assistant or logs it.

Sidecar close never waits indefinitely. If its bounded shutdown expires after
kill, `waitpid` ownership transfers to a daemon reaper so the vendor/realtime
thread can continue without leaving the child unowned. This device-process
budget is separate from the bridge App Server close-confirmation barrier below.

The bridge gives the old realtime session 100 ms to produce its
`thread/realtime/closed` notification. Confirmed closure permits same-thread
reuse and reports `context_retained: true`; timeout, error, or an absent close
transfers the old epoch to tracked isolated-thread cleanup, starts the
replacement on a new thread, and reports false. Context retention does not
guarantee audible-history correctness: interrupted unheard
assistant output can remain in provider context, and recent pre-roll can
overlap samples seen by the old peer.

The direct Frameless channel has no public cancel/truncate control or provider
interruption acknowledgement. A synthetic same-peer canary was rejected when
old RTP continued beyond the five-second media-fence deadline. The former
`response.interrupt`/`interrupt.fenced` experiment is retained only as rejected
evidence, not production behavior. Fresh-peer rollover is a safe
subscription-backed approximation, not exact ChatGPT same-session semantics,
and it adds a measurable negotiation handoff. A historical two-worker build
passed a reference-device hardware double-interruption canary twice with the
exact artifact at that installation's qualified 60% setting. Four local cuts
were 208–211 ms and four rollovers were 1.29–1.57 s; each run recycled its same
two worker PIDs without a cold replacement and retained context twice. Those
measurements do not physically validate the current single-worker build and do
not replace the per-installation acceptance matrix. The historical device-side
stop acknowledgement was separate from the bridge's 100 ms App Server
close-confirmation barrier. See the
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
same session. That result does not validate active native-AEC3 v2 and does not
qualify another installation. Dormant v3 also has the
reference-device hardware double-interruption result above at that
installation's qualified 60% setting, alongside automated
protocol, sidecar, queue, barge-in, cleanup, and runtime-install coverage. It
does not qualify another speaker or replace the full acceptance matrix. Install
and qualify the static PulseAudio AEC assets in [`deploy`](deploy) first. The
active reference fixes the sink/playback anchor and relative stream at 100%,
while a saved user level such as 80% is non-amplifying attenuation below that
anchor. The full-output acoustic canary must pass before normal use; schema
support alone is not physical qualification. Speex is available
only on a different firmware build that actually compiles that engine.

The production-qualified fallback remains this static Adrian topology plus the
bounded render-aware guard described above. Selecting native AEC3 enters its
canary-first path; it does not inherit Adrian's acoustic qualification. The
native hardware-loopback AEC3 slice under
[`aec3_capture`](aec3_capture) is disabled by default. The overlay has an early,
fail-closed startup hook, but installing its files alone does not select it;
the normal selector is `capture_backend: "native_aec3"` in the secure enabled
realtime configuration. `CODEX_AEC3_CAPTURE=1` remains an explicit override for
controlled use, not required service setup. Keep Adrian available as the
playback-DMA keepalive during initial qualification, and do not call the native
path production-qualified until playback-only, near-end, double-talk, and
long-run gates pass on the physical unit.

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
retains the older LED-only, cue-free latency path for the optional turn-based
Assist route. Active native v2 deliberately uses a deterministic boundary:
the accepted Okay Nabu wake immediately queues the thinking/pulsing LED, drops
all wake and connecting PCM, and waits for exact session readiness before
playing the pinned roughly 0.400-second cue once. Only its EOF queues the
listening LED and opens capture. The ThirdReality LED command is queued on one
daemon worker, so the DBus subprocess cannot block microphone capture.
Commands are serialized and bounded by the vendor's two-second timeout. If the
bounded queue fills, stale pending animations are coalesced into the newest
state; timed-out children are reaped. Both the vendor base class and the pinned
ThirdReality subclass are patched directly so later base-method rebinding does
not bypass either guarded lifecycle.

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
aec3_capture/
  CMakeLists.txt
  README.md
  THIRD_PARTY_NOTICES.md
  __init__.py
  build_aarch64.py
  recorder.py
  cmake/
  include/
  src/
deploy/
  README.md
  prepare_mic_gain_boot.py
  prepare_pulseaudio_aec.py
  init/
    S49codex-mic-gain
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
The active v2 client also emits `User-Agent:
ha-codex-voice-thirdreality/2`. Native AEC3 barge-in cuts playback locally and
keeps the causal PCM in order on the same socket while the server replaces the
provider generation. Manual stop/mute/disconnect remains terminal.
Historical v3 performed that rollover in the device sidecar through wire-v3
signaling instead of on the bridge media server.

The device never receives tool schemas, tool calls, results, or a Home
Assistant credential. The native App Server thread alone receives the exact
empty-input `end_conversation` declaration; the bridge executes it as terminal
session control and rejects every other tool. No Home Assistant tool is
available. **Provide Home Assistant tools to realtime voice** applies only to
older strict-v2 clients that omit `conversation_mode`. The integration may
maintain that compatibility broker, but its presence does not affect the
active explicit-native v2 session and adds nothing to device configuration or
signaling.

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

### Dormant v3 runtime (not required for active v2)

The following runtime procedure is retained for the disabled `device_webrtc`
experiment. Active `bridge_pcm` never launches the sidecar and does not require
this archive.

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

At voice-process startup the overlay prewarms exactly one reusable isolated
sidecar process with `/usr/bin/python3 -I -S`. This idle prewarm proves only
that the `Popen` is alive; it does not request, drain, or validate an SDP offer.
That worker creates and validates its initial peer offer only after an accepted
wake owns startup. Once the peer is ready and the confirmation cue has completed
and opened capture, the same process may create one bounded, offer-warm logical
standby. Rollover promotes it in place and then prepares the following standby.
An absent or invalid required standby terminates the outer session; the client
does not launch another process at that rollover boundary. Every
executable, source, runtime, and dependency
path must resolve to a root-owned file or directory with no group/other write
access. Source/runtime directories are mode 0755 and files mode 0644 so each
child can traverse and read them without modifying them. The launcher assigns
UID/GID 65534, removes supplementary groups, supplies a minimal fixed
environment, and uses umask 077. The child receives one bounded Unix
sequenced-packet descriptor. No long-lived application credential is placed in
argv or the environment or sent through IPC; offer/answer SDP crosses IPC and
contains ephemeral ICE credentials and DTLS negotiation material. The child
cannot read the root-owned mode-0600 realtime configuration or runtime archive.
This is privilege separation, not a general filesystem/syscall/network
sandbox, so treat reviewed sidecar/native dependencies as trusted device code.
Prewarm failure did not weaken that historical route: an absent or invalid
standby terminated the outer session without another worker.

### Active v2 configuration

Create `/data/conf/codex-realtime.json` as a root-owned regular file with mode
0600. Start from [`codex-realtime.example.json`](codex-realtime.example.json),
replace the documentation address and placeholder token, then explicitly set
`enabled` to `true`. The repository template already shows the active Okay Nabu
realtime-only route; this is the minimal active configuration:

```json
{
  "enabled": true,
  "url": "ws://192.0.2.10:8787/v1/realtime",
  "connect_address": "192.0.2.10",
  "token": "REPLACE_WITH_DISTINCT_REALTIME_DEVICE_TOKEN",
  "wake_phrase": "okay nabu",
  "realtime_only": true,
  "wake_probability_cutoff": 0.85,
  "voice": "cove",
  "prompt": "Responde en español latinoamericano de México salvo que el usuario pida explícitamente otro idioma. Usa un acento mexicano natural, estable y claro; mantén separados el idioma y el acento, y no cambies de idioma solo por el acento del usuario. Sé conciso.",
  "handshake_timeout_seconds": 10,
  "media_transport": "bridge_pcm",
  "capture_backend": "native_aec3",
  "barge_in_mode": "provider_control",
  "full_duplex": true,
  "pulse_aec_source": "codex_echo_cancel_source",
  "pulse_aec_sink": "codex_echo_cancel_sink",
  "pulse_aec_method": "adrian",
  "aec_sink_volume_ceiling_percent": 100,
  "playback_volume_percent": 100,
  "direct_capture_gain_db": 12
}
```

The public template is disabled in the repository so installation remains an
explicit act. Active v2 does not require the pinned sidecar runtime, but it
does assert that native AEC3 and the physically qualified AEC/playback topology
are installed.
On the observed stock v1.1.7 PulseAudio build, `adrian` is the available engine;
the AEC names and 100% sink ceiling must exactly match the reviewed static block
and the device's media-player preference. Playback must not exceed that
ceiling. Do not set only
`enabled: true` until the
native-AEC3 build verification, static topology checks, and physical echo/double-talk
canaries have passed.

The active reference uses a 100% fixed physical anchor so the speaker buttons
retain their full range. Its previous 60% result is historical and does not
qualify the new full-output path or another speaker or room.

The configured playback value is the qualified physical anchor, not a hardcoded
audible level. While direct realtime owns audio, Home Assistant may select any
level from silence through that anchor without moving the AEC sink; higher
requests are reported and persisted at the anchor. Qualified deployments may
set both configuration values up to the enforced 100% maximum, but every
anchor increase requires a new physical echo/double-talk canary. Render
correlation does not make an unqualified higher anchor safe: downstream
clipping is nonlinear and can no longer resemble the reference.

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

When omitted, `idle_timeout_seconds` defaults to 120 seconds and
`max_session_seconds` defaults to 900 seconds. The direct-session idle clock
starts after the transport is ready and is refreshed by semantic microphone,
playback, or lifecycle activity—not pings or sub-audible decode residue. The
hard clock starts before local AEC/player preflight and covers all startup,
runtime, and rollover work. Deployments may lower the values within their
enforced 5–120-second and 15–900-second ranges, respectively.

`wake_probability_cutoff` is optional and accepts 0.5–0.99. It applies only to
the exact normalized configured realtime phrase. The current trial uses the
installed Okay Nabu detector and `realtime_only: true`; every accepted match
selects direct v2 and no Assist detector is active. In a later split deployment,
the guarded ordering can prioritize a distinct Okay Computer realtime detector
when several active models become ready in the same recorder block.

For a reversible realtime-only trial, set `wake_phrase` to `okay nabu`, set
`realtime_only` to `true`, and leave only `okay_nabu` active in
`/data/conf/sound.json`. This intentionally replaces the normal Assist wake
route without removing any Assist code. A matching Okay Nabu detection starts
the direct session; every other detector is ignored, a realtime guard mismatch
fails closed, and the v2 buffered Assist fallback is disabled. Restore split
mode by setting `realtime_only` to `false`, returning `wake_phrase` to a
distinct phrase such as `okay computer`, and re-enabling both detector IDs.

Before physical input qualification, install the guarded early microphone-gain
hook described in [`deploy/README.md`](deploy/README.md). The pinned firmware
otherwise writes the configured PDM gain only after PulseAudio has opened
capture, so `amixer` can report the requested value while the live samples
still use the boot default. The hook is a separate `S49` init file; it does not
modify vendor boot scripts, restart a service, or touch ADB. Its 0–100 integer
preference validation falls back to the vendor's 30% default rather than
clamping malformed data. Reopen ALSA capture—normally with a controlled reboot
that exercises the persistent boot ordering—and run an acoustic capture
canary. A separately controlled PulseAudio reopen can also latch the value; a
voice-only restart and `amixer cget` are not proof that the codec did so.

To qualify full duplex, also follow the static AEC procedure in that deployment
guide.
The pinned PulseAudio server starts with `--disallow-module-loading`, and its
`default.pa.d` include occurs before the raw hardware masters; dynamic module
loading or a naive drop-in cannot establish the required startup order. The
guarded helper dry-runs by default, backs up the root config, and appends an
exact fail-closed block after the `hw:0,2` capture and `hw:0,1` playback
masters. Its `--aec-method` allowlist is `webrtc`, `speex`, and `adrian`; an
omitted flag means WebRTC and never triggers a fallback. The stock v1.1.7 image
must use `--aec-method adrian`. The helper never restarts services, changes
the live volume, or changes ADB. It requires an explicit 1–100% startup sink
value (`--aec-sink-volume-percent`, default 25) and writes its exact raw
PulseAudio value into the managed block after sink creation. The stock vendor
voice process subsequently applies its Home Assistant media-player preference,
which must be set to the same value and verified after restart. Once its static
and physical canaries pass, select the active v2 transport and add the complete AEC
settings together. Normally select native hardware-loopback capture in the
root-owned mode-0600 realtime configuration itself; no voice-service
environment edit is required:

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
  "direct_capture_gain_db": 12
}
```

`CODEX_AEC3_CAPTURE=1` remains an explicit environment override of the early
selection hook for controlled diagnostics. It does not replace the validated
`capture_backend` session contract. `CODEX_AEC3_ACTIVE=1` is
internal proof written only after the early recorder patch succeeds; do not set
it in the service environment.

For the full-range reference, install the static block with
`--aec-sink-volume-percent 100`, set the sink ceiling and active v2 playback
anchor to `100`, and set the live AEC sink to 100% for the worst-case acoustic
canary. The saved device level may start at 80%; it is implemented by the one
runtime attenuator and the physical buttons can still reach 100%. The startup
anchor uses raw `65536` and must remain fixed across restart or reboot.
Existing released blocks without a startup-volume line must be explicitly
removed and then reinstalled; the helper will not silently rewrite a different
managed block.
The legacy `aec_test_volume_percent` key remains accepted as an alias that sets
both values, but it cannot be combined with either explicit key.

PulseAudio object names must start with an ASCII letter, contain only ASCII
letters, digits, `.` or `_`, and be at most 128 characters. Supplying an AEC
route or method while full duplex is disabled, omitting either route while it
is enabled, selecting a method outside the three-value allowlist, combining the
legacy and explicit volume keys, or setting either volume outside 1–100 fails
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

### Active v2 transport and dormant-v3 rollback

The current root-only configuration keeps the guarded overlay installed and
selects the server-offloaded transport:

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
  "direct_capture_gain_db": 12
}
```

Restart only the long-lived voice process and verify that start/started frames
negotiate [wire v2](../../protocol/realtime-wire-v2.md), binary 16 kHz input,
binary 24 kHz output, full duplex, and explicit native mode. The installed
`/data/conf/codex-webrtc` runtime can remain in place as a dormant rollback
artifact because `bridge_pcm` never launches it.

Active native/realtime-only v2 discards wake-tail and pre-ready PCM and never
falls through to Assist. Older non-realtime-only split configurations may
retain compatibility replay; that is not an active acceptance target. Complete
overlay removal is documented in [Acceptance and
rollback](#acceptance-and-rollback).

In an optional split mode, enable both installed detector
identifiers—`okay_nabu` and `okay_computer`—in the device's existing sound
configuration without replacing its other settings. That future design must
give Assist and realtime distinct phrases. The current deployment keeps only
Okay Nabu active and routes it to realtime.

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
ADB connection. A current direct physical wake must show no cue before exact
session readiness and then exactly one playback of the pinned
`wake_word_triggered_old.wav`; capture must remain closed until its EOF. Merely
importing `sitecustomize` in a separate probe process is not acceptance.

Python imports `sitecustomize` during process startup. The script first
validates the wake/LED compatibility group atomically before installing the
latency wrapper. When direct configuration is present, a second guard covers
the audio/stop handlers, configuration mutation, both constructors, and the
separately executed microphone-loop bytecode before enabling direct ownership
and detector ordering. An initial-group mismatch leaves vendor wake behavior
untouched; a direct-group mismatch retains only the separately guarded latency
path and the Assist implementation for explicit rollback. The current
`realtime_only` matching wake still fails closed. Both cases emit a content-free
warning.

## Acceptance and rollback

Active server-offloaded v2 is accepted only when the exact deployed artifact
passes all of the following on the physical speaker:

1. Okay Nabu is detected reliably from 1.5 m. Every accepted wake immediately
   pulses the LED, retries within one 12-second owner deadline, plays exactly
   one cue only after strict-v2 `started`, and opens capture only at cue EOF.
2. Forced connection, readiness, cue, and playback failures always restore the
   idle LED and release ownership. No captured audio enters Assist/Hermes.
3. Normal, quiet, loud, close, 1.5 m, and room-edge speech reaches the active
   provider with the configured native AEC3 and +12 dB post-AEC gain without
   clipping.
4. At the fixed 100% anchor and representative software levels through 100%, no-user
   playback does not self-interrupt. Early, middle, and late near-end speech
   cuts playback promptly and the exact causal words become the next request;
   no second sentence or wake is needed.
5. Long multi-turn conversation stays on one device WebSocket and provider
   peer across interruptions. Output epochs remain strictly increasing; replies
   have no systematic prefix/tail loss or crackle, and volume changes do not
   move the fixed sink anchor or introduce a second attenuation stage.
6. Spanish `terminar`/`terminar llamada`, an unambiguous English end request,
   manual stop, mute, disconnect, idle expiry, and hard lifetime expiry all
   return the device to idle with no stale `paplay` child.
7. Repeated sessions leave one stable vendor voice process, no device sidecar,
   bounded queues/RSS, and no new OOM events. Updates restart only the voice
   service; PulseAudio remains running and TCP ADB port 5555 remains enabled.

### Historical v3 coverage and evidence

The remaining coverage list and measurements describe the dormant
device-owned WebRTC implementation unless they explicitly say active v2. They
are preserved as regression evidence, not as acceptance of the current route.

The repository tests cover immediate owner/thinking-LED ordering, discard of
the wake frame and every v3 pre-ready microphone frame, the exact ready-to-cue
and cue-EOF-to-capture boundaries, cue failure/timeout, stop during connecting
and confirmation, three-attempt/12-second startup ownership, terminal cleanup,
VAD/run-end/disconnect/mute races, startup guards and exceptions,
active-session wake suppression, and v3 fail-closed startup,
retained v2 pre-ready fallback/replay, strict v2 and v3 framing, device-owned
SDP negotiation, absence of bridge PCM in v3, bounded sidecar IPC and media
queues, receiver-owned `media.started`/`media.quiet` boundaries,
pre-preflight sink restoration plus complete PulseAudio AEC verification,
continuous full-duplex capture, RTP-before-start and stopped-before-tail
ordering, trusted-AEC-only fresh-peer rollover, ordered retirement of the old
logical peer and in-process standby promotion, consecutive epoch validation,
bounded pre-roll/live capture replay in
order, actual-RTP-consumption freshness, no post-trigger capture to the old
peer, queue/age/timeout failure, standby re-poll and terminal invalid-standby failure,
post-interruption rearm only after eight consecutive detector-quiet 64 ms
callbacks, suppression of continuous-speech retriggers across peer epochs,
pre-ack lifecycle/playback bounds and inaudibility, exact-integer rejection,
normal stop in every phase, expired-close reaper ownership,
outer owner/WebSocket/ready-latch retention, 100 ms bridge close-confirmation
grace, same-thread reuse on confirmation, tracked isolated-thread cleanup on
ambiguity, stale-epoch rejection, fixed-argv non-blocking `paplay`,
immediate abort SIGKILL, retained v2 native/managed interruption
acknowledgements and fresh-session fallback, deterministic hash-locked runtime
build/install/rollback validation, guarded static-AEC installation and
rollback, interrupt cleanup, timer interruption, serialized non-blocking LED
execution, newest-state overload coalescing, DBus timeout/nonzero handling,
explicit worker shutdown, and the atomic unknown-bytecode fail-closed path.

Each direct session now emits one INFO-level, content-free summary that makes a
stalled LED diagnosable without recording speech. It reports whether the
handshake became ready, the current phase, aggregate peak/RMS and counts for
PCM actually sent to the sidecar, bounded counts for allowlisted lifecycle
event types, signal-bearing playback aggregates, capture-age bounds, duration,
and terminal outcome. A failure warning contains only the phase and exception
class. Neither record includes PCM, transcripts, provider payloads, item or
turn identifiers, SDP, prompts, URLs, or credentials.

Every physical wake also emits one fixed-vocabulary syslog selection such as
`wake_detector=realtime selection=configured_phrase` or
`wake_detector=assist selection=normal_phrase`. This records classifier intent,
not transport startup success. It contains no phrase text, detector ID,
confidence, audio, prompt, endpoint, or credential.

The 2026-08-11 reference-device root-fix canary used the then-active v3 session,
AEC source, sidecar, Opus/RTP path, provider VAD, and `paplay` at the qualified
60% setting. It reached full peer readiness in 6.749 seconds, observed first
output at 12.046 seconds, and received 536 signal-bearing playback packets with
no captured audio or transcript persisted. Before 20 ms reframing, the same
input reached `session.started` but produced no speech lifecycle or playback.

Automated checks are not physical v3 acceptance. A historical two-worker build
passed the reference-device hardware double-interruption canary twice at that
installation's qualified 60% setting with the exact artifact: four local cuts
were 208–211 ms and four rollovers were 1.29–1.57 s. Each run recycled its same
two worker PIDs without a cold replacement and retained context twice. The
detector boundary also passed a stricter physical rearm probe: seven quiet
callbacks did not rearm, eight did, and only the later speech edge caused the
second rollover. The retired PeerConnection's stop acknowledgement followed
replacement negotiation before its existing
worker was recycled. These historical measurements do not physically validate
the current single-worker build; this device event is not the bridge's 100 ms
App Server close barrier. The following full matrix remains required for each
deployment.

The historical v3 physical matrix was:

1. In the current `realtime_only` trial, Okay Nabu starts direct voice and
   cannot enter Home Assistant Assist/Hermes. Separately validate the deferred
   Assist route before restoring a split configuration.
2. Okay Nabu speaks a direct response, returns to listening,
   accepts a spoken interruption and a follow-up without another wake, and does
   not let its own output activate the legacy terminal stop-word detector.
3. Okay Nabu cannot control Home Assistant even when a compatibility
   realtime authority is registered. Its start and `started` frames both carry
   `conversation_mode: "native"`. The App Server thread must expose exactly
   `end_conversation`, reject every other tool, and expose no Home Assistant
   entity schema. A clear spoken goodbye must invoke the terminal tool and
   return the device LED to idle.
4. On each accepted wake, verify that thinking/pulsing is queued immediately;
   the trigger, wake tail, connecting frames, and cue-time frames never reach
   the initial peer. Up to three attempts must share one absolute 12-second
   owner deadline and each attempt's handshake must remain bounded at ten
   seconds. Alive-only process prewarm must not be reported as an offer-ready
   peer.
5. Require SDP applied, peer connected, `oai-events` ready,
   `transport_ready`, and the exact bridge `started` before readiness. Then
   verify exactly one root-owned pinned cue (SHA-256
   `6b25dd2abaf7537865222ca9fd6e14fbf723458526fb79bbe29d8261d1320724`) and no
   capture until EOF; only EOF may switch the LED to listening. The local stop
   detector must remain suspended throughout direct ownership. Verify that the
   first logical standby offer is requested only after this cue EOF/capture-open
   boundary, while exactly one sidecar OS process remains alive. Cue timeout at
   two seconds, forced pre-ready/post-ready
   failure, owner-deadline expiry, and attempt exhaustion must return idle with
   no Home Assistant fallback.
6. Repeated direct turns—and standard turns only during deliberate rollback or
   split-route validation—leave one stable voice process, no stale direct or v2
   `paplay` child, no LED/duck state behind, and bounded memory.
7. At the configured and previously qualified sink/playback value, speak over
   early, middle, and late response audio from near and far positions. Playback
   must stop at speech detection, the command must be recognized once, and
   response audio must not appear as user speech. Repeat after an idle gap and reconnect.
   For each newly negotiated direct session, the fixed-argv controller must set
   and verify the exact raw `playback_volume_percent` on that dedicated sink
   before the SDP offer. A forced preflight or preparation mismatch must fail
   before negotiation or outbound capture. Trace child execution to confirm
   that ordinary interruption never runs `pactl`; playback begin/resume performs
   one exact raw check and only a mismatch performs bounded repair. Exercise
   Home Assistant volume plus the physical up/down buttons during early, middle,
   and late response audio. The sink must return to the exact anchor before two
   recorder callbacks, the entity must report the bounded requested level, and
   playback must neither self-interrupt nor block a genuine interruption after
   the short transition boundary. Direct `paplay`
   must target only that sink, use raw stream volume 65536, accept non-blocking
   20 ms writes with the reviewed 60 ms/20 ms settings, and never enumerate or
   mutate a sink-input.
8. During native v3 speech, exercise early, middle, and late trusted local
   AEC barge-in plus RTP-before-output-start and
   stopped-before-audio-tail timing. Prefix and tail audio must remain intact
   during normal playback. Interruption must drop queued PCM and SIGKILL
   `paplay` immediately, retire the old PeerConnection epoch, and prevent every
   later capture frame from reaching it. Hold speech continuously through
   replacement output and prove it cannot retire a second peer. Prove that
   seven detector-quiet 64 ms callbacks are insufficient, signal resets that
   partial quiet count, eight consecutive quiet callbacks rearm, and only a later
   two-frame speech edge triggers the next rollover. Verify consecutive epoch signaling,
   bounded recent pre-roll plus live-speech capture, exactly-once ordered delivery to the new
   peer, and retention of the same outer vendor owner/session/player,
   WebSocket, and ready latch. Force capture to expire after queueing but before
   RTP consumption and require terminal failure. Inject replacement lifecycle
   and PCM before acknowledgement: it must remain inaudible within
   `output_queue_bytes`, then replay in order only after exact
   `rollover_started`. Verify exactly one sidecar process, reject a failed
   logical standby, and prove the outer session terminates without launching a
   replacement worker;
   expire a child-close budget and
   prove its daemon reaper owns the final `waitpid`. Reject float/bool protocol
   or epoch values and treat `stop` as normal during each rollover phase.
   Separately, a confirmed old realtime-closed
   notification within the 100 ms bridge grace must report retained context;
   forced timeout/error/absent close must transfer the old epoch to tracked
   cleanup, isolate the replacement on a new thread, and report false. Neither result may
   be treated as proof that interrupted unheard assistant audio is absent from
   context. Exercise queue,
   age, deadline, sidecar, and invalid-epoch failures: all must close the outer
   session without Assist fallback or audio logging. Verify stop, mute, and
   disconnect still end it, while later detector hits cannot preempt an active
   realtime owner. Measure local playback
   stop and fresh-peer response separately because negotiation adds handoff
   latency. Repeat two successive interruptions under adverse timing. The
   reference-device double-interruption canary now passes, but every deployment
   must repeat it as part of this matrix.
9. A deliberately missing AEC module or mismatched default source/sink must fail
   direct startup before sending microphone audio to the bridge or provider,
   must not hand captured audio to Home Assistant, and must leave a later direct
   Okay Nabu wake usable. With native AEC3 selected, deliberately break its
   library/device contract and verify voice startup fails closed without a raw
   microphone fallback; then restore the exact artifact and repeat the wake.
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

Reject the overlay if it accepts speech before cue EOF, plays the ready cue
before readiness or more than once, forwards cue audio as microphone input,
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
