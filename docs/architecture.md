# Architecture

## Standard Assist pipeline

```text
ThirdReality Assist satellite
  -> Home Assistant Assist pipeline
     -> Codex Voice STT entity
     -> Codex Voice Conversation entity
     -> Codex Voice TTS entity
  -> authenticated HTTP/WebSocket bridge API
  -> local bridge process
  -> Codex App Server over JSON-RPC/stdio
  -> Codex-managed ChatGPT login
```

The component is a normal Home Assistant integration with one parent config
entry and Conversation, STT, and TTS config subentries. This makes all three
providers selectable in the official Assist pipeline. The existing
ThirdReality satellite needs no firmware change for this turn-based mode.

Conversation turns use stable App Server thread and turn methods. Selected Home
Assistant LLM tools are advertised as dynamic tools. When Codex requests a tool,
the component executes it through Home Assistant's LLM API; the bridge has no
direct home-control authority.

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

## Subscription audio adapter

Codex 0.146.0 does not expose independent subscription-backed STT or TTS RPCs.
The bridge therefore creates short-lived realtime-conversation sessions:

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

## Finite speech latency and session ownership

Home Assistant's STT provider opens `/v1/transcribe/stream` before consuming
the microphone iterator. After validating the start message, the bridge starts
the thread and WebRTC handshake in a task while it continues receiving bounded
PCM frames. The completed capture is normalized and released to that task only
after explicit EOF. This overlaps remote setup with capture while preserving a
finite, whole-utterance recognition boundary.

The production lifecycle creates a fresh realtime resource per speech
operation. Automatic reuse is disabled because live v3 sessions emitted
assistant output before finite STT completion and the supported Frameless Bidi
outbound protocol has no response-cancel control. This keeps the official Home
Assistant provider flow isolated and deterministic.

```text
STT active
  -> transcript result -> stop session -> delete thread
  -> TTS -> start fresh session -> stream speech -> stop/delete
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
quota-neutral idle lifetime. Current latency work is therefore limited to
capture overlap and progressive TTS delivery. See [performance and ThirdReality
tuning](performance.md) for the live measurements and acceptance criteria.

## Realtime client mode

The bridge's `/v1/realtime` WebSocket is a project-owned LAN protocol, not the
App Server transport. A capable client sends PCM/control messages to the bridge;
the bridge relays them through its internal WebRTC peer and returns transcripts
and PCM output. This separates the ThirdReality firmware protocol from the
version-coupled Codex protocol.

Stock ThirdReality firmware can use only the standard Assist path. Full duplex,
continuous listening, and barge-in require the firmware audio-stream extension
described in [the ThirdReality plan](../thirdreality-codex-realtime-plan.md).

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
