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
- The current ThirdReality strict-v2 client explicitly requests
  `conversation_mode: "native"`. The bridge ignores any Home Assistant broker
  snapshot for that session and keeps exactly one active native App Server
  realtime provider generation behind a stable device WebSocket. Every
  generation has exactly one dynamic tool, `end_conversation`. The tool has
  an empty-object input schema and only terminates this voice session; the bridge
  rejects every other provider tool request with `do_not_retry` and ends the
  session. The bridge accepts bounded binary v2 PCM but never exposes raw
  provider data-channel events to the speaker. In the current controlled
  deployment, Okay Nabu selects
  this direct route with `realtime_only: true`; Home Assistant Assist/Hermes and
  entity tools are deferred, so it has no Home Assistant control authority.
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
  STT, TTS, and realtime threads are deleted when their session ends. An active
  native-v2 conversation owns one active realtime provider generation; a
  confirmed close may reuse its thread, while an ambiguous close transfers the
  retired thread to bounded cleanup and creates an isolated replacement. Every
  thread owned by the device session is ultimately deleted. A legacy managed session owns two
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
- Active device-facing realtime v2 carries binary microphone/output PCM and
  content-free lifecycle only. The bridge owns RTP and `oai-events` to/from the
  provider; the speaker never sees provider events. V2 rejects
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
- Okay Nabu selects explicit native v2 in the current `realtime_only` trial and
  gains no Home Assistant authority. Once a direct session owns the device,
  later detector hits are ignored; interruption and follow-up are driven only
  by bounded live audio and local AEC-qualified barge-in. A later split
  deployment may restore Assist and a distinct direct phrase, but that authority
  split is not active now.
- The device may retain at most six idle microphone frames in its generic
  compatibility ring: 384 ms, or 12 KiB of PCM16 in memory only. An accepted
  active-v2 wake discards all of it and drops every recorder callback during connecting
  and ready-cue playback. The initial peer therefore receives zero wake or
  pre-ready PCM and does not wait on the 64 KiB live input queue. That queue
  opens empty after cue EOF and then bounds accepted live/rollover pressure.
  Stop, mute, disconnect, teardown, deadline, exhaustion, cue failure, and every
  terminal clear remaining capture without forwarding, persisting, logging, or
  handing it to Home Assistant. Pre-ready Assist replay exists only in older
  optional compatibility configurations, not active native/realtime-only v2.
- An accepted direct wake queues the thinking/pulsing LED immediately and gives
  at most three fresh session attempts one shared absolute 12-second owner
  deadline. Each session has its own ten-second signaling-handshake bound
  inside the remaining owner time. Compose keeps the bridge/App Server/media
  stack warm, but no connected provider peer is assigned before wake.
  Construction, AEC/player preflight, bridge setup,
  peer readiness, terminal state, deadline, or attempt exhaustion all fail
  closed without widening authority to Home Assistant.
- `RealtimeSession.ready` is set only after the server has applied the answer,
  the provider peer and `oai-events` channel are ready, and the bridge returns
  exact accepted strict-v2 `started`. Only then may the root process
  play once the pinned root-owned PCM16 mono 22,050 Hz cue
  `/usr/lib/python3.11/site-packages/sounds/wake_word_triggered_old.wav`, SHA-256
  `6b25dd2abaf7537865222ca9fd6e14fbf723458526fb79bbe29d8261d1320724`, about
  0.400 seconds. The local stop detector remains suspended for the entire
  direct ownership window, so the wake tail cannot cancel signaling and
  playback echo cannot terminate the live session. Capture remains closed
  through cue EOF; EOF switches to the listening LED and opens live provider
  capture. Missing EOF or cue failure has a two-second bound and is terminal;
  there is no device worker or logical standby peer.
  The sole spoken terminal control is `end_conversation`; its result closes the
  session and normal cleanup restores the prior detector membership and idle
  LED.
- The active ThirdReality controller is standard-library code imported into the
  existing root voice process. Native AEC3, capture, playback, and the local
  interruption cut remain on-device; `bridge_pcm` launches no `aiortc` sidecar.
  The root-owned mode-0600 configuration contains only the route-scoped device
  bearer, never OAuth or a Home Assistant token. The Compose server runs the
  media stack and App Server under a non-root UID matching the owner-only OAuth
  file, with a read-only root filesystem and bounded temporary storage.
- Active `bridge_pcm` requires full duplex, `capture_backend: "native_aec3"`,
  the reviewed AEC source/sink and Adrian topology, a 100% fixed sink/playback
  anchor, and a 100%-relative stream. Dynamic volume is non-amplifying software
  attenuation over mute at 0 and audible levels 1–100%; a saved 80% initial
  level remains below the anchor and the physical buttons can still reach full
  hardware output. Native capture applies a 10 dB baseline plus adaptive
  digital gain capped to a -50 dBFS output noise floor, a limiter, and moderate
  noise suppression inside APM. Transport gain remains 0 dB and playback PCM
  is never amplified by the capture path.
  AEC-filtered near-end speech cuts local playback while its original causal
  PCM continues on the same socket. The device sends one exact nonterminal
  `barge`; the bridge fences the retired generation, keeps up to 320 ms of
  already-resampled pre-roll, and buffers live PCM within the 2,250 ms total
  rollover bound. It replays that audio once through a replacement provider
  peer. Provider VAD/cancellation is not an authority or correctness boundary.
- Dormant v3 direct media ran in a separate
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
  retaining the separately guarded Assist implementation for explicit rollback;
  the current `realtime_only` matching wake still fails closed. Its JSON
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
  the bridge socket, enforces a configured 1–100% sink ceiling with a safe 25%
  default, then at direct startup uses fixed-argv `pactl` to set and verify the
  dedicated sink at the exact configured raw playback value. Both checks finish
  before the SDP offer or bridge connection. Each attempt's five-second
  signaling deadline begins only after this local preparation, while the shared
  12-second wake-owner deadline spans preparation and every retry. V3 runs `paplay`
  only on that allowlisted sink with raw stream volume 65536 (100% relative),
  non-blocking stdin, and fixed format and latency arguments. It never
  enumerates or mutates a sink-input. Each response verifies the exact anchor;
  a mismatch is repaired or output fails closed. Home Assistant volume requests
  are applied as bounded software attenuation, and the guarded 50 ms physical-
  button loop restores a displaced anchor. Ordinary interruption performs no
  blocking volume subprocess work. Active v2 uses the same fixed anchor,
  100%-relative stream, and one software attenuation stage. The guard compares raw PulseAudio units
  to the exact linear ceiling rather than trusting rounded display percentages.
  The installer writes the matching raw setpoint in the static startup block
  immediately after sink creation. The stock voice process later applies its
  persistent Home Assistant media-player preference, which must match;
  deferred PulseAudio restore state alone is not trusted across reboot.
  Uncoordinated sink mutation is unsupported and is repaired or fences output.
  A successfully loaded Adrian topology still requires a
  physical double-talk canary on each installation at its configured sink and
  stream values. The reference device's bounded 25% pass exercised the prior
  v2 path. Its separate v3 canary ran at that installation's qualified 60%
  setting. Neither result is transferable evidence for another device. The
  active 100% reference configuration requires its own complete physical
  canary.
- Native hardware-loopback AEC3 is explicitly enabled for the active reference
  route. Its selector is `capture_backend: "native_aec3"` in the root-owned
  mode-0600 `/data/conf/codex-realtime.json` with `bridge_pcm`. The overlay
  reads that no-follow, bounded configuration before vendor microphone
  selection; `CODEX_AEC3_CAPTURE=1` is only an explicit environment override,
  not a required companion flag. It publishes `CODEX_AEC3_ACTIVE=1` internally
  only after installing the recorder, and session preflight treats that marker
  as proof that the configured backend is active. Operators must not set it.
  An invalid native library, ABI, device, or capture path fails startup closed.
  Merely installing the library does not select it, never authorizes raw-
  microphone fallback, and does not satisfy physical qualification.
- Dormant v3 provider response/output lifecycle never labeled or gated its direct
  RTP lane; local media boundaries come only from first decoded audio and an
  actual roughly 120 ms receiver quiet gap. Trusted AEC-filtered v3 barge-in
  drops queued media, immediately SIGKILLs `paplay` in the privileged parent,
  retires the old PeerConnection epoch, and prevents later capture from reaching
  that peer. The outer vendor owner/session/player, authenticated bridge
  WebSocket, and ready latch remain attached. Exactly one reusable isolated
  sidecar process holds the active peer and at most one fresh, offer-warm
  standby peer. Ordered promotion fences and stops the retired peer before
  later capture reaches the standby, and the same worker then prepares the
  following standby. The hard process cap remains one; an absent or invalid
  standby terminates the outer session without another worker. Exactly 4 KiB (two 64 ms
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
  closed. Stop, mute, and disconnect also end it; later detector hits remain
  ignored while the owner is live. Captured direct audio is neither handed to
  Home Assistant nor persisted/logged.
- Capture age is checked again at actual RTP consumption; anything older than
  2.25 seconds is terminal. The logical standby is validated before use, and an
  absent or invalid peer terminates the outer session. Pre-ack replacement lifecycle and PCM
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
  not exact ChatGPT same-session semantics. A historical two-worker build
  passed a reference-device physical double-interruption canary twice with the
  exact artifact at that installation's qualified 60% setting. Four cuts were
  208–211 ms and four rollovers were 1.29–1.57 s; each run recycled its same two
  worker PIDs without a cold replacement and retained context twice. Those
  measurements do not physically validate the current single-worker build and
  do not replace the full per-installation acceptance matrix. Current App Server
  documentation supports realtime WebRTC v1 and v3, not v2; that statement
  concerns the provider surface, not the project's active v2 LAN protocol. A
  live v1 subscription canary did not complete startup. On active native v2,
  the device socket stays open while the bridge strictly replaces the
  non-interruptible provider generation. Confirmed
  `thread/realtime/closed` within the 100 ms reuse grace permits same-thread
  startup-context reuse; a timeout, error, or otherwise ambiguous close
  isolates the replacement on a fresh thread. No provider VAD or cancel
  acknowledgement is assumed. On the
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

The root Compose deployment uses host networking so server-owned WebRTC avoids
container NAT. Host firewalling is therefore part of the security boundary.
The service runs without root or Linux capabilities, with
`no-new-privileges`, a read-only root filesystem, bounded temporary files, and
an init process. Only the existing owner-only `auth.json` file is bind-mounted;
the container UID/GID must match its owner so Codex can refresh it in place.
Do not mount the whole Codex home. Bearers live in an ignored mode-0600
environment file. `docker compose config` expands and can print those values,
so never attach its output to diagnostics.

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

Native-v2 rollover logs contain only the monotonic generation number,
confirmed-reuse versus isolated-close outcome, replacement readiness time, and
retained PCM byte count. They contain no PCM, transcript, prompt, thread or
provider identifier, or tool content.

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
