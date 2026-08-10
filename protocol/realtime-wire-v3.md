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

Provider SCTP lifecycle does not label, admit, gate, split, or retire RTP. The
decoded receiver owns one continuous media lane. Its first audio frame opens a
monotonic local media generation and emits transcript-free `media.started`
immediately before that PCM crosses IPC. Every decoded frame resets a receiver
timer; only about 120 ms with no decoded audio emits `media.quiet` and closes
that media generation. A later frame opens the next generation. This actual
receiver boundary preserves audio that arrives before an SCTP output-start
event and tail audio that arrives after an SCTP stopped event.

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

## Barge-in and cancellation

Qualified full-duplex AEC keeps device capture active during provider
playback. Two consecutive locally qualifying AEC-filtered capture frames, or
an explicit preserving interrupt, immediately kill the local `paplay` child,
drop its queued PCM and already queued playback IPC, and ask the sidecar to
fence continuation. The sidecar mutes decoded RTP while the fence is pending.

The sidecar uses generated client event IDs and the provider state it has
actually observed. Normally it sends `response.cancel` only when a response is
in progress and `output_audio_buffer.clear` only when provider output is
active; when both are valid, cancel precedes clear on the ordered `oai-events`
channel. RTP and SCTP are independently ordered, however. If decoded RTP has
already arrived before any response/output lifecycle, that actual media is
authoritative evidence of output: an explicit interrupt sends cancel then
clear, ignores any stale caller-supplied response identifier, and omits
`response_id` from cancel. Both controls still carry generated `event_id`
values. Outside that fail-safe case, either, both, or neither control may be
valid. A causally matched cancel-no-op error is recoverable. A clear error or
any unmatched provider error fails closed.

Provider `input_audio_buffer.speech_started` has different ownership: provider
VAD already performs automatic cancellation/truncation. That event kills local
playback and starts the same media fence, but sends no duplicate cancel or
clear.

Same-peer continuation is allowed only after both the required provider
control settlement and a fresh, complete receiver-quiescence window that began
at the interruption fence have been observed. A quiet state cached before the
fence is insufficient; the sidecar starts a new roughly 120 ms window at the
fence, and every decoded frame while muted restarts the full interval. The
child then emits transcript-free `interrupt.fenced`, unmutes, and lets the next
decoded audio open a fresh media generation. If it cannot prove that fence by
its bounded deadline, it fails the direct session so a later utterance must use
a fresh peer/thread. SCTP lifecycle never labels or gates RTP in this process.
These media-fence behaviors have automated coverage, but the repository does
not claim a physical v3 ordering/barge-in canary yet. Unlike v2, there is no
bridge `interrupt` acknowledgement or bridge-side `remote_cancelled` claim in
this protocol.

Stop, normal-wake preemption, mute, disconnect, or vendor-owner teardown uses a
non-preserving interrupt: playback is aborted, the device peer is stopped, and
the device sends the bridge `stop` control for remote cleanup.

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
