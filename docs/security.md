# Security model

Codex Voice keeps Home Assistant access and ChatGPT authentication on opposite
sides of a narrow bridge API.

## Trust boundaries

- Home Assistant stores only the bridge URL and a dedicated bearer token.
- The bridge delegates authentication, storage, and refresh to the installed
  Codex CLI. It locates an existing file-backed `auth.json` but does not parse,
  copy, log, or return it. The credential is linked into a mode-0700 temporary
  Codex home, and file-backed CLI authentication makes refreshes update the
  source credential through that link.
- The bridge never receives a Home Assistant long-lived access token.
- Home Assistant prepares the selected LLM tools, validates tool arguments,
  executes the calls, and sends only their results back to the bridge.
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
  Server can delete them immediately with `thread/delete`. One-shot STT, TTS,
  and realtime threads are deleted when their session ends; reusable
  Conversation threads are deleted when retired, evicted, or the bridge shuts
  down.

The authentication source is resolved from `HA_CODEX_AUTH_FILE`,
`${CODEX_HOME}/auth.json`, or `${HOME}/.codex/auth.json`. The bridge fails
closed if it cannot find a secure, file-backed credential. It does not copy an
OAuth secret into Home Assistant or the repository, accept a ChatGPT token in
its API, or silently fall back to an OpenAI Platform API key.

Treat voice input as untrusted input. A spoken instruction must not bypass Home
Assistant's exposed-entity policy or Codex's permission boundary.

## Network deployment

The bearer-token protocol is suitable for a private, trusted LAN when firewall
rules restrict the bridge port to the Home Assistant host. Use HTTPS/WSS through
a reverse proxy across any shared or untrusted network. Do not expose port 8787
directly to the internet.

Generate a unique high-entropy token for this service. Do not reuse a Home
Assistant token, Codex credential, GitHub token, or password. Keep it in a
root- or service-user-readable environment file rather than a shell history or
repository file.

## Diagnostics and logs

Component diagnostics redact access tokens, credentials, prompts, instructions,
email addresses, and nested fields whose names imply secrets. The bridge does
not log audio payloads, transcripts, SDP, authorization headers, or raw account
objects at normal log levels.

## Experimental subscription transport

Subscription audio uses an under-development Codex App Server WebRTC interface
and consumes ChatGPT subscription availability rather than OpenAI Platform API
quota. It must never silently fall back to an OpenAI Platform API key. Upgrade
Codex only after running the local contract tests and the opt-in WebRTC probe.

Realtime transcription and speech rendering are behaviors of a conversational
voice model. They are not the separately billed Speech-to-Text and Text-to-
Speech APIs. In particular, spoken output may paraphrase supplied text. Do not
use it for safety-critical, compliance-sensitive, or legally exact messages.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md) for the private reporting process. Never put
tokens, SDP offers, transcripts, recorded audio, or Home Assistant diagnostics
containing personal data into a public issue.
