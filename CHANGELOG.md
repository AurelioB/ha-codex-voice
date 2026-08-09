# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use semantic versioning.

## [Unreleased]

## [0.1.8] - 2026-08-08

### Added

- Add observed-event boundary validation and a content-private live probe for
  the dormant STT-to-TTS handoff experiment.
- Document measured latency boundaries, ThirdReality tuning acceptance checks,
  firmware backup and rollback, and the requirement to preserve TCP ADB on the
  measured device.

### Changed

- Start the Codex thread and WebRTC handshake when streaming STT capture opens,
  overlapping remote setup with the finite Home Assistant microphone capture.
- Keep STT-to-TTS ticket issuance disabled in both the component and bridge
  after live v3 validation observed assistant output before finite
  transcription completed.

### Fixed

- Make retained-session expiry, replacement, cancellation, remote invalidation,
  component unload, and successful consumption converge on bounded, exactly-once
  session and thread cleanup.
- Invalidate dormant reuse on observed assistant audio or output at checked
  STT-to-TTS boundaries, including simultaneous receiver and claim races.
- Preserve a valid STT transcript when unexpected assistant output invalidates
  the reuse experiment, then close the unsafe session and use fresh TTS.

## [0.1.7] - 2026-08-08

### Added

- Stream mono 24 kHz PCM WAV audio from the bridge and Home Assistant TTS
  entity as realtime speech frames arrive, while retaining the finite
  synthesis endpoint for compatibility.
- Emit numeric-only, privacy-safe stage timings for disposable STT and TTS
  attempts so handshake, recognition, first-audio, tail, and cleanup latency
  can be measured without logging content or identifiers.

### Changed

- Remove redundant queued STT silence by default and finalize transcript
  fragments relative to meaningful audio rather than optional padding.
- Advertise WAV as the native TTS output while allowing Home Assistant to
  convert explicitly requested alternate formats.

## [0.1.6] - 2026-08-08

### Fixed

- Remove only confidently silent prefixes longer than two seconds before
  pacing finite STT audio over WebRTC, while retaining 320 milliseconds of
  pre-roll and preserving quiet or ambiguous early speech.
- Recompute transcription feed deadlines after trimming, preventing long
  post-command waits caused by replaying the satellite's leading silence in
  real time.

## [0.1.5] - 2026-08-08

### Fixed

- Normalize unusually quiet microphone audio with bounded, clipping-safe gain,
  allowing low-level ThirdReality captures to reach the realtime recognizer.
- Assemble realtime v3 user transcript fragments and finish finite STT input
  after the complete clip and a quiet period when no terminal event arrives.
- Treat a valid user transcript as authoritative when the remote recognizer
  stops pulling trailing silence, instead of hanging on a local input-drain
  marker that may never complete.
- Preserve transcript events while observing audio drain, recognize current v3
  handoff and delegation shapes, and reject assistant text as STT input.

## [0.1.4] - 2026-08-08

### Fixed

- Admit only one subscription-backed realtime speech session at a time and
  reject overlapping STT, TTS, or duplex requests immediately with HTTP 409,
  instead of allowing a losing WebRTC session to time out after 90 seconds.
- Retry transient WebRTC input-drain and missing-terminal-transcript stalls in
  fresh, fully cleaned sessions under one bounded transcription budget.
- Release speech-session admission after success, failure, timeout, or
  cancellation, and log privacy-safe audio level metrics when transcription
  reaches its deadline.

## [0.1.3] - 2026-08-08

### Fixed

- Run App Server in a mode-0700 temporary Codex home linked only to the managed
  file-backed ChatGPT login, keeping normal Codex history, configuration, apps,
  plugins, and MCP sidecars outside voice sessions.
- Persist threads only inside that private home and delete them when their
  bridge-managed lifetime ends, preventing finished Conversation, STT, TTS,
  and realtime threads from accumulating during App Server's idle-unload
  window.
- Audit effective configuration layers for injected MCP servers and accept
  bounded App Server JSON-RPC messages larger than asyncio's small default.

## [0.1.2] - 2026-08-08

### Fixed

- Restore the documented Python 3.11+ bridge compatibility and enforce it in
  CI with standalone bridge tests on the minimum supported Python version.

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
