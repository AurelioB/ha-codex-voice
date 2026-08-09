# Codex Voice bridge

This process exposes a small bearer-authenticated HTTP/WebSocket API to the
Home Assistant custom component and owns a local `codex app-server` child. The
child uses the machine's existing file-backed Codex/ChatGPT login through a
private temporary Codex home; no OAuth token is copied into Home Assistant.

## Run

Python 3.11 or newer, Codex CLI 0.146 or newer, and a working file-backed
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
negotiates strict v2. A device-token v1 or malformed negotiation is rejected
before provider/thread startup, and the device token is rejected by every
other route. This lets a speaker use content-private realtime audio without
storing the Home Assistant/component credential.

## API

- `GET /health`
- `GET /v1/conversation` WebSocket: `start`, streamed `delta`, `tool_call`,
  `tool_result`, and `done` messages. Stable Home Assistant `conversation_id`
  values reuse a bounded in-memory Codex thread for multi-turn context. A
  start may select `service_tier` as `standard` or `priority`; priority targets
  lower latency while increasing subscription usage.
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
- `GET /v1/realtime` WebSocket: legacy v1 JSON/base64 messages or the strict
  device-facing v2 binary PCM16 transport. V2 emits content-free lifecycle
  controls, filters continuous WebRTC silence, gates output with monotonic
  epochs, and supports local-flush/fresh-session interruption. It does not
  expose transcripts, provider payloads, or tools. See the
  [v2 wire contract](../protocol/realtime-wire-v2.md).

Device-facing v2 is the transport used by the pinned ThirdReality v1.1.7
in-process client. “Okay Computer” enters this direct chat-only mode; “Okay
Nabu” remains on Home Assistant's official Assist flow for exposed-entity
control. The device sends 16 kHz mono PCM16 and receives 24 kHz mono PCM16. The
bridge applies a 2,250 ms input-track limit only to v2 live sessions; finite STT
keeps its existing whole-utterance input capacity.

The reference device keeps at most 64 KiB (2.048 s) of startup/fallback input,
drains a post-handshake backlog at no more than 2× capture rate, and keeps at
most 48 KiB (about 1.024 s) queued for playback. Provider output also crosses a
bounded bridge queue. A bound violation is terminal or triggers the documented
pre-ready Home Assistant fallback; audio is never silently discarded to make
latency metrics look better. Catch-up prevents a permanent handshake-sized
offset once v2 becomes ready, but it cannot make the cold App Server/WebRTC
handshake or provider response generation disappear.

The released device mode is turn-taking. It gates microphone input while local
output remains potentially audible and does not configure acoustic echo
cancellation. `interrupt` flushes local output, closes the socket and remote
resource, and requires a fresh session; the negotiated capability remains
`remote_cancel: false` because the provider transport has no reliable response
truncate operation.

Audio uses Codex app-server's experimental WebRTC v3 path by default. The peer
creates a real paced outbound audio track and `oai-events` data channel before
the SDP offer; app-server supplies the remote SDP answer and transcript
notifications. This is the subscription-compatible path. Raw Realtime v2
WebSockets require API-key authentication and are intentionally not used.

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
relayed to the official Conversation WebSocket or a legacy v1 realtime client.
Device-facing realtime v2 is chat-only; the normal Home Assistant Assist path
retains home-control authority.
