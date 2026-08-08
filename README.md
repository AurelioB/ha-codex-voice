# Codex Voice for Home Assistant

Codex Voice is an unofficial Home Assistant integration that exposes a
Conversation agent, speech-to-text, text-to-speech, and an experimental
full-duplex audio transport backed by a user's existing ChatGPT/Codex login.

> [!WARNING]
> This project relies on experimental Codex App Server interfaces. It can
> break when Codex changes, is not an official OpenAI or Home Assistant
> integration, and is not a substitute for the stable, separately billed
> OpenAI API. ChatGPT plan availability, quotas, and usage policies still
> apply. The project never converts, exports, or presents OAuth credentials as
> an API key.

## Architecture

Home Assistant and the ChatGPT login are deliberately separated:

```text
Assist satellite
  -> Home Assistant pipeline (STT -> Conversation -> TTS)
  -> Codex Voice custom integration
  -> authenticated LAN connection
  -> Codex Voice bridge
  -> local `codex app-server`
  -> Codex-managed ChatGPT OAuth and subscription quota
```

HACS installs only `custom_components/codex_voice`. The bridge must run on a
machine with the Codex CLI and a valid, file-backed `codex login`. It locates
the existing `auth.json`, links that credential into a mode-0700 temporary
Codex home, and forces file-backed CLI authentication so Codex refreshes the
original credential through the link. Normal Codex history, configuration,
apps, plugins, and MCP servers are not imported into voice sessions. The
bridge sends Home Assistant only text/audio results; Home Assistant never
receives the ChatGPT credential. Entity exposure and tool execution remain
inside Home Assistant, so its user and Assist policies stay authoritative.

## Status

- Milestone 1: standard Home Assistant Conversation, STT, and TTS entities.
- Milestone 2: experimental realtime duplex-audio proxy with barge-in-ready
  session primitives.
- Target Home Assistant version: 2026.8.0 or newer.
- Target Codex CLI version: 0.146.0 or newer.

The standard Assist pipeline remains turn-based. Realtime mode is a separate
transport for clients that can maintain a duplex PCM stream; it is not exposed
as a pretend STT/TTS pipeline.

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

Create a long random bridge token and keep it private. From this repository:

```bash
export HA_CODEX_BRIDGE_TOKEN="replace-with-a-long-random-value"
export HA_CODEX_BRIDGE_HOST="0.0.0.0"
uv run --extra bridge python -m bridge
```

The default port is `8787`. Binding beyond loopback is appropriate only on a
trusted LAN with firewall rules limiting access to Home Assistant. Put the
bridge behind TLS when the path is not fully trusted.

### 3. Install through HACS

Until the repository is accepted into HACS defaults:

1. Open HACS and choose **Custom repositories**.
2. Add `https://github.com/AurelioB/ha-codex-voice` as an **Integration**.
3. Download **Codex Voice** and restart Home Assistant.
4. Add the Codex Voice integration.
5. Enter the bridge URL, such as `http://192.168.8.10:8787`, and the separate
   bridge token.

The config flow creates Conversation, STT, and TTS subentries. Select their
entities in an Assist pipeline.

## Home Assistant controls

When the Home Assistant Assist LLM API is enabled for the Conversation
subentry, the model receives only tools selected by Home Assistant. Tool calls
are returned to the integration, executed inside Home Assistant, and sent back
to the same Codex turn. The bridge does not need a Home Assistant long-lived
access token.

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
cache.

No OAuth secret is copied into Home Assistant or this repository. The isolated
home contains a link to the source credential so refreshes remain owned by the
Codex CLI on the bridge host.

Running the bridge as a dedicated OS user or container remains recommended as
defense in depth.

## Realtime protocol

`ws://BRIDGE/v1/realtime` accepts authenticated JSON messages from LAN
clients:

- `start`: create an audio-output Codex realtime session, selecting `model`,
  `voice`, and optional session `prompt`.
- `audio`: append base64 PCM16 audio with sample rate and channel metadata.
- `text`: append a role-bearing text item.
- `speech`: append text intended to be spoken.
- `stop`: close the realtime session.

The bridge converts that narrow WebSocket protocol to a genuine WebRTC peer:
an active audio track carries media and the `oai-events` data channel carries
control events. It sends the offer to Codex App Server and applies the returned
answer. Subscription authentication has been verified with realtime v3 on
Codex 0.146.0. App Server's raw realtime WebSocket route is deliberately not
used because it requires API-key authentication in that release.

## Development

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

## Known limitations

- Subscription-backed audio depends on an experimental Codex realtime
  conversation feature and consumes ChatGPT subscription availability, not
  OpenAI Platform API quota. It is not the stable OpenAI Audio API.
- Standalone STT and TTS are implemented as bounded realtime-conversation
  compatibility sessions, not OpenAI's separately billed `/v1/audio/*`
  endpoints. Transcription may differ from the Speech-to-Text API, and speech
  output is best-effort conversational rendering: the live voice model may
  paraphrase or expand text instead of reading it verbatim. Do not use this
  TTS entity for safety-critical or legally exact announcements.
- The Codex subscription realtime surface is admitted one speech session at a
  time. Overlapping STT, TTS, or duplex requests fail immediately as busy so a
  caller can retry, rather than occupying the bridge until a timeout.
- HACS cannot install the bridge process. Run it separately or use the future
  add-on/container packaging.
- ThirdReality hardware can use milestone 1 through its existing Home
  Assistant Assist satellite entity. Direct full-duplex firmware transport
  depends on hardware/firmware support for the bridge's authenticated PCM
  WebSocket protocol.
- ThirdReality firmware 1.01.07 does not forward microphone audio until its
  wake confirmation sound has completely finished. The bundled sound has a
  short silent tail, so speech begun immediately after the audible tone can be
  lost; wait about 0.3 seconds after the tone. Shortening that gap or raising
  the device's hardware microphone gain requires a device-side configuration
  or firmware change and cannot be controlled by this Home Assistant
  integration.

## License

MIT. See [LICENSE](LICENSE).
