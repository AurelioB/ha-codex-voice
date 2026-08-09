# Realtime device wire protocol v2

This document defines the authenticated device-facing WebSocket contract at
`GET /v1/realtime`. Version 2 is a narrow, low-latency PCM transport between a
LAN audio endpoint and the host bridge. The bridge remains the only Codex App
Server/WebRTC peer and the device never receives ChatGPT credentials.

Version 1 remains supported for existing clients. A start message with no
`protocol_version` selects v1 and continues to use JSON objects containing
base64 audio. V2 never guesses or silently switches framing.

The route requires a bearer in the WebSocket `Authorization` header. Set a
distinct `HA_CODEX_REALTIME_DEVICE_TOKEN` on the bridge so the speaker cannot
use the Home Assistant/component credential or any non-realtime route. That
device bearer is authorized only after the first start message successfully
negotiates v2; missing, malformed, v1, and unsupported negotiations fail before
provider/thread startup. The primary bridge token remains valid for legacy v1
compatibility whether or not a device token is configured. Use WSS through a
trusted reverse proxy outside a source-restricted trusted LAN; this protocol
does not make plaintext bearer authentication safe on an untrusted network.

## Start and negotiation

The first WebSocket frame is UTF-8 JSON:

```json
{
  "type": "start",
  "protocol_version": 2,
  "audio_transport": "binary",
  "input_sample_rate": 16000,
  "input_channels": 1,
  "conversation_id": "optional-device-session-id",
  "voice": "optional-voice",
  "prompt": "optional-session-prompt"
}
```

`audio_transport` must be `binary`. `input_sample_rate` is an integer from
8,000 through 192,000 Hz; the ThirdReality client uses 16,000 Hz.
`input_channels` must be `1`. Samples are signed little-endian PCM16.
V2 rejects unknown start fields, device `tools`, model or realtime-version
overrides, startup-context/handoff overrides, and `initial_items`. The bridge
selects those App Server policies. Optional `conversation_id`, `voice`, and
`prompt` values are length-bounded before any thread is created.

The reference ThirdReality client omits `voice` and `prompt` unless they are
explicitly configured. It accepts a safe 1–64 character ASCII voice name that
starts with a letter and a printable prompt of at most 1,024 characters (within
the bridge's 4,096
character wire limit), then verifies the actual compact, ASCII-escaped start
frame fits its configured `max_message_bytes`. Language and accent are separate
prompt policies: for Mexican Spanish, specify Spanish as the response language
unless the user explicitly requests another language, independently request a
stable natural Mexican accent, and do not switch language based only on the
user's accent.

The server acknowledges a successful negotiation with JSON. The legacy
`sample_rate` and `channels` fields remain present, while v2 adds explicit
input/output shapes and capabilities:

```json
{
  "type": "started",
  "protocol_version": 2,
  "audio_transport": "binary",
  "input_sample_rate": 16000,
  "input_channels": 1,
  "output_sample_rate": 24000,
  "output_channels": 1,
  "sample_rate": 24000,
  "channels": 1,
  "capabilities": {
    "binary_pcm16": true,
    "local_flush": true,
    "remote_cancel": false
  }
}
```

`local_flush` means the device is expected to discard queued speaker PCM when
it interrupts playback. `remote_cancel: false` is intentionally explicit: the
subscription-backed App Server transport does not expose a reliable response
truncation operation. The bridge never reports that already-generated remote
speech was truncated.

Invalid or ambiguous negotiation produces a JSON `error` and closes the
socket before a Codex thread is created.

## Audio frames

After `started`:

- Every device-to-bridge binary WebSocket frame is mono PCM16 at the negotiated
  `input_sample_rate`.
- Every bridge-to-device binary WebSocket frame is mono PCM16 at 24,000 Hz.
- JSON/base64 `audio` messages are invalid in v2. Binary frames are invalid in
  v1.
- Binary input frames must be non-empty, sample-aligned, and at most 65,536
  bytes.

The bridge keeps one stateful resampler for the entire socket, so interpolation
state and sample counts survive arbitrary WebSocket chunk boundaries. Clients
should send 20–64 ms at a time and pace capture in real time.
The bridge caps only a v2 live session's paced WebRTC input track at 2,250 ms.
Finite STT sessions retain their existing whole-utterance capacity; the live
cap does not change those provider contracts. The decoded provider-audio queue
holds at most 25 chunks (normally roughly 500 ms). Queue overflow and
unexpected media EOF are terminal protocol errors; audio is not silently
dropped to hide latency.

The reference ThirdReality client keeps at most 64 KiB of microphone input and
64 KiB of the same pre-ready audio for Home Assistant fallback. Each represents
2.048 seconds at 16 kHz mono PCM16; they are parallel ownership copies, not a
4.096-second serial backlog. After `started`, the client sends queued frames at
no more than 2× capture rate while more than one frame remains, then returns to
normal capture cadence. This preserves accepted queued audio without an
unbounded burst. It shrinks a startup offset but does not remove cold
thread/WebRTC negotiation or provider response latency. The reference playback
queue is 48 KiB, about 1.024 seconds at 24 kHz mono PCM16.

The provider WebRTC track can contain continuous silence even when the
assistant is not answering. The bridge drains that track but does not proxy
pre-response silence. It quarantines at most 200 ms of non-silent leading audio
and arms output only after an allowlisted provider lifecycle signal. The gate
opens after both that signal and non-silent PCM; an arm with no audio or
terminal event expires after a bounded timeout. Every output epoch begins
before its first binary frame:

```json
{"type":"control","event_type":"speaking.started","output_epoch":1}
```

It ends after the terminal boundary and a bounded tail:

```json
{"type":"control","event_type":"speaking.stopped","output_epoch":1}
```

`output_epoch` increases monotonically for the lifetime of the socket. A
client must gate playback from these controls and must not infer that any bare
binary frame starts a new response. After `speaking.stopped`, PCM for that
epoch is no longer forwarded.

The reference v1.1.7 client is turn-taking: its configuration accepts only
`full_duplex: false`, and it gates microphone submission from
`speaking.started` until queued PCM and the playback child have drained. A
`max_message_bytes` setting ranges from 2,048 through 65,536 payload bytes and
defaults to 65,536; WebSocket framing overhead is separate, and the minimum
carries one fixed 2,048-byte recorder frame. Protocol v2 does not supply
acoustic echo cancellation, and the released device setup has no active AEC.
Simultaneous listen/speak, true double-talk, and barge-in are therefore outside
this milestone.

## JSON control messages

The following client-to-bridge controls remain JSON:

- `text`: append non-empty text with an optional `role`.
- `speech`: request speech from non-empty text.
- `ping`: request `{"type":"pong"}`.
- `stop`: end the session normally.
- `interrupt`: end the current remote realtime session immediately.

V2 device mode is chat-only. A v2 start containing `tools`, an incoming
`tool_result`, or a provider tool call is rejected and never forwarded to the
speaker. Okay Nabu and Home Assistant retain home-control authority. An
audited, Home Assistant-owned tool broker is future work. Legacy v1 keeps its
existing tool exchange, including one-shot result consumption.

An interrupt returns the following control before the socket closes:

```json
{
  "type": "stopped",
  "reason": "interrupt",
  "fresh_session_required": true,
  "remote_cancelled": false
}
```

The device should flush its local playback queue on receipt. Continuing after
an interrupt requires a new WebSocket and therefore a fresh remote realtime
session. This is session teardown, not a claim that the provider truncated a
response.

Control RPCs and WebSocket sends have finite deadlines. A failure is returned
as an `error` when the socket remains writable, followed by exactly-once
realtime-session and thread cleanup.

## Server events and privacy

V2 does not expose transcript text, raw items, tool calls, arbitrary App Server
RPC events, or provider error payloads. Provider data-channel payloads are
never proxied verbatim. The bridge continuously drains that channel and may
emit only an allowlisted, content-free signal:

```json
{"type":"control","event_type":"turn.done"}
```

The allowlist is limited to session, speech-boundary, response-boundary, and
turn-boundary event names. Transcript fragments, deltas, delegation payloads,
unknown future events, malformed JSON, and every other provider field are
dropped at the trust boundary.

## Version 1 compatibility

Existing clients may continue to omit `protocol_version` and send:

```json
{
  "type": "audio",
  "audio": "<base64 PCM16>",
  "sample_rate": 24000,
  "channels": 1
}
```

V1 responses keep the existing JSON/base64 audio envelope. V2-only binary
frames, negotiation fields, and sanitized data-channel controls are not sent
to a v1 client. V1 also retains its existing transcript, item, and generic RPC
event behavior for backward compatibility; those content-bearing messages are
never emitted on v2.
