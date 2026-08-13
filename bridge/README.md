# Codex Voice bridge

This process exposes a small bearer-authenticated HTTP/WebSocket API and owns a
local `codex app-server` child. In the active realtime architecture it is the
media server for the ThirdReality speaker: strict-v2 PCM arrives over the LAN,
and the bridge keeps one provider generation active behind the stable device
WebSocket. A qualified interruption replaces that generation without making
the speaker reconnect.
Home Assistant exposed-entity tools are the default smart-home authority on the
active Okay Nabu path; an external memory/deep-task agent is optional. The child
uses the machine's existing file-backed
Codex/ChatGPT login through a private temporary Codex home; no OAuth token is
copied into Home Assistant or onto the speaker.

## Run

Python 3.11 or newer, Codex CLI 0.147.0 or newer, and a working file-backed
`codex login` are required. The bridge auto-detects `auth.json` below
`CODEX_HOME` or `$HOME/.codex`; set `HA_CODEX_AUTH_FILE` to an absolute path
when it lives elsewhere. Keyring-only and group/world-readable credentials
fail closed.

```bash
python -m venv .venv
.venv/bin/pip install -r bridge/requirements.txt
export HA_CODEX_BRIDGE_TOKEN="$(openssl rand -hex 32)"
export HA_CODEX_REALTIME_DEVICE_TOKEN="$(openssl rand -hex 32)"
.venv/bin/python -m bridge
```

For the active always-running server, use the root `compose.yaml` and the
ignored mode-0600 environment files described in
[`server-offloaded-realtime-architecture.md`](../server-offloaded-realtime-architecture.md).
Compose keeps the listener, App Server process, OAuth state, and media stack
warm; it does not allocate a provider peer until an accepted wake.

The default listener is `127.0.0.1:8787`. Override it with
`HA_CODEX_BRIDGE_HOST` and `HA_CODEX_BRIDGE_PORT`. `CODEX_APP_SERVER_COMMAND`
can replace the default hardened app-server command. The bridge creates
mode-0700 temporary `HOME`, `CODEX_HOME`, and runtime directories, linking only
the managed `auth.json`. It starts all Codex threads with the named
`ha-voice-minimal` least-privilege permission profile and `approvalPolicy:
never`. Threads persist only in that private home so the bridge can delete them
immediately when their managed lifetime ends. The default command grants only
Codex's minimal runtime paths and disables shell,
web, plugins, apps, MCP servers, hooks, and inherited command environment
variables. Startup and every new thread fail closed unless that profile is
available and active. Startup also audits App Server's effective configuration
layers and rejects any configured MCP server.

If the installed `codex` launcher depends on a Node.js interpreter that is not
available to the service, set `HA_CODEX_BINARY` to the absolute native Codex
executable. This replaces only the first executable in the default command and
preserves every hardened argument above. A complete
`CODEX_APP_SERVER_COMMAND` override takes precedence and remains responsible
for reproducing all required hardening.

Finite STT keeps a two-second fragment-quiet fallback by default. A deployment
that has passed short, long, paused, number, and name transcription trials may
set `HA_CODEX_TRANSCRIBE_LIVE_FRAGMENT_QUIET_SECONDS` between `0.5` and `2.0`.
Values below `2.0` are an explicit accuracy/latency tradeoff: local WebRTC input
drain does not prove that remote recognition has emitted its final fragment.
The shorter value is still gated on successful drain, a calibrated unity-gain
live feed, and no retained-session handoff.

If `CODEX_APP_SERVER_COMMAND` is overridden, it must enable the experimental
`realtime_conversation` feature. The bridge still injects and verifies the
profile selected by `HA_CODEX_PERMISSION_PROFILE` (default
`ha-voice-minimal`) on every thread. Do not weaken the inline profile or
replace it with legacy `read-only`, which permits broad host reads.

All routes, including `GET /health`, accept
`Authorization: Bearer <HA_CODEX_BRIDGE_TOKEN>`. The primary token retains
legacy realtime v1 compatibility. When the optional, distinct
`HA_CODEX_REALTIME_DEVICE_TOKEN` is set, it is accepted only for
`GET /v1/realtime` and only after the first start message successfully
negotiates strict v2 or v3. A device-token v1 or malformed negotiation is rejected
before provider/thread startup, and the device token is rejected by every
other route. This lets a speaker use content-private realtime audio without
storing the Home Assistant/component credential. In particular, the device
token cannot connect to `/v1/home-assistant/tools`; that authority route
requires the primary Home Assistant bridge token.

## API

- `GET /health`
- `GET /v1/conversation` WebSocket: `start`, streamed `delta`, `tool_call`,
  `tool_result`, and `done` messages. Stable Home Assistant `conversation_id`
  values reuse a bounded in-memory Codex thread for multi-turn context. A
  start may select `service_tier` as `standard` or `priority`; priority targets
  lower latency while increasing subscription usage. Its optional, bounded
  BCP-47 `language` is attached to every turn as trusted application context;
  the Home Assistant pipeline supplies `es-MX` for Mexican Spanish.
- `POST /v1/transcribe`: up to 60 seconds of base64 PCM16/WAV plus audio
  metadata; returns JSON `{ "text": "..." }` under a bounded end-to-end
  deadline.
- `GET /v1/transcribe/stream` WebSocket: a validated v1 start object, bounded
  binary PCM16 frames, and explicit `end`/`cancel` control. It returns one
  transcript result and lets bridge setup overlap Home Assistant capture.
- `POST /v1/synthesize`: text, voice, language, and an optional supported
  output tuple; returns mono 16-bit WAV at 16 or 24 kHz.
- `POST /v1/synthesize/stream`: the same request contract, returned as a
  progressively delivered mono PCM16 WAV stream at the requested supported
  rate.
- `POST /v1/speech-session/release`: idempotently release a private,
  unconsumed STT-to-TTS handoff ticket.
- `GET /v1/home-assistant/tools` WebSocket: one primary-token, Home
  Assistant-owned realtime tool authority. The first message registers a
  bounded immutable generation of rendered instructions, locale, and selected
  LLM API tools; correlated tool calls/results remain on this socket. The
  route-scoped realtime-device token is never accepted here.
- `POST /v1/agent/announce`: optional report-back with exact JSON
  `{"text":"..."}`. A distinct `HA_CODEX_AGENT_ANNOUNCE_TOKEN` is valid only
  on this route; the primary bridge token is also accepted for operator tests.
  The message is appended for speech only while an idle native session is
  active. It returns 503 rather than waking or reopening an idle speaker.
- `GET /v1/realtime` WebSocket: legacy v1 JSON/base64 messages, strict v2
  binary PCM16, or strict v3 device-owned WebRTC signaling. V3 relays a
  validated SDP offer/answer and then carries JSON lifeline controls only;
  audio and `oai-events` go directly between the device peer and provider.
  V2 emits content-free lifecycle controls, filters continuous WebRTC silence,
  gates output with socket-lifetime monotonic epochs, and supports exact native
  `barge` rollover as well as the legacy negotiated interrupt exchange. It does
  not expose transcripts, provider payloads, tool calls, or tool results to the
  device.
  The active strict-v2 route requires `conversation_mode: "native"`; an
  accepted value is echoed in `started`. Native v2 declares bridge-owned
  `end_conversation` and `search_web`, the immutable Home Assistant authority
  snapshot captured at startup, and optional external-agent tools.
  A v2 `text` control is a bounded user message; its role must be omitted or
  exactly `user`. See the [active v2
  contract](../protocol/realtime-wire-v2.md) and dormant [v3 direct-media
  contract](../protocol/realtime-wire-v3.md).

Authenticated health includes a content-free `home_assistant_tools` object:
registration/open-transport readiness, locale, tool count, pending calls,
exclusive process-lifetime outcome counters, and the duration of the most
recent completed attempt. It never includes authority IDs, tool names,
schemas, arguments, results, prompts, or conversation content.

### Active server-offloaded ThirdReality route

Okay Nabu uses strict v2 `bridge_pcm`, explicit native mode, and full duplex.
The speaker continuously sends 16 kHz mono PCM16; the bridge resamples and
paces it into the active provider WebRTC peer, then returns 24 kHz mono PCM16
inside monotonic speaking epochs. The device WebSocket remains open across
responses and interruptions. The active provider-control policy sends exact
`{"type":"provider_barge"}` after a qualified local cut. The bridge mirrors the
desktop WebRTC hush pair (`response.cancel`, then
`output_audio_buffer.clear`) while keeping that peer and its input stream live.
The bridge explicitly requests `gpt-live-1-codex`; the model is overridable by
`HA_CODEX_REALTIME_MODEL` for a controlled rollback or canary.

For a temporary debugging installation, set
`HA_CODEX_REALTIME_LOG_TRANSCRIPTS=true`. Native realtime then writes bounded
final user and assistant transcript text to the rotated Docker log. It defaults
to false because these records contain conversation content; disable it and
rotate/remove the test logs before ordinary household use.

The bridge does not wait for a provider VAD boundary. If same-peer provider
control is unavailable, `barge_in_mode: "rollover"` retains the conservative
generation-replacement path: strict stop and a matching
`thread/realtime/closed` notification within the 100 ms reuse grace before
reusing the existing thread with startup context. An error, timeout, or
otherwise ambiguous close starts the replacement on a fresh isolated thread so
late output from the retired generation cannot cross the fence.

The bridge sends `started` only after the App Server thread, SDP exchange,
WebRTC transport, and provider session are ready. That is the device's
cue-gated capture boundary. Native v2 has no completed-transcript executor or
`appendSpeech` render handoff. It exposes bridge-owned `end_conversation`, Home
Assistant tools from the generation snapshot captured before `thread/start`,
and—only when configured—`ask_agent` and `recall_memory`. Calls execute
asynchronously so PCM forwarding continues. The narrow bilingual
terminal-phrase fallback still emits terminal `stopped`; undeclared and
device-declared tools fail closed.

### Default public-web search

When managed Codex OAuth is configured, the bridge advertises `search_web` and
uses the same subscription-backed `alpha/search` endpoint as the pinned Codex
App Server. It reads the managed OAuth snapshot for each request so App Server
token refreshes remain authoritative; credentials and raw provider payloads are
never returned to the realtime model. This is an experimental Codex endpoint,
so it is version-pinned and not treated as a stable public API.

The normal Compose stack also starts a digest-pinned SearXNG service whose
published port is restricted to `127.0.0.1`. It is an automatic fallback when
subscription search is unavailable and is the primary backend only when no
managed OAuth file is configured. Each call accepts one bounded query and
returns at most six bounded public HTTP or HTTPS results with title, URL,
excerpt, and optional publication date. It does not expose a general browser,
arbitrary fetch endpoint, host network tool, or OpenAI API key.

Search is independent of PCM forwarding and Home Assistant authority. Results
are explicitly untrusted and may contain inaccurate or adversarial text; the
model is instructed to compare sources and never treat web content as
authorization or smart-home state. Set a different local SearXNG endpoint with
`HA_CODEX_WEB_SEARCH_URL`. With managed OAuth present, omitting that variable
disables only the local fallback, not subscription search.

An optional agent may report a completed background task to the currently
active session through `/v1/agent/announce`. This path has a separate bearer,
accepts at most 600 characters, refuses busy/idle sessions, and never receives
Home Assistant or Codex credentials. It deliberately does not create a new
speaker connection after the user has ended a conversation.

### Private wake-sample collection

Wake-sample collection is disabled by default and the normal Compose service
has no writable recording mount. To opt in for a controlled training session,
create an owner-only directory, set `HA_CODEX_VOICE_SAMPLE_ROOT_HOST` to its
absolute path, and add the explicit override:

```bash
install -d -m 700 /absolute/private/voice-samples
docker compose -f compose.yaml -f compose.voice-lab.yaml \
  --env-file .codex-voice/compose.env up -d --build bridge
```

Also set `voice_sample_collection_enabled: true` in the root-owned speaker
configuration. The speaker retains at most three seconds of AEC-cleaned idle
PCM in memory, detaches that ring at an accepted wake, and queues it to a lazy
background uploader. The microphone callback never performs HTTP or file I/O.
The bridge writes mode-`0600` WAV/JSON pairs beneath the mode-`0700` inbox and
initially labels each capture `unreviewed-wake`. When the user explicitly says
the wake was accidental, the native-only `mark_false_wake` tool changes only a
recent unreviewed record to `wake-negative`/`false-activation`. It never infers
labels automatically. Remove the override and speaker flag to restore the
zero-allocation default.

### Optional local speaker identification

Speaker identification is disabled by default. Enabling
`compose.speaker-identity.yaml` starts a separate non-root worker with pinned
`sherpa-onnx`/NumPy dependencies and an exact hash-verified TitaNet model. The
ordinary bridge image remains unchanged. Configure a distinct token and
owner-only model/profile paths:

```bash
export HA_CODEX_SPEAKER_IDENTITY_TOKEN="replace-with-a-distinct-random-token"
export HA_CODEX_SPEAKER_IDENTITY_MODEL_HOST="/absolute/private/nemo_en_titanet_large.onnx"
export HA_CODEX_SPEAKER_PROFILES_HOST="/absolute/private/speaker-profiles"
docker compose --env-file .codex-voice/compose.env \
  -f compose.yaml -f compose.speaker-identity.yaml \
  up -d --build
```

The bridge captures at most one five-second post-wake PCM window and submits it
asynchronously over loopback. The worker strips silence and requires both a
calibrated cosine threshold and separation margin. A confident match is
appended later as advisory developer context; `unknown`, timeout, worker
failure, or an absent profile changes nothing. Identity never delays readiness,
capture, interruption, or the first response and never authorizes a sensitive
Home Assistant action. Enrollment and threshold evaluation are documented in
[`docs/development.md`](../docs/development.md#optional-speaker-identification).

The Compose service keeps the bridge, App Server, OAuth state, media stack, and
listener warm. It still creates the provider thread and peer at wake; a
connected standby session is deliberately deferred until measurement proves
remote negotiation is the dominant remaining delay.

### Dormant device-owned v3 route

The following route is retained for rollback and research; it is not used by
the active Okay Nabu deployment. Device-facing v3 was the direct-media
transport used by the earlier ThirdReality
v1.1.7 client. The aarch64 Buildroot Linux speaker creates an `aiortc` offer
with audio and `oai-events`; the bridge starts one native App Server realtime
session with only `end_conversation` and returns the provider SDP answer.
The operator may select documented WebRTC v1 or v3 at the bridge, with v3 as
the supported default; the device cannot override it. The
device must acknowledge its applied answer, connected peer, and open data
channel before the bridge sends `started`. The bridge never constructs a v3
media peer or accepts binary audio on that socket. It owns OAuth, App Server
start/stop, SDP relay, unexpected-tool rejection, sanitized remote lifecycle,
and bounded cleanup of every thread created by the outer session only.

The initial peer is implicit epoch 1. Its strict
`start`/`answer`/`transport_ready`/`started` objects remain unchanged. Trusted
AEC barge-in extends the same WebSocket with consecutive epoch-tagged
`rollover`, `rollover_answer`, `rollover_transport_ready`, and
`rollover_started` controls. Since the exact initial acknowledgement has no
capability field, deploy the rollover-capable bridge before the new device. The
new bridge remains compatible with the old device; the old bridge rejects a
rollover message. Protocol and epoch values require exact JSON integers;
booleans and floating-point values are rejected.

“Okay Computer” entered this historical native route while “Okay Nabu”
remained on Home Assistant's official Assist flow. The bridge did not capture a Home
Assistant broker snapshot, wait for a transcript, create an executor, or call
`thread/realtime/appendSpeech` for v3. Device/provider RTP and data-channel
traffic bypass the bridge, and the route-scoped device bearer cannot access
the Home Assistant broker.

The v3 signaling, cleanup, rejection, and no-bridge-media boundaries have local
automated coverage. This documentation does not claim that the new v3 path has
completed the physical ThirdReality acceptance matrix.

### Active v2 and compatibility details

Device-facing v2 is now the active `bridge_pcm` route. It sends 16 kHz
mono PCM16 to the bridge and receives 24 kHz mono PCM16. The bridge applies its
2,250 ms input-track limit to these v2 live sessions; finite STT keeps its
existing whole-utterance input capacity. Active native/realtime-only startup
discards pre-ready PCM and never falls through to Assist.

Strict-v2 clients that omit `conversation_mode` retain compatibility with the
previous automatic policy. When exactly one Conversation subentry is opted in,
App Server realtime v3 and its captured broker snapshot select a managed
two-thread route: a tool-free speech frontend and an isolated executor that
alone receives the selected tools. Without all of those conditions, an
omitted-mode session remains native. The reference ThirdReality client does
not use this compatibility route.

The legacy managed frontend starts with immutable routing instructions,
`clientManagedHandoffs: true`, and `delegationAckFiller: false`. App Server v3
can route native delegation before a notification is observable, so the
separate tool-bearing thread—not the prompt alone—is the authority boundary.
The frontend also disables unrelated repository startup context and admits the
device after the required `session.started`; v3 does not consistently emit a
separate startup context acknowledgement.
Any frontend, foreign, stale, or post-interrupt tool call is answered with
`unowned_home_assistant_tool_call` and `do_not_retry: true` without invoking
Home Assistant. An identified raw v3 user `turn.created`/`turn.done` pair—not
the identity-free normalized transcript notification—owns each microphone
request. On a completed executor turn, the bridge selects the final agent
answer and sends one valid UTF-8 prefix of at most 500 bytes to the frontend
with `thread/realtime/appendSpeech`. It drops frontend PCM until
`session.context.appended` acknowledges the append and a session-unique
assistant `turn.created` identifies and arms the current bridge generation.
The first authorized non-silent PCM begins the speaking epoch, and only the
matching assistant `turn.done` releases the serialized render slot;
unsolicited, replayed, and stale audio remains inaudible. Managed v3 ignores
response/output-buffer lifecycle aliases. Codex 0.147 frameless cancellation
terminates through the identified turn lifecycle; a missing matching terminal
fails the next render closed rather than permitting overlap.

Executor completion is independently bounded by the configured bridge request
timeout. A failed terminal or expired turn returns a generic device error and
closes the session instead of waiting indefinitely. Stop, disconnect, and
service shutdown tombstone and interrupt an active executor before its event
subscription closes; transport and two-thread deletion then continue in
tracked, cancellation-shielded cleanup.

The active device keeps capture closed until the ready cue reaches EOF, so no
v2 startup backlog or pre-ready Assist copy exists. Its 64 KiB input queue and
48 KiB (about 1.024 s) playback queue bound live scheduling pressure only.
Realtime-only failure clears both queues and returns idle. Catch-up cannot make
cold App Server/WebRTC setup or provider response generation disappear.

Historically, dormant v3 bounded its sequenced-packet IPC and bypassed bridge
media queues; older optional v2 split deployments retained bounded pre-ready
Assist replay. Neither behavior describes the active route.

### Dormant v3 safety details

V3 `device_webrtc` requires full duplex as a device-side contract: the pinned
client must verify the reviewed static PulseAudio
`module-echo-cancel` topology with the exact configured allowlisted AEC engine,
exact source/sink and capture-process routing, and a configured 1–60% sink
ceiling (25% by default) before
it opens this route. WebRTC is the default when no method is supplied; there is
no automatic fallback. The stock ThirdReality v1.1.7 module rejects WebRTC and
Speex, so its qualified configuration must explicitly select Adrian. The client
checks the ceiling and sets and verifies the dedicated AEC sink at the exact
configured raw playback value once per direct session, before requesting its
SDP offer or connecting to the bridge. Direct `paplay` targets that sink with
raw stream volume 65536, non-blocking 20 ms writes, and reviewed 60 ms/20 ms
latency/process arguments; no sink-input is mutated and no `pactl` subprocess
runs from the live response or interruption path. Active v2 uses the same
qualified fixed sink anchor and raw 65536 (100%-relative) stream, with one
non-amplifying software attenuator for user volume; it does not apply the old
second linear stream-volume reduction. The
guarded device installer writes the matching raw sink value in the static
PulseAudio startup block. The stock voice process later reapplies the persistent
Home Assistant media-player preference, which must match the configured
ceiling; runtime restore state alone is not treated as reboot evidence.

### Active and compatibility v2 interruption

V2 advertises `remote_cancel: false` because clients may never infer remote
cancellation from a local flush. It separately advertises
`same_session_interrupt_ack: true` for the legacy `interrupt` exchange. The v2
ThirdReality client sends `User-Agent: ha-codex-voice-thirdreality/2`; that
header retains compatibility with the managed interrupt policy but does not
select it. On a legacy
broker-managed interrupt, the bridge invalidates the executor/output generation
and asks the frontend provider to cancel only when an identified assistant
render is active; it never cancels an idle or pending frontend. Before Home
Assistant tool dispatch it tombstones and interrupts the executor turn; after dispatch it
lets the tool-bearing turn settle, suppresses the old final, and queues the
newest request when its transcript arrives.
It then returns `fresh_session_required: false`, `remote_cancelled: false`, and
`continuation_safe: true`; the local generation gate makes continuation safe
without claiming remote cancellation. A legacy client gets the established
fresh-session fallback and socket teardown.

The active explicit-native client instead sends exact nonterminal
`provider_barge` after its local AEC-qualified cut. Capture and the device
WebSocket stay live while the bridge sends `response.cancel` and
`output_audio_buffer.clear` over the same provider peer. The bridge never waits
for provider VAD. Exact `barge` remains the conservative peer-replacement
fallback.

### Dormant v3 rollover details

V3 barge-in does not use a bridge or provider interrupt acknowledgement. The
device immediately kills local `paplay`, retires the old sidecar, and sends no
later capture to that peer. It retains the outer vendor owner/session/player,
bridge WebSocket, and ready latch while a prewarmed child negotiates the next
peer epoch. A bounded recent AEC pre-roll plus queued/live speech is sent once
and in order to the replacement peer. Queue/age/timeout, sidecar, or invalid
epoch failure ends the outer session closed; stop, mute, disconnect, and
normal-wake preemption still end it. A device `stop` is normal termination
during old-session closure and every answer/readiness phase, not a rollover
protocol failure.

The bridge stops the retired App Server realtime session and waits for the
matching `thread/realtime/closed` notification. The stop RPC alone only
enqueues close. A confirmed notification permits same-thread replacement with
`includeStartupContext: true` and `context_retained: true`. Timeout, error, or
an absent close starts an isolated thread and reports `context_retained: false`
so a delayed old close cannot terminate the replacement. Retained context does
not prove audible-history correctness; interrupted unheard assistant output may
remain, and recent pre-roll may overlap audio already sent to the retired peer.

Frameless v3 exposes no public cancel/truncate control or provider interruption
acknowledgement. A synthetic same-peer canary was rejected when old RTP
continued beyond the five-second media-fence deadline, so the former
`response.interrupt`/`interrupt.fenced` experiment is not the production path.
Fresh-peer rollover is a safe subscription-backed approximation, not exact
ChatGPT same-session interruption, and adds measurable negotiation latency.
The reference installation passed a physical v3 double-interruption canary at
its qualified 60% setting in two exact-artifact runs: four local cuts were
208–211 ms and four rollovers were 1.29–1.57 s. Each run recycled its same two
worker PIDs without a cold replacement and retained context twice. That
evidence is installation-specific; each deployment must still pass its own
physical acceptance matrix. See the normative
[v3 interruption contract](../protocol/realtime-wire-v3.md#barge-in-and-interruption).

### Server-owned media adapters

Bridge-owned v1/v2/STT/TTS audio adapters use Codex App Server's experimental
WebRTC v3 path. Their peer creates a real paced outbound audio track and
`oai-events` data channel before the SDP offer. Device protocol v3 instead
relays the device's offer and never creates that bridge peer. App Server
supplies the remote SDP answer and lifecycle notifications. Codex 0.147.0 or
newer is required for the managed frontend's
delegation acknowledgement control. This is the subscription-compatible path.
The public Realtime v2 WebRTC/client-event dialect is unsupported on this
subscription-backed direct route; raw Realtime v2 WebSockets require API-key
authentication and are intentionally not used. That restriction is unrelated
to this project's active wire-v2 `bridge_pcm` LAN transport.

This App Server realtime surface is experimental and not the documented OpenAI
Realtime API. Native selection removes the bridge-created transcript/executor/
render sequence, but a new direct wake still pays for cold Codex thread
creation, App Server/WebRTC negotiation, service admission, network latency,
and provider response generation. The bridge does not claim parity with an
already-open ChatGPT voice session.

Current App Server documentation exposes WebRTC start/stop for realtime v1 and
v3, not v2. The direct device route remains on tagged Frameless v3; a live v1
subscription canary failed before startup and is not an operational fallback.

The subscription realtime voice is conversational, not a verbatim
text-to-speech API. `/v1/synthesize` sends a tightly constrained text turn but
still provides best-effort conversational speech and returns the header
`X-Codex-Synthesis-Mode: conversational-best-effort`; callers must not assume
that spoken wording exactly matches the input. A cold `appendSpeech` variant
was also tested through the official Home Assistant `tts.speak` path and
timed out at 90.047 s with HTTP 504; the constrained `appendText` turn remains
the supported implementation.

## Streaming STT and guarded session-handoff experiment

The component opens `/v1/transcribe/stream` before it reads Home Assistant's
microphone iterator. Once the bridge validates the start object, it starts the
Codex thread and realtime WebRTC handshake concurrently with capture. The
bridge performs one bounded level calibration after it observes sustained
speech, then normalizes and feeds subsequent audio while capture continues.
Quiet or ambiguous captures remain buffered until explicit EOF. The complete
raw capture is retained so a retry always starts in a fresh session with a
whole-utterance normalization pass. This preserves Home Assistant's finite STT
result while allowing remote recognition to overlap capture.

The bundled component does not request STT-to-TTS session handoff. Live
realtime v3 validation found that the remote session can begin assistant output
before finite STT completes, while the supported Frameless Bidi client protocol
has no response-cancel message. The official Assist path therefore uses a
fresh TTS session.

The bridge retains the experimental wire schema and validation machinery for
future protocol work, but the released build never retains the STT session or
issues a ticket. A request containing the following field is validated and then
takes the same isolated cold path; production clients should omit it.

```json
{
  "speech_session_handoff": {
    "version": 1,
    "voice": "cove",
    "language": "en-US"
  }
}
```

The dormant protocol defines a versioned random 256-bit ticket, its voice,
normalized language, and `expires_in_ms: 30000`. The handoff language must
normalize to the same tag as the outer transcription metadata. If a future
causally safe implementation enables it, a compatible `/v1/synthesize` or
`/v1/synthesize/stream` request may present the ticket once as
`speech_session_handoff_token` with that same language. The bridge then uses
`appendSpeech` on the sanitized STT session instead of creating a second
thread and WebRTC session. Only one offer can be outstanding on the bridge's
single speech lane.

The released bridge does not reach ticket issuance. The dormant machinery
stores only a ticket's SHA-256 digest and never logs the raw value. In that
protocol, the bearer-authenticated request/response transport carries the
ticket, and Home Assistant would bind it in memory to the exact bridge client,
`ChatSession`, official pre-STT TTS preparation, voice, and language. Custom
TTS instructions, another client/session, direct `tts.speak`, mismatch,
expiry, remote output, or remote failure cannot implicitly claim it. An
incompatible request closes the offer before taking the normal cold path. If
an already-claimed reuse attempt fails before first PCM, synthesis may
cold-start within its original deadline; it never restarts after PCM has been
exposed to the caller.

The release route accepts
`{ "speech_session_handoff_token": "..." }` and returns `204` even when a
matching offer has already expired or been cleaned. Expiry, explicit release,
replacement, shutdown, cancellation, and successful consumption converge on
the same exactly-once session stop and thread deletion. Tickets are bearer
secrets despite their short lifetime and must stay out of URLs, logs, task
names, and diagnostics.

The bridge intentionally does not maintain an always-on remote prewarm. An
idle WebRTC peer continues emitting silent RTP, would occupy the single speech
lane, and has no documented quota-neutral lifetime. The supported optimization
is capture overlap plus progressive TTS delivery. See
[performance and ThirdReality tuning](../docs/performance.md) for live
measurement scope and the prewarm rationale.

Finite transcription does not append synthetic silence by default. Set
`HA_CODEX_TRANSCRIBE_SILENCE_MS` only when an explicit nonzero compatibility
tail is required. Successful STT and TTS attempts log numeric stage durations
only; speech, transcripts, prompts, credentials, SDP, and remote identifiers
are never included in those timing records. Streaming STT also logs capture,
handshake/capture overlap, post-capture, and total durations so deployments can
verify overlap without recording content.

Interactive approvals, permission requests, and unsupported server-initiated
requests fail closed. Only explicitly declared dynamic Home Assistant tools are
relayed to the official Conversation WebSocket, a legacy v1 realtime client,
or the isolated executor of a legacy auto-selected managed v2 session bound to the current Home
Assistant broker generation. The managed speech frontend has no tools and its
unexpected requests are explicitly rejected. V2 devices never declare,
receive, or answer tool calls themselves. Exactly one
explicitly opted-in Conversation subentry may register at most 128 tools; tool
schemas, arguments, results, pending calls, and per-session calls are bounded.
Home Assistant renders the authority instructions, defaults its realtime
locale to `es-MX`, executes calls through the selected LLM API, and returns
correlated results. Missing, ambiguous, replaced, disconnected, timed-out, or
invalid authority fails closed. The component uses the `conversation`
assistant exposure namespace, matching the official Conversation flow. The
deadline hierarchy is 25 seconds for tool execution, 5 seconds for component
result delivery, 35 seconds for the complete bridge broker transaction, 5
seconds for provider-result delivery, and a 45-second App Server fallback.
Unknown outcomes carry `do_not_retry`, disable further authority calls in that
session, and cannot be amplified with fresh provider IDs. A 20-second
post-result continuation watchdog bounds a provider/App Server wedge.
