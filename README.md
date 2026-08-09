# Codex Voice for Home Assistant

Codex Voice is an unofficial Home Assistant integration that exposes a
Conversation agent, text-to-speech, an experimental speech-to-text adapter,
and an experimental full-duplex audio transport backed by a user's existing
ChatGPT/Codex login. The reliable Assist configuration composes those providers
with local Whisper STT through Home Assistant's native Wyoming integration.

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
  -> Home Assistant pipeline
     -> Wyoming faster-whisper STT (local)
     -> Codex Voice Conversation (ChatGPT OAuth)
     -> Codex Voice TTS (experimental subscription realtime)
  -> speaker
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
External Wyoming service templates and smoke scripts are repository assets,
not part of the component-only HACS ZIP.

## Status

- Milestone 1: a standard Home Assistant pipeline using native Wyoming local
  STT with Codex Voice Conversation and TTS entities.
- New installations do not create the experimental Codex STT subentry by
  default. Existing subentries remain available for explicit diagnostics.
- Milestone 1 TTS progressively delivers realtime speech frames instead of
  waiting for the entire rendered response and remote cleanup.
- Newly created Conversation profiles default to low reasoning effort and App
  Server's configurable `priority` tier; upgraded profiles preserve standard
  usage until reconfigured, and `standard` remains available when lower
  subscription usage matters more than latency.
- Automatic experimental STT-to-TTS session reuse is disabled: current
  realtime v3 sessions can begin assistant output before finite transcription
  completes.
- Milestone 2 foundation: an experimental bridge-side realtime duplex-audio
  proxy. A ThirdReality duplex client, playback interruption, acoustic echo
  cancellation, and end-to-end barge-in are still pending; this transport is
  not yet a user-facing realtime voice mode.
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

### 3. Add reliable local STT

Install the official Whisper app on Home Assistant OS, or run the official
`wyoming-faster-whisper` server on a trusted host. Add it through Home
Assistant's **Wyoming Protocol** integration. See [reliable local
speech-to-text](docs/local-stt.md) for the tested external service and systemd
configuration.

### 4. Install through HACS

Until the repository is accepted into HACS defaults:

1. Open HACS and choose **Custom repositories**.
2. Add `https://github.com/AurelioB/ha-codex-voice` as an **Integration**.
3. Download **Codex Voice** and restart Home Assistant.
4. Add the Codex Voice integration.
5. Enter the bridge URL, such as `http://192.168.8.10:8787`, and the separate
   bridge token.

The config flow creates stable Conversation and TTS subentries. In an Assist
pipeline select the Wyoming faster-whisper entity for STT, Codex Voice for
Conversation, and Codex Voice for TTS.

For the lowest latency on ordinary device-control commands, enable **Prefer
handling commands locally** on that pipeline. Home Assistant will handle
matching built-in intents locally and retain Codex Voice as the fallback for
open-ended conversation.

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
clients. This is a bridge protocol primitive, not a Home Assistant entity or a
finished ThirdReality client:

- `start`: create an audio-output Codex realtime session, selecting `model`,
  `voice`, and optional session `prompt`.
- `audio`: append base64 PCM16 audio with sample rate and channel metadata.
- `text`: append a role-bearing text item.
- `speech`: append text intended to be spoken.
- `stop`: close the realtime session.

The standard TTS entity uses the authenticated
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
answer. Subscription authentication has been verified with realtime v3 on
Codex 0.146.0. App Server's raw realtime WebSocket route is deliberately not
used because it requires API-key authentication in that release.

## Performance

On the measured i5-13600K host, a warm multilingual faster-whisper `base`
service transcribed the same non-sensitive reference WAV three times in 0.775,
0.617, and 0.599 seconds. The prior subscription-backed adapter timed out after
roughly 19 seconds in an observed physical run and also intermittently missed
clean input. These are small diagnostic samples, not latency guarantees.

A later physical ThirdReality canary completed local recognition 0.497 seconds
after VAD ended and reached its Conversation/TTS result 7.703 seconds after
pipeline start, including capture. The answer played and the satellite returned
to idle.

Streaming Codex TTS exposed first PCM 4.222 seconds before the equivalent
finite bridge response in one earlier probe.

A controlled acoustic A/B on the same ThirdReality device had reduced the old
adapter's STT completion from 9.519 to 6.138 seconds, but that tuning could not
remove the backend's missing-transcript failure mode. It is retained only as
historical diagnostic evidence.

Automatic STT-to-TTS session reuse is deliberately disabled in the bundled
component. Live v3 validation observed genuine assistant output before the
user transcript completed, and the tagged Frameless Bidi client protocol has
no supported response-cancel message. Both the component and bridge therefore
disable ticket issuance; a handoff-shaped diagnostic request is validated but
takes the isolated cold path. The production Assist path always cold-starts TTS
in a fresh thread and session.

Always-on remote prewarming is not enabled: there is no provider hook before a
wake word, an idle WebRTC peer still sends silent RTP, App Server does not
document idle sessions as quota-neutral, and speculative sessions would occupy
the single speech lane. See [performance and ThirdReality
tuning](docs/performance.md) for measurement scope, handoff privacy and
fallback behavior, safe device settings, and the firmware canary decision.

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
4. If it was installed only for this pipeline, remove the Wyoming integration
   and stop its faster-whisper service separately.

## Known limitations

- Subscription-backed audio depends on an experimental Codex realtime
  conversation feature and consumes ChatGPT subscription availability, not
  OpenAI Platform API quota. It is not the stable OpenAI Audio API.
- The reliable pipeline's STT is local faster-whisper, not OpenAI-hosted and
  does not consume ChatGPT quota. ChatGPT subscription OAuth does not expose a
  supported standalone transcription endpoint. The retained Codex STT adapter
  can connect successfully without producing a transcript.
- TTS is a bounded realtime-conversation compatibility session, not OpenAI's
  separately billed `/v1/audio/*` endpoint. The live voice model may paraphrase
  or expand text instead of reading it verbatim. Do not use this TTS entity for
  safety-critical or legally exact announcements.
- The Codex subscription realtime surface is admitted one speech session at a
  time. Overlapping experimental Codex STT, TTS, or duplex requests fail
  immediately as busy so a caller can retry, rather than occupying the bridge
  until a timeout. Wyoming STT does not use that lane.
- HACS cannot install the bridge process. Run it separately or use the future
  add-on/container packaging.
- ThirdReality hardware can use milestone 1 through its existing Home
  Assistant Assist satellite entity. Direct full-duplex firmware transport
  depends on hardware/firmware support for the bridge's authenticated PCM
  WebSocket protocol.
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
