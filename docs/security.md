# Security model

Codex Voice keeps Home Assistant access and ChatGPT authentication on opposite
sides of a narrow bridge API.

## Trust boundaries

- Home Assistant stores only the bridge URL and a dedicated bearer token.
- A ThirdReality realtime endpoint stores a separate route-scoped bearer in a
  root-owned, mode-0600 device file. When configured, that token works only on
  `/v1/realtime` after strict v2 negotiation; it cannot enter legacy v1 or call
  health, Conversation, STT, TTS, or the Home Assistant tool-authority route.
- The bridge delegates authentication, storage, and refresh to the installed
  Codex CLI. It locates an existing file-backed `auth.json` but does not parse,
  copy, log, or return it. The credential is linked into a mode-0700 temporary
  Codex home, and file-backed CLI authentication makes refreshes update the
  source credential through that link.
- The bridge never receives a Home Assistant long-lived access token.
- Home Assistant prepares the selected LLM tools, validates tool arguments,
  executes the calls, and sends only their results back to the bridge.
- The current ThirdReality client explicitly requests
  `conversation_mode: "native"`. The bridge ignores any Home Assistant broker
  snapshot for that session and creates one tool-free native App Server WebRTC
  voice thread. Okay Computer therefore has no Home Assistant control
  authority; Okay Nabu retains the official Assist/tool route.
- The bridge echoes an accepted explicit mode in `started`; the reference
  client requires `conversation_mode: "native"` and fails closed if the echo is
  absent or different. Native audio never crosses the legacy completed-
  transcript, executor, or `thread/realtime/appendSpeech` boundaries.
- Legacy automatic realtime tool authority never comes from a device message.
  Exactly one explicitly opted-in Conversation subentry may
  open `/v1/home-assistant/tools` with the primary bridge token. It registers
  an immutable, generation-scoped snapshot of that subentry's rendered
  instructions, `es-MX`-by-default locale, and selected LLM API tools. Zero or
  ambiguous authorities register nothing.
- Tool registrations, schemas, arguments, results, messages, pending calls,
  and calls per realtime session are bounded. Home Assistant and the bridge
  independently reject undeclared tools, duplicate or stale correlation,
  oversized/non-JSON values, replacement, disconnect, and timeout. An unknown
  outcome is never retried implicitly.
- Only a strict wire-v2 request that **omits** `conversation_mode`, uses App
  Server v3, and captures a broker snapshot enters the compatibility managed
  two-thread route. Its provider-facing speech frontend is
  created with no tools; a separate executor thread alone receives the
  immutable Home Assistant instructions and tool view. Other realtime modes
  retain their native behavior. Codex CLI 0.147.0 or newer is required for the
  managed route's `clientManagedHandoffs: true` and
  `delegationAckFiller: false` controls.
- The frontend cannot be trusted to enforce isolation from instructions alone:
  App Server v3 may route native delegation before notifying the bridge. A
  frontend, foreign, stale, or post-interrupt tool request is therefore
  answered with `unowned_home_assistant_tool_call` and `do_not_retry: true`
  without crossing the Home Assistant authority boundary.
- Conversation start and tool-result events use Home Assistant's canonical JSON
  serializer. Nested temporal values are normalized to ISO text; unsupported
  values are rejected before transmission with an error that does not include
  their representation or contents.
- Every Codex thread starts in an empty directory with a named permission
  profile that exposes only Codex's minimal runtime paths. Shell, web, plugins,
  apps, MCP servers, hooks, and inherited command environment variables are
  disabled. `approvalPolicy: never` applies, and server-initiated approval or
  permission requests are rejected.
- App Server receives private temporary `HOME` and `CODEX_HOME` directories,
  not the user's normal Codex profile. Only the managed authentication file is
  linked in, so normal history, configuration, apps, plugins, and discovered
  MCP servers cannot enter a voice session.
- Startup audits App Server's effective configuration layers and fails closed
  if a local or managed layer still configures an MCP server.
- Threads are deliberately non-ephemeral inside that isolated home so App
  Server can delete them immediately with `thread/delete`. Experimental Codex
  STT, TTS, and realtime threads are deleted when their session ends. A native
  device session owns one realtime thread. A legacy managed session owns two
  threads, and cleanup independently deletes
  both its tool-free frontend and its executor. Reusable Conversation threads
  are deleted when retired, evicted, or the bridge shuts down.
- Production STT audio travels only from Home Assistant to the selected local
  Wyoming faster-whisper service. It does not enter Codex App Server or consume
  ChatGPT/OpenAI quota.
- After Codex returns the Conversation response, Home Assistant sends that text
  only to the selected local Wyoming Piper service for synthesis. Piper does
  not receive the ChatGPT credential, bridge bearer, microphone audio, or Home
  Assistant access token, and synthesis does not consume the subscription
  speech lane.
- Device-facing realtime v2 remains audio/control only. It rejects
  device-declared tools and device tool results, and never exposes provider
  tool calls or Home Assistant results to the speaker. Explicit native mode
  ignores a separately registered authority. In the legacy automatic managed
  route, the bridge sends provider calls only to the captured Home Assistant
  generation; Home Assistant's selected LLM API retains exposed-entity policy
  and execution authority.
- In the legacy managed path, only a session-unique, identified raw v3 user
  turn or bounded v2 user `text` control can start an executor turn. Only the completed
  executor final is returned through a single, at-most-500-byte
  `thread/realtime/appendSpeech`; frontend PCM is dropped until both
  `session.context.appended` and a session-unique assistant turn identify and
  arm that render for the current bridge generation. Replayed, role-conflicting,
  unsolicited, direct, or stale frontend events cannot open command or audio
  authority.
- Managed executor turns have an absolute completion deadline. Failed or
  missing terminals close the device session with a content-free error. During
  teardown the active turn is tombstoned and interrupted before its event
  consumer closes, and provider/thread cleanup remains tracked and shielded
  from request-handler cancellation.
- “Okay Computer” selects explicit native v2 and gains no Home Assistant
  authority. “Okay Nabu” selects the official Assist path;
  a normal wake can preempt a direct session and reclaim the microphone.
- The device retains at most six idle microphone frames for the direct wake:
  384 ms, or 12 KiB of PCM16, in process memory only. Okay Computer atomically
  consumes it; Okay Nabu, stop, mute, disconnect, and teardown discard it. It
  is never written to configuration, disk, diagnostics, or logs, remains inside
  existing queue bounds, and is trimmed or omitted to preserve 32 KiB of live
  post-wake capacity.
- The ThirdReality client is standard-library code imported into the existing
  root voice process. Exact vendor bytecode guards fail closed before patching.
  Its JSON configuration must be a root-owned, non-symlink regular file with
  mode 0600; source directories/files must not be writable by group or other.
- Full duplex is off by default and fails closed without a reviewed static
  PulseAudio `module-echo-cancel` block using the exact configured allowlisted
  AEC engine, exact raw masters and default AEC routes, and the current voice
  process's capture stream routed through the AEC source. The allowlist is
  WebRTC, Speex, and Adrian. WebRTC is the omitted-value default; the installer
  and client never probe or automatically downgrade to another engine. Stock
  v1.1.7 must explicitly select Adrian because its module rejects the uncompiled
  WebRTC and Speex engines. The client checks the exact method before opening
  the bridge socket, enforces a configured 1–60% sink ceiling with a safe 25%
  default, rechecks every
  sink channel before each response, and starts `paplay` on that sink with a
  fixed stream volume no greater than the ceiling. The guard compares raw
  PulseAudio units to the exact linear ceiling rather than trusting rounded
  display percentages. The installer writes the matching raw setpoint in the
  static startup block immediately after sink creation. The stock voice process
  later applies its persistent Home Assistant media-player preference, which
  must match; deferred PulseAudio restore state alone is not trusted across
  reboot. A successfully loaded Adrian topology still requires a
  physical double-talk canary on each installation at its configured sink and
  stream values; the reference device's bounded 25% pass is not transferable
  evidence for another device or for an increase up to the explicit 60% maximum.
- Local playback flush or provider VAD is not evidence of remote cancellation.
  On the current native v2 path, resume still requires a sanitized provider
  `response.cancelled` event correlated to the exact active response. On the
  legacy broker-managed path, a new utterance invalidates the bridge generation
  and best-effort cancellation cannot reopen the local output gate. Before Home
  Assistant tool dispatch, the bridge tombstones and interrupts the executor;
  after dispatch it lets the potentially side-effecting call settle without
  cancellation or replay, suppresses the stale final, and queues the newest
  request.
- Legacy managed same-socket continuation is enabled only for the exact
  negotiated `User-Agent: ha-codex-voice-thirdreality/2`. Its acknowledgement sets
  `fresh_session_required: false`, `remote_cancelled: false`, and
  `continuation_safe: true`; safety refers to bridge-owned generation
  invalidation, not confirmed provider cancellation. Older clients receive the
  fresh-session fallback and the socket is closed.

The authentication source is resolved from `HA_CODEX_AUTH_FILE`,
`${CODEX_HOME}/auth.json`, or `${HOME}/.codex/auth.json`. The bridge fails
closed if it cannot find a secure, file-backed credential. It does not copy an
OAuth secret into Home Assistant or the repository, accept a ChatGPT token in
its API, or silently fall back to an OpenAI Platform API key.

Treat voice input as untrusted input. A spoken instruction must not bypass Home
Assistant's exposed-entity policy or Codex's permission boundary.

## Network deployment

The bearer-token protocol is suitable for a private, trusted LAN when firewall
rules restrict the bridge port to the Home Assistant host and intended voice
endpoints. Use HTTPS/WSS through a reverse proxy across any shared or untrusted
network. Do not expose port 8787 directly to the internet.

Wyoming TCP has no application bearer authentication. Bind faster-whisper port
`10300` and Piper port `10200` only to explicit addresses Home Assistant can
reach, then restrict both ports to the Home Assistant host. Never expose either
service to the internet or an untrusted network. The supplied external Piper
example defaults to loopback and must be deliberately changed for a separate
Home Assistant host.

Generate unique high-entropy primary and realtime-device tokens. They must
differ. Do not reuse a Home Assistant token, Codex credential, GitHub token, or
password. Keep host tokens in a service-user-readable environment file and the
device token in its root-only configuration, never shell history or the
repository. Neither bridge token enters the App Server child environment.
Home Assistant uses the primary token for both its normal component API and
outbound tool-authority socket; the device token cannot broker tools even when
realtime authority is enabled.

The release archive contains only a disabled example with a placeholder token.
Never package a populated `codex-realtime.json`. The device URL accepts only
`ws` or `wss`; use verified WSS outside a source-restricted trusted LAN. The
separate numeric `connect_address` prevents resolver changes from silently
moving the connection to another host, while the URL host remains the HTTP
Host and TLS server name.

On the measured ThirdReality deployment, unauthenticated root ADB on TCP port
5555 is an explicitly preserved recovery path. The overlay, deployment, and
rollback procedures never stop `adbd` or clear its port setting, and acceptance
checks connectivity after restart and reboot. Because the service is still a
high-risk root interface, network policy must restrict port 5555 to the
designated administration host or management network and block internet and
general-LAN access.

## Diagnostics and logs

Component diagnostics redact access tokens, credentials, prompts, instructions,
email addresses, and nested fields whose names imply secrets. The bridge does
not log audio payloads, transcripts, SDP, authorization headers, or raw account
objects at normal log levels. Conversation serialization failures report only
that an unsupported JSON value was present; they do not interpolate that
value's potentially sensitive representation.

Realtime event-shape tracing records only deduplicated source/event types,
allowlisted item types, and coarse role/target labels. It omits transcript text,
agent deltas, prompts, tool names, arguments, results, raw provider payloads,
SDP, audio, and provider identifiers.

Upstream `wyoming-faster-whisper` 3.5.0 logs recognized text at INFO. The
supplied systemd unit deliberately starts it through the privacy runner, which
raises only `wyoming_faster_whisper.dispatch_handler` to WARNING. Do not replace
that runner with the package's direct console entry point unless transcript
logging is explicitly acceptable for the deployment.

Treat text sent to Piper as potentially sensitive even though it remains on the
local Wyoming path. Review the selected runtime's logs before deployment and do
not enable debug logging or retain synthesized test content unless that is an
explicit operational choice.

The external Piper path pins the Mexican-Spanish model to an immutable upstream
revision and verifies exact sizes and SHA-256 digests before atomic
installation. The service re-verifies the files at every start, exposes the
model directory read-only, advertises only the locked voice, and refuses
requests that would download a different model.

## Experimental subscription transport

Subscription audio uses an under-development Codex App Server WebRTC interface
and consumes ChatGPT subscription availability rather than OpenAI Platform API
quota. It is not the documented OpenAI Realtime API, has no project-level
latency or availability guarantee, and can change with Codex CLI/App Server.
It must never silently fall back to an OpenAI Platform API key. The
supported minimum is Codex CLI 0.147.0 because managed realtime disables
delegation acknowledgement filler explicitly. Upgrade Codex only after running
the local contract tests and the opt-in WebRTC probe.

Experimental Codex transcription and speech rendering are behaviors of a
conversational voice model. They are not the separately billed Speech-to-Text
and Text-to-Speech APIs. A realtime input session may return no transcript, and
spoken output may paraphrase supplied text. Production STT therefore uses local
Wyoming faster-whisper, and recommended production TTS uses local Wyoming
Piper. Do not use experimental Codex speech output for safety-critical,
compliance-sensitive, or legally exact messages.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md) for the private reporting process. Never put
tokens, SDP offers, transcripts, recorded audio, or Home Assistant diagnostics
containing personal data into a public issue.
