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

If `CODEX_APP_SERVER_COMMAND` is overridden, it must enable the experimental
`realtime_conversation` feature. The bridge still injects and verifies the
profile selected by `HA_CODEX_PERMISSION_PROFILE` (default
`ha-voice-minimal`) on every thread. Do not weaken the inline profile or
replace it with legacy `read-only`, which permits broad host reads.

All routes, including `GET /health`, require
`Authorization: Bearer <HA_CODEX_BRIDGE_TOKEN>`.

## API

- `GET /health`
- `GET /v1/conversation` WebSocket: `start`, streamed `delta`, `tool_call`,
  `tool_result`, and `done` messages. Stable Home Assistant `conversation_id`
  values reuse a bounded in-memory Codex thread for multi-turn context.
- `POST /v1/transcribe`: up to 60 seconds of base64 PCM16/WAV plus audio
  metadata; returns JSON `{ "text": "..." }` under a bounded end-to-end
  deadline.
- `GET /v1/transcribe/stream` WebSocket: a validated v1 start object, bounded
  binary PCM16 frames, and explicit `end`/`cancel` control. It returns one
  transcript result and lets bridge setup overlap Home Assistant capture.
- `POST /v1/synthesize`: text, voice, and language; returns mono 24 kHz WAV.
- `POST /v1/synthesize/stream`: the same request contract, returned as a
  progressively delivered mono 24 kHz PCM16 WAV stream.
- `POST /v1/speech-session/release`: idempotently release a private,
  unconsumed STT-to-TTS handoff ticket.
- `GET /v1/realtime` WebSocket: full-duplex PCM16 `audio`, `text`, `speech`,
  transcripts, tool calls, and stop messages.

Audio uses Codex app-server's experimental WebRTC v3 path by default. The peer
creates a real paced outbound audio track and `oai-events` data channel before
the SDP offer; app-server supplies the remote SDP answer and transcript
notifications. This is the subscription-compatible path. Raw Realtime v2
WebSockets require API-key authentication and are intentionally not used.

The subscription realtime voice is conversational, not a verbatim
text-to-speech API. `/v1/synthesize` sends a tightly constrained text turn but
still provides best-effort conversational speech and returns the header
`X-Codex-Synthesis-Mode: conversational-best-effort`; callers must not assume
that spoken wording exactly matches the input.

## Streaming STT and one-time session handoff

The component opens `/v1/transcribe/stream` before it reads Home Assistant's
microphone iterator. Once the bridge validates the start object, it starts the
Codex thread and realtime WebRTC handshake concurrently with capture. The
bridge still waits for explicit capture EOF before normalizing and feeding the
finite utterance. This overlaps setup without sending partially normalized
audio or changing Home Assistant's finite STT semantics.

The stream start object may opt into the private handoff protocol with the
following field. The bundled component adds it only when the official Assist
pipeline prepared its TTS result before STT in the same `ChatSession`; other
clients can omit it.

```json
{
  "speech_session_handoff": {
    "version": 1,
    "voice": "cove",
    "language": "en-US"
  }
}
```

On eligible realtime v3 success, the result additionally contains a versioned
random 256-bit ticket, its voice, normalized language, and `expires_in_ms:
30000`. The handoff language must normalize to the same tag as the outer
transcription metadata. A compatible `/v1/synthesize` or
`/v1/synthesize/stream` request may present the ticket once as
`speech_session_handoff_token` with that same language. The bridge then uses
`appendSpeech` on the sanitized STT session instead of creating a second
thread and WebRTC session. Only one offer can be outstanding on the bridge's
single speech lane.

The bridge stores only the ticket's SHA-256 digest and never logs the raw
value. The bearer-authenticated request/response transport carries the ticket;
Home Assistant otherwise binds it in memory to the exact bridge client,
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
is capture overlap plus the explicitly correlated one-turn handoff. See
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
relayed to an attached WebSocket client.
