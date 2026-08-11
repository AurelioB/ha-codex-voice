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
are covered by local protocol and static tests; the repository does not yet
claim an end-to-end physical-device v3 acceptance run.

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

The complete successful sequence is:

```text
device                      bridge                    App Server/provider
  | create aiortc offer       |                               |
  |-- start + SDP offer ----->|-- thread/start -------------->|
  |                            |-- thread/realtime/start v3 --->|
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
`thread/realtime/start` with App Server realtime `version: "v3"`, audio output,
no startup context, and no client-managed handoff.

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

## Direct media and data-channel behavior

After the answer is applied, microphone audio is sent from the device peer to
the provider over WebRTC RTP. Provider audio returns on the same peer and is
decoded on the device. No PCM WebSocket frame, JSON/base64 audio object, or raw
provider data-channel payload is accepted by the bridge in v3.

The reference implementation:

- accepts 16 kHz mono PCM16 from the vendor capture callback and preserves
  contiguous sample indices and monotonic capture timestamps into `aiortc`;
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
stopped event. This roughly 120 ms normal-generation boundary is independent of
the interruption fence below.

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
frames are the trusted preserving-interrupt trigger. The vendor-process parent
immediately drops queued playback PCM and IPC, closes player stdin, and sends
SIGKILL to the active `paplay` child. The second qualifying capture frame is an
exact monotonic watermark. The parent keeps the input backlog paced, sends the
zero-field local IPC packet `{"type":"response.interrupt"}` immediately after
the packet carrying that watermark, and sends no later capture packet before
the token. The sidecar then mutes decoded RTP. Capture remains live, the child
continues consuming it, and microphone RTP continues to the provider while the
fence is pending. This packet is internal and never crosses the provider data
channel.
Manual, explicit non-speech, stop, normal-wake preemption, mute, disconnect,
and vendor-owner teardown cannot enter this preserving fence; after local
playback abort they must tear down and require a fresh peer/session.

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
lifecycle nor receiver quiet can causally prove that the provider interrupted
the old response. Same-peer reuse is instead an explicitly **empirical WebRTC
auto-truncation invariant** for the pinned route, not a protocol guarantee,
remote-cancellation claim, or completed physical validation.

On receipt of the trusted local token, the child snapshots the outbound capture
cursor, the newest pre-token capture sample end, and a monotonic absolute
deadline. It derives one required consumed end equal to the greater of the
pre-token watermark and the snapshot cursor plus 4,000 samples. It may emit
`interrupt.fenced` only after all four of these conditions hold:

1. the outbound WebRTC track has consumed through the pre-token watermark,
   proving that the two qualifying frames reached the sender;
2. it has also consumed at least 4,000 samples beyond the token-time cursor
   (250 ms at 16 kHz); queued-but-unconsumed capture does not count;
3. at least 750 ms has elapsed since the token; and
4. after the capture proof holds, the sole decoded-audio consumer has observed
   a receiver-barrier request and then measured a fresh continuous 500 ms with
   no decoded RTP frame. The interval begins in the consumer, not in the fence
   timer; every queued, ready, or subsequently delivered frame resets it. Only
   responsive 20 ms receiver heartbeats accumulate silence; a scheduling slip
   resets the complete interval rather than counting stalled wall time.

The exact pinned aiortc 1.15 receiver boundary is part of condition 4. During
the synchronous `track` callback, before aiortc starts its decoder worker, the
sidecar verifies and wraps both the empty unbounded encoded-frame decoder queue
and `RemoteStreamTrack._queue`. The synchronous wrappers publish monotonic
producer, in-flight/completed-decode, and processed-output serials before any
cross-thread scheduling can hide them. Decoder termination is terminal state,
not silence. The fence requires both queues to be quiet and drained for 500 ms.
At the serialized final commit it replaces the verified audio
`JitterBuffer(capacity=16, prefetch=4)` to discard retained encoded tail and
recreates the receiver-owned PyAV resampler to discard its filter tail. If the
pinned version, private queue/jitter shape, ordering canary, or serial sequence
differs, the direct session fails closed. This deliberately private boundary is
safe only because the runtime and every wheel are hash pinned.

The conditions overlap; their durations are not added. Every decoded RTP frame
is recorded before resampling and restarts only the 500 ms absence interval. It
does not restart either capture requirement, the 750 ms guard, or the deadline.
Thus the earliest successful fence is 750 ms after the token when the capture,
fresh receiver-silence, and guard conditions are also satisfied. The
normal-generation roughly 120 ms `media.quiet` boundary remains separate and
cannot satisfy the 500 ms fence condition.

When all four conditions hold, the child first emits `media.quiet` if a normal
media generation remains open. Only after that IPC write and transcript-free
`interrupt.fenced` both succeed may it unmute decoded RTP and permit the parent
to return `READY`. Internal lifecycle names are reserved and rejected on
provider ingress, so provider data cannot forge either boundary. One fixed
absolute five-second deadline begins at the local token, is checked after the
receiver proof, after an optional `media.quiet`, and immediately before the
final `interrupt.fenced` write. A successful ordered `interrupt.fenced` write
is the commit point, so one fence cannot report both success and timeout. The
decoder and output producer locks are held across jitter reset and the no-await
`media.quiet`/`interrupt.fenced` writes and unmute, preventing queued or
in-flight decode from crossing that decision. The deadline never restarts. If
the conditions do not all hold before it—or a
lifecycle write fails—the child remains muted and the direct session fails
closed. It emits the content-free fatal
`media_fence_capture_timeout` when either capture condition is unmet; otherwise
it emits `media_fence_timeout`. Neither diagnostic restarts or extends the
deadline, and a later utterance requires a fresh peer/thread. Continuing
provider RTP through that deadline disables same-peer reuse rather than
weakening the fence.

If the bounded parent capture queue is full, unaccepted audio never receives a
capture watermark. The direct client still runs the bounded detector over that
frame. If it completes the two-frame trigger on an unaccepted frame, it cannot
authorize this preserving fence: it immediately selects the fresh-session
teardown path. A later accepted frame may complete a sequence started by a
full-queue frame, but the fence then uses only that accepted frame's real
watermark. Quiet full-queue frames reset the sequence.

This invariant must be requalified after provider behavior or the pinned Codex
version changes and under real double-talk/RTP timing. Qualification must cover
ordinary mid-response gaps: if decoded output can pause for 500 ms and later
resume without barge-in, same-peer reuse is unqualified and must remain disabled
in favor of a fresh session. The repository's automated coverage is not an
end-to-end physical v3 acceptance claim. Unlike project wire v2, v3 has no
bridge `interrupt` acknowledgement or bridge-side `remote_cancelled` claim.

Stop, normal-wake preemption, mute, disconnect, manual interruption, or
vendor-owner teardown uses a non-preserving interrupt: playback is aborted, the
device peer is stopped, and the device sends the bridge `stop` control for
remote cleanup.

## Sideband controls and termination

After `started`, the device WebSocket accepts JSON control only:

- `{"type":"ping"}` receives `{"type":"pong"}`. WebSocket ping/pong frames
  may also be used by the reference client for its bounded liveness timers.
- `{"type":"stop"}` requests normal teardown. The bridge stops the App
  Server realtime session, deletes its one owned thread, and closes the
  WebSocket.

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

## Failure and fallback policy

V3 direct mode fails closed. If the isolated runtime, AEC preflight, sidecar,
offer, bridge, App Server, answer, ICE/DTLS/SCTP connection, data channel,
playback, queue, or protocol check fails, the device stops the direct owner,
clears bounded audio, and returns to idle. Captured Okay Computer audio is not
replayed or handed to Home Assistant.

This is deliberately different from the retained v2 `bridge_pcm` path, whose
pre-ready compatibility behavior can replay its bounded prefix into the
official Assist path. To roll back transport without removing the overlay, set
`media_transport` to `bridge_pcm` and follow the complete
[v2 wire contract](realtime-wire-v2.md) and device rollback procedure.
