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
"Okay Computer" -> in-process stdlib client -> realtime wire v2 -> bridge
                 -> Codex App Server WebRTC -> local device playback

one explicitly opted-in Conversation subentry
  -> Home Assistant LLM API snapshot
  -> primary-token /v1/home-assistant/tools broker -> bridge
  -> provider dynamic tools for the captured direct session
```

The direct device remains an untrusted audio/control endpoint. It never
receives a Home Assistant credential, advertises tools, sends tool results, or
receives provider tool calls or transcript content. Realtime home control is
disabled by default. If an operator explicitly designates exactly one
Conversation subentry as authority, the Home Assistant integration opens the
separate broker with the primary bridge token, registers only that subentry's
selected and bounded LLM API view, executes correlated calls locally, and
returns results to the bridge. The route-scoped device token cannot open the
broker. A normal Okay Nabu wake can preempt direct mode and regain the
microphone on the same vendor capture thread.

The authority is selected from Conversation config subentries, not from a
device message. Zero or multiple opted-in subentries disable registration; the
configuration flow also rejects a second authority. A successful registration
captures one immutable generation containing the rendered Home Assistant
instructions, an `es-MX`-by-default locale, and no more than 128 selected tools.
Each v2 provider session snapshots that generation. Schema, argument, result,
message, pending-call, and per-session-call limits are enforced on both sides;
an authority replacement, disconnect, timeout, correlation mismatch, or
undeclared provider tool fails closed without exposing the broker exchange to
the device.

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
and realtime threads as soon as their session ends. The bundled Home Assistant
component does not retain STT sessions for TTS, and the released bridge never
issues a handoff ticket. Dormant validation and ownership machinery remains for
future protocol work. Cached Conversation threads are deleted when retired,
evicted, or the bridge closes. Deletion unloads the thread immediately instead
of retaining it for App Server's idle-unload period.

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
device mode below begins only after its explicit wake; it is not a speculative
prewarm. See
[performance and ThirdReality tuning](performance.md) for the live measurements
and acceptance criteria.

## Realtime client mode

The bridge's `/v1/realtime` WebSocket is a project-owned LAN protocol, not the
App Server transport. Legacy v1 keeps its JSON/base64 compatibility behavior;
strict v2 accepts binary 16 kHz mono PCM16 input and returns binary 24 kHz mono
PCM16 only between explicit speaking-epoch controls. The bridge owns the
version-coupled App Server WebRTC peer and filters v2 events at that trust
boundary.

The pinned ThirdReality v1.1.7 overlay imports a standard-library-only client
inside the existing root voice process. It does not replace vendor modules or
add a second daemon. Its exact vendor bytecode guards are evaluated before any
patch is installed. A missing, disabled, insecure, or invalid root-only config
leaves direct mode inactive; unrecognized vendor code leaves all target methods
untouched.

```text
vendor microphone callback (16 kHz PCM16)
  -> static PulseAudio WebRTC-AEC source when full duplex is enabled
  -> direct-only idle pre-roll (up to 6 × 64 ms / 12 KiB in RAM)
  -> bounded device input/fallback queues (64 KiB / 2.048 s)
  -> paced v2 binary WebSocket (up to 2x while catching up)
  -> bridge v2-only WebRTC input cap (2,250 ms)
  -> Codex App Server realtime v3

provider audio (48 kHz WebRTC)
  -> bridge downmix/resample and content-free epoch gate (24 kHz PCM16)
  -> bounded device playback queue (48 KiB / about 1.024 s)
  -> fixed-argument paplay child pinned to the AEC sink and <=25% stream volume
```

The recorder callback runs before local wake-model activation. The overlay
therefore retains the newest six idle frames and atomically transfers them only
when Okay Computer selects the direct route. Okay Nabu discards that history
before official Assist starts. Stop, mute, disconnect, and teardown clear it,
and no copy is written to disk or logs. Pre-roll remains inside the configured
queue bounds and is trimmed or omitted to reserve at least 32 KiB (1.024 s) of
live post-wake capacity in both the direct input and pre-ready fallback queues.

The 2× transfer is bounded catch-up, not burst replay: it runs only while more
than one captured frame is queued and returns to capture cadence at the live
edge. It preserves accepted startup audio and prevents a permanent
handshake-sized delay, but cannot remove cold thread/WebRTC setup or provider
latency. The v2 2,250 ms input limit is applied per session before start;
finite STT retains its existing whole-utterance track capacity.

The client remains turn-taking by default: a speaking epoch gates microphone
submission until both its queued PCM and playback child have drained. Opt-in
full duplex requires a statically loaded PulseAudio `module-echo-cancel` with
`aec_method=webrtc`, the reviewed raw hardware masters, exact default AEC
source/sink names, and the already-open vendor capture stream routed through
that source. A startup preflight verifies the topology and configured 1–25%
sink ceiling before any microphone audio leaves the device. The ceiling is
rechecked at every `speaking.started`, and every `paplay` child is pinned to
the AEC sink with a fixed stream volume at or below that ceiling.

In verified full duplex, provider VAD continues receiving capture during
playback. `input_audio_buffer.speech_started` immediately flushes local output
and quarantines late PCM, but does not itself prove provider cancellation. The
bridge truthfully keeps `remote_cancel: false` and separately advertises
`same_session_interrupt_ack: true`. Same-socket continuation occurs only when
the bridge's explicit cancel request is followed by a provider
`response.cancelled` event whose response identifier matches the active
response. Timeout, mismatch, or ambiguity returns
`fresh_session_required: true` / `remote_cancelled: false`, closes the socket,
and disposes the remote thread/session.

The device stores a distinct route-scoped bearer in a root-owned mode-0600
file. The bridge accepts it only on `/v1/realtime`; the primary Home Assistant
bridge token, ChatGPT credential, and Home Assistant token do not go to the
speaker. The device bearer cannot open the tool-authority route; Home Assistant
uses the primary bridge token for that outbound connection. See the [wire
protocol](../protocol/realtime-wire-v2.md) and [device deployment
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
