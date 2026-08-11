# Realtime device wire protocol v3

This document defines the authenticated device-facing WebSocket contract at
`GET /v1/realtime` for a device-owned WebRTC peer. Version 3 is a signaling and
sideband-control protocol: audio and provider data-channel traffic do **not**
cross the bridge WebSocket.

The shipped implementation targets the ThirdReality `1.01.07`/upstream
`v1.1.7` image, which is a Python 3.11, aarch64 Buildroot Linux system. It is
not Android. The device runs `aiortc` in an isolated child process and creates
the WebRTC offer, audio transceiver, and ordered `oai-events` data channel. The
bridge uses its existing Codex login only to create and stop the App Server
realtime session, relay the SDP answer, reject impossible tool calls, monitor
the provider lifecycle, and dispose the owned thread.

Here, “isolated” means a separate interpreter and dependency tree, bounded IPC,
and an unprivileged UID/GID 65534 with no supplementary groups and a minimal
fixed environment. Root-owned mode-0755 directories and mode-0644 runtime files
are readable but not writable by the child; the mode-0600 device configuration
and runtime archive are not readable. This is not a general filesystem,
syscall, or network sandbox, so the sidecar remains trusted device code.

```text
ThirdReality microphone/speaker
  <-> isolated device aiortc peer
  <================ WebRTC RTP audio ================> provider
  <=============== oai-events data =================> provider

device client
  <-------- authenticated JSON signaling -----------> bridge
                                                       |
                                                       +-> Codex App Server
                                                           thread start/stop,
                                                           OAuth and SDP only
```

V3 is always native, tool-free realtime voice. It never attaches the Home
Assistant tool broker, creates a transcript/executor/TTS handoff, or exposes a
Home Assistant credential to the speaker. Use the separate Okay Nabu Assist
route for Home Assistant controls.

This App Server realtime surface is experimental and version-coupled. It is
not the documented OpenAI Realtime API. The v3 code and deterministic runtime
are covered by local protocol and static tests. The reference installation also
passed a physical double-interruption rollover canary at its qualified 60%
setting, but that result does not replace any deployment's acceptance matrix.

Current App Server documentation exposes realtime start/stop with WebRTC for
versions v1 and v3; WebRTC v2 is unsupported. This device contract remains on
tagged Frameless Bidi v3. A live v1 subscription canary did not complete
startup, so v1 is not presented as an operational fallback.

## Authentication and admission

The WebSocket handshake carries:

```http
Authorization: Bearer <HA_CODEX_REALTIME_DEVICE_TOKEN>
```

Use a distinct, route-scoped device token. The bridge accepts that token only
on `/v1/realtime`, and only when the first message successfully negotiates
protocol v2 or v3. It cannot open `/health`, the Home Assistant tool broker, or
any Conversation, STT, or TTS route. The primary bridge token remains valid
for legacy clients.

Use WSS with ordinary certificate and hostname validation outside a
source-restricted trusted LAN. A bearer does not make plaintext WebSockets
safe on an untrusted network.

The bridge admits only one subscription-backed speech session at a time.
Concurrent v1, v2, v3, experimental STT, or experimental TTS use can receive a
content-free `busy` error.

## Negotiation sequence

The complete initial-session sequence is unchanged:

```text
device                      bridge                    App Server/provider
  | create aiortc offer       |                               |
  |-- start + SDP offer ----->|-- thread/start -------------->|
  |                            |-- thread/realtime/start ------>|
  |                            |<-- SDP answer + started -------|
  |<-- answer + SDP ----------|                               |
  | apply answer               |                               |
  | wait for ICE/DTLS,         |                               |
  | WebRTC connected, and      |                               |
  | oai-events open            |                               |
  |-- transport_ready -------->|                               |
  |<-- started ----------------|                               |
  |                            |                               |
  |<======== RTP audio and oai-events directly ==============>|
  |-- ping/stop JSON --------->|-- lifecycle stop/cleanup ---->|
```

The bridge does not send `started` until the device has applied the SDP answer
and explicitly confirmed transport readiness. Provider failure wins a
simultaneous readiness signal, so a dead peer is never acknowledged as ready.
Any invalid, duplicate, out-of-order, binary, oversized, or unsupported
message fails closed and ends the session.

The initial peer is implicit epoch `1`. Fresh-peer rollover extends this
contract after `started` without changing the exact initial `start`, `answer`,
`transport_ready`, or `started` objects. Consequently capability negotiation
cannot be added to the initial acknowledgement: deploy the rollover-capable
bridge before the rollover-capable device. The new bridge remains compatible
with the old device; an old bridge rejects a new device's rollover control.

## Start offer

The first WebSocket frame is one UTF-8 JSON object:

```json
{
  "type": "start",
  "protocol_version": 3,
  "conversation_mode": "native",
  "transport": {
    "type": "webrtc",
    "sdp": "v=0\r\n..."
  },
  "voice": "cove",
  "prompt": "Responde en español latinoamericano de México."
}
```

The only allowed top-level fields are `type`, `protocol_version`,
`conversation_mode`, `transport`, and the optional `conversation_id`, `voice`,
and `prompt` preferences. The only allowed transport fields are `type` and
`sdp`.

- `type` must be `start`.
- `protocol_version` must be the integer `3`.
- `conversation_mode` must be `native`; it cannot be omitted.
- `transport.type` must be `webrtc`.
- `transport.sdp` must be non-empty UTF-8 text of at most 16,384 bytes, start
  with `v=0`, contain both an audio media line and an application/data-channel
  media line, and contain no NUL byte.
- `conversation_id`, when present, is at most 128 characters.
- `voice`, when present, is at most 64 characters. The reference device
  accepts a safe ASCII name and normalizes it to lowercase.
- `prompt`, when present, is at most 4,096 characters on the bridge. The
  reference device applies a stricter printable 1,024-character bound.

V3 rejects device tools, model or realtime-version overrides, unknown fields,
and the v1/v2 PCM fields `audio_transport`, `input_sample_rate`, and
`input_channels`. The bridge forwards the exact validated SDP offer to
`thread/realtime/start` with its operator-configured App Server realtime v1 or
v3 version (v3 by default), audio output, no initial startup context, and no
client-managed handoff. The device cannot override that bridge policy. The
supported deployment remains v3 because the live v1 subscription canary did
not complete startup.

For Mexican Spanish deployments, keep locale and accent separate in the
bounded prompt. The supplied example requests `es-MX`-style Mexican Spanish by
default while allowing an explicit user request to change language.

## SDP answer

After App Server returns both a realtime-started event and one consistent SDP
answer for the owned thread, the bridge sends exactly:

```json
{
  "type": "answer",
  "protocol_version": 3,
  "transport": {
    "type": "webrtc",
    "sdp": "v=0\r\n..."
  }
}
```

The answer has the same 16,384-byte, `v=0`, audio-media, application-media,
and NUL checks as the offer. The bridge relays it without constructing an
`aiortc` peer, adding credentials, proxying candidates, or modifying media.
Conflicting App Server answers fail the session.

The device applies the answer to its peer and waits for all three local
conditions:

1. the answer was applied;
2. the WebRTC peer connection reached `connected`; and
3. the ordered `oai-events` data channel opened.

The reference sidecar uses no configured public STUN server, gathers the
device's candidates, and bounds offer creation. Network routing must therefore
be validated for the actual device and provider environment.

## Transport readiness and started acknowledgement

Once the three device conditions above hold, the device sends exactly:

```json
{"type":"transport_ready","protocol_version":3}
```

The bridge then sends exactly:

```json
{
  "type": "started",
  "version": "v3",
  "protocol_version": 3,
  "conversation_mode": "native",
  "transport": "webrtc",
  "audio_over_bridge": false,
  "sideband_control": true
}
```

No SDP, thread ID, realtime-session ID, conversation ID, transcript, tool
metadata, or credential appears in `started`. The reference client compares
the complete object and fails closed on any missing or extra field.

## Fresh-peer rollover

Trusted AEC barge-in replaces the provider media peer while retaining the
outer device conversation. The device sends the next consecutive epoch and a
fresh offer:

```json
{
  "type": "rollover",
  "protocol_version": 3,
  "epoch": 2,
  "transport": {
    "type": "webrtc",
    "sdp": "v=0\r\n..."
  }
}
```

The bridge validates the SDP using the initial-offer rules and replies:

```json
{
  "type": "rollover_answer",
  "protocol_version": 3,
  "epoch": 2,
  "transport": {
    "type": "webrtc",
    "sdp": "v=0\r\n..."
  }
}
```

After applying that answer and observing the fresh peer connected with its
ordered `oai-events` channel open, the device sends exactly:

```json
{"type":"rollover_transport_ready","protocol_version":3,"epoch":2}
```

The bridge completes the handshake with exactly one of:

```json
{"type":"rollover_started","protocol_version":3,"epoch":2,"context_retained":true}
```

```json
{"type":"rollover_started","protocol_version":3,"epoch":2,"context_retained":false}
```

Epochs are consecutive positive integers from implicit `1` through `1024`. A
duplicate, skipped, stale, out-of-order, oversized, or otherwise invalid
rollover message fails the outer session closed.
`protocol_version` and `epoch` are exact JSON integers: floating-point values
and booleans are rejected on both device and bridge controls.

Before reusing the Codex thread, the bridge stops the old realtime session and
requires the matching `thread/realtime/closed` notification. The pinned stop
RPC only enqueues closure; that notification follows awaited shutdown of the
old input and event fanout and is the reuse barrier. A confirmed barrier starts
the replacement on the same thread with `includeStartupContext: true` and
reports `context_retained: true`. A timeout, stop error, missing notification,
or otherwise ambiguous close creates a new isolated thread and reports
`context_retained: false`, preventing a delayed old close from terminating the
replacement. Cleanup of the retired session/thread remains bounded.

`context_retained: true` is only a control-plane fact. It does not prove
audible-history correctness: assistant speech that the user interrupted before
hearing may remain in provider context. A recent microphone pre-roll may also
include samples already delivered to the retired peer. The device writes each
retained/replayed sample exactly once and in order to the replacement peer, but
the protocol does not claim provider-wide exactly-once interpretation.

The prewarmed standby is health-polled while the active peer runs and re-polled
immediately before rollover use. Exit, EOF, fatal/error output, or any
unexpected post-offer output disqualifies it and terminates the outer session;
the fixed two-slot pool does not cold-launch a replacement or third child.
Replacement lifecycle and decoded PCM received before acknowledgement share one
ordered buffer bounded by configured `output_queue_bytes`. They remain
inaudible and cannot mutate player lifecycle until the exact matching
`rollover_started` object arrives; the complete batch is then replayed in order
through the normal media handlers. Overflow or an invalid item is terminal.

## Direct media and data-channel behavior

After the answer is applied, microphone audio is sent from the device peer to
the provider over WebRTC RTP. Provider audio returns on the same peer and is
decoded on the device. No PCM WebSocket frame, JSON/base64 audio object, or raw
provider data-channel payload is accepted by the bridge in v3.

The reference implementation:

- accepts 16 kHz mono PCM16 from the vendor capture callback and preserves
  contiguous sample indices and monotonic capture timestamps into `aiortc`;
- checks capture age again when the outbound RTP track actually consumes each
  packet, not only when it enters a parent/child queue. A stale packet emits a
  content-free fatal state and stops the direct session;
- receives the negotiated provider audio and converts it to 24 kHz mono PCM16
  for bounded device playback;
- once per direct session, before requesting the SDP offer or opening the
  bridge socket, checks the qualified AEC sink ceiling and has a fixed-argv
  `pactl` controller set and verify that dedicated sink to the exact raw
  configured playback value. The live response, playback begin/resume, and
  interruption paths do not invoke `pactl`. The one owned `paplay` stream is
  forced to raw 65536 (100% relative to that sink); no sink-input is enumerated
  or mutated;
- fails rather than silently dropping capture or playback when a bounded IPC,
  queue, or sequence check is violated; and
- sanitizes provider data messages to lifecycle type, role, bounded control
  identifiers, and content-free state before they cross from the isolated
  sidecar to the vendor process. Transcript text, model text, tool arguments,
  arbitrary nested values, and raw data-channel messages do not cross that IPC
  boundary.

Provider SCTP response/output lifecycle does not label, admit, gate, split, or
retire the normal RTP lane. The decoded receiver owns one continuous media
lane. Its first audio frame opens a monotonic local media generation and emits
transcript-free `media.started` immediately before that PCM crosses IPC. Every
decoded frame resets a receiver timer; only about 120 ms with no decoded audio
emits `media.quiet` and closes that media generation. A later frame opens the
next generation. This actual receiver boundary preserves audio that arrives
before an SCTP output-start event and tail audio that arrives after an SCTP
stopped event. This roughly 120 ms normal-generation boundary is not an
interruption acknowledgement and does not authorize reuse of that peer.

Direct playback owns at most one fixed-argv `paplay` child with raw 24 kHz,
mono, signed-16-bit input, `--latency-msec=60`, and
`--process-time-msec=20`. Its stdin is non-blocking, and each network service
pass writes at most 20 ms of PCM from the bounded queue. The child may remain
open across receiver-quiescence media boundaries so a stopped-before-tail
ordering cannot discard audible tail. Interruption clears queued bytes, closes
stdin, and sends immediate SIGKILL without waiting on the realtime path; reap
is bounded separately; no in-process Pulse playback path participates.

```text
/usr/bin/paplay --raw --rate=24000 --format=s16le --channels=1 \
  --latency-msec=60 --process-time-msec=20 \
  --device=<validated AEC sink> --volume=65536
```

The command is an argument vector with `shell=False`; the sink name has already
passed the configuration allowlist.

The bridge separately watches App Server thread lifecycle events so it can
sanitize remote failure, reject any unexpected provider tool call with
`direct_voice_has_no_tools`/`do_not_retry`, and clean up the owned realtime
session and thread. That control plane does not put the bridge in the media
path.

## Barge-in and interruption

Qualified full-duplex AEC keeps device capture active during provider
playback. Exactly two consecutive locally qualifying AEC-filtered capture
frames trigger fresh-peer rollover. The vendor-process parent immediately
drops queued playback PCM and IPC, closes player stdin, and sends SIGKILL to
the active `paplay` child. It then retires the old sidecar and sends no later
capture to that peer.

The network thread arms a post-interruption detector gate only after it accepts
the request for the current output epoch and commits the local playback abort.
One uninterrupted local speech segment can therefore retire only one peer
epoch. Eight consecutive detector-quiet 64 ms capture frames (512 ms) rearm
the detector; only a later two-frame qualifying speech edge may retire the
replacement.
A qualifying signal before the eighth quiet frame resets the quiet count.
A stale request from an earlier output epoch neither interrupts nor arms this
gate.

The outer vendor owner, realtime-session object, logical player, bridge
WebSocket, and ready latch remain attached. The device freezes a bounded recent
AEC-filtered pre-roll through the trigger and queues subsequent live speech
while a fresh prewarmed sidecar creates the next epoch's offer. After the
rollover handshake, it replays those samples exactly once and in capture order
to the replacement peer, then resumes live pacing. No audio is persisted or
logged. Queue capacity, maximum sample age, handshake timeout, sidecar error,
or invalid epoch fails the entire direct session closed; captured Okay Computer
audio is never handed to Home Assistant.

Manual stop, normal-wake preemption, mute, disconnect, explicit non-speech
interruption, or vendor-owner teardown still ends the outer session instead of
rolling it over. `stop` is normal termination during every rollover phase,
including old-session closure, answer/readiness waits, and pre-ack output
buffering; it is not reported as an epoch or provider failure.

This subscription-backed Codex connection speaks tagged **Frameless Bidi**,
not the generic public Realtime API client-event dialect. The pinned
[Codex 0.147 `RealtimeOutboundMessage`
enum](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/codex-api/src/endpoint/realtime_websocket/protocol.rs#L50-L85)
contains audio/context/session messages but no `response.cancel` or
`output_audio_buffer.clear`. The device therefore sends neither event and must
not borrow an equivalent public-Realtime client control. Public Realtime v2
WebRTC and its client-event/`session.update` dialect are unsupported on this
ChatGPT-subscription direct route; live attempts to configure VAD with
`session.update` were rejected. This does not deprecate this project's separate
historical wire-v2 `bridge_pcm` rollback.

The direct Frameless data channel provides no provider interruption
acknowledgement. Live evidence observed only `session.started`; it produced no
`speech_started`, `turn.done`, or transcript event. Consequently neither SCTP
lifecycle nor receiver quiet can causally prove interruption. A synthetic
same-peer canary was attempted and rejected: old RTP continued beyond the
five-second media-fence deadline. The former `response.interrupt` /
`interrupt.fenced` experiment is not the production interruption path and does
not authorize same-peer resume.

Fresh-peer rollover is therefore a safe subscription-backed approximation,
not exact ChatGPT same-session interruption semantics. It removes stale local
audio and prevents subsequent capture from reaching the retired peer, but
fresh WebRTC/provider negotiation adds measurable handoff latency. On the
reference installation's qualified 60% setting, the physical double-interruption
canary passed twice with the exact artifact: four local cuts were 208–211 ms and
four rollovers were 1.29–1.57 s. Each run recycled its same two worker PIDs
without a cold replacement and retained context twice. Every installation still
requires its own physical acceptance matrix.

## Sideband controls and termination

After `started`, the device WebSocket accepts JSON control only:

- `{"type":"ping"}` receives `{"type":"pong"}`. WebSocket ping/pong frames
  may also be used by the reference client for its bounded liveness timers.
- The exact epoch-tagged `rollover`, `rollover_answer`,
  `rollover_transport_ready`, and `rollover_started` controls implement the
  fresh-peer sequence above.
- `{"type":"stop"}` requests normal teardown. The bridge stops the App
  Server realtime session, deletes every thread owned by the outer session,
  and closes the WebSocket.

If App Server closes first, the bridge may send:

```json
{"type":"stopped","reason":"remote_closed"}
```

Errors are content-safe JSON objects such as:

```json
{"type":"error","error":"realtime provider error"}
```

Private provider payloads, OAuth details, prompts, and transcripts are not
included. Disconnects, timeouts, invalid controls, provider errors, tool-call
attempts that cannot be rejected safely, and cleanup races all terminate the
session with bounded, cancellation-shielded provider cleanup.

Sidecar close is bounded. If a killed child does not complete `waitpid` within
that budget, ownership transfers to a daemon reaper which performs the eventual
wait without blocking the realtime or vendor thread.

## Failure and fallback policy

V3 direct mode fails closed. If the isolated runtime, AEC preflight, sidecar,
offer, bridge, App Server, answer, ICE/DTLS/SCTP connection, data channel,
playback, queue/age bound, rollover deadline, epoch, or protocol check fails,
the device stops the direct owner, clears bounded audio, and returns to idle.
Captured Okay Computer audio is not replayed or handed to Home Assistant.

This is deliberately different from the retained v2 `bridge_pcm` path, whose
pre-ready compatibility behavior can replay its bounded prefix into the
official Assist path. To roll back transport without removing the overlay, set
`media_transport` to `bridge_pcm` and follow the complete
[v2 wire contract](realtime-wire-v2.md) and device rollback procedure.
