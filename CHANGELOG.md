# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use semantic versioning.

## [Unreleased]

## [0.1.1] - 2026-08-08

### Fixed

- Bound conversation completion waits, with ambiguous turns interrupted and retired.
- Reject overlapping turns instead of allowing an unbounded background queue.
- Apply one deadline and bounded event backlog to realtime handshakes.
- Fail transcription promptly when app-server exits and bound accepted audio to
  60 seconds under an end-to-end request deadline.

## [0.1.0] - 2026-08-08

### Added

- Home Assistant Conversation, speech-to-text, and text-to-speech entities.
- Home Assistant LLM tool forwarding with bounded multi-turn conversation reuse.
- Local bearer-authenticated bridge using Codex-managed ChatGPT OAuth.
- Subscription-backed WebRTC v3 speech adapters and an experimental realtime
  duplex-audio WebSocket.
- HACS packaging, diagnostics, security hardening, tests, and live smoke tools.
