# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use semantic versioning.

## [Unreleased]

## [0.2.0] - 2026-08-09

### Added

- Add a hardened user-service template and deployment guide for the official
  Wyoming faster-whisper server, keeping the multilingual `base` model warm for
  Home Assistant's native streaming STT provider while suppressing the
  upstream INFO-level transcript log. The validated dependency stack and model
  revision are pinned, native inference is serialized, and the service can read
  only its isolated runtime directory within the user's home.
- Add a local Wyoming known-WAV smoke test that hides transcript text by default
  and an opt-in Codex WebRTC transcription probe that prints its transcript and
  consumes ChatGPT subscription quota.

### Changed

- Make the production Assist architecture use local Wyoming faster-whisper for
  a reliable finite STT boundary while retaining ChatGPT OAuth for Codex Voice
  Conversation and the experimental TTS/realtime paths. New integrations create
  Conversation and TTS subentries by default. Existing Codex STT subentries are
  preserved; users must manually select Wyoming in stored Assist pipelines, and
  the entity/setup UI describes Codex STT as an experimental diagnostic.
- Make the pinned ThirdReality v1.1.7 wake path use an LED-only acknowledgement:
  pre-arm microphone forwarding on the pinned microphone thread, queue the
  Home Assistant Assist start request, and duck music without playing the local
  confirmation cue. Dispatch serialized LED DBus calls on a bounded daemon
  worker so their two-second vendor timeout cannot stall microphone capture,
  coalescing an overloaded backlog toward the newest state. The overlay now
  requires an atomic match across four exact vendor bytecode hashes, patches the
  pinned ThirdReality subclass directly, and preserves transactional rollback
  while ensuring teardown wins startup races.

## [0.1.10] - 2026-08-09

### Changed

- Default newly created Conversation profiles to App Server's configurable
  `priority` service tier, while preserving `standard` for existing profiles
  that predate the option. Priority targets lower response latency and consumes
  more subscription availability.
- Align selectable reasoning efforts with the installed model catalog by
  adding `ultra`, removing `none`, and safely mapping legacy unsupported values
  to the latency-oriented `low` default.
- Add an opt-in guarded live-STT fragment completion path. The safe default
  remains 2 s because local WebRTC input drain is not remote recognition
  completion. A measured deployment may explicitly select 0.5–2 s; the shorter
  deadline is used only after successful input drain with a normalized,
  unity-gain live feed and no handoff. Completion diagnostics remain numeric
  and privacy-safe.
- Calibrate streaming STT incrementally with a bounded 600 ms analysis window
  and recognize speech-like quiet input without opening on digital silence,
  steady noise, or isolated clicks. This prevents quiet ThirdReality captures
  from being replayed only after EOF and removes the prior full-prefix rescan
  cost as utterances grow.
- Bound realtime-session shutdown to 5 s and private-thread disposal to one
  separate 5 s wall-clock budget. Delete and legacy unsubscribe fallback now
  share that budget, including a stalled App Server write, instead of adding a
  nominal 30 s failure tail before a retry or error can return.
- Pin a reversible, bytecode-guarded ThirdReality overlay for the tested v1.1.7
  client. It lets Home Assistant prepare the pipeline during the 0.399592 s
  wake cue without forwarding cue audio, and rejects stale callbacks after a
  run ends, disconnects, is cancelled, or is replaced. Transactional rollback
  and a bounded missing-EOF watchdog prevent mute, player, send, and cue
  failures from wedging later wakes. The global mpv-cache experiment is not
  included because its small observed difference was noisy and it also affected
  sustained music playback. Device launch guidance also disables Python bytecode
  writes so a permissive firmware umask cannot create a writable root import
  artifact.

### Fixed

- Retain constrained `appendText` synthesis for the official Home Assistant
  `tts.speak` path. A cold `appendSpeech` experiment timed out at 90.047 s with
  HTTP 504 and is not enabled.

## [0.1.9] - 2026-08-08

### Added

- Negotiate Home Assistant's native mono 16-bit WAV output at 16 or 24 kHz
  and incrementally resample realtime speech when 16 kHz is requested.
- Record privacy-safe successful-capture level and live-feed metrics for
  diagnosing microphone gain and recognition overlap without logging speech.

### Changed

- Feed bounded, one-time calibrated speech to realtime recognition while Home
  Assistant capture continues, retaining the complete raw utterance for a
  fresh normalized retry and buffering quiet or ambiguous captures until EOF.
- Default voice conversation turns to low reasoning effort to favor latency;
  the integration option remains configurable.
- Set the ThirdReality v1.2.1 production decision to no-go after confirming
  that the target is single-slot and the available files cannot provide exact
  rollback. A future spare-device canary remains conditional on authenticated
  provenance, complete device read-backs, and physically rehearsed recovery.
- Record end-to-end acoustic wake canaries at 700 ms and 300 ms command gaps,
  both completing STT, a local intent, TTS playback, and satellite recovery
  without pipeline errors.

### Fixed

- Use an explicit empty ICE-server configuration so the local
  subscription-backed WebRTC path does not wait five seconds for the default
  public STUN probe during every STT and TTS handshake.
- Bound missing-transcript retries to two attempts and each result wait to four
  seconds, reducing the penalty of failed or exceptionally quiet recognition.

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
