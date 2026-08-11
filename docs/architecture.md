# Architecture

## Standard Assist pipeline

```text
ThirdReality Assist satellite
  -> Home Assistant Assist pipeline
     -> Wyoming faster-whisper STT entity
     -> Codex Voice Conversation entity
     -> Wyoming Piper TTS entity (external host, es_MX-ald-medium)
  -> ThirdReality speaker

Codex Voice Conversation
  -> authenticated HTTP/WebSocket bridge API
  -> Codex App Server over JSON-RPC/stdio
  -> Codex-managed ChatGPT login

Wyoming STT
  -> local persistent faster-whisper model

Wyoming TTS
  -> local persistent Piper model
```

The component is a normal Home Assistant integration with one parent config
entry. New entries create Conversation and subscription-backed TTS subentries;
Home Assistant's native Wyoming integration owns the reliable STT and
recommended TTS entities. The retained Codex TTS entity remains an explicit
experimental option. The retained Codex STT subentry is an explicit
experimental diagnostic because Codex realtime conversation does not provide a
deterministic finite transcription contract. The existing ThirdReality
satellite needs no firmware change for this turn-based mode.

Conversation turns use stable App Server thread and turn methods. Selected Home
Assistant LLM tools are advertised as dynamic tools. When Codex requests a tool,
the component executes it through Home Assistant's LLM API; the bridge has no
direct home-control authority. Conversation start and tool-result events are
encoded with Home Assistant's canonical JSON serializer, including nested
temporal values such as `date`, `time`, and `datetime`. Values outside that
policy are rejected locally with a data-safe protocol error before a WebSocket
message is sent.

The pinned v1.1.7 device overlay adds a second, explicitly separate route:

```text
"Okay Nabu" -> stock satellite protocol -> Home Assistant Assist/tools
"Okay Computer" -> stdlib controller + isolated device aiortc sidecar
                 -> realtime wire v3 SDP/sideband -> bridge/App Server/OAuth
                 -> direct RTP audio + oai-events between device and provider
                 -> local device playback

No Home Assistant broker snapshot, transcript executor, or appendSpeech render
handoff participates in the Okay Computer session.
```

The direct device is the aarch64 Buildroot Linux speaker, not Android, and
remains an untrusted audio/control endpoint. It never
receives a Home Assistant credential, advertises tools, sends tool results, or
receives provider tool calls or transcript content. The current reference
client always requests native mode. The bridge ignores any registered realtime
authority for that session, so Okay Computer is tool-free; Okay Nabu owns Home
Assistant Assist and entity control. Qualified device AEC makes Okay Computer
the full-duplex/barge-in route. A normal Okay Nabu wake can preempt direct
mode and regain the microphone on the same vendor capture thread.

For v3 the bridge owns the managed Codex login, one active App Server realtime
thread per peer epoch, SDP relay, lifecycle sideband, unexpected-tool rejection,
and cleanup only. It
does not construct the peer or relay PCM/provider data. The device's isolated,
hash-locked `aiortc` runtime owns the audio transceiver and ordered
`oai-events` channel. Protocol v2 `bridge_pcm` remains an explicit rollback and
legacy compatibility path.

For legacy auto/managed compatibility, authority is selected from Conversation
config subentries, not from a device message. Zero or multiple opted-in
subentries disable registration; the
configuration flow also rejects a second authority. A successful registration
captures one immutable generation containing the rendered Home Assistant
instructions, an `es-MX`-by-default locale, and no more than 128 selected tools.
Each v2 provider session snapshots that generation. Schema, argument, result,
message, pending-call, and per-session-call limits are enforced on both sides;
an authority replacement, disconnect, timeout, correlation mismatch, or
undeclared provider tool fails closed without exposing the broker exchange to
the device.

## Legacy auto/managed realtime compatibility

The current ThirdReality v3 client does not enter this route. It sends
`conversation_mode: "native"`, requires the bridge to echo that selection, and
therefore gets exactly one native App Server WebRTC voice thread. A native
session does not wait for a transcript, create an executor, or call
`thread/realtime/appendSpeech`.

The two-thread route remains for older strict-v2 clients that omit
`conversation_mode`. Under that compatibility policy it activates only when
all three inputs are present: App Server realtime version v3, a captured Home
Assistant broker snapshot, and an omitted conversation mode. A session without
all three inputs keeps native single-thread behavior. Codex CLI 0.147.0 or newer
is required because the managed route explicitly disables App Server's
delegation acknowledgement filler.

For the managed route, the bridge creates a tool-free realtime speech frontend
and a separate executor thread carrying the immutable Home Assistant context
and selected tools. The frontend receives immutable routing instructions plus
the bounded device language, voice-style, and brevity preferences. Its App
Server start sets `clientManagedHandoffs: true` and
`delegationAckFiller: false`. App Server v3 can route a native delegation before
the bridge observes a handoff notification, so prompts and client-managed
handoffs alone are not the security boundary; physical thread separation keeps
the provider-facing frontend unable to execute Home Assistant side effects.

```text
identified raw v3 user turn (or bounded v2 user text)
  -> bridge generation -> turn/start on isolated executor
  -> allowlisted tool call -> captured HA broker generation -> Home Assistant
  -> correlated result -> same executor turn
  -> completed final agent answer
  -> one <=500-byte UTF-8 appendSpeech on tool-free frontend
  -> session.context.appended for the same bridge generation
  -> unique assistant turn.created -> authorize and arm frontend PCM
  -> first authorized non-silent PCM -> begin speaking epoch
  -> matching assistant turn.done -> retire render -> device
```

Executor events are accepted only for the owned executor thread and active
turn. A bounded buffer handles events that arrive before `turn/start` returns.
The bridge prefers a completed `final_answer` agent message, otherwise the
latest completed non-commentary agent message, and never speaks an incomplete
or stale turn. A tool request from the frontend, a foreign or stale executor
turn, or a turn already tombstoned for interruption receives
`unowned_home_assistant_tool_call` with `do_not_retry: true`; it never crosses
the Home Assistant broker.

The frontend's provider audio is unauthorized by default. Only a completed
executor answer submitted through a single `thread/realtime/appendSpeech`
context frame, followed by the provider's `session.context.appended`
acknowledgement and a new identified assistant turn for the current generation,
can open the output gate. Only its matching `turn.done` retires the one-render
slot. Turn IDs are claimed for the socket lifetime across both roles, so replay
and role swapping cannot reopen command or audio authority. Direct frontend
answers, acknowledgements, late PCM, and stale-generation output stay blocked
or are dropped. Once a stale assistant render is identified as started,
provider cancellation is requested as a latency optimization; idle and merely
pending sessions are never cancelled. The local generation gate is the
authoritative suppression boundary.

## Isolated App Server profile and thread lifecycle

The bridge does not launch App Server against the user's everyday Codex home.
For each App Server process it creates mode-0700 temporary `HOME` and
`CODEX_HOME` directories and links only the existing managed ChatGPT
`auth.json` into that profile. File-backed CLI authentication is forced so a
Codex refresh updates the source credential through the link. The bridge
auto-detects `${CODEX_HOME}/auth.json` or `${HOME}/.codex/auth.json`; deployments
with another location can set `HA_CODEX_AUTH_FILE`. Startup fails if no secure,
file-backed credential is available.

This profile boundary keeps the user's normal Codex configuration, history,
apps, plugins, and MCP servers—including automatically discovered sidecars—out
of voice sessions. App Server's effective configuration layers are also
audited at startup; a configured MCP server aborts startup. No OAuth secret is
copied into Home Assistant or the repository.

Threads start with `ephemeral: false`, but their persistence is confined to the
temporary profile. This is intentional: App Server cannot apply
`thread/delete` to an ephemeral thread. The bridge deletes one-shot STT, TTS,
and realtime threads as soon as their session ends. A native device session
owns one active realtime thread per peer epoch and cleans every replacement it
created. A legacy broker-managed session owns both a frontend
and executor thread, and its cleanup disposes both
even if stopping or deleting the other resource fails. The bundled Home
Assistant component does not retain STT sessions for TTS, and the released
bridge never issues a handoff ticket. Dormant validation and ownership
machinery remains for future protocol work. Cached Conversation threads are
deleted when retired, evicted, or the bridge closes. Deletion unloads the
thread immediately instead of retaining it for App Server's idle-unload period.

## Experimental subscription audio adapters

Codex App Server does not expose independent subscription-backed STT or TTS
RPCs. The retained Codex TTS entity and experimental STT entity therefore
create short-lived realtime-conversation sessions:

1. Create an `aiortc` peer with a paced audio track and `oai-events` data
   channel.
2. Send the SDP offer to `thread/realtime/start` with realtime v3 and WebRTC
   transport.
3. Apply the answer returned by App Server.
4. Send or receive RTP audio and observe transcript events.
5. Stop and dispose of the session on success, timeout, cancellation, or error.

The active outbound track is required even for synthesis: a transceiver-only
offer negotiates successfully but the subscription service does not deliver
remote audio until outbound RTP activates the media path. Native media cadence
is 48 kHz, 16-bit PCM, 960 samples per 20 ms frame. Incoming WebRTC audio is
decoded from 48 kHz stereo, downmixed and resampled to 24 kHz mono PCM, and
returned to Home Assistant as WAV.

App Server's raw realtime WebSocket route is not used. In the pinned release it
requires API-key authentication, while the WebRTC call-creation path works with
Codex-managed ChatGPT OAuth and consumes ChatGPT subscription availability,
not OpenAI Platform API quota.

This App Server realtime surface is experimental and is not the documented
OpenAI Realtime API. Its methods, events, admission behavior, and latency may
change with Codex CLI/App Server. A native direct wake still performs cold
thread creation and WebRTC negotiation; single-thread routing removes the
bridge-created turn pipeline, not those startup costs.

## Reliable finite STT boundary

The production pipeline sends Home Assistant's bounded PCM16 stream to a local
Wyoming faster-whisper service. Home Assistant owns stream cancellation and
language selection; Wyoming keeps the model warm and returns one standard STT
result after capture EOF. This path does not open a Codex thread, consume a
subscription speech slot, or silently fall back to the experimental adapter.

```text
capture -> local Wyoming STT -> transcript -> Codex Conversation
        -> local Wyoming Piper TTS
```

See [reliable local speech-to-text](local-stt.md) for deployment and acceptance
checks.

## Reliable local TTS boundary

The recommended production pipeline sends the completed Conversation response
through Home Assistant's native Wyoming integration to a local Piper service.
For Mexican Spanish, the validated external service pins
`wyoming-piper==2.3.1`, keeps `es_MX-ald-medium` available, and listens on
`tcp://HOST:10200` after the operator replaces the loopback default with an
explicit trusted-LAN bind address.

The external deployment pins that voice to an immutable upstream revision.
An installer verifies exact sizes and SHA-256 digests before atomic placement,
and the private service runner verifies them again at startup. The hardened
unit exposes the model directory read-only and restricts Wyoming requests to
the single reviewed voice.

```text
Codex response text -> Home Assistant Wyoming client -> local Piper synthesis
                    -> PCM audio -> satellite playback
```

This stage does not create a Codex thread, use the subscription speech lane, or
send response text to a remote speech provider. The official Home Assistant
Piper add-on (shown as an app in current UI) is the simplest Home Assistant OS
path. On affected virtualized
x86-64 installations its current runtime may require the guest CPU model to
expose x86-64-v2 instructions; an external Wyoming Piper service is the
supported fallback when that guest CPU contract cannot be changed. This is a
bounded virtualization caveat, not a general requirement for every Piper
deployment or architecture.

See [reliable local text-to-speech](local-tts.md) for both setup paths,
pipeline selection, measurements, and acceptance checks.

## Experimental finite subscription adapter

When explicitly configured, the Codex STT provider opens
`/v1/transcribe/stream` before consuming the microphone iterator. After
validating the start message, the bridge starts the thread and WebRTC handshake
in a task while it continues receiving bounded PCM frames. Once sustained
speech supplies enough evidence for a bounded, one-time level calibration,
normalized audio is released to that task during capture. The bridge retains
the complete raw utterance for a same-path retry. Neither successful signaling
nor complete media delivery guarantees that the conversational backend emits a
user transcript, so this cannot serve as the reliable pipeline boundary.

The experimental lifecycle creates a fresh realtime resource per speech
operation. Automatic reuse is disabled because live v3 sessions emitted
assistant output before finite STT completion and the supported Frameless Bidi
outbound protocol has no response-cancel control.

```text
Experimental Codex STT active
  -> transcript result -> stop session -> delete thread
  -> experimental Codex TTS -> start fresh session -> stream speech
                            -> stop/delete
```

The dormant diagnostic handoff protocol uses a 256-bit single-use ticket. Its
design stores only a SHA-256 digest in the bridge; outside the authenticated
request/response transport, the raw value would remain in the component's
private in-memory context. The component-side design binds it to the exact
bridge client, Home Assistant `ChatSession` object, pre-STT TTS preparation,
voice, and normalized language. The bridge-side design also requires a matching
ticket digest, unexpired offer, compatible voice and language, no custom TTS
instructions, and an observed quiet boundary.

This would make reuse explicit rather than interpreting “the next TTS request”
as ownership. Calls without the marker preserved in the pipeline's prepared TTS
result—including ordinary direct `tts.speak` service calls, even in the same
chat session—take a fresh session. A mismatched or expired offer is cleaned
before the new cold session starts. If claimed reuse fails before the first PCM
leaves the bridge, synthesis can try the cold path within the original
deadline; it cannot restart after first PCM without risking duplicate speech.

The bundled component omits the private request, so STT and TTS always use
separate remote contexts. Diagnostic tickets remain bearer secrets and are
excluded from logs and diagnostics.

The bridge does not prewarm a remote session before a future wake word. A
custom STT provider has no reliable Home Assistant callback before wake
detection, an idle peer continues sending paced silent RTP, and a speculative
session would occupy the single subscription speech lane without a documented
quota-neutral idle lifetime. Experimental Codex adapter optimization is
therefore limited to capture overlap and progressive TTS delivery. The
recommended Wyoming providers do not use those remote adapters. The direct
device prewarms exactly two reusable local isolated `aiortc` processes. One is
active and one is the offer-warm standby; they alternate fresh PeerConnections,
and recycle the retired epoch's process as standby. An absent or invalid standby
ends the outer session without a cold replacement or third process. This local
prewarm does not create a Codex thread, open a bridge socket, negotiate remote
WebRTC, or consume the speech lane until the explicit wake. See
[performance and ThirdReality tuning](performance.md) for the live measurements
and acceptance criteria.

## Realtime client mode

The bridge's `/v1/realtime` WebSocket is a project-owned LAN protocol, not the
App Server transport. Legacy v1 keeps JSON/base64 compatibility; v2 keeps its
binary bridge-PCM contract; v3 carries strict SDP and JSON sideband only. For
v3 the bridge owns the managed login and App Server signaling lifecycle but
never constructs the media peer.

Current App Server documentation exposes realtime start/stop with WebRTC for
v1 and v3; v2 WebRTC is unsupported. The direct device path remains on tagged
Frameless v3. A live subscription-backed v1 canary did not complete startup and
is not treated as a fallback.

The pinned ThirdReality v1.1.7 overlay targets Python 3.11 on aarch64 Buildroot
Linux, not Android. Its standard-library controller remains in the existing
root voice process. Exactly two reusable prewarmed children run `aiortc` from a
complete hash-locked runtime under `/usr/bin/python3 -I -S`, with root-owned
immutable source/runtime paths and one bounded Unix sequenced-packet descriptor
per child. The launcher gives each child UID/GID 65534, no supplementary groups, and a minimal fixed
environment. Mode-0755 directories and mode-0644 source/runtime files remain
readable but immutable to it; the root-owned mode-0600 device configuration
and staging archive do not. It is not a separately supervised device daemon or
a general syscall/network sandbox. Exact vendor bytecode guards are evaluated
before any patch is installed; missing, disabled, insecure, invalid, or unknown
inputs leave direct mode inactive or fail the selected wake closed.

```text
vendor microphone callback (16 kHz PCM16)
  -> statically qualified PulseAudio AEC source
  -> direct-only idle pre-roll (up to 6 × 64 ms / 12 KiB in RAM)
  -> bounded device input queue (64 KiB / 2.048 s; up to 2x catch-up)
  -> bounded timestamped sidecar IPC
  -> device aiortc RTP track ==============================> provider

provider oai-events <=====================================> device data channel
provider audio RTP =======================================> device aiortc peer
  -> continuous decoded lane; first audio / ~120 ms quiet media boundaries
  -> bounded 24 kHz mono PCM16 IPC; fixed-argv non-blocking paplay
  -> exact raw sink value; paplay raw relative volume 65536
  -> AEC sink at configured 1–60% ceiling (25% default)

device v3 WebSocket -> SDP offer/answer + ready/ping/stop -> bridge/App Server
                      (no PCM or raw provider data)
```

The recorder callback runs before local wake-model activation. The overlay
retains the newest six idle frames and transfers them only to Okay Computer.
Okay Nabu discards that history before official Assist starts. Stop, mute,
disconnect, teardown, and every v3 failure clear it without forwarding,
persisting, or logging it. Pre-roll remains inside configured queue bounds and
is trimmed or omitted to reserve at least 32 KiB (1.024 s) of live post-wake
capacity. V3 never replays captured direct audio into Home Assistant; the user
must invoke Okay Nabu separately.

The v3 start requires `conversation_mode: "native"`, a device SDP offer with
audio and application media lines, and no device tools or PCM fields. App
Server returns the answer through the bridge. The device applies it and waits
for answer-applied, connected peer, and open `oai-events` before sending
`transport_ready`; only then does the bridge send a content-free `started` with
`audio_over_bridge: false` and `sideband_control: true`. The bridge ignores any
Home Assistant broker snapshot, so there is no transcript boundary, executor,
or `appendSpeech` render handoff.

The initial peer is implicit epoch 1 and its exact v3
`start`/`answer`/`transport_ready`/`started` shapes do not change. Trusted AEC
barge-in extends the existing authenticated WebSocket with consecutive
`rollover`, `rollover_answer`, `rollover_transport_ready`, and
`rollover_started` controls. Because the strict initial acknowledgement cannot
advertise the extension, deployment is ordered bridge first, then device. A
new bridge still accepts an old device; an old bridge rejects rollover.

`device_webrtc` requires `full_duplex: true` plus a statically loaded
PulseAudio `module-echo-cancel` with the exact configured allowlisted method,
reviewed hardware masters, default AEC source/sink, already-open capture stream
routed through that source, and configured sink ceiling. Allowed methods are
`webrtc`, `speex`, and `adrian`; omission means WebRTC and never causes an
automatic fallback. Stock v1.1.7 rejects WebRTC and Speex but loads Adrian, so
its active configuration explicitly selects Adrian. Startup verifies the
topology and ceiling before constructing the peer. Once per direct session,
before the SDP offer or bridge connection, a fixed-argv `pactl` controller sets
and verifies the dedicated sink to the exact raw playback setting. The signaling
handshake deadline starts only after that local AEC and player preparation
finishes; the maximum-session deadline still spans the complete startup. Direct
`paplay` uses that sink with raw stream volume 65536 (100% relative),
non-blocking 20 ms writes, and 60 ms/20 ms latency/process arguments; it never
enumerates or mutates a sink-input. No blocking volume subprocess runs from
`response.created`, playback begin/resume, or the interruption path. The
guarded installer and later vendor media-player preference must agree on the
sink volume, and nothing may mutate it while a direct session is live.

Provider response/output lifecycle observes control state but never labels,
gates, splits, or retires the normal RTP lane. First decoded audio emits
internal `media.started`; every frame resets a receiver timer, and only an
actual roughly 120 ms gap emits `media.quiet`. This continuous lane keeps
RTP-before-start prefixes and stopped-before-tail audio. That normal-generation
boundary is not an interruption acknowledgement and does not authorize reuse
of the peer.

During v3 playback, two consecutive qualifying AEC-filtered capture frames
immediately clear pending playback and SIGKILL `paplay` in the vendor-process
parent. It retires the old PeerConnection epoch and sends no later capture to that peer. The
outer vendor owner, session/player objects, bridge WebSocket, and ready latch
remain attached. The two reusable sidecar processes alternate active/standby
roles, creating a fresh PeerConnection each epoch and recycling the retired
epoch's process. Exactly those two prewarmed slots are used; an absent or
invalid standby ends the outer session. Exactly 4 KiB (two 64 ms frames,
128 ms) of recent AEC pre-roll through the trigger plus queued/live speech is
sent once and in order to the replacement.
The network thread arms a rearm gate only when that current-epoch interruption
commits. Continuous speech cannot retire the replacement epoch; eight
consecutive detector-quiet 64 ms capture callbacks (512 ms) establish a new
speech edge. Qualifying signal before the eighth resets the quiet count, and
rejected stale-epoch requests do not arm the gate.

Capture freshness is rechecked at actual RTP consumption; packets older than
2.25 seconds are terminal. The standby is re-polled immediately before use, and
an absent or invalid slot terminates the outer session without a cold launch.
Replacement
lifecycle plus PCM is ordered within configured `output_queue_bytes` and stays
inaudible until the exact epoch-matching `rollover_started`, then enters the
normal handlers in order. Exact integer fields reject floats/bools. `stop` is
normal in every rollover phase, and a killed child whose close budget expires
transfers final `waitpid` ownership to a daemon reaper. That device-process
budget is independent of the bridge App Server barrier below.

The bridge stops the old App Server realtime session and gives it 100 ms to
produce the matching `thread/realtime/closed` notification. The stop RPC itself
only enqueues close; the notification follows awaited input/fanout shutdown and
is the same-thread reuse barrier. Confirmed closure starts the replacement with
`includeStartupContext: true` and reports `context_retained: true`. Timeout,
error, or an absent close transfers the old epoch to tracked isolated-thread
cleanup, starts the replacement on a new thread, and reports false, preventing a
delayed old close from killing the replacement. Context retention
does not prove audible-history correctness: interrupted unheard assistant
output can remain in provider context, and recent pre-roll can overlap samples
seen by the old peer.

Frameless v3 sends no public `response.cancel`,
`output_audio_buffer.clear`, truncate substitute, or provider interruption
acknowledgement. It also rejects public Realtime `session.update` VAD
configuration. A synthetic same-peer canary was rejected after old RTP
continued beyond the five-second media-fence deadline; the former
`response.interrupt`/`interrupt.fenced` experiment is not the production path.
Public Realtime v2 WebRTC is unsupported on this subscription-backed route;
the historical wire-v2 `bridge_pcm` rollback is separate.

Fresh-peer rollover is a safe subscription-backed approximation, not exact
ChatGPT same-session interruption. Queue/age/timeout, sidecar, or epoch failure
ends the outer session closed. Manual stop, mute, disconnect, and normal-wake
preemption still end it. No failure forwards direct audio to Home Assistant or
logs it. At that installation's qualified 60% setting, a reference-device
physical double-interruption canary passed twice with the exact artifact. Four
cuts were 208–211 ms and four rollovers were 1.29–1.57 s; each run recycled its
same two worker PIDs without a cold replacement and retained context twice.
This passes that reference rollover canary, not the full per-installation
acceptance matrix. The normative
contract is [wire v3](../protocol/realtime-wire-v3.md#barge-in-and-interruption).

The reference device's earlier 25% echo-residual/double-talk results exercised
the v2 bridge-PCM route. The public example also remains at 25%; the separate
v3 reference qualification at 60% does not qualify another speaker or room.
The v3 protocol, sidecar, runtime installer, queue, cancellation, and cleanup
paths have automated local coverage plus the hardware canary above, but the
full physical v3 acceptance matrix remains installation-specific.

The explicit `media_transport: "bridge_pcm"` rollback retains v2 binary input,
bridge-owned WebRTC media, bridge-side speaking epochs, a 2,250 ms live input
cap, and bounded pre-ready Assist replay. Older v2 clients may also omit
`conversation_mode` to enter the legacy auto/managed two-thread compatibility
policy. V2 interruption acknowledgements, native correlated cancellation, and
managed `continuation_safe` generation invalidation remain unchanged and never
apply to a v3 socket.

The device stores a distinct route-scoped bearer in a root-owned mode-0600
file. The bridge accepts it only on `/v1/realtime` after v2 or v3 negotiation;
the primary bridge token, ChatGPT credential, and Home Assistant token do not
go to the speaker. The bearer cannot open the tool-authority route. See the
[v3 wire protocol](../protocol/realtime-wire-v3.md), [v2 rollback
protocol](../protocol/realtime-wire-v2.md), [deterministic runtime
guide](../device/thirdreality/webrtc-runtime.md), and [device deployment
contract](../device/thirdreality/README.md).

ThirdReality v1.2 is a native C++ rewrite with a changed audio, AEC, playback,
and continued-conversation path; it is not merely a drop-in performance flag.
The target is single-slot, so upgrade testing needs a separate canary device,
complete partition/data/environment read-backs, authenticated image provenance,
and a physically rehearsed full-image downgrade and restoration; flashing the
only production speaker is not an A/B test. Its v1.2.1 image also exposes
unauthenticated root ADB on TCP port 5555 and password-authenticated root SSH
with a documented default; both services must be isolated, hardened, and
re-verified after reboot and updates. The safe settings, canary matrix,
recovery checklist, and access-service requirements are documented in
[performance and ThirdReality
tuning](performance.md#official-v12-c-firmware-canary-evaluation).

The v1.1.7 in-process overlay does not require this firmware canary. It neither
starts nor stops `adbd`; deployment, restart, rollback, and reboot verification
must preserve the approved TCP ADB port 5555 recovery path.
