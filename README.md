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
machine with the Codex CLI and a valid `codex login`; it owns all ChatGPT
credentials and sends Home Assistant only text/audio results. The Home
Assistant integration keeps entity exposure and tool execution inside Home
Assistant, so its user and Assist policies remain authoritative.

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
integration.

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
  conversation feature, not the stable OpenAI Audio API.
- Standalone STT and TTS are implemented as bounded realtime-conversation
  compatibility sessions, not OpenAI's separately billed `/v1/audio/*`
  endpoints. Transcription may differ from the Speech-to-Text API, and speech
  output is best-effort conversational rendering: the live voice model may
  paraphrase or expand text instead of reading it verbatim. Do not use this
  TTS entity for safety-critical or legally exact announcements.
- HACS cannot install the bridge process. Run it separately or use the future
  add-on/container packaging.
- ThirdReality hardware can use milestone 1 through its existing Home
  Assistant Assist satellite entity. Direct full-duplex firmware transport
  depends on hardware/firmware support for the bridge's authenticated PCM
  WebSocket protocol.

## License

MIT. See [LICENSE](LICENSE).
