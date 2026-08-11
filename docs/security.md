# Security model

Codex Voice keeps Home Assistant access and ChatGPT authentication on opposite
sides of a narrow bridge API.

## Trust boundaries

- Home Assistant stores only the bridge URL and a dedicated bearer token.
- A ThirdReality realtime endpoint stores a separate route-scoped bearer in a
  root-owned, mode-0600 device file. When configured, that token works only on
  `/v1/realtime` after strict v2 or v3 negotiation; it cannot enter legacy v1 or call
  health, Conversation, STT, TTS, or the Home Assistant tool-authority route.
- The bridge delegates authentication, storage, and refresh to the installed
  Codex CLI. It locates an existing file-backed `auth.json` but does not parse,
  copy, log, or return it. The credential is linked into a mode-0700 temporary
  Codex home, and file-backed CLI authentication makes refreshes update the
  source credential through that link.
- The bridge never receives a Home Assistant long-lived access token.
- Home Assistant prepares the selected LLM tools, validates tool arguments,
  executes the calls, and sends only their results back to the bridge.
- The current ThirdReality v3 client explicitly requests
  `conversation_mode: "native"`. The bridge ignores any Home Assistant broker
  snapshot for that session and creates one active tool-free native App Server
  realtime thread per peer epoch. It relays validated SDP and lifecycle sideband but never
  carries v3 PCM or raw provider data. Okay Computer therefore has no Home Assistant control
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
  device epoch owns one realtime thread; rollover may reuse it only when close
  confirms inside the 100 ms grace. Otherwise the old epoch transfers to tracked
  isolated-thread cleanup, the replacement uses a new thread, and outer cleanup
  deletes every thread it owned. A legacy managed session owns two
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
- Device-facing realtime v3 carries signaling/control only. The isolated
  device peer carries RTP audio and `oai-events` directly to/from the provider;
  the bridge rejects binary v3 media, device-declared tools, and device tool
  results. Provider transcript/model text and arbitrary nested data are removed
  before lifecycle metadata crosses the sidecar IPC boundary. Device-facing v2
  remains the explicit bridge-PCM rollback and compatibility path. It rejects
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
- “Okay Computer” selects explicit native v3 and gains no Home Assistant
  authority. “Okay Nabu” selects the official Assist path;
  a normal wake can preempt a direct session and reclaim the microphone.
- The device retains at most six idle microphone frames for the direct wake:
  384 ms, or 12 KiB of PCM16, in process memory only. Okay Computer atomically
  consumes it; Okay Nabu, stop, mute, disconnect, and teardown discard it. It
  is never written to configuration, disk, diagnostics, or logs, remains inside
  existing queue bounds, and is trimmed or omitted to preserve 32 KiB of live
  post-wake capacity. Any v3 startup/runtime failure clears its captured direct
  audio and returns idle rather than handing it to Home Assistant. The v2
  rollback alone preserves bounded pre-ready Assist replay.
- The ThirdReality controller is standard-library code imported into the
  existing root voice process. Direct media runs in a separate
  `/usr/bin/python3 -I -S` child with a complete hash-locked Python
  3.11/aarch64 runtime, root-owned immutable source/runtime paths, and bounded
  sequenced-packet IPC. The launcher assigns UID/GID 65534 with no supplementary
  groups, a minimal fixed environment, and umask 077. No long-lived application
  credential—the Home Assistant token, route-scoped device bearer, or Codex
  OAuth credential—is placed in argv or the environment or sent through IPC;
  offer/answer SDP crosses IPC and contains ephemeral ICE credentials and DTLS
  negotiation material. Prompt, transcript/model text, tool data, and raw
  provider data-channel payloads also remain outside child IPC. Root-owned mode-0755 runtime/source
  directories and mode-0644 files remain readable but not writable, while the
  mode-0600 device configuration and staging archive are unreadable to the
  child. This is privilege separation, not a general filesystem, syscall, or
  network sandbox. Exact vendor guards are staged: the wake/LED group is
  validated before the latency patch, and the broader audio/configuration/
  constructor/microphone-loop group is validated before direct ownership and
  detector ordering. A second-stage mismatch disables direct mode while
  retaining only the separately guarded normal Assist path. Its JSON
  configuration must be a root-owned, non-symlink regular file with mode 0600;
  source directories/files must not be writable by group or other.
- V3 `device_webrtc` requires full duplex and fails closed without a reviewed static
  PulseAudio `module-echo-cancel` block using the exact configured allowlisted
  AEC engine, exact raw masters and default AEC routes, and the current voice
  process's capture stream routed through the AEC source. The allowlist is
  WebRTC, Speex, and Adrian. WebRTC is the omitted-value default; the installer
  and client never probe or automatically downgrade to another engine. Stock
  v1.1.7 must explicitly select Adrian because its module rejects the uncompiled
  WebRTC and Speex engines. The client checks the exact method before opening
  the bridge socket, enforces a configured 1–60% sink ceiling with a safe 25%
  default, then once per direct session uses fixed-argv `pactl` to set and
  verify the dedicated sink at the exact configured raw playback value. Both
  checks finish before the SDP offer or bridge connection, and only then does
  the signaling handshake deadline begin; the maximum-session deadline still
  spans local preparation. V3 runs `paplay`
  only on that allowlisted sink with raw stream volume 65536 (100% relative),
  non-blocking stdin, and fixed format and latency arguments. It never
  enumerates or mutates a sink-input; the live response/interruption path
  performs no blocking volume subprocess work, and the v2 rollback retains its
  configured stream-volume behavior. The guard compares raw PulseAudio units
  to the exact linear ceiling rather than trusting rounded display percentages.
  The installer writes the matching raw setpoint in the static startup block
  immediately after sink creation. The stock voice process later applies its
  persistent Home Assistant media-player preference, which must match;
  deferred PulseAudio restore state alone is not trusted across reboot. Other
  software must not mutate the qualified sink during a live direct session. A
  successfully loaded Adrian topology still requires a
  physical double-talk canary on each installation at its configured sink and
  stream values. The reference device's bounded 25% pass exercised the prior
  v2 path. Its separate v3 canary ran at that installation's qualified 60%
  setting. Neither result is transferable evidence for another device, and the
  public example remains at the conservative 25% default.
- Provider response/output lifecycle never labels or gates the normal direct
  RTP lane; local media boundaries come only from first decoded audio and an
  actual roughly 120 ms receiver quiet gap. Trusted AEC-filtered v3 barge-in
  drops queued media, immediately SIGKILLs `paplay` in the privileged parent,
  retires the old PeerConnection epoch, and prevents later capture from reaching
  that peer. The outer vendor owner/session/player, authenticated bridge
  WebSocket, and ready latch remain attached. Exactly two reusable isolated
  sidecar processes alternate active/standby roles with a fresh PeerConnection
  each epoch, and the retired epoch's process is recycled. Exactly those two
  prewarmed slots are used; an absent or invalid standby terminates the outer
  session without a cold replacement or third process. Exactly 4 KiB (two 64 ms
  frames, 128 ms) of recent AEC pre-roll and
  the live speech queue is written once and in order to the replacement peer.
  A committed interruption disarms further triggers from the same uninterrupted
  local speech segment; eight detector-quiet 64 ms callbacks (512 ms) rearm the
  detector. Qualifying signal before the eighth resets the quiet count. Stale
  output-epoch requests neither interrupt nor arm that gate.
- The initial strict v3 messages remain unchanged; rollover adds exact
  epoch-tagged controls. Deploy the compatible bridge before the new device
  because the initial acknowledgement cannot advertise this extension. Invalid
  epoch, queue/age/timeout, sidecar, or transport failure ends the outer session
  closed. Stop, normal wake, mute, and disconnect also end it. Captured direct
  audio is neither handed to Home Assistant nor persisted/logged.
- Capture age is checked again at actual RTP consumption; anything older than
  2.25 seconds is terminal. Standby health is re-polled before use, and an absent
  or invalid slot terminates the outer session. Pre-ack replacement lifecycle and PCM
  share the configured `output_queue_bytes` bound and remain inaudible until an
  exact matching `rollover_started`, then replay in order. Protocol/epoch
  integers reject floats and booleans. `stop` remains normal during all phases.
  If bounded killed-sidecar close expires, a daemon reaper takes `waitpid`
  ownership so cleanup does not block the privileged vendor thread. This
  device-process budget is independent of the bridge App Server barrier below.
- The bridge gives the old realtime session 100 ms for its
  `thread/realtime/closed` notification to confirm that awaited input/fanout
  shutdown finished. A timeout, error, or absent close transfers the old epoch
  to tracked isolated-thread cleanup and starts the replacement on a new thread,
  preventing a delayed old close from terminating it.
  `context_retained: true` reports only that barrier/startup-context choice; it
  does not prove audible-history correctness. Interrupted unheard assistant
  output can remain in context, and replay pre-roll can overlap samples already
  delivered to the retired provider peer.
- Tagged Frameless v3 exposes no public cancel/truncate control or provider
  interruption acknowledgement. A synthetic same-peer canary was rejected when
  old RTP continued beyond the five-second media-fence deadline; the former
  `response.interrupt`/`interrupt.fenced` experiment is not production
  behavior. Fresh-peer rollover is a safe subscription-backed approximation,
  not exact ChatGPT same-session semantics. A reference-device physical
  double-interruption canary passed twice with the exact artifact at that
  installation's qualified 60% setting. Four cuts were 208–211 ms and four
  rollovers were 1.29–1.57 s; each run recycled its same two worker PIDs without
  a cold replacement and retained context twice. This passes that reference canary, not the full
  per-installation acceptance matrix. Current App Server
  documentation supports realtime WebRTC v1 and v3, not v2; the direct path
  remains on v3, and a live v1 subscription
  canary did not complete startup. On the native v2
  rollback path, resume still requires a sanitized provider
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

The ThirdReality overlay emits one device-syslog wake selection using only
fixed detector-class and reason enums. It never includes the spoken phrase,
detector ID, confidence, audio, configuration, or credential data.

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
