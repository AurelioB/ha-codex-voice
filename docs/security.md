# Security model

Codex Voice keeps Home Assistant access and ChatGPT authentication on opposite
sides of a narrow bridge API.

## Trust boundaries

- Home Assistant stores only the bridge URL and a dedicated bearer token.
- The bridge delegates authentication, storage, and refresh to the installed
  Codex CLI. It never reads, returns, imports, or copies Codex OAuth files.
- The bridge never receives a Home Assistant long-lived access token.
- Home Assistant prepares the selected LLM tools, validates tool arguments,
  executes the calls, and sends only their results back to the bridge.
- Every Codex thread starts in an empty directory with a named permission
  profile that exposes only Codex's minimal runtime paths. Shell, web, plugins,
  apps, MCP servers, hooks, and inherited command environment variables are
  disabled. `approvalPolicy: never` applies, and server-initiated approval or
  permission requests are rejected.

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

Subscription audio uses an under-development Codex App Server WebRTC interface.
It must never silently fall back to an OpenAI Platform API key. Upgrade Codex
only after running the local contract tests and the opt-in WebRTC probe.

Realtime transcription and speech rendering are behaviors of a conversational
voice model. They are not the separately billed Speech-to-Text and Text-to-
Speech APIs. In particular, spoken output may paraphrase supplied text. Do not
use it for safety-critical, compliance-sensitive, or legally exact messages.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md) for the private reporting process. Never put
tokens, SDP offers, transcripts, recorded audio, or Home Assistant diagnostics
containing personal data into a public issue.
