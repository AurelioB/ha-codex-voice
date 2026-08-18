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

## Active server-offloaded realtime route

The current controlled deployment binds the installed `Okay Nabu` detector to
native realtime and sets `realtime_only: true`:

```text
"Okay Nabu" -> speaker wake/LED/cue/native AEC3/capture/playback
             -> strict wire v2 binary PCM over the trusted LAN
             -> Docker/host bridge + App Server/OAuth
             -> one active bridge-owned WebRTC provider generation

Home Assistant's exposed-entity tool snapshot is captured before provider
startup. Its Assist conversation/STT/TTS flow, transcript executor, and
appendSpeech render handoff do not participate in the direct session. An
external memory/deep-task agent is optional.
```

The device is the aarch64 Buildroot Linux speaker, not Android, and remains an
untrusted audio endpoint. It never receives a Home Assistant credential or
Codex OAuth state, imports `aiortc`, creates SDP, advertises tools, or receives
transcript content. Native AEC3 stays local because the microphone and physical
render reference are sample-aligned there. A 10 dB baseline, noise-limited
adaptive digital gain, a limiter, and moderate noise suppression run inside
APM, so wake detection and realtime capture share conditioned samples;
outgoing bridge PCM uses 0 dB transport gain.

The always-running server owns the authenticated v2 socket, the managed Codex
login, the App Server realtime provider generation, its paced WebRTC peer,
response lifecycle, interruption replacement, and cleanup. The active provider
generation exposes Home Assistant's captured entity tools, bridge-owned
`end_conversation`, and optional agent tools. A narrow normalized
Spanish/English terminal-phrase fallback handles explicit end requests that are
acknowledged without a tool call.

The direct wake boundary is deterministic. An accepted Okay Nabu detection
immediately claims the vendor owner and queues the non-blocking
thinking/pulsing LED. Initial v2 startup discards the trigger/wake history and
every recorder callback produced before local confirmation completes; it does
not seed or wait on a microphone backlog. At most three fresh session attempts
share one absolute 12-second owner deadline, while each attempt keeps its own
10-second signaling-handshake bound inside the remaining budget. Deadline,
attempt exhaustion, terminal state, or setup failure releases the owner and
returns the LED to idle without entering Home Assistant.

`RealtimeSession.ready` is narrower than socket connection. It means the
server has applied the SDP answer, connected the provider peer/data channel,
and returned exact strict-v2 `started`. Only then does the device play once the
root-owned pinned PCM16 mono 22,050 Hz cue
`/usr/lib/python3.11/site-packages/sounds/wake_word_triggered_old.wav` (SHA-256
`6b25dd2abaf7537865222ca9fd6e14fbf723458526fb79bbe29d8261d1320724`, about
0.400 seconds). The vendor stop-word detector remains suspended throughout the
direct ownership window, so the wake tail cannot cancel signaling and playback
echo cannot terminate the live session. Capture remains closed until cue EOF;
EOF opens live capture and switches the LED to listening. The cue must finish
within two seconds. Cue failure/timeout and any terminal race fail closed. The
sole spoken terminal control is `end_conversation`; its terminal result tears
down the socket and normal cleanup restores the detector and idle LED. No Home
Assistant tool is present.

During provider playback the microphone stays open. Qualified AEC-filtered
near-end speech immediately kills and flushes local playback and sends one
exact `provider_barge` boundary on the same device socket. The bridge sends the
desktop-compatible `response.cancel` and `output_audio_buffer.clear` pair on
the current provider WebRTC data channel, fences queued local output, and keeps
the thread, peer, capture, and input pacing live. Correctness does not depend on
provider `speech_started`; `barge_in_mode: "rollover"` retains strict provider
replacement as a conservative fallback.

The dedicated sink and `paplay` stream remain at the fixed 100% physical
anchor. One non-amplifying software attenuator implements dynamic user volume:
0 is mute and 1–100% is audible. A saved initial level such as 80% is below the
anchor and can still be raised to the full hardware range with the physical
buttons, avoiding the earlier v2 double attenuation.

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

The current ThirdReality native-v2 client does not enter this route. It sends
`conversation_mode: "native"`, requires the bridge to echo that selection, and
therefore gets exactly one server-owned App Server/WebRTC voice thread. A native
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

The active ThirdReality strict-v2 route keeps at most one audio-empty provider
session warm. Voice-process startup and completed conversations arm a
five-minute slot; a 0.50 probable wake score creates or refreshes a ten-second
slot before final detector acceptance. The real wake claims that exact device
WebSocket, thread, and server-owned WebRTC peer. Expiry or failure stops it and
falls back to bounded cold startup. This deliberately occupies the single
subscription speech lane, but never admits microphone PCM before assignment.
The recommended Wyoming providers do not use this policy. The dormant direct
device route prewarms exactly one reusable local isolated `aiortc` worker. Idle
process prewarm means only that its `Popen` is alive; it does not request,
drain, or validate an SDP offer. The worker creates its first peer offer only
inside an accepted wake attempt. After that peer is ready and the confirmation
cue has completed and opened capture, the same process may prepare one fresh,
offer-warm logical standby. Rollover promotes it in place, then the worker
prepares the following standby. An absent or invalid required standby ends the
outer session without launching another process. This local worker does not
create a Codex thread, open a bridge socket,
negotiate remote WebRTC, or consume the speech lane before the explicit wake.
See [performance and ThirdReality tuning](performance.md) for the live
measurements and acceptance criteria.

## Realtime client mode

The bridge's `/v1/realtime` WebSocket is a project-owned LAN protocol, not the
App Server transport. Legacy v1 keeps JSON/base64 compatibility; active strict
v2 carries binary bridge PCM; dormant v3 carries SDP and JSON sideband only.

```text
accepted Okay Nabu wake
  -> thinking/pulsing LED; capture closed; wake tail discarded
  -> <=3 strict-v2 attempts inside one absolute 12 s owner deadline
  -> server App Server thread + aiortc peer ready -> exact started
  -> pinned ~0.400 s cue (2 s EOF timeout)
  -> cue EOF -> listening LED + live native-AEC3 capture

16 kHz mono PCM16 -> authenticated v2 socket -> server resampler/RTP -> provider
provider audio -> server decode -> v2 speaking epoch + 24 kHz PCM16 -> paplay
```

The active device is full duplex. Its fixed 100% sink anchor and
100%-relative playback stream are separated from one non-amplifying
software-volume stage that implements the physical 0/1–100% range.
Local AEC-qualified speech cuts output immediately while its samples continue
on the same device socket. The bridge replaces the non-interruptible provider
generation and replays the retained utterance once at normal media pace. The
server exposes the captured Home Assistant tools plus `end_conversation`, and
realtime-only failure returns idle without an Assist/Hermes fallback.

### Dormant device-owned v3 experiment (historical)

The following design and physical measurements are retained for rollback and
research. They do not describe the active Okay Nabu media path.

Current App Server documentation exposes realtime start/stop with WebRTC for
v1 and v3; v2 WebRTC is unsupported. The direct device path remains on tagged
Frameless v3. A live subscription-backed v1 canary did not complete startup and
is not treated as a fallback.

The pinned ThirdReality v1.1.7 overlay targets Python 3.11 on aarch64 Buildroot
Linux, not Android. Its standard-library controller remains in the existing
root voice process. Exactly one reusable prewarmed child runs `aiortc` from a
complete hash-locked runtime under `/usr/bin/python3 -I -S`, with root-owned
immutable source/runtime paths and one bounded Unix sequenced-packet descriptor.
The launcher gives the child UID/GID 65534, no supplementary groups, and a minimal fixed
environment. Mode-0755 directories and mode-0644 source/runtime files remain
readable but immutable to it; the root-owned mode-0600 device configuration
and staging archive do not. It is not a separately supervised device daemon or
a general syscall/network sandbox. Exact vendor bytecode guards are evaluated
before any patch is installed; missing, disabled, insecure, invalid, or unknown
inputs leave direct mode inactive or fail the selected wake closed.

```text
accepted Okay Nabu wake
  -> queue thinking/pulsing LED
  -> discard wake and all pre-ready PCM
  -> <=3 attempts within one absolute 12 s owner deadline
  -> exact v3 transport readiness
  -> one pinned ~0.400 s cue (2 s EOF timeout; capture still closed)
  -> cue EOF -> listening LED + live capture

live vendor microphone callback (16 kHz PCM16)
  -> statically qualified PulseAudio AEC source
  -> bounded live device input queue (64 KiB / 2.048 s)
  -> bounded timestamped sidecar IPC
  -> device aiortc RTP track ==============================> provider

provider oai-events <=====================================> device data channel
provider audio RTP =======================================> device aiortc peer
  -> continuous decoded lane; first audio / ~120 ms quiet media boundaries
  -> bounded 24 kHz mono PCM16 IPC; fixed-argv non-blocking paplay
  -> exact raw sink value; paplay raw relative volume 65536
  -> AEC sink at configured 1–100% ceiling (25% default)

device v3 WebSocket -> SDP offer/answer + transport_ready/started/ping/stop
                      -> bridge/App Server (no PCM or raw provider data)
```

The recorder callback runs before local wake-model activation. The overlay
may retain the newest six idle frames in the compatibility ring, but the
historical Okay Nabu v3 wake discarded all 384 ms / 12 KiB instead of transferring it. It
also drops every connecting and cue-time callback. The 64 KiB input queue
therefore starts empty when cue EOF opens capture; it bounds accepted live and
rollover pressure, not cold negotiation. Initial 32 KiB headroom and wake
pre-roll transfer remain v2-only compatibility behavior. Stop, mute,
disconnect, teardown, and every v3 failure clear capture without forwarding,
persisting, logging, or replaying it into Home Assistant.

Initial startup must not be confused with live rollover. After capture has
opened, a committed trusted-AEC interruption deliberately retains 4 KiB /
128 ms of recent live capture and merges it with the rollover queue for the
replacement peer. That bounded live pre-roll never seeds epoch 1.

The v3 start requires `conversation_mode: "native"`, a device SDP offer with
audio and application media lines, and no device tools or PCM fields. App
Server returns the answer through the bridge. The device applies it and waits
for answer-applied, connected peer, and open `oai-events` before sending
`transport_ready`; only then does the bridge send a content-free `started` with
`audio_over_bridge: false` and `sideband_control: true`. The bridge ignores any
Home Assistant broker snapshot, so there is no transcript boundary, executor,
or `appendSpeech` render handoff. The native thread declares only
`end_conversation` with an empty input schema. The bridge returns a successful
terminal result for that exact call and a `do_not_retry` rejection for any
other tool request, then ends the epoch so device cleanup cannot remain live.

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
and verifies the dedicated sink to the exact raw playback setting. The
per-attempt ten-second signaling deadline starts only after that local AEC and
player preparation finishes, while the absolute 12-second wake-owner deadline
also bounds preparation and all retries. The maximum-session deadline remains a
separate hard lifetime cap. Direct
`paplay` uses that sink with raw stream volume 65536 (100% relative),
non-blocking 20 ms writes, and 60 ms/20 ms latency/process arguments; it never
enumerates or mutates a sink-input. Each response rechecks the exact anchor and
repairs a mismatch before admitting audio or fails output closed. Matching Home
Assistant volume requests use bounded software attenuation instead of moving
the sink, and the guarded 50 ms physical-button loop restores a displaced
anchor. Ordinary interruption runs no volume subprocess. Uncoordinated live
sink mutation remains unsupported and is repaired or fences output.

The native hardware-loopback AEC3 capture slice is disabled by default. Its
normal selector is `capture_backend: "native_aec3"` in the enabled root-owned
mode-0600 `/data/conf/codex-realtime.json` for `device_webrtc`. The early
overlay hook reads that secure configuration before vendor microphone import;
`CODEX_AEC3_CAPTURE=1` is only an explicit environment override. After a
successful native patch the overlay publishes `CODEX_AEC3_ACTIVE=1` as internal
proof consumed by session preflight, not as an operator selector. Library, ABI,
device, or capture errors fail startup closed, and merely installing the files
does not select them. Adrian remains available as the playback-DMA keepalive
during the initial physical acceptance program; native AEC3 remains canary-first
and unqualified until the physical gates pass.

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
remain attached. The one reusable sidecar process holds the active peer and at
most one fresh, offer-warm standby. Its ordered promotion fence stops the old
peer before later capture reaches the promoted standby; the worker then prepares
the following standby. The hard process cap is one, and an absent or invalid
standby ends the outer session. Exactly 4 KiB (two 64 ms frames,
128 ms) of recent AEC pre-roll through the trigger plus queued/live speech is
sent once and in order to the replacement.
The network thread arms a rearm gate only when that current-epoch interruption
commits. Continuous speech cannot retire the replacement epoch; eight
consecutive detector-quiet 64 ms capture callbacks (512 ms) establish a new
speech edge. Qualifying signal before the eighth resets the quiet count, and
rejected stale-epoch requests do not arm the gate.

Capture freshness is rechecked at actual RTP consumption; packets older than
2.25 seconds are terminal. The logical standby is validated immediately before
use, and an absent or invalid peer terminates the outer session without another
worker launch.
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
Public Realtime v2 WebRTC was unsupported on that subscription-backed route;
the project's active wire-v2 `bridge_pcm` LAN transport is separate.

Fresh-peer rollover is a safe subscription-backed approximation, not exact
ChatGPT same-session interruption. Queue/age/timeout, sidecar, or epoch failure
ends the outer session closed. Manual stop, mute, and disconnect still end it;
later detector hits remain ignored while the owner is live. No failure forwards
direct audio to Home Assistant or logs it. A historical two-worker build passed
a reference-device physical double-interruption canary twice at that
installation's qualified 60% setting with the exact artifact. Four cuts were
208–211 ms and four rollovers were 1.29–1.57 s; each run recycled its same two
worker PIDs without a cold replacement and retained context twice. Those
measurements do not physically validate the current single-worker build and do
not replace the full per-installation acceptance matrix. The normative
contract is [wire v3](../protocol/realtime-wire-v3.md#barge-in-and-interruption).

The reference device's earlier 25% echo-residual/double-talk results exercised
an older v2 bridge-PCM configuration, and the historical v3 qualification was
at 60%. The active reference configuration instead fixes the physical anchor
at 100% and requires its own full-output qualification; none of those results
transfers to another speaker or room.
The v3 protocol, sidecar, runtime installer, queue, cancellation, and cleanup
paths have automated local coverage plus the hardware canary above, but the
full physical v3 acceptance matrix remains installation-specific.

### Active strict-v2 contract summary

The active `media_transport: "bridge_pcm"` route uses v2 binary input,
bridge-owned WebRTC media, bridge-side speaking epochs, and a 2,250 ms live
input cap. Native/realtime-only startup discards pre-ready PCM and exposes Home
Assistant tools plus `end_conversation`; it does not enter Assist or the managed compatibility
route. Older v2 clients may omit `conversation_mode` and retain the legacy
two-thread policy. Those compatibility semantics never apply to explicit
native mode or a v3 socket.

The device stores a distinct route-scoped bearer in a root-owned mode-0600
file. The bridge accepts it only on `/v1/realtime` after v2 or v3 negotiation;
the primary bridge token, ChatGPT credential, and Home Assistant token do not
go to the speaker. The bearer cannot open the tool-authority route. See the
[dormant v3 wire protocol](../protocol/realtime-wire-v3.md), [active v2
protocol](../protocol/realtime-wire-v2.md), [historical sidecar runtime
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
