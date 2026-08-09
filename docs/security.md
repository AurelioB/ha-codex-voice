# Security model

Codex Voice keeps Home Assistant access and ChatGPT authentication on opposite
sides of a narrow bridge API.

## Trust boundaries

- Home Assistant stores only the bridge URL and a dedicated bearer token.
- A ThirdReality realtime endpoint stores a separate route-scoped bearer in a
  root-owned, mode-0600 device file. When configured, that token works only on
  `/v1/realtime` after strict v2 negotiation; it cannot enter legacy v1 or call
  health, Conversation, STT, or TTS routes.
- The bridge delegates authentication, storage, and refresh to the installed
  Codex CLI. It locates an existing file-backed `auth.json` but does not parse,
  copy, log, or return it. The credential is linked into a mode-0700 temporary
  Codex home, and file-backed CLI authentication makes refreshes update the
  source credential through that link.
- The bridge never receives a Home Assistant long-lived access token.
- Home Assistant prepares the selected LLM tools, validates tool arguments,
  executes the calls, and sends only their results back to the bridge.
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
  STT, TTS, and realtime threads are deleted when their session ends; reusable
  Conversation threads are deleted when retired, evicted, or the bridge shuts
  down.
- Production STT audio travels only from Home Assistant to the selected local
  Wyoming faster-whisper service. It does not enter Codex App Server or consume
  ChatGPT/OpenAI quota.
- Device-facing realtime v2 is chat-only. It rejects device-declared tools and
  tool results and never forwards provider tool calls. Home control remains on
  the normal Home Assistant pipeline, where `ChatLog` and the selected LLM API
  enforce exposed-entity policy.
- “Okay Computer” selects that chat-only v2 route. “Okay Nabu” selects the
  official Assist path; a normal wake preempts a direct session rather than
  lending its weaker device credential any Home Assistant authority.
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

Wyoming TCP has no application bearer authentication. Bind port 10300 to an
explicit trusted-LAN address and restrict it to the Home Assistant host. Never
expose it to the internet or an untrusted network.

Generate unique high-entropy primary and realtime-device tokens. They must
differ. Do not reuse a Home Assistant token, Codex credential, GitHub token, or
password. Keep host tokens in a service-user-readable environment file and the
device token in its root-only configuration, never shell history or the
repository. Neither bridge token enters the App Server child environment.

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

Upstream `wyoming-faster-whisper` 3.5.0 logs recognized text at INFO. The
supplied systemd unit deliberately starts it through the privacy runner, which
raises only `wyoming_faster_whisper.dispatch_handler` to WARNING. Do not replace
that runner with the package's direct console entry point unless transcript
logging is explicitly acceptable for the deployment.

## Experimental subscription transport

Subscription audio uses an under-development Codex App Server WebRTC interface
and consumes ChatGPT subscription availability rather than OpenAI Platform API
quota. It must never silently fall back to an OpenAI Platform API key. Upgrade
Codex only after running the local contract tests and the opt-in WebRTC probe.

Experimental Codex transcription and speech rendering are behaviors of a
conversational voice model. They are not the separately billed Speech-to-Text
and Text-to-Speech APIs. A realtime input session may return no transcript, and
spoken output may paraphrase supplied text. Production STT therefore uses local
Wyoming faster-whisper. Do not use experimental speech output for
safety-critical, compliance-sensitive, or legally exact messages.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md) for the private reporting process. Never put
tokens, SDP offers, transcripts, recorded audio, or Home Assistant diagnostics
containing personal data into a public issue.
