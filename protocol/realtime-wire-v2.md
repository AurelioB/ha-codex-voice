# Realtime device wire protocol v2

> [!NOTE]
> Protocol v2 is the active server-offloaded `media_transport: "bridge_pcm"`
> path for constrained ThirdReality speakers. The experimental
> [protocol v3](realtime-wire-v3.md) remains available as an explicit rollback;
> selecting either transport never silently changes the other's framing.

This document defines the authenticated device-facing WebSocket contract at
`GET /v1/realtime`. Version 2 is a narrow, low-latency PCM transport between a
LAN audio endpoint and the host bridge. The bridge remains the only Codex App
Server/WebRTC peer and the device never receives ChatGPT credentials. V2
deliberately grants the device only audio and bounded lifecycle-control
authority.

The current reference client explicitly requests
`conversation_mode: "native"`. That selection is session-scoped and forces a
native Codex App Server WebRTC provider generation. The bridge keeps at most
one generation active at a time behind the device WebSocket and ignores any captured
Home Assistant broker snapshot for that session: it does not wait for a
completed transcript, start an executor thread, or render text with
`thread/realtime/appendSpeech`.

For compatibility, a strict-v2 client may omit `conversation_mode`. Omission
retains the previous automatic policy: App Server realtime v3 plus a captured
Home Assistant broker snapshot selects the managed two-thread implementation;
otherwise the bridge selects native realtime. This legacy auto/managed route
is an internal bridge policy, not additional device authority. New clients
should request their intended mode explicitly.

The upstream Codex App Server realtime surface used behind this wire is
experimental and is not the documented OpenAI Realtime API. Native mode removes
the bridge-created turn pipeline, but every fresh socket still incurs cold
thread creation, WebRTC negotiation, service admission, network, and provider
response latency.

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

The current reference client also sends the exact WebSocket handshake header
`User-Agent: ha-codex-voice-thirdreality/2`. This negotiates support for the
legacy managed `continuation_safe` interrupt acknowledgement described below.
It does not change authentication or grant access to the Home Assistant
broker. An older client without that exact value remains valid but receives
the fresh-session fallback after a managed interrupt.

## Start and negotiation

The first WebSocket frame is UTF-8 JSON:

```json
{
  "type": "start",
  "protocol_version": 2,
  "conversation_mode": "native",
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
`conversation_mode`, when present, must be exactly `native`; `managed`, `null`,
and other values are rejected. A native request captures the currently
registered Home Assistant authority snapshot and selects an initial App Server
realtime voice thread. Omitting
the field preserves legacy automatic route selection. V2 rejects unknown start
fields, device `tools`, model or realtime-version overrides,
startup-context/handoff overrides, and `initial_items`. The bridge selects
those App Server policies. Optional `conversation_id`, `voice`, and `prompt`
values are length-bounded before any thread is created.

The reference ThirdReality client always sends `conversation_mode: "native"`;
this is not a device configuration option. It omits `voice` and `prompt` unless
they are explicitly configured. It accepts a safe 1–64 character ASCII voice
name that starts with a letter and a printable prompt of at most 1,024
characters (within the bridge's 4,096-character wire limit), then verifies the
actual compact, ASCII-escaped start frame fits its configured
`max_message_bytes`. Language and accent are separate prompt policies: for
Mexican Spanish, specify Spanish as the response language unless the user
explicitly requests another language, independently request a stable natural
Mexican accent, and do not switch language based only on the user's accent.

The server acknowledges a successful negotiation with JSON. The legacy
`sample_rate` and `channels` fields remain present, while v2 adds explicit
input/output shapes and capabilities:

```json
{
  "type": "started",
  "protocol_version": 2,
  "conversation_mode": "native",
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
    "remote_cancel": false,
    "same_session_interrupt_ack": true,
    "server_owned_media": true,
    "native_end_conversation": true
  }
}
```

When the start request includes `conversation_mode`, the acknowledgement must
echo the accepted value. The current reference client requires
`conversation_mode: "native"` in `started` and fails closed if it is absent or
different. For a legacy strict-v2 request that omits the field, the bridge also
omits it from `started`.

`local_flush` means the device is expected to discard queued speaker PCM when
it interrupts playback. `remote_cancel: false` is intentionally explicit: a
client may never infer remote cancellation from a local flush or VAD event.
`same_session_interrupt_ack: true` means the bridge can separately acknowledge
whether the legacy `interrupt` exchange may continue on this socket. On its
native compatibility path, that requires a provider `response.cancelled` event
whose identifier matches the active response. On the legacy managed two-thread
path, a `/2` client can instead receive `continuation_safe: true` after the
bridge invalidates its owned executor/output generation. The latter explicitly keeps
`remote_cancelled: false`. The bridge never claims that already-played audio
was unheard or that an uncorrelated response was truncated. The active explicit
native client uses the non-acknowledged `barge` rollover below instead of this
legacy cancellation exchange.
`server_owned_media: true` proves that the bridge, not the speaker, owns the
provider WebRTC peer. For an explicit native start,
`native_end_conversation: true` proves that the sole bridge-owned terminal
control is available. The reference client requires every capability value
shown above and fails closed against an older bridge;
`same_session_interrupt_ack` remains a compatibility/version floor and does
not make active `barge` acknowledgement-based.

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

### Native same-peer provider control

A native v2 client using the desktop-compatible provider-control policy sends
exactly:

```json
{"type":"provider_barge"}
```

The bridge immediately fences local output, then sends `response.cancel`
followed by `output_audio_buffer.clear` on the existing provider WebRTC data
channel. The device keeps the same WebSocket and microphone stream alive; the
bridge keeps the same Codex thread and provider peer. A later provider
`output_audio_buffer.cleared` event is forwarded as content-free lifecycle
metadata. The next audible response receives the next monotonic device output
epoch.

This mode requires explicit native binary v2. No extra fields are accepted.
The reference speaker qualifies the same local AEC-backed near-end boundary as
rollover mode, so it cuts local playback before sending this control and never
waits for upstream VAD.

### Native barge rollover fallback

The conservative fallback interrupts assistant playback with exactly this
JSON object:

```json
{"type":"barge"}
```

No additional fields are permitted. `barge` is nonterminal and has no
acknowledgement frame: the device flushes its local playback immediately,
keeps the same WebSocket and microphone capture open, and continues sending
binary PCM. It must not wait for a provider event before sending the speech
that caused the interruption. This control is accepted only for an explicit
`conversation_mode: "native"` binary-v2 session; it is not an alias for the
legacy `interrupt` exchange described below.

The bridge owns the provider replacement behind that stable device socket. On
`barge`, it atomically advances the provider generation, fences every old
generation JSON/control and binary-audio sender, and preserves up to 320 ms of
already-resampled microphone pre-roll. PCM arriving while replacement is in
progress is appended in capture order, within the existing 2,250 ms live-input
bound. Once the replacement is ready, the bridge feeds the retained pre-roll
followed by during-rollover PCM to its paced input track exactly once. Old
provider audio and controls can never become output for the new generation.

Before reusing a Codex thread, the bridge requires strict stop and a matching
provider `thread/realtime/closed` confirmation within the 100 ms reuse grace. A
confirmed close starts the replacement on the same thread with startup context
retained. An RPC error, provider error, timeout, provider/App Server disconnect,
or otherwise ambiguous stop isolates the replacement on a fresh thread without
startup context while the old thread is retired under bounded cleanup ownership.
Ambiguity is never treated as proof that same-thread reuse is safe.

During rollover, `ping` still receives `pong`, `stop` remains terminal, and a
duplicate exact `barge` is idempotent because the current output generation is
already retired. Other controls are protocol errors. A device stop or
disconnect also abandons retained PCM and closes/disposes any replacement that
finishes late; it does not leave a provider or thread orphaned.

`output_epoch` remains monotonic for the lifetime of the WebSocket, not the
lifetime of one provider peer. Thus the first response after a rollover uses
an epoch greater than every response sent before that barge.

For full-duplex realtime-only operation, the reference ThirdReality client
discards the wake tail and all pre-ready microphone audio. It plays exactly one
acknowledgement cue only after `started`; cue EOF atomically opens provider
capture. There is therefore no startup replay or Home Assistant fallback on
this path. Once live, microphone frames are paced in real time with a bounded
64 KiB input queue. The reference playback queue is 48 KiB, about 1.024 seconds
at 24 kHz mono PCM16.

The provider WebRTC track can contain continuous silence even when the
assistant is not answering. The bridge drains that track but does not proxy
pre-response silence. PCM received before an allowlisted provider lifecycle
signal is dropped. That signal authorizes and arms output; it does not begin a
speaking epoch by itself. After authorization, the bridge retains at most 200
ms of non-silent leading audio, and the first authorized non-silent PCM begins
the epoch. An arm with no audio or terminal event expires after a bounded
timeout. Every output epoch begins before its first binary frame:

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

An explicit native session has no transcript/executor/render handoff in its
media path:

```text
paced microphone PCM -> active App Server WebRTC provider generation
                     -> provider audio -> speaking epoch -> device playback
```

Provider speech is the direct output of the active realtime generation. The
bridge does not require a user transcript to complete, create a parallel
executor thread, or call `thread/realtime/appendSpeech`. An ambiguous strict
stop may create a fresh isolated native thread for the replacement generation,
as specified above.

The legacy auto-selected managed two-thread path adds a stricter bridge-owned
authorization gate:

```text
identified raw v3 user turn (or bounded v2 user text)
  -> isolated executor turn and optional Home Assistant broker calls
  -> completed executor final
  -> one <=500-byte UTF-8 appendSpeech on the tool-free frontend
  -> session.context.appended for the current generation
  -> unique assistant turn.created -> authorize and arm frontend PCM
  -> first authorized non-silent PCM -> begin a speaking epoch
  -> matching assistant turn.done -> retire the serialized render slot
```

The bridge starts the frontend with immutable routing instructions,
`clientManagedHandoffs: true`, and `delegationAckFiller: false`; this requires
Codex CLI 0.147.0 or newer. It disables unrelated repository startup context
and drops all frontend PCM received before the context append and identified
assistant turn agree with the same generation. Managed v3 accepts only its
identified `turn.created`/`turn.done` lifecycle for render ownership; response
and output-buffer aliases are ignored. Direct, replayed, or unsolicited
frontend answers, provider acknowledgement filler, and late output from an old
generation therefore cannot reach the device. Provider cancellation is best
effort; generation matching is the authoritative local gate.

The isolated executor must complete within the bridge request timeout. A
failed terminal or missing completion produces a generic `error` and closes
the session. During stop or disconnect, the bridge tombstones and interrupts
an active executor turn before closing its event subscription and disposing
both owned threads.

The reference v1.1.7 client remains turn-taking by default: with
`full_duplex: false`, it gates microphone submission from `speaking.started`
until queued PCM and the playback child have drained. A `max_message_bytes`
setting ranges from 2,048 through 65,536 payload bytes and defaults to 65,536;
WebSocket framing overhead is separate, and the minimum carries one fixed
2,048-byte recorder frame.

Full duplex is an explicit, fail-closed device option; protocol v2 itself does
not provide AEC. The active ThirdReality deployment uses native AEC3 against
the device's sample-aligned physical render reference and applies its bounded
capture gain exactly once before WebSocket egress. Before opening the socket,
the reference client also verifies the reviewed static PulseAudio topology,
the configured allowlisted `aec_method`, exact raw hardware masters and AEC endpoint
names, those endpoints as defaults, and every current-process native capture
stream routed through the uncorked AEC source. The allowlist is `webrtc`,
`speex`, and `adrian`; omitted configuration defaults to WebRTC and never falls
back automatically. Every AEC sink channel must be at or below the configured
`aec_sink_volume_ceiling_percent`, which defaults to 25 and is limited to
1–100. The same sink ceiling is rechecked before every speaking epoch, and
`paplay` is pinned to the AEC sink with the independently configured fixed
linear `playback_volume_percent`, also defaulting to 25 and limited to 1–100.
Configuration is rejected when playback exceeds the sink ceiling.
The active reference route fixes both values at 100 so a separate runtime
software control can expose mute at 0 and audible levels 1–100 without moving
the AEC anchor. A saved initial level such as 80 is non-amplifying attenuation
below that anchor; no user-facing setting amplifies playback PCM.
The reference deployment helper writes the matching raw sink setpoint in the
static PulseAudio startup block after AEC sink creation. On the stock device,
the later vendor media-player preference must be persisted at the same value.
These deployment properties complement the runtime protocol guard; they do not
weaken or replace the per-response recheck.
The legacy `aec_test_volume_percent` key maps to both values only when neither
explicit key is present. The sink guard
compares raw PulseAudio volume units against the exact linear ceiling; it does
not trust the rounded displayed percentage. An engine loading and producing
the expected endpoints does not replace physical echo-rejection and double-talk
qualification.

Only after those checks does the client keep microphone submission active
during playback. The active reference client uses bounded AEC-filtered local
speech detection to flush the player and send exact `barge`; it never waits for
provider VAD. The bridge's generation fence and strict replacement—not
`input_audio_buffer.speech_started`, response cancellation, or a remote
acknowledgement—decide when retained audio may enter the new provider peer.
Clients using the legacy `interrupt` control remain subject to the correlated
acknowledgement rules below.

## JSON control messages

The following client-to-bridge controls remain JSON:

- `text`: submit a non-empty, length-bounded **user** message. `role` may be
  omitted (it defaults to `user`) or must be exactly `user`; v2 does not accept
  assistant/output text roles. In a legacy auto-selected managed path this
  starts an isolated executor turn; on the native path it is appended to the
  one realtime session.
- `speech`: legacy compatibility control for an omitted-mode native session.
  Explicit native rejects it so `thread/realtime/appendSpeech` cannot enter the
  route; a legacy managed path also rejects it because only an executor final
  may enter its frontend rendering channel.
- `ping`: request `{"type":"pong"}`.
- `stop`: end the session normally.
- `barge`: exact explicit-native, nonterminal provider rollover as defined
  above. No fields other than `type` are accepted.
- `interrupt`: legacy compatibility exchange that requests cancellation of the
  active provider response.

The v2 device remains audio/control only. A v2 start containing `tools` or an
incoming device `tool_result` is rejected. Provider tool calls and Home
Assistant results are never forwarded to the speaker. An explicit native
session exposes bridge-owned `end_conversation`, the captured Home Assistant
tools, and optional bridge-owned agent tools. Spanish and English terminal
instructions require the model to
call it immediately without a spoken promise. An exact normalized terminal
transcript such as `Terminar`, `Terminar llamada`, `goodbye`, or `hang up` is a
narrow fallback. The bridge then sends
`{"type":"stopped","reason":"end_conversation"}` and closes the owned
session. Every undeclared provider tool request is rejected internally. The
first Home Assistant Conversation subentry is the default authority; additional
subentries remain non-authoritative unless selected explicitly. That subentry
opens the separate
`/v1/home-assistant/tools` WebSocket with the primary bridge token, registers a
bounded snapshot of its selected Home Assistant LLM API tools and rendered
instructions, and executes correlated calls locally. Its locale defaults to
`es-MX`. The route-scoped device bearer cannot open that broker, and zero,
ambiguous, stale, disconnected, timed-out, or invalid authority fails closed.
Legacy v1 keeps its existing device-visible tool exchange for compatibility.

The authority uses Home Assistant's `conversation` exposure namespace, so its
entity-dependent tool view is the same one used by the official Conversation
flow. The complete broker write/execution/result exchange is deadline-bound;
an unknown outcome is returned to the provider with `do_not_retry`, disables
further authority calls for that session, and is never replayed by the bridge.
After a result is delivered, assistant output or a terminal provider event must
arrive within 20 seconds or the session fails closed. These errors remain
internal/provider-facing; the speaker still receives no tool payload.

For a strict-v2 request that **omits** `conversation_mode`, App Server v3 plus a
captured authority snapshot selects the compatibility managed route. Selected
tools then exist only on a separate executor thread. The WebRTC speech frontend
is started with an empty tool list. A frontend, foreign, stale, or post-interrupt
tool request is answered internally with
`unowned_home_assistant_tool_call` and `do_not_retry: true` and is never sent to
Home Assistant. App Server v3 may route native delegation before notifying the
bridge, so this thread boundary—not frontend prompt compliance—is what prevents
side effects.

Every legacy `interrupt` revokes the bridge's current output epoch. The
acknowledgement then depends on the internal route and negotiated client
behavior. Active `barge` instead follows the nonterminal generation-replacement
contract above and returns no acknowledgement frame.

For a legacy broker-managed session from the exact `/2` User-Agent, the bridge
advances its executor/output generation and asks the tool-free frontend
provider to cancel only if an identified assistant render has started. Idle
and merely pending frontend sessions are never cancelled. If the executor has
not dispatched a Home Assistant tool, the bridge tombstones the turn before
`turn/interrupt`, waits for its terminal event, and rejects any late tool
request. Once a tool has been dispatched, it does not interrupt or replay that
potentially side-effecting turn; it lets the result settle, suppresses the
stale final, and, if barge-in produces another transcript, runs the newest
queued transcript afterward. The acknowledgement is:

```json
{
  "type": "stopped",
  "reason": "interrupt",
  "fresh_session_required": false,
  "remote_cancelled": false,
  "continuation_safe": true
}
```

The client may continue on the same WebSocket. `continuation_safe` means that
the bridge invalidated the owned executor/output generation and will not pass
stale PCM; it does **not** mean the provider confirmed cancellation. The
frontend is tool-free, so an unconfirmed provider response cannot dispatch a
Home Assistant action.

A legacy broker-managed session from an older client does not opt into that
local continuation contract. The bridge returns the established safe fallback
and closes the socket:

```json
{
  "type": "stopped",
  "reason": "interrupt",
  "fresh_session_required": true,
  "remote_cancelled": false
}
```

The next wake uses a fresh WebSocket, frontend, and executor. This preserves
backward compatibility: an older deployed client need not understand the new
field or upgrade in lockstep with the bridge.

For an explicit-native compatibility client that sends legacy `interrupt`
rather than `barge`, same-socket continuation remains tied to remote
cancellation. If and only if the bridge
receives a provider `response.cancelled` event whose response identifier
matches the response active when the request was sent, it returns:

```json
{
  "type": "stopped",
  "reason": "interrupt",
  "fresh_session_required": false,
  "remote_cancelled": true
}
```

That compatibility client may then continue on the same WebSocket. A cancellation event for a
different response, a completion event, send failure, or the bounded
confirmation timeout cannot produce this acknowledgement; the bridge instead
returns the same `fresh_session_required: true` /
`remote_cancelled: false` fallback above and closes the socket.

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
turn-boundary event names, including the content-free name
`response.cancelled`. The provider response identifier is consumed only inside
the bridge to correlate an outstanding legacy `interrupt`; it is not included in the
device control. Transcript fragments, deltas, tool payloads, delegation
payloads, unknown future events, malformed JSON, and every other provider field
are dropped at the trust boundary.

A user turn start carries the single bounded marker `"role":"user"`. The
speaker uses that marker to discard already-buffered assistant audio instead of
gracefully draining an unheard response tail. No turn identifier, transcript,
or other provider field crosses the boundary.

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

Strict-v2 clients deployed before `conversation_mode` remain wire-compatible:
omission selects the legacy automatic native/managed policy and receives no
mode field in `started`. This compatibility rule must not be mistaken for the
current reference client's behavior; it always requests and verifies native
mode.
