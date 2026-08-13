# Codex Voice for Home Assistant

Codex Voice is an unofficial Home Assistant integration that exposes a
Conversation agent, an experimental text-to-speech adapter, an experimental
speech-to-text adapter, and an experimental direct realtime transport
backed by a user's existing ChatGPT/Codex login. The recommended Assist
configuration uses Home Assistant's native Wyoming integration for local
faster-whisper STT and local Piper TTS, with Codex Voice providing the
Conversation stage between them.

> [!WARNING]
> This project relies on experimental Codex App Server interfaces. It can
> break when Codex changes, is not an official OpenAI or Home Assistant
> integration, and is not a substitute for the stable, separately billed
> OpenAI API. ChatGPT plan availability, quotas, and usage policies still
> apply. The project never converts, exports, or presents OAuth credentials as
> an API key.

## Architecture

The current controlled voice route deliberately leaves Home Assistant and
Hermes out of the media path:

```text
ThirdReality: "Okay Nabu"
  -> deterministic device controller
     -> wake ownership, LED, ready cue, native AEC3, microphone and playback
  -> strict realtime wire v2 (binary PCM16) over the trusted LAN
  -> local Docker/host bridge
     -> Codex App Server + existing ChatGPT OAuth
     -> one active server-owned aiortc/WebRTC provider generation
  -> streamed speech back to the speaker
```

The aarch64 Buildroot Linux speaker—not Android—is intentionally a small audio
appliance. It does not import `aiortc`, create SDP, hold OAuth state, interpret
general tools, or call Home Assistant. Native AEC3 remains on the speaker so
capture and the physical render reference stay sample-aligned. The bridge owns
Codex App Server, OAuth, WebRTC, provider lifecycle, bridge-owned
`end_conversation`, the default Home Assistant exposed-entity tool snapshot,
default public-web search, and optional external-agent tools.

The reference deployment also supplies trusted Mexico City location/timezone
context and an exact local `get_current_time` tool.

An accepted wake pulses the LED and claims the microphone, but does not admit
speech. The bridge completes the initial provider session, returns strict-v2
`started`, and only then does the speaker play the acknowledgement cue. Cue EOF
changes the LED to listening and opens capture. During playback the microphone remains
open; qualified AEC-filtered near-end speech cuts local playback immediately
and sends exact `{"type":"provider_barge"}` while the device socket and
capture stay open. The bridge cancels the response and clears queued output on
that same provider peer while the utterance continues upstream. Startup failure returns to idle and
never falls through to a single-turn Assist request.

The standard Home Assistant Conversation/STT/TTS integration is retained in
the repository, but Home Assistant Assist and Hermes remain outside this media
path. Home Assistant is nevertheless the default smart-home tool authority:
its selected Conversation subentry exposes only Home Assistant-approved tools
to the native realtime model. The former wire-v3
device-owned WebRTC/sidecar implementation is also retained, disabled, as a
rollback and research artifact; its measurements below are labeled
historical.

## Status

- Milestone 1: a standard Home Assistant pipeline using native Wyoming local
  STT and TTS around the Codex Voice Conversation entity. The recommended
  Mexican Spanish voice is Piper `es_MX-ald-medium`.
- New installations do not create the experimental Codex STT subentry by
  default. Existing subentries remain available for explicit diagnostics.
- The retained experimental Codex TTS entity progressively delivers realtime
  speech frames instead of waiting for the entire rendered response and remote
  cleanup, but it is not the recommended production TTS stage.
- Newly created Conversation profiles default to low reasoning effort and App
  Server's configurable `priority` tier; upgraded profiles preserve standard
  usage until reconfigured, and `standard` remains available when lower
  subscription usage matters more than latency.
- Automatic experimental STT-to-TTS session reuse is disabled: historical
  realtime validation showed assistant output can begin before finite
  transcription completes.
- Milestone 2 is now the active server-offloaded realtime route. “Okay Nabu”
  starts strict v2 `bridge_pcm`, `conversation_mode: "native"`, full duplex,
  and native AEC3. The device owns only the physical audio boundary; the
  always-running local bridge owns the App Server and one active WebRTC provider
  generation behind the stable device WebSocket. No firmware flash or
  separately supervised device daemon is required.
- The route-scoped device token cannot open the Home Assistant broker. Active
  native v2 captures the broker's immutable exposed-entity snapshot before
  startup, rejects device-declared tools, and adds bridge-owned
  `end_conversation`, configured `get_current_time`, plus default `search_web`.
  Optional agent tools are advertised only when configured.
- The active reference configuration uses `native_aec3` with a 10 dB native
  baseline, noise-limited adaptive digital gain, a limiter, and moderate noise
  suppression, plus 0 dB transport gain, a fixed 100% AEC/playback anchor, a 100%-relative
  playback stream, and one non-amplifying software-volume stage. The physical
  controls retain their full range: 0 is mute and 1–100% is audible. A saved
  initial level such as 80% is attenuation below the anchor, not PCM
  amplification. Physical AEC, echo-rejection, normal-distance speech, and
  interruption tests remain mandatory for each device and room.
- The dormant v3 implementation and pinned runtime retain automated
  protocol/static coverage. A historical two-worker v3 build passed a
  reference-device
  physical double-interruption canary twice at that installation's qualified
  60% setting:
  four local cuts were 208–211 ms and four fresh-peer rollovers were
  1.29–1.57 s. Each run recycled its same two worker PIDs without a cold
  replacement, and every rollover retained context. Those results predate and
  do not physically validate the current single-worker build; they also do not
  replace the per-installation acceptance matrix.
  These figures are historical evidence only and do not describe the active
  server-owned v2 rollover path.
- Target Home Assistant version: 2026.8.0 or newer.
- Target Codex CLI version: 0.147.0 or newer.

The retained Assist pipeline remains turn-based. The active Okay Nabu route is
a separate full-duplex server-owned WebRTC conversation, not an STT/text/TTS
simulation. Home control uses the default Home Assistant exposed-entity tool
authority, while external memory/deep-task agent access is optional.

## Quick start

### 1. Authenticate Codex on the bridge host

Install the official Codex CLI, then complete its normal login:

```bash
codex login
codex --version
```

The bridge asks Codex App Server to use that managed session. Do not copy
`auth.json` into Home Assistant and do not paste its contents into this
integration. It auto-detects `${CODEX_HOME}/auth.json` and then
`${HOME}/.codex/auth.json`. If the login file is elsewhere, point to the
existing file explicitly when starting the bridge:

```bash
export HA_CODEX_AUTH_FILE="/path/to/auth.json"
```

The bridge fails clearly when it cannot find a secure, file-backed login. It
does not support silently substituting an OpenAI API key or importing a
keyring-only login.

### 2. Run the bridge

Create separate long random tokens for Home Assistant and the realtime device,
and keep both private. From this repository:

```bash
export HA_CODEX_BRIDGE_TOKEN="replace-with-a-long-random-value"
export HA_CODEX_REALTIME_DEVICE_TOKEN="replace-with-another-random-value"
export HA_CODEX_BRIDGE_HOST="0.0.0.0"
uv run --extra bridge python -m bridge
```

The default port is `8787`. Binding beyond loopback is appropriate only on a
trusted LAN with firewall rules limiting access to Home Assistant and intended
realtime endpoints. Put the bridge behind TLS when the path is not fully
trusted.

For an always-warm Linux deployment, the root `compose.yaml` builds a hardened
non-root bridge image with Codex CLI 0.147.0 and mounts only the existing
owner-only OAuth file. Copy and edit the two examples in `deploy/docker`, then
start it with:

```bash
docker compose --env-file .codex-voice/compose.env up --detach --build
```

Native conversations receive a bounded `search_web` tool backed first by the
same Codex search endpoint and OAuth subscription used by the pinned App Server;
no OpenAI API key or optional external agent is required. The default Compose
stack also runs a digest-pinned SearXNG service bound only to host loopback and
uses it automatically if subscription search is unavailable. Returned titles,
URLs, and excerpts are untrusted evidence and cannot authorize Home Assistant
actions.

When the Home Assistant tool authority is connected, its configured location
name, latitude/longitude, and IANA timezone are authoritative for new voice
sessions. The reference Compose deployment keeps
`HA_CODEX_ASSISTANT_TIMEZONE=America/Mexico_City` and
`HA_CODEX_ASSISTANT_LOCATION=Mexico City, Mexico` only as the disconnected or
older-component fallback. `get_current_time` reads the bridge clock again in
the effective timezone whenever exact date or time is needed.

The Compose service uses host networking for reliable server-owned WebRTC, so
apply a host firewall rule for port 8787. See the
[server-offloaded realtime architecture](server-offloaded-realtime-architecture.md)
for setup, security, lifecycle, rollout, and physical acceptance details.

Optional local speaker identification is a separate image and never enlarges
the normal bridge container. After enrolling private profiles and downloading
the exact documented TitaNet model, enable it explicitly with:

```bash
docker compose --env-file .codex-voice/compose.env \
  -f compose.yaml -f compose.speaker-identity.yaml \
  up --detach --build
```

The worker receives one bounded post-wake PCM window over loopback, returns
`unknown` unless score and margin both pass, and can only add advisory
personalization context. It is never an authentication factor. See
[development](docs/development.md#optional-speaker-identification).

### 3. Add reliable local STT

Install the official Whisper app on Home Assistant OS, or run the official
`wyoming-faster-whisper` server on a trusted host. Add it through Home
Assistant's **Wyoming Protocol** integration. See [reliable local
speech-to-text](docs/local-stt.md) for the tested external service and systemd
configuration.

### 4. Add reliable local TTS

Install the official Piper add-on (shown as an app in current Home Assistant
UI) on Home Assistant OS, or run the pinned `wyoming-piper==2.3.1` service on a
trusted external host. Add it through Home Assistant's native **Wyoming
Protocol** integration and select
`es_MX-ald-medium` for Mexican Spanish. The external service listens on
`tcp://HOST:10200` after its bind address is configured. Its supplied installer
pins and verifies that voice model, and the hardened service exposes the model
directory read-only.

On some virtualized x86-64 Home Assistant OS installations, the current
official Piper add-on may require the guest CPU model to expose x86-64-v2
instructions. This is specific to affected x86-64 virtualization setups, not a
general Piper limitation. Expose an appropriate CPU model when practical, or
use the supported external Wyoming service path. See [reliable local
text-to-speech](docs/local-tts.md).

### 5. Install through HACS

Until the repository is accepted into HACS defaults:

1. Open HACS and choose **Custom repositories**.
2. Add `https://github.com/AurelioB/ha-codex-voice` as an **Integration**.
3. Download **Codex Voice** and restart Home Assistant.
4. Add the Codex Voice integration.
5. Enter the bridge URL, such as `http://192.0.2.10:8787`, and the separate
   bridge token.

The config flow creates Conversation and subscription-backed TTS subentries.
In the recommended Assist pipeline, select the Wyoming faster-whisper entity
for STT, Codex Voice for Conversation, and the Wyoming Piper entity for TTS.
The Codex TTS entity remains available for explicit experimental use and
comparison.

For Mexican Spanish, set the pipeline and Conversation languages to `es-MX`,
select `es` for Wyoming faster-whisper STT, set Piper TTS language to `es_MX`,
and select voice `es_MX-ald-medium`. The bridge treats the pipeline locale as
trusted response-language context. The component leaves Home Assistant's
global interface language unchanged.

Conversation profiles also expose **Provide Home Assistant tools to realtime
voice** and **Realtime voice language**. Tool authority is off by default and
may be enabled on exactly one Conversation subentry. Its realtime language
defaults to `es-MX`. Only the Home Assistant LLM APIs selected on that subentry
are registered; enabling it does not give the speaker a Home Assistant token.
This authority supports the compatibility auto/managed wire route. It does not
override `conversation_mode: "native"` from the current ThirdReality client.

For the lowest latency on ordinary device-control commands, enable **Prefer
handling commands locally** on that pipeline. Home Assistant will handle
matching built-in intents locally and retain Codex Voice as the fallback for
open-ended conversation.

### 6. Optional ThirdReality realtime mode

Release assets include `thirdreality-realtime.zip` for the pinned Python-based
ThirdReality v1.1.7 client. It contains the guarded `sitecustomize.py`, the
stdlib-only controller, the dormant `webrtc_sidecar`, deterministic
aarch64/Python 3.11 runtime lock/installer material, and a secret-free disabled
example.
Follow the [device deployment, verification, and rollback
contract](device/thirdreality/README.md). The hash-locked
[runtime guide](device/thirdreality/webrtc-runtime.md) applies only to dormant
v3; active server-offloaded v2 does not require that sidecar runtime.

The active root-owned mode-0600 configuration uses these values:

```json
{
  "wake_phrase": "okay nabu",
  "realtime_only": true,
  "media_transport": "bridge_pcm",
  "capture_backend": "native_aec3",
  "full_duplex": true,
  "pulse_aec_source": "codex_echo_cancel_source",
  "pulse_aec_sink": "codex_echo_cancel_sink",
  "pulse_aec_method": "adrian",
  "aec_sink_volume_ceiling_percent": 100,
  "playback_volume_percent": 100,
  "direct_capture_gain_db": 18
}
```

`conversation_mode: "native"` is fixed by the client rather than exposed as a
device configuration field. The full shipped example also contains the bridge
URL, route-scoped token, voice, Mexican Spanish prompt, deadlines, and queue
bounds.

Native AEC3 is selected before vendor microphone construction and uses the
physical render reference. Its 10 dB baseline, noise-limited adaptive digital
gain, limiter, and moderate noise suppression condition the samples used by
both wake detection and live capture. Transport gain remains 0 dB so there is
no second gain stage after the limiter. The playback sink and
`paplay` stream stay at their fixed 100% physical anchor. User-facing volume is
0 for mute or 1–100% audible and uses one non-amplifying software attenuator;
the reference deployment may start from a saved 80% level without preventing
the physical buttons from reaching 100%. This avoids the old v2
double-attenuation behavior. Any different device or room must pass the full
echo/noise/barge-in matrix, including the 100% worst case.

The route is `realtime_only`: setup failure clears captured audio, restores a
known idle state, and never invokes Home Assistant Assist/Hermes. The device
stores only the distinct realtime bearer, never the Home Assistant token or
Codex OAuth credential. Deployment and rollback must preserve TCP ADB port
5555. Restart only the vendor voice service for ordinary client updates; do
not restart or kill PulseAudio during those iterations.

The realtime client always requests `conversation_mode: "native"` and requires
the bridge to echo it in `started`. The bridge exposes the current Home
Assistant tool snapshot plus `end_conversation`; optional `ask_agent` and
`recall_memory` tools are absent unless configured. Optional `voice` and
`prompt` settings select `cove` and Mexican Spanish in the reference deployment.

## Home Assistant controls

When the Home Assistant Assist LLM API is enabled for the Conversation
subentry, the model receives only tools selected by Home Assistant. Tool calls
are returned to the integration, executed inside Home Assistant, and sent back
to the same Codex turn. The bridge does not need a Home Assistant long-lived
access token.

The current Okay Nabu realtime-only route captures this authority before its
native server-owned WebRTC thread starts. Home Assistant remains the sole
smart-home owner; `end_conversation` is bridge-owned and optional agent tools
handle only memory or deeper non-entity work.

Exactly one Conversation subentry owns the realtime authority; the default
entry is selected automatically and additional entries remain opt-in. Home
Assistant captures that subentry's
rendered instructions, `es-MX`-by-default realtime locale, and selected LLM API
tool schemas, then registers a bounded generation over
`/v1/home-assistant/tools` with the primary bridge token. Each legacy-auto
session captures one immutable generation. With strict wire v2 and App Server
v3, that snapshot activates a two-thread path: a tool-free realtime frontend
emits an
identified raw v3 user turn, and a separate executor thread alone owns the
selected tools. The bridge forwards only allowlisted, size-bounded executor
calls to Home Assistant and fails closed if the authority changes, disconnects,
times out, or returns an invalid result. A tool request from the frontend,
another thread, a stale turn, or a turn already tombstoned by interruption is
rejected with `do_not_retry` and never reaches Home Assistant. The device token
cannot use the broker, and the device wire remains audio/control only.

After the executor turn completes, the bridge selects its final answer and
sends a single UTF-8 prefix of at most 500 bytes to the frontend with
`thread/realtime/appendSpeech`. Frontend PCM is dropped until
`session.context.appended` acknowledges that append and a new, session-unique
assistant `turn.created` identifies and arms the render for the current bridge
generation. The first authorized non-silent PCM begins its speaking epoch, and
only the matching `turn.done` releases the one-render slot.
Unsolicited, direct, replayed, or stale frontend audio therefore cannot reach
the speaker. A new utterance invalidates the previous generation immediately;
the bridge requests provider cancellation only after an identified assistant
render has actually started, never against an idle or merely pending session.
The local generation gate, not cancellation confirmation, is authoritative.

Barge-in before an executor tool dispatch tombstones and interrupts that turn;
a late tool request is rejected. Raw user and assistant turn IDs remain claimed
for the socket lifetime, so provider replay cannot execute or render twice.
Once a Home Assistant call has been dispatched, the bridge does not cancel or
replay the ambiguous side effect. It lets the old executor turn finish,
discards its stale speech result, and starts the newest queued request
afterward.

Each executor turn is bounded by the configured bridge request timeout. A
failed terminal or missing completion produces a generic device error instead
of leaving the microphone session apparently stuck. On socket teardown, the
bridge tombstones and interrupts any active executor turn while its event
consumer is still present, then deletes both owned threads through tracked,
cancellation-shielded cleanup.

The broker uses Home Assistant's supported `conversation` assistant exposure
namespace, so its entity-dependent tools match the entities exposed to the
official Conversation flow. Tool execution and every transport layer have
nested deadlines. Unknown outcomes are explicitly marked `do_not_retry`, trip
a session circuit breaker, and are never retried by the bridge. If the provider
does not produce audible output or a correctly correlated terminal lifecycle
event within 20 seconds after accepting a tool result, the realtime session
terminates instead of remaining stuck.

Codex threads use a required named permission profile that exposes only minimal
runtime paths. The default bridge command also disables shell, web, plugins,
apps, MCP servers, hooks, command-environment inheritance, and interactive
approvals. The bridge fails closed if that profile is missing or inactive.
It also audits App Server's effective configuration layers at startup and
rejects any configured MCP server.
Each App Server also runs with private temporary `HOME` and `CODEX_HOME`
directories containing only a link to the managed login. Threads are persisted
only inside that isolated home, then deleted with `thread/delete` when their
bridge-managed lifetime ends. This keeps ordinary Codex history and locally
installed apps, including automatically discovered MCP sidecars, outside voice
sessions and prevents finished threads from lingering in App Server's idle
cache. Only an authority-enabled legacy strict-v2 managed session owns both a
speech frontend and executor thread; teardown deletes both independently. The
active native-v2 session owns one active provider generation. A confirmed stop
can reuse its thread; an ambiguous stop transfers the retired thread to bounded
cleanup and creates an isolated replacement. Every owned thread is deleted when
the conversation ends. Dormant v3 deletes every peer-epoch thread it creates.

No OAuth secret is copied into Home Assistant or this repository. The isolated
home contains a link to the source credential so refreshes remain owned by the
Codex CLI on the bridge host.

Outbound Conversation start and tool-result events use Home Assistant's
canonical JSON serializer. Nested `date`, `time`, and `datetime` values from
speech slots or tool results therefore cross the bridge in their ISO forms;
unsupported values fail before transmission with a data-safe protocol error.

Running the bridge as a dedicated OS user or container remains recommended as
defense in depth.

## Realtime protocol

`ws://BRIDGE/v1/realtime` supports legacy JSON/base64 v1, binary-PCM v2, and
direct-device WebRTC v3. The active ThirdReality route negotiates strict v2,
requires `conversation_mode: "native"`, sends binary 16 kHz mono PCM16 to the
bridge, and receives binary 24 kHz mono PCM16 inside monotonic speaking epochs.
`full_duplex: true` keeps capture open throughout provider playback.

The bridge constructs and paces the active WebRTC provider generation. On
qualified native-AEC3 near-end speech, the device immediately cuts playback,
sends exact `provider_barge`, and keeps its WebSocket and capture open. The
bridge sends `response.cancel` followed by `output_audio_buffer.clear` on the
same provider WebRTC peer, matching the public desktop-compatible hush flow.
The model and input stream stay live, and output epochs remain monotonic.

The active path does not depend on provider `speech_started`: the speaker's
qualified local AEC boundary initiates cancellation. Conservative peer
replacement remains available as `barge_in_mode: "rollover"` if same-peer
controls regress. There is no transcript executor or synthesized turn handoff.

Strict-v2 `started` is the session-readiness boundary. The speaker plays its
pinned cue only after that message and opens capture only at cue EOF. The
server declares bridge-owned `end_conversation`, `get_current_time`, and
`search_web`, the captured
Home Assistant tools, and optional agent tools, plus a narrow normalized
Spanish/English terminal-phrase fallback. A successful end emits a terminal
`stopped` event and normal cleanup restores idle. Device-declared and undeclared
tools are rejected.

See the [active v2 wire contract](protocol/realtime-wire-v2.md) and the
[server-offloaded architecture](server-offloaded-realtime-architecture.md).

### Historical wire-v3 device-owned WebRTC experiment

The following design and measurements are retained for rollback and research;
they are not the active Okay Nabu deployment. The dormant direct example
negotiates v3 with required
`conversation_mode: "native"` and an SDP offer containing audio plus the
ordered `oai-events` data channel. The bridge passes that offer to Codex App
Server using its managed login and returns the exact SDP answer. Once the
device confirms that its answer, ICE/DTLS peer, and data channel are ready, the
bridge acknowledges:

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

RTP audio and provider data events then travel directly between the device's
isolated `aiortc` peer and the provider. The bridge WebSocket accepts JSON
lifeline controls only; binary media is a protocol error. The bridge still owns
the historical native App Server thread with only `end_conversation`, rejects
unexpected tool requests, watches
for remote failure, and performs bounded stop/delete cleanup, but it never
constructs a media peer for v3.

The initial peer is implicit epoch 1 and retains the exact v3
`start`/`answer`/`transport_ready`/`started` shapes above. Rollover adds exact
epoch-tagged `rollover` -> `rollover_answer` ->
`rollover_transport_ready` -> `rollover_started` controls on the same bridge
WebSocket. Epochs must be consecutive. Because strict initial `started` cannot
advertise the extension, deploy the rollover-capable bridge before the device;
the new bridge accepts an old device, while an old bridge rejects rollover.

The device keeps capture/startup audio in a bounded 64 KiB queue, retains at
most 12 KiB of direct-only pre-roll inside that bound, and reserves at least 32
KiB for post-wake capture. Accepted backlog catches up at no more than 2× before
returning to realtime pacing. Each isolated sidecar preserves timestamped 16 kHz
mono PCM16 input, decodes provider output to bounded 24 kHz mono PCM16 playback,
and fails rather than dropping on IPC or queue pressure. Provider
response/output lifecycle does not label or gate the normal RTP lane: first
decoded audio emits internal `media.started`, and only an actual roughly 120 ms
receiver gap emits `media.quiet`. This preserves RTP-before-start prefixes and
stopped-before-tail audio. That normal-generation 120 ms boundary is separate
from interruption and does not authorize reuse of a peer. V3 never enters the
bridge's v2 2,250 ms media queue.

Qualified full duplex detects two consecutive AEC-filtered speech frames and
immediately kills local `paplay`, drops queued playback IPC, retires the old
PeerConnection epoch, and stops sending later capture to that peer. The outer vendor owner,
session/player objects, bridge WebSocket, and ready latch remain attached. The
one reusable isolated sidecar process holds the active PeerConnection and one
fresh, offer-warm standby PeerConnection. After the initial peer is ready, the
confirmation cue completes, and capture opens, that logical standby is prepared
for the next consecutive epoch. Rollover uses an ordered in-process promotion
that fences and stops the retired peer before later capture reaches the promoted
standby; the worker then prepares the following standby. The hard process cap is
one. If the standby is absent or invalid, rollover terminates the outer session
instead of launching another worker.
Exactly 4 KiB (two 64 ms frames, 128 ms) of recent AEC pre-roll
plus queued/live speech is delivered once and in order to the replacement peer.
After the network thread commits that interruption, continuing speech cannot
retire the replacement epoch. Eight consecutive detector-quiet 64 ms callbacks
(512 ms) rearm the detector before a later speech edge can interrupt again;
qualifying signal before the eighth resets the quiet count.

Capture age is rechecked when RTP actually consumes each packet; capture older
than 2.25 seconds is terminal even if it passed queue admission. The logical
standby is validated before use; an absent or invalid peer terminates the outer
session. Replacement lifecycle and PCM remain inaudible in one ordered
`output_queue_bytes`-bounded buffer until the exact matching
`rollover_started`, then replay through normal handlers. Float/bool protocol or
epoch controls are rejected, `stop` is normal in every rollover phase, and an
expired killed-sidecar close transfers final `waitpid` ownership to a daemon
reaper. That device-process close budget is independent of the bridge App
Server close-confirmation barrier below.

Generic public Realtime API client controls are not valid on this
subscription-backed Codex data channel: the pinned
[Codex 0.147 Frameless Bidi outbound
enum](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/codex-api/src/endpoint/realtime_websocket/protocol.rs#L50-L85)
defines no `response.cancel` or `output_audio_buffer.clear`, and the direct
route rejects public Realtime `session.update` VAD configuration. Live evidence
observed only provider `session.started`, with no `speech_started`, `turn.done`,
or transcript event; no provider acknowledgement can causally prove
interruption. Public Realtime v2 WebRTC/client-event semantics are unsupported
on this ChatGPT-subscription direct route; that does not affect the separate
active project wire-v2 `bridge_pcm` LAN transport. A synthetic same-peer canary
was rejected after old RTP continued beyond the five-second fence; the former
`response.interrupt`/`interrupt.fenced` experiment is not used in production.

The bridge gives the retired realtime session a 100 ms grace period to produce
its `thread/realtime/closed` notification. A confirmed barrier permits
same-thread startup-context reuse and reports `context_retained: true`; timeout,
error, or an absent close transfers the old epoch to tracked isolated-thread
cleanup, starts the replacement on a new thread, and reports false. This
prevents a delayed old stop from terminating a replacement but does not prove
audible-history correctness.
Interrupted unheard assistant output may remain in retained context, and
pre-roll may overlap samples already seen by the retired provider peer.

Fresh-peer rollover is a safe subscription-backed approximation, not exact
ChatGPT same-session interruption. It adds a measurable WebRTC/provider
negotiation handoff. Queue/age/timeout, sidecar, and epoch errors fail the outer
session closed; manual stop, mute, and disconnect still end it. Later detector
hits are ignored until realtime releases the microphone. A historical
two-worker build cut playback in 208–211 ms and completed rollovers in
1.29–1.57 s across two consecutive exact-artifact runs at that installation's
qualified 60% setting. Each run recycled the same two worker PIDs with no cold
replacement and retained context on both rollovers. Those measurements do not
physically validate the current single-worker build and do not replace the full
acceptance matrix for any installation. The historical retired-PeerConnection
stop acknowledgement was separate from the bridge's independent 100 ms App
Server close-confirmation barrier.

Historically, v3 failures never handed captured Okay Computer audio to Home
Assistant. Startup or runtime failure cleared the direct queues and returned
idle; that route is now dormant.

### Active strict-v2 route

The active `media_transport: "bridge_pcm"` route negotiates strict v2,
streams binary 16 kHz mono PCM16 to the bridge, and receives binary 24 kHz mono
PCM16 between speaking epochs. The active native/realtime-only configuration
discards pre-ready audio, has no Assist fallback, uses one server-owned peer,
and exposes Home Assistant tools plus `end_conversation`. Optional agent tools
are configuration-gated. Omitted-mode managed behavior remains
legacy compatibility only. See the complete [v2 wire
contract](protocol/realtime-wire-v2.md), dormant [v3 wire
contract](protocol/realtime-wire-v3.md), and [device deployment
guide](device/thirdreality/README.md).

### Other speech adapters

The retained experimental Codex TTS entity uses the authenticated
`POST /v1/synthesize/stream` route. It sends an EOF-terminated PCM16 WAV stream
through Home Assistant's TTS proxy as audio arrives; the finite
`POST /v1/synthesize` route remains available for older clients and diagnostics.
The component negotiates mono 16-bit WAV at 16 or 24 kHz and the bridge emits
the requested native rate, avoiding an extra bridge-side format mismatch.

The experimental Codex STT entity opens the authenticated
`/v1/transcribe/stream` WebSocket before consuming the microphone stream. It is
kept for protocol diagnostics and compatibility, not selected by the reliable
pipeline. Codex thread and WebRTC setup begin after the validated start message
while Home Assistant continues capture. Even after successful media delivery,
the conversational service is not guaranteed to emit a user transcript.

The bridge converts that narrow WebSocket protocol to a genuine WebRTC peer:
an active audio track carries media and the `oai-events` data channel carries
control events. It sends the offer to Codex App Server and applies the returned
answer. Historical subscription-authentication validation used realtime v3 on
Codex 0.146.0. The supported bridge now requires Codex 0.147.0 or newer for the
managed frontend controls. App Server's raw realtime WebSocket route is
deliberately not used because it required API-key authentication in the
historically tested release.

This subscription-backed App Server realtime surface is experimental and is
not the [documented OpenAI Realtime API WebRTC
interface](https://developers.openai.com/api/docs/guides/realtime-webrtc).
Native mode removes the artificial
transcript/executor/render sequence, but it cannot eliminate cold Codex thread
creation, WebRTC negotiation, network, service admission, or provider response
latency. The ThirdReality client therefore keeps one bounded, audio-empty
production session warm for five minutes after startup/completion and starts a
ten-second speculative session at the first strong pre-wake score. A real wake
claims it without repeating App Server/WebRTC negotiation; expiry or failure
uses the unchanged bounded cold path.

## Performance

On the measured i5-13600K host, a warm multilingual faster-whisper `base`
service transcribed the same non-sensitive reference WAV three times in 0.775,
0.617, and 0.599 seconds. The prior subscription-backed adapter timed out after
roughly 19 seconds in an observed physical run and also intermittently missed
clean input. These are small diagnostic samples, not latency guarantees.

On that host, the repository smoke probe measured Piper
`es_MX-ald-medium` to the first non-empty Wyoming PCM chunk. Across two service
restarts, cold first PCM ranged from 0.714 to 0.956 seconds and complete
synthesis from 0.824 to 1.072 seconds. Five warm requests reached first PCM in
0.025, 0.024, 0.044, 0.028, and 0.035 seconds: a 0.028-second median and
0.044-second maximum. Their median complete synthesis time was 0.116 seconds
for 2.949 to 3.367 seconds of audio. Three controlled Codex TTS requests for
the same text had a
2.025-second median to first audio (1.671 to 2.898 seconds), about 72 times
Piper's warm median at this provider boundary.

A separate physical Home Assistant call changed the ThirdReality
`media_player` state to playing at 0.018097 seconds and back to idle at
3.564543 seconds, but actual audible onset was not instrumented. These small,
host-side and state-boundary samples are not latency guarantees.

A subsequent controlled self-acoustic Spanish canary traversed the physical
speaker and microphone, wake detection, local STT, Codex Conversation, Piper,
and response playback without errors. STT ended 6.590 seconds after pipeline
start, Codex Conversation took 1.734 seconds, the satellite entered responding
at 8.324 seconds, and it returned to idle at 13.919 seconds. The request was
recognized with the intended words and the response was non-empty `es-MX`.
These state boundaries still do not measure first audible sound.

A later physical ThirdReality canary completed local recognition 0.497 seconds
after VAD ended and reached its Conversation/TTS result 7.703 seconds after
pipeline start, including capture. The answer played and the satellite returned
to idle.

Streaming Codex TTS exposed first PCM 4.222 seconds before the equivalent
finite bridge response in one earlier probe. That historical comparison is
between two Codex adapter modes, not between Codex and Piper.

A controlled acoustic A/B on the same ThirdReality device had reduced the old
adapter's STT completion from 9.519 to 6.138 seconds, but that tuning could not
remove the backend's missing-transcript failure mode. It is retained only as
historical diagnostic evidence.

Automatic STT-to-TTS session reuse is deliberately disabled in the bundled
component. Live v3 validation observed genuine assistant output before the
user transcript completed, and the tagged Frameless Bidi client protocol has
no supported response-cancel message. Both the component and bridge therefore
disable ticket issuance; a handoff-shaped diagnostic request is validated but
takes the isolated cold path. When both experimental Codex speech entities are
selected, TTS starts in a fresh thread and session. The recommended local Piper
stage does not use this remote handoff mechanism.

The Compose deployment keeps the bridge process, Codex App Server, OAuth
state, Python media stack, and listener warm. The first milestone still creates
the provider thread and server-owned WebRTC peer after wake: a permanently
connected provider slot would consume a live session and needs expiry and
assignment semantics. Add it only if measurements show remote negotiation is
the remaining dominant wake cost. The dormant v3 sidecar/standby experiment is
not launched by active `bridge_pcm`. See
[performance and ThirdReality
tuning](docs/performance.md) for measurement scope, handoff privacy and
fallback behavior, safe device settings, and the firmware canary decision.

## Development

See the [development workflow](docs/development.md) for the focused pytest,
disposable local Home Assistant, and staged production SSH deployment loops.

```bash
uv sync --extra test --extra lint
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/component
uv run pytest -p no:homeassistant tests/bridge
```

The integration follows current Home Assistant entity and config-subentry
patterns. CI runs Ruff, tests, hassfest, and HACS validation. Live tests are
opt-in and must never print OAuth tokens or recorded audio.

## Removal

1. Remove the Codex Voice integration from Home Assistant under **Settings →
   Devices & services**.
2. Remove Codex Voice from HACS and restart Home Assistant.
3. Stop and disable the separately running bridge process if no other client
   uses it, then delete its dedicated bridge token.
4. If they were installed only for this pipeline, remove the Wyoming
   integrations and stop the faster-whisper and Piper services separately.
5. If direct ThirdReality mode was installed, follow the device rollback
   contract, remove the route-scoped token from the bridge, and verify the
   approved TCP ADB port 5555 recovery path before and after the voice restart.

## Known limitations

- Subscription-backed audio depends on an experimental Codex realtime
  conversation feature and consumes ChatGPT subscription availability, not
  OpenAI Platform API quota. It is not the stable OpenAI Audio or Realtime API,
  and Codex CLI/App Server upgrades may change it.
- The reliable pipeline's STT is local faster-whisper, not OpenAI-hosted and
  does not consume ChatGPT quota. ChatGPT subscription OAuth does not expose a
  supported standalone transcription endpoint. The retained Codex STT adapter
  can connect successfully without producing a transcript.
- Recommended Assist TTS is local Piper over Wyoming and does not use the
  ChatGPT subscription speech lane. The retained Codex TTS entity is a bounded
  realtime-conversation compatibility session, not OpenAI's separately billed
  `/v1/audio/*` endpoint. Its live voice model may paraphrase or expand text
  instead of reading it verbatim. Do not use that experimental entity for
  safety-critical or legally exact announcements.
- The Codex subscription realtime surface is admitted one speech session at a
  time. Overlapping experimental Codex STT, TTS, or realtime requests fail
  immediately as busy so a caller can retry, rather than occupying the bridge
  until a timeout. Wyoming STT does not use that lane.
- HACS cannot install the bridge process. Run it separately or with the root
  Docker Compose deployment.
- The active ThirdReality configuration must explicitly select strict-v2
  `bridge_pcm`, `conversation_mode: "native"`, full duplex, `native_aec3`, the
  reviewed PulseAudio route, the 10 dB native baseline with noise-limited
  adaptive gain and moderate noise suppression, 0 dB transport gain, and a 100%
  fixed sink/playback anchor. The output stream stays at 100% relative volume;
  only one non-amplifying software attenuator implements the physical 0/1–100%
  user range. A syntactically valid topology is not acoustic proof, so qualify
  normal-distance speech, no-user echo, and early/middle/late interruption on
  every device and room at full output.
- Active v2 is fail-closed rather than failover: startup or runtime failure
  clears Okay Nabu audio and returns idle without invoking Home Assistant.
- Okay Nabu is an untrusted native audio/control endpoint. It never receives
  Home Assistant credentials, schemas, arguments, or results; the server model
  can control only entities in Home Assistant's captured exposed-entity tool
  snapshot. `end_conversation` is bridge-owned and external-agent tools are
  optional.
- ThirdReality firmware 1.01.07 normally withholds microphone audio until its
  wake confirmation sound finishes, then may block the microphone thread for
  up to two seconds while updating the LED. The optional pinned overlay uses an
  LED-only acknowledgement, makes microphone forwarding effective as soon as
  the local Assist request and music-duck calls complete, and dispatches
  serialized LED updates off the microphone thread. Applying the overlay,
  selecting aggressive finished-speaking detection, or raising hardware
  microphone gain are
  device-side changes with explicit accuracy, compatibility, and clipping
  acceptance checks.
- That firmware also applies stored PDM microphone gain after PulseAudio has
  already opened capture. The guarded `S49codex-mic-gain` deployment hook
  latches the validated 0–100 preference before `S50pulseaudio`, without
  replacing a vendor script or touching ADB. Invalid values use the vendor's
  30% fail-safe. Each change requires an ALSA capture reopen and real capture
  canary rather than trusting the mixer display alone; the hook guarantees the
  ordering on reboot, while a separately controlled PulseAudio reopen can also
  latch it.
- The official ThirdReality v1.2 firmware is a substantial Python-to-C++
  rewrite, but the target has one boot/system/recovery set rather than A/B
  slots. Do not flash the sole production speaker. Test only on a spare after
  capturing the actual partitions, data, boot environment, and device security
  state, then physically rehearsing a full-image downgrade and restoration.
  v1.2.1 also enables unauthenticated root ADB over TCP port 5555 and ships
  password-authenticated root SSH with a documented default. Isolate and
  harden both services, then verify the ports after reboot and updates. The
  tagged updater disables TLS peer and hostname verification, and a locally
  calculated SHA-256 identifies bytes but does not authenticate their
  publisher. No production flash is allowed without independently
  authenticated provenance.

## License

MIT. See [LICENSE](LICENSE).
