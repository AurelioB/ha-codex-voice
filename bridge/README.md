# Codex Voice bridge

This process exposes a small bearer-authenticated HTTP/WebSocket API to the
Home Assistant custom component and owns a local `codex app-server` child. The
child inherits the machine's existing Codex/ChatGPT login; no OAuth token is
copied into Home Assistant.

## Run

Python 3.11 or newer, Codex CLI 0.146 or newer, and a working `codex login` are
required.

```bash
python -m venv .venv
.venv/bin/pip install -r bridge/requirements.txt
export HA_CODEX_BRIDGE_TOKEN="$(openssl rand -hex 32)"
.venv/bin/python -m bridge
```

The default listener is `127.0.0.1:8787`. Override it with
`HA_CODEX_BRIDGE_HOST` and `HA_CODEX_BRIDGE_PORT`. `CODEX_APP_SERVER_COMMAND`
can replace the default hardened app-server command. The bridge creates a
private empty runtime directory and starts all Codex threads with the named
`ha-voice-minimal` least-privilege permission profile and `approvalPolicy:
never`. The
default command grants only Codex's minimal runtime paths and disables shell,
web, plugins, apps, MCP servers, hooks, and inherited command environment
variables. Startup and every new thread fail closed unless that profile is
available and active.

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
- `POST /v1/transcribe`: base64 PCM16/WAV plus audio metadata; returns JSON
  `{ "text": "..." }`.
- `POST /v1/synthesize`: text, voice, and language; returns mono 24 kHz WAV.
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

Interactive approvals, permission requests, and unsupported server-initiated
requests fail closed. Only explicitly declared dynamic Home Assistant tools are
relayed to an attached WebSocket client.
