# Performance and ThirdReality tuning

The retained standard pipeline uses local Wyoming faster-whisper for STT and
local Wyoming Piper for TTS. Its remaining provider-side remote latency comes
from the ChatGPT-backed Conversation stage. On the current controlled speaker,
however, Okay Nabu is bound to `realtime_only` direct speech and Home Assistant
Assist/Hermes is deferred. The retained experimental Codex TTS entity also uses
subscription-backed realtime speech. Treat the figures below as diagnostic reference points, not
service-level guarantees. CPU load, network path, ChatGPT load and quota, Codex
CLI version, Home Assistant pipeline choices, utterance length, and
media-player buffering all affect a turn.

> [!IMPORTANT]
> The current Okay Nabu `realtime_only` implementation uses strict wire v2 with
> full-duplex `bridge_pcm` and native AEC3. The aarch64 Buildroot Linux speaker
> owns only wake/LED/cue/AEC/capture/playback/local cut; the local server owns
> OAuth, App Server, and one active `aiortc` provider generation behind a stable
> device WebSocket. A qualified cut replaces that generation server-side.
> Startup and runtime fail closed without Assist/Hermes fallback. A historical
> device-owned-v3 two-worker build's
> reference-device hardware double-interruption canary passed twice with exact gzip artifact SHA-256
> `5209f6bda3625b50c7413772414a74e12765c6fba2fa23155f79c24d1936e615`
> at that installation's qualified 60% setting. Cuts were 210/211 ms and
> 211/208 ms; rollovers were 1,408/1,303 ms and 1,569/1,292 ms. Each run
> recycled its same two worker PIDs without a cold replacement, and all four
> rollovers retained context. A subsequent strict boundary run proved that
> seven quiet callbacks do not rearm while eight do; it recorded 209/211 ms
> cuts and 1,432/1,276 ms rollovers with the same two PIDs and retained context
> twice. These results do not physically validate the current single-worker
> build and are not the full per-installation acceptance matrix.
> Those v3 results do not validate the active v2 route. Measurements that
> mention pre-ready Assist replay, turn-taking v2, or double stream attenuation
> describe older configurations unless explicitly labeled active.

## What is optimized

The standard Assist path remains turn-based:

```text
wake cue -> capture -> STT -> Conversation -> TTS -> speaker playback
```

Several enabled optimizations shorten different parts of that path:

1. Home Assistant streams 16 kHz PCM to its native Wyoming integration. A
   persistent faster-whisper `base` model performs finite STT locally without
   a remote handshake, subscription speech lease, or same-path retry.
2. Home Assistant sends completed response text through native Wyoming to a
   persistent local Piper `es_MX-ald-medium` model. Production TTS therefore
   has no remote WebRTC handshake or subscription speech lease.
3. Newly created Conversation profiles default to low reasoning effort and App
   Server's `priority` service tier to favor response latency. Existing
   profiles preserve standard usage until reconfigured, and users can select
   `standard` to reduce subscription usage.

The retained experimental Codex TTS entity progressively returns an
EOF-terminated PCM16 WAV stream and uses an explicit empty ICE-server list.
Those optimizations remain relevant to diagnostic Codex TTS and direct
realtime speech, but they are no longer on the retained Assist TTS path.

Automatic experimental Codex STT-to-TTS session reuse is not enabled. Live
validation found that the realtime v3 session can start genuine assistant
output before the user transcript completes, and the supported tagged
Frameless Bidi outbound protocol does not define a response-cancel message.

These paths preserve the finite Home Assistant provider contracts. They do not
turn the standard Assist pipeline into a full-duplex or barge-in session.

## Direct realtime path

The current controlled ThirdReality deployment selects this path with Okay
Nabu and `realtime_only: true`; Home Assistant Assist/Hermes and entity control
are deferred. The client requests `conversation_mode: "native"`; the bridge
echoes it, ignores Home Assistant broker state, and keeps one active App Server
realtime provider generation behind the device's WebSocket. Exactly one
empty-input tool, `end_conversation`, can terminate it. There is no finite
transcript gate, executor, `appendSpeech` render, or device `aiortc` import.
Qualified interruption replaces the provider peer on the server while the
device session stays open.

Native mode does not make cold startup free. The active client now keeps one
bounded, audio-empty provider session warm for five minutes after voice-process
startup and after a completed conversation. A probable Okay Nabu score at 0.50
also creates or refreshes a ten-second speculative slot before final wake
acceptance. A matching wake atomically claims the same device WebSocket, Codex
thread, and server-owned WebRTC peer; no second negotiation is started. An
expired, failed, or incompatible slot is stopped and the existing three-attempt
cold path remains available. Capture remains closed and no microphone PCM is
queued while the slot is unclaimed. Report wake to exact
`started`, `started` to cue EOF/capture-open, capture-open to speech endpoint,
endpoint to first PCM, and first PCM to audible playback separately. The
experimental App Server surface provides no
latency or availability SLA, and an already-open ChatGPT voice session is not
an equivalent cold-start comparison.

Project wire v2 is only the LAN framing; the bridge still uses the supported
App Server WebRTC surface for its provider peer. Older strict-v2 clients that
omit `conversation_mode` retain automatic native/managed compatibility, but
the reference ThirdReality client never selects it.

The active latency and safety bounds are:

| Boundary | Active bound | Purpose |
|---|---:|---|
| Wake-owner startup | At most 3 attempts / one absolute 12 s deadline | Bounds connection, server peer readiness, cue, retry, and terminal races |
| Ready cue | About 0.400 s / 2 s EOF timeout | Starts only after exact `started`; capture opens only at EOF |
| Pre-ready microphone | 0 bytes admitted | Discards wake-tail, connecting, and cue-time PCM |
| Live input queue | 64 KiB / 2.048 s | Bounds post-cue scheduling pressure, never cold-start replay |
| Server input-track age | 2,250 ms | Fails instead of presenting stale microphone audio |
| Device playback queue | 48 KiB / about 1.024 s | Bounds 24 kHz mono PCM16 output |
| Capture conditioning | +10 dB APM baseline, noise-limited adaptive gain, limiter, moderate NS; 0 dB transport | Raises distant speech for both wake/realtime capture without a second post-limiter gain stage |
| Playback | Fixed 100% sink anchor, 100%-relative stream | One later non-amplifying software stage exposes mute at 0 and audible levels 1–100% without PCM amplification |
| Session lifetime | 120 s semantic idle / 900 s hard maximum | Normal provider delay does not end the session; explicit end/terminal state does |
| Barge-in | Two qualifying AEC-filtered callbacks | Cuts local playback, sends exact `provider_barge`, then cancels and clears output on the same provider peer while microphone pacing continues |

### Historical device-owned v3 bounds and measurements

The table and discussion below preserve the former v3 experiment and older
compatibility routes. They are not the active acceptance contract.

The shipped bounds are intentionally small and fail closed:

| Boundary | Default bound | Purpose |
|---|---:|---|
| Initial v3 wake/pre-ready capture | 0 bytes | Discards the triggering/wake-tail PCM and every connecting/cue callback; epoch 1 starts empty after cue EOF |
| Direct startup owner | At most 3 attempts / one absolute 12 s deadline | Bounds construction, local preparation, negotiation, retry, and pre-ready terminal races across the accepted wake |
| Per-attempt v3 signaling handshake | 10 s | Bounds offer/answer, SDP application, peer/data readiness, `transport_ready`, and exact `started` within the remaining owner budget |
| Ready confirmation cue | About 0.400 s; 2 s EOF timeout | Plays the pinned root-owned PCM16 mono 22,050 Hz WAV once after exact readiness; capture remains closed until EOF |
| Device live microphone queue | 64 KiB / 2.048 s | Bounds accepted post-cue live audio and rollover pressure; it is not a cold-start queue |
| Device Opus/RTP input frame | 320 source samples / 20 ms, expanded to 960 samples at 48 kHz | Produces exactly one pinned-aiortc Opus payload and one 960-sample RTP timestamp per track read, continuously across 64 ms recorder callback boundaries |
| Device realtime lifetime | 120 s semantic idle / 900 s hard maximum | Idle begins after readiness and is refreshed by semantic microphone, playback, or lifecycle activity; the hard clock begins before local preflight and covers startup, runtime, and rollover. Configurable ranges remain 5–120 s and 15–900 s |
| Older v2 pre-ready Assist copy | 64 KiB / 2.048 s | Historical split configuration only; active native/realtime-only v2 discards it |
| V2 reserved startup headroom | 32 KiB / 1.024 s | Compatibility-only pre-roll rule; it does not apply to initial v3 startup |
| Bridge v2 WebRTC input track | 2,250 ms | Active v2 still uses this bound; dormant v3 media bypassed the bridge |
| Bridge provider-audio queue | 25 decoded chunks / roughly 500 ms | V1/v2/adapters only; v3 provider audio stays on the device peer |
| Device playback queue | 48 KiB / about 1.024 s | Bounds v2 child input and the v3 direct player's configured buffer allowance |
| V3 decoded-receiver quiet boundary | About 120 ms without PCM meeting both peak 64 and RMS 8 | Splits only normal media generations; exact silence and sub-audible Opus residue are not played or semantic, while every decoded RTP frame still participates in the independent interruption fence |
| V3 first-playback AEC settle | One 512 ms window per fresh peer/new `paplay` onset | Sends timestamp-preserving capture silence and ignores local barge-in evidence while the physical AEC converges; a normal quiet boundary that reuses the player does not restart it |
| V3 fresh-peer rollover | 4 KiB / 128 ms recent AEC pre-roll; eight detector-quiet 64 ms frames (512 ms) to rearm after a committed interruption; 2.25 s maximum capture age rechecked at RTP consumption; configured handshake deadline | Stops local output immediately; one uninterrupted local speech segment retires only one peer; exactly one reusable worker holds an active plus offer-warm standby PeerConnection and promotes the standby in order; an absent/invalid standby terminates the outer session without another worker; pre-ack output is inaudible within `output_queue_bytes`; negotiation remains measurable |
| Full-duplex AEC sink ceiling | Configured guard, 1–100% (25% default), restored exactly at direct-session preflight, checked before every response, and reconciled by the guarded 50 ms firmware settings loop after a physical-button change | The ordinary unchanged tick only reads the small settings file; physical reconciliation has one 75 ms whole-transaction deadline, starts a 128 ms stale-tail guard before I/O, and fails output closed if the anchor cannot be restored exactly |
| Legacy managed: Home Assistant tool execution + result send | 25 s + 5 s | Bounds the compatibility authority action and component transport separately |
| Legacy managed: bridge tool transaction + provider delivery | 35 s + 5 s | Covers send-lock acquisition, WebSocket write, result wait, and App Server response write |
| Legacy managed: App Server tool fallback | 45 s | Remains responsible until the result write completes |
| Legacy managed: post-tool provider continuation | 20 s | Requires output or a terminal response after result delivery |
| V3 direct `paplay` | Dedicated AEC sink set/verified to exact raw `playback_volume_percent`; stream forced to raw 65536; 60 ms latency, 20 ms process time and writes | One fixed-argv child, non-blocking stdin, immediate SIGKILL on abort, no sink-input manipulation |
| Older v2 `paplay` stream | Configured 1–60% (25% default) | Historical second linear attenuation, removed from the active route |

An accepted wake queues the thinking/pulsing LED immediately. Here, exact
readiness means SDP applied, peer connected, `oai-events` data ready,
`transport_ready` sent, and the bridge's exact `started` accepted. The one cue
is `/usr/lib/python3.11/site-packages/sounds/wake_word_triggered_old.wav`,
SHA-256
`6b25dd2abaf7537865222ca9fd6e14fbf723458526fb79bbe29d8261d1320724`.
The local stop-word detector stays suspended for the entire direct ownership
window, so the wake tail cannot cancel signaling and playback echo cannot end
the live session. Cue EOF switches the LED to listening and opens capture. A
two-second cue timeout, session terminal, 12-second owner deadline, or
three-attempt exhaustion releases the owner and queues idle without Home
Assistant fallback. The sole spoken terminal control is `end_conversation`;
terminal cleanup likewise returns idle.

The generic overlay may retain six idle recorder callbacks, but an accepted v3
wake discards all 12 KiB / 384 ms and drops every subsequent callback until the
ready cue reaches EOF. The live 64 KiB queue therefore begins empty; no 2×
catch-up overlaps initial negotiation. Paced catch-up applies only to accepted
post-cue backlog and rollover pressure. Only v2 owns and may replay a pre-ready
Assist copy, subject to its 32 KiB live-headroom rule. If any v3 startup or
runtime bound is exhausted, the direct owner clears its queue and returns idle
without invoking Assist.

For initial v3 startup, each attempt's ten-second signaling deadline begins
after its local AEC preflight and exact player/sink preparation, but all such
work and all retries share the absolute 12-second wake-owner deadline. The
separate maximum-session clock remains a hard lifetime cap. Rollover gets its
own configured handshake interval, bounded by the remaining outer-session
lifetime. Time to
first audible v3 output must therefore separate wake to SDP answer,
answer to `transport_ready`/`started`, started to cue EOF/live capture, capture
to the first receiver-owned `media.started`, and decoded PCM to audible
playback. Omitting the cue boundary would hide a deliberate user-input gate.
For a legacy managed path, also separate
speech endpoint to completed transcript, transcript to executor completion
(including any Home Assistant broker time), executor completion to
`session.context.appended`, acknowledgement to identified assistant turn, and
that turn to the first authorized PCM.

Historically, v2 was turn-taking by default on v1.1.7. With
`full_duplex: false`,
the microphone gate stays closed from `speaking.started` until the
corresponding PCM has drained from both the local queue and playback child.
V3 `device_webrtc` requires full duplex and a reviewed static PulseAudio AEC topology using the
exact configured allowlisted engine, exact current-process capture and playback
routing, and a configured sink ceiling from 1–100% with a 25% default. WebRTC is the omitted-value
default and never automatically falls back. The stock v1.1.7 build rejects
WebRTC and Speex, so active stock-device deployments explicitly select Adrian.
The client checks that topology, method, and ceiling before constructing the
device peer or opening the bridge socket. A fixed-argv `pactl` controller then
sets and verifies the dedicated AEC sink itself at the exact raw playback value
before the SDP offer. It checks the exact anchor again before each response and
repairs a mismatch before admitting audio. Direct v3 uses one fixed-argv `paplay` child on that
sink with raw stream volume 65536 (100% relative), non-blocking 20 ms writes,
and 60 ms/20 ms latency/process arguments. Abort clears queued PCM and issues
SIGKILL immediately without waiting on the network loop; reap is bounded
separately. No sink-input is enumerated or mutated; an ordinary volume command
or interruption runs no `pactl`, and playback begin/resume performs only its
exact check unless repair is necessary. Older v2 deployments instead derived
their `paplay` stream volume from the configured percentage. Active v2 uses the
same raw 100%-relative stream and the software-volume stage below.

During a live direct owner, matching Home Assistant volume commands also avoid
PulseAudio entirely. The overlay consumes volume/mute/unmute before the vendor
players, accepts 0 as mute and 1–100% as audible, caps the desired level at the
configured playback anchor, and updates the saved entity state. The active
100% anchor therefore lets physical buttons use the full hardware range. A
deployment may save 80% as its initial audible level; the parent applies the
PulseAudio-compatible cubic ratio to PCM at the next 20 ms staging boundary
with a 40 ms non-amplifying ramp. It never boosts PCM above the incoming raw
amplitude, and partially written staged PCM is never transformed twice. Capture
and local barge-in remain continuous. Consequently the AEC sink stays fixed
and its render reference contains the exact attenuated waveform.

The stock physical buttons otherwise bypass Home Assistant and write both the
default PulseAudio sink and `sound.json`. The guarded overlay reduces the
pinned firmware settings interval from 500 ms to 50 ms. An unchanged tick does
not touch PulseAudio; a changed logical volume asks the live session to verify
and, when needed, restore the exact anchor under the output lock before applying
the corresponding software level. The physical repair transaction has one
75 ms deadline across its probe, set, and verification; a response-start check
uses 250 ms before any audio is admitted. The old FIR remains only as an
untrusted seed: prior delay, render timing, and validity are discarded, and the
guard searches its full 20–320 ms range. Three consecutive render-backed signal
frames with correlation at least 600‰, delay spread no greater than 24 ms, and
third-frame residual no greater than 45% requalify it. A 128 ms transition guard
starts before repair I/O, after which clear near-end speech remains
interruptible. Eight unsuccessful correlated/ambiguous evidence frames fence
output. Quiet capture, zero software volume, and `media.quiet` preserve the
pending generation without consuming that evidence bound. The guard never
changes raw capture retained for local detection. On active native AEC3 it also
never rewrites accepted bridge PCM: every frame reaches the provider input and
the server's rollover pre-roll after the configured outbound gain. The
PulseAudio-AEC compatibility backend may still turn affirmative render-correlated
or uncertain capture into timestamp-preserving, equal-length silence on the
current provider peer; clear decorrelated near-end speech passes raw. A failed
or unequal-channel verification fences output instead of guessing. Saved volume
state shares the physical key's lock and is atomically replaced so concurrent
JSON updates cannot tear. Each persisted change also arms exactly one next-tick
anchor verification, which clears without persisting again; this closes the
opposite-order sink race without turning the 50 ms monitor into a pactl loop.

The rendered write observer also drives a bounded double-talk guard. It reduces
at most 1.024 seconds of exact post-volume render to 4 kHz, calibrates a delay
and 24-tap residual model only during the existing 512 ms first-playback settle,
then searches only a narrow band around that learned delay. Classification uses
the prior frozen model rather than a same-frame fit. High-confidence correlated
echo resets local interruption evidence; clear near-end speech retains the
two-callback path; ambiguous evidence fails open after four callbacks. Missing,
stale, quiet, or uncalibrated references fail open immediately. The guard never
changes the raw capture queue or pre-roll and adds no blanket
volume-transition microphone blackout. It silences only render-correlated or
uncertain frames on the response's current peer; an interruption replacement
peer receives their original bytes. Its content-free counters make a physical
echo/double-talk canary diagnosable without retaining audio.

The native hardware-loopback AEC3 capture slice is disabled by default. Its
normal selector is `capture_backend: "native_aec3"` in the enabled root-owned
mode-0600 `/data/conf/codex-realtime.json` for full-duplex `bridge_pcm` or the
dormant `device_webrtc` route;
`CODEX_AEC3_CAPTURE=1` is only an explicit diagnostic override. The early
fail-closed hook sets `CODEX_AEC3_ACTIVE=1` internally after the recorder patch
succeeds; session preflight consumes it to prove that config and capture agree.
Operators must not set that proof variable. A successful build, activation
marker, or standalone counter
does not by itself establish near-end/double-talk quality. Adrian remains
available as the initial playback-DMA keepalive and rollback topology during
physical qualification.

Provider response/output lifecycle never labels or gates the normal RTP lane.
The decoded receiver opens a local media epoch on the first frame meeting both
the fixed peak-64 and RMS-8 PCM bounds and closes it after roughly 120 ms
without another qualifying frame. Exact silence and sub-audible Opus decode
residue are not played and do not extend semantic activity; every decoded RTP
frame still participates in the separate interruption fence. This preserves
audible RTP-before-start prefixes and stopped-before-tail audio without letting
keepalive residue hold `paplay` open. The normal-generation boundary is not an
interruption acknowledgement and does not authorize peer reuse.

The device writes one aggregate summary when a v3 session ends. Use its
handshake phase, sent-capture packet/byte/peak/RMS/signal counts, allowlisted
lifecycle counts, signal-bearing playback metrics, capture-age bounds,
duration, and outcome to distinguish a microphone/VAD stall from an output
stall. This diagnostic is deliberately content-free: failure records include
only phase and exception class, and neither record contains PCM, transcripts,
provider payloads, identifiers, SDP, prompts, URLs, or credentials.

At the first audible frame on each fresh peer, the sidecar applies one 512 ms
AEC-settle window keyed to the original capture timestamps. It still consumes
and emits every exact 20 ms frame, but zero-fills those inside the window so
PTS/RTP progression, sender cadence, freshness checks, and consumption
watermarks remain continuous. The parent simultaneously ignores local
two-frame barge-in evidence until the matching deadline. Receiver quiet followed
by another media generation on the still-active `paplay` child does not restart
the window; after it expires, full-duplex capture is unchanged. This onset guard
is independent of the eight-quiet-callback rearm required after a committed
interruption.

In the physical before/after canary, the unguarded response stopped unfinished
after 22 playback packets, about 0.44 seconds. The guarded run delivered 626
packets, about 12.52 seconds, completed both turns, recorded
`session.started=1`, and performed no rollover.

With those checks active, capture continues during playback. Two consecutive
qualifying AEC-filtered 64 ms frames immediately kill `paplay` and drop queued
playback IPC in the parent, retire the old PeerConnection epoch, and prevent
later capture from reaching it. The outer vendor owner/session/player, bridge WebSocket, and
ready latch remain attached. Exactly 4 KiB (two 64 ms frames, 128 ms) of recent
AEC pre-roll is merged with the bounded live queue and delivered once and in
capture order to the fresh standby PeerConnection. Exactly one reusable sidecar
process holds that standby beside the active peer. Ordered promotion fences and
stops the old peer before later capture reaches the standby, and the same worker
then prepares the following standby. An absent or invalid standby ends the outer
session without launching another worker.
Capture older than 2.25 seconds, queue pressure, handshake timeout, sidecar
failure, or an invalid epoch closes the outer session.
After the network thread commits an interruption, it suppresses further
triggers from the same uninterrupted local speech segment across the replacement
boundary. Eight consecutive detector-quiet callbacks (512 ms) rearm it for a
new speech edge; qualifying signal before the eighth resets the quiet count,
and a stale output-epoch request does not arm the gate.

The 20 ms capture reframer is a transport correctness boundary, not a cosmetic
latency tweak. With the old 1,024-sample / 64 ms track reads, pinned aiortc
returned three or four Opus payloads from one encoder call and stamped that
whole group with one RTP timestamp. An offline pass through the pinned jitter
buffer and decoder recovered only 80 × 20 ms from the 5.184-second reference
input. With exact 960-sample / 48 kHz track frames, every encoder call returns
one payload and timestamps advance by 960.

The age check runs again when the RTP sender actually consumes a packet, so a
frame cannot become stale while waiting behind negotiation. Replacement
lifecycle and PCM received before the exact `rollover_started` share an ordered
`output_queue_bytes` bound and remain inaudible until that acknowledgement;
they are then replayed in order. Standby health is re-polled immediately before
use; an absent or invalid slot terminates the outer session. Float/bool
integer controls fail closed, while `stop` is normal during
every rollover phase. A killed child that exceeds its close budget transfers
eventual `waitpid` to a daemon reaper rather than blocking the realtime thread.
That device-process budget is independent of the bridge App Server barrier below.

The bridge gives the old realtime session 100 ms to produce its
`thread/realtime/closed` notification. Confirmed closure retains startup context
on the same thread; ambiguous closure transfers the old epoch to tracked
isolated-thread cleanup and starts the replacement on a new thread so a delayed
old stop cannot kill it.
Neither result proves audible-history correctness, and interrupted unheard
assistant speech may remain in retained provider context.

The direct Frameless channel supplies no public cancel/truncate control or
provider interruption acknowledgement. A synthetic same-peer canary was
rejected when old RTP continued beyond the five-second media fence; the former
`response.interrupt`/`interrupt.fenced` experiment is not production behavior.
Fresh-peer rollover is a safe subscription-backed approximation, not exact
ChatGPT same-session semantics. Measure local stop latency separately from
rollover offer, old-session close barrier, replacement readiness, and first new
audible PCM; fresh WebRTC/provider negotiation cannot be optimized away.
The historical two-worker reference-device double-interruption canary passed at
that installation's qualified 60% setting in two consecutive exact-artifact runs.
Local cuts were 210/211 ms and 211/208 ms; fresh-peer rollovers were
1,408/1,303 ms and 1,569/1,292 ms. Each run recycled its same two worker PIDs
without a cold replacement and retained context twice. Each installation must
still repeat the complete physical acceptance matrix. A separate strict
seven-versus-eight-quiet-callback run also passed at 209/211 ms local cuts and
1,432/1,276 ms rollovers, with the same two PIDs reused and context retained
twice. These measurements do not physically validate the current single-worker
build. The retired
PeerConnection's stop acknowledgement followed replacement negotiation before
its existing worker was recycled. That device event is distinct from the
bridge's independent 100 ms App Server close-confirmation barrier.

For legacy broker-managed sessions, every new utterance invalidates the previous
executor/output generation. Frontend cancellation is requested only after an
identified assistant render has started, avoiding an invalid idle-session
cancel. Before any Home Assistant tool dispatch, the active executor turn is
tombstoned and interrupted; after dispatch, it is deliberately allowed to settle without
cancellation or replay, its stale final is suppressed, and the newest request
waits in a one-item queue. This preserves the Home Assistant side-effect
boundary at the cost of serializing a barged-in request behind an already
dispatched call.

### Active v2 semantics and acceptance

The v2 ThirdReality path advertises
`User-Agent: ha-codex-voice-thirdreality/2`; this retains compatibility with the
managed `continuation_safe` acknowledgement but does not select that route.
Active v2 native interruption uses exact `{"type":"provider_barge"}`, not the
legacy interrupt acknowledgement. The device immediately cuts playback and
continues capture on the same WebSocket. The bridge sends `response.cancel`
then `output_audio_buffer.clear` on the same provider WebRTC peer while the
replacement instruction continues upstream. It does not wait for provider
`speech_started`. Exact `barge` and strict provider replacement remain the
reversible fallback policy.

Adrian loading with the reviewed raw masters and creating 16 kHz mono endpoints
is a static-topology result, not an acoustic result. On the reference device's
historical v2 path at 25%, a 5.531-second playback canary caused no false interruption across 86 mic
frames (maximum peak 2 and integer RMS 0), and staged double-talk flushed
playback in 141 ms versus 2.650 seconds when waiting for the provider-only
boundary. The same v2 socket continued and produced the next response. These
measurements do not validate active native-AEC3 v2.

Physical acceptance must cover reliable 1.5 m wake and speech; immediate
thinking LED; at most three attempts in the shared 12-second deadline; exact
server readiness; one pinned cue and its two-second timeout; zero admitted
pre-ready PCM; cue-EOF capture opening; a long conversation on one stable
device WebSocket across provider replacements; first audio; queue failure;
repeated sessions; and recovery after bridge or Wi-Fi
loss. At the fixed 100% anchor and representative software-volume levels,
including 0, 1, 25, 60, 80, and 100%, test no-user
self-echo plus early/middle/late near-end speech. The playback cut must be
prompt and the causal words must be replayed exactly once to the replacement
provider without a second sentence, reconnect, or another wake. Also verify the
10 dB baseline plus noise-limited adaptive APM profile and limiter behavior,
normal-distance recognition, single-stage volume, absence of persistent
crackle, sole-tool Spanish/English termination, bounded memory/player cleanup,
no Home Assistant authority or fallback, and unchanged TCP ADB port 5555.

### Physical quick-command regression canary

One pre-v3 bridge-PCM post-deployment 2026-08-09 physical canary replayed the exact sample that
had previously failed, with 308 ms of silence between the Okay Computer wake
phrase and its command. Device-local input playback began at 21:52:39 and ended
at 21:52:42. The bridge completed its handshake in 1.308 s, the command was
captured, and device-local answer playback began at 21:52:45.

This is an end-to-end v2 regression result for that short-gap clipping case. It
is one controlled run, not a benchmark distribution, percentile, v3 rollover
acceptance, or general latency guarantee.

## Local STT measurement

On 2026-08-09, the official Wyoming faster-whisper 3.5.0 server used the
multilingual `base` model with CPU `int8`, beam size 1, eight CPU threads, and
VAD filtering on the measured i5-13600K host. Three warm requests for the same
non-sensitive 16 kHz reference WAV returned the expected transcript in 0.775,
0.617, and 0.599 seconds. The model remained resident between requests.

This narrow test excludes microphone capture and later Conversation/TTS stages.
It establishes that repeated finite local recognition is both working and far
below the prior remote failure tail; it is not a p95 latency claim.

### Physical local-STT canary

After the local-STT production switch, but before the later Piper TTS switch, a
2026-08-09 self-acoustic canary
exercised the ThirdReality speaker, microphone, wake detector, Home Assistant
VAD, local Wyoming STT, Codex Conversation/TTS, and response playback. The
successful run had these event boundaries:

| Interval from pipeline start | Time |
|---|---:|
| VAD start | 1.710 s |
| VAD end | 4.147 s |
| STT end | 4.644 s |
| Intent/TTS result ready | 7.703 s |

Local recognition after VAD end took **0.497 s**. The transcript contained all
25 expected characters, the response played, and the satellite returned to
idle. The device log for that run contained no wake-confirmation WAV playback,
and the Wyoming journal contained only numeric duration/VAD messages—not the
transcript. This is one controlled physical path check, not a latency guarantee.

## Local Piper TTS measurement

On the measured i5-13600K host, the repository smoke probe used
`wyoming-piper==2.3.1` with voice `es_MX-ald-medium` and measured time to the
first non-empty Wyoming `AudioChunk`, not the earlier metadata-only
`AudioStart`. Across two service restarts, cold first PCM ranged from 0.714 to
0.956 seconds and complete synthesis from 0.824 to 1.072 seconds. The next five
warm requests reached first PCM in 0.025, 0.024, 0.044, 0.028, and 0.035
seconds: a 0.028-second median and 0.044-second maximum. Their median complete
synthesis time was 0.116 seconds for 2.949 to 3.367 seconds of output audio.

Three controlled Codex TTS requests for the same text reached first audio in
2.898, 1.671, and 2.025 seconds, a 2.025-second median. Piper's warm median was
about 72 times faster at this measured host-side provider boundary;
it does not establish the same difference at the speaker. In a separate
physical Home Assistant call, the ThirdReality `media_player` entity entered
`playing` 0.018097 seconds after the call and returned to `idle` at 3.564543
seconds. Those state transitions do not instrument actual audible onset.
Home Assistant delivery, satellite buffering, audio-device startup, and
physical playback therefore remain unmeasured boundaries. See [reliable local
text-to-speech](local-tts.md) for deployment and acceptance checks.

### Physical Piper pipeline canary

A later controlled self-acoustic Spanish canary exercised the ThirdReality
speaker and microphone, wake detector, Home Assistant VAD, local
faster-whisper, Codex Conversation, local Piper, response playback, and return
to idle. The generated non-sensitive request was recognized with the intended
words, the response was non-empty with language `es-MX`, and the trace contained
no errors.

| Interval from pipeline start | Time |
|---|---:|
| VAD start | 1.626 s |
| VAD end | 5.936 s |
| STT end | 6.590 s |
| Codex Conversation duration | 1.734 s |
| Satellite responding | 8.324 s |
| Satellite idle | 13.919 s |

Local recognition after VAD end took 0.653 seconds. Home Assistant created its
streaming TTS result in 0.000315 seconds, which is not a first-PCM or audible
onset measurement; the Wyoming probe above supplies the provider-side PCM
timing.

## Historical remote-adapter measurements

Unless another date is shown, figures in this section are **live measurements
from 2026-08-08** of the experimental Codex STT adapter, not simulated test
results. They explain why local Wyoming replaced it as the production STT
boundary. Physical pipeline measurements used one ThirdReality 3RSPK whose
Home Assistant firmware display reported `1.01.07`; the direct bridge/session
probes use narrower timing boundaries and are labeled separately. Sample sizes
are deliberately shown because these small probes are not population
benchmarks.

### Confirmed missing-transcript boundary

A physical 2026-08-09 run reached Home Assistant capture, VAD end, bridge
normalization, and successful WebRTC signaling. The first remote attempt spent
9.698 seconds and the fresh same-path retry spent 9.687 seconds; neither
received an accepted user-transcript event. The stream returned an error after
19.386 seconds total. The retry used the complete normalized utterance at a
higher bounded gain, so additional gain, silence, or device wake tuning was not
an evidence-backed fix.

An isolated Codex 0.147.0 canary also missed one of four requests for a clean
known WAV while the other three succeeded. That small sample does not estimate
a failure rate, but it proves the failure is not exclusive to the physical
ThirdReality microphone. The public pipeline therefore does not describe the
second session as a fallback and does not select this adapter for production
STT.

Developers can repeat that opt-in protocol check with a non-sensitive PCM16
WAV. The command prints the transcript and consumes ChatGPT subscription quota:

```bash
uv run --extra bridge python scripts/probe_transcription.py sample.wav
```

### Physical pipeline reference

Six physical voice commands produced these Home Assistant event intervals:

| Interval | Median | Observed range |
|---|---:|---:|
| intent start to TTS start | 2.093 s | 1.488–5.421 s |
| TTS start to announcement finished | 17.493 s | 13.970–20.436 s |
| intent start to announcement finished | 20.887 s | — |

The end-to-end median is the median of the six complete turns; it is not the
sum of the two independently calculated sub-interval medians. “Announcement
finished” includes generation, Home Assistant serving, device buffering, and
physical playback, so it should not be compared directly with a bridge
first-byte measurement.

### Controlled acoustic wake canary

Two additional 2026-08-08 canaries exercised the actual speaker, microphone,
wake detector, shortened confirmation cue, Home Assistant pipeline, Codex STT,
local intent routing, TTS, and speaker playback. Each WAV used the repository's
two-second “Okay Nabu” test sample, then a fixed non-sensitive “What time is
it?” command. The second run reduced the silence between those source clips
from 700 ms to 300 ms.

All intervals below begin at Home Assistant's pipeline `run-start` event, not
at the start of WAV playback.

| Source gap | Run to VAD start | Run to VAD end | Run to STT end | Run to pipeline end | Run to satellite idle |
|---:|---:|---:|---:|---:|---:|
| 700 ms | 1.852 s | 2.892 s | 9.655 s | 9.669 s | 23.082 s |
| 300 ms | 1.613 s | 2.523 s | 9.422 s | 9.423 s | 22.580 s |

Both runs returned the local `action_done` response type, emitted no pipeline
error, played the answer, and returned the Assist satellite to `idle`. The
300 ms source gap therefore did not lose enough initial speech to prevent
correct routing. This is stronger than a software-only injection because the
audio traversed the device's physical output/input path, but it is still a
controlled self-acoustic test rather than a substitute for near-, normal-, and
far-field human trials in the deployment room.

### Immediate wake and non-blocking LED overlay

The pinned v1.1.7 Python client normally starts the confirmation cue and waits
for cue EOF before it asks Home Assistant to start Assist. After starting that
asynchronous cue, its ThirdReality wrapper calls the LED DBus helper
synchronously on the microphone thread with a two-second timeout. Three
successful human baselines captured on 2026-08-09 reached Home Assistant VAD
1.37, 2.46, and 3.27 seconds after pipeline start. Those end-to-end figures
include the combined device and Home Assistant path; they show the user-visible
variable delay but do not isolate the LED call.

At the time of these historical measurements, the reversible device overlay
used an LED-only acknowledgement without active acoustic echo cancellation. It
sent the Home Assistant start request and music duck while pre-arming microphone
forwarding on the same pinned microphone thread, without playing the local cue.
Forwarding could not actually handle a frame until wake setup returned. It also
serialized LED DBus commands on a separate daemon worker. The vendor's
two-second command timeout remained, but it could not hold the microphone
processing thread; timed-out children were reaped, and an overloaded pending
queue coalesced toward the newest state. The current optional full-duplex path
adds a separately qualified static PulseAudio AEC engine; it does not change
this historical latency sample.

The pinned ThirdReality subclass is patched directly in addition to the base
class. Each replacement voice process reads the validated root-owned mode-0600
`/data/conf/codex-realtime.json` through the early overlay before vendor
microphone import. After changing the secure configuration, restart the voice
process through its supervised path and verify the resulting long-lived
session; native selection proves its recorder patch internally with
`CODEX_AEC3_ACTIVE=1`.

The override is guarded atomically by SHA-256 hashes of four installed vendor
code objects spanning both base and ThirdReality modules. Tests verify
request/duck/stream ordering, first-frame forwarding, transactional rollback,
ordered non-blocking LED work, newest-state overload handling, explicit worker
shutdown, and the fail-closed path. See the device overlay README for deployment
and rollback checks.

### Historical Codex streaming TTS probe

One v0.1.7 live bridge probe of the same synthesis request measured:

| Observation | Time |
|---|---:|
| finite endpoint response ready | 10.717 s |
| streaming endpoint first PCM | 6.495 s |
| streaming endpoint complete | 10.457 s |

Streaming exposed the first PCM **4.222 s earlier** than the finite Codex
response. It did not shorten the remote model's complete rendering by that
amount, and the figure excludes downstream speaker buffering and playback.
It is not the Piper comparison above.

### Streaming STT capture overlap

A reference paced STT run with a 2.0 s clip reached STT completion in
11.709 s. Three direct runs through the capture-overlap path measured:

| Run | Total | After capture | Handshake overlapping capture |
|---:|---:|---:|---:|
| 1 | 9.832 s | 7.832 s | 2.005 s |
| 2 | 9.473 s | 7.473 s | 2.005 s |
| 3 | 9.680 s | 7.680 s | 2.005 s |

The three-run median was 9.680 s, 2.029 s below the reference run. This is a
small live comparison, not proof of a fixed two-second saving. The overlap log
is the useful verification signal: setup should advance while microphone
capture is active instead of beginning only after the final audio chunk.

### Calibrated live STT feed

A later paired physical-speaker canary used the same ThirdReality device,
command audio, runtime microphone gain, and aggressive finished-speaking
detection. The second run enabled calibrated feeding during capture:

| Path | Capture | After capture | Total STT |
|---|---:|---:|---:|
| finite feed at EOF | 3.858 s | 5.661 s | 9.519 s |
| calibrated live feed | 3.984 s | 2.153 s | 6.138 s |

The live-feed run had 0.905 s of queued audio when the remote handshake became
ready and saved 3.381 s overall. It completed without a retry or pipeline
error. This is one controlled self-acoustic A/B, not a latency guarantee or a
replacement for human near-, normal-, and far-field trials.

### Guarded live-fragment completion

The live path has a deliberately narrow, operator-controlled fast-completion
guard. The public default remains the 2 s quiet-fragment window because local
WebRTC input drain does not prove remote recognition completion. A measured
deployment can explicitly choose 0.5–2 s with
`HA_CODEX_TRANSCRIBE_LIVE_FRAGMENT_QUIET_SECONDS`. A value below 2 s is used
only after input drain completed successfully, the live feed was normalized at
unity gain, and no handoff is in progress. Gain-assisted, quiet or ambiguous
captures, failed drains, retries, and handoff-shaped requests keep the 2 s
fallback.

Live activation itself uses a one-pass, bounded calibrator. It retains at most
600 ms of per-frame analysis plus the 320 ms audio preroll, keeps the original
utterance separately for an exact cold retry, and adds a speech-like quiet path
for the low microphone levels observed on the ThirdReality unit. Digital
silence, stationary noise, high-crest isolated clicks, and other ambiguous
input remain buffered until EOF. In a synthetic 15-second quiet-utterance
benchmark, analysis CPU time fell from about 4.7 s with repeated full-prefix
rescans to 0.017 s with 30 retained analysis frames. This is a host benchmark;
physical latency is recorded separately after deployment.

Realtime stop and private-thread disposal are also bounded independently at
five seconds each. Thread deletion receives four seconds and the legacy
unsubscribe fallback can use only the remainder of the shared disposal
deadline. The outer deadline includes time waiting to write the App Server RPC,
so a failed speech attempt cannot silently append the previous nominal 20 s
delete plus 10 s fallback tail before retrying or returning an error.

On the measured deployment after explicitly selecting the shorter guard, the
same short acoustic canary's Home Assistant
`listening`-to-`processing` interval was 7.161 s at the prior setting, 5.928 s
with a 0.75 s guard, and 5.666 s with the 0.5 s guard. The transcript remained
`Say the word ready ready`. A separate long multi-fragment canary completed in
7.090 s with the full words `amber violet juniper`. These are single controlled
runs, not service-level guarantees or evidence that every accent, pause, name,
or network condition is safe at 0.5 s. Logs expose only completion reason,
drain-to-result, and numeric stage timings; speech and transcript content are
not recorded. Return the setting to 2 s if late words are ever omitted.

### Priority conversation service tier

New Conversation profiles default to App Server's `priority` tier and expose
`standard` as a configurable alternative. Existing profiles without a stored
tier remain on standard until the user explicitly reconfigures them. The
installed Codex model catalog describes priority as a faster mode with
increased usage. In one paired direct turn, standard produced first text at
2.271 s and finished at 2.481 s; priority produced first text at 2.091 s and
finished at 2.234 s. This single pair proves that both settings work through
the bridge, not a repeatable latency saving.

### Experimental Codex TTS output format

The Codex Voice TTS entity advertises mono 16-bit WAV at 16 and 24 kHz and
forwards Home Assistant's selected native tuple to the bridge. The bridge
incrementally resamples its 24 kHz realtime output when 16 kHz is requested,
without waiting for the full response. This removes a format mismatch for the
experimental adapter; downstream Home Assistant or media-player conversion and
buffering can still add latency. Recommended Piper TTS is owned independently
by Home Assistant's Wyoming integration.

### Cold synthesis experiment rejected

The bridge retains constrained `appendText` for the official Home Assistant
`tts.speak` contract. Replacing that cold turn with `appendSpeech` was tested
against the same path and produced a 90.047 s request ending in HTTP 504, so it
is not a latency optimization and remains disabled.

### Retained-session exploratory probe

An exploratory direct realtime v3 run measured a 5.998 s initial handshake and
3.844 s to obtain a transcript. After the client later sent `appendSpeech` on
that connection, it received a non-empty queued audio frame 0.018 s later. The
probe did not continuously drain and verify a quiet realtime stream before
`appendSpeech`, so that frame cannot be causally attributed to the speech
request and is not a warm-TTS latency measurement.

A later causal probe observed non-empty assistant transcript and output events
before the user transcript completed. The bridge rejected that session for
reuse while still returning the valid STT result. Codex 0.147.0's tagged
[Frameless Bidi outbound message
definitions](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/codex-api/src/endpoint/realtime_websocket/protocol.rs#L50-L85)
include session close and context/delegation appends but no response-cancel
control. There is therefore no retained-session latency claim, and automatic
handoff remains disabled. A future implementation must first expose a supported
cancel-or-transcription-only protocol and prove a quiet causal boundary before
running a physical A/B.

## One-time STT-to-TTS session handoff

Handoff is a dormant diagnostic wire path in the bridge. The bundled Home
Assistant component never prepares or requests it, and the released bridge
never retains STT or issues a ticket. When the experimental Codex STT and TTS
entities are selected, they therefore always use separate realtime threads and
sessions. The recommended faster-whisper and Piper providers are independent
local Wyoming services and do not use this mechanism. The parser and ownership
machinery remain covered for future protocol work.

If a future supported implementation enables the path, Home Assistant reads an
entity's default options before it merges an Assist pipeline's voice override.
Preparation therefore reserves the TTS subentry's
default voice. If a pipeline overrides it with another supported voice, the
later exact-voice check safely rejects reuse and takes the cold path; warm reuse
for per-pipeline voice overrides is not currently supported.

A ticket is usable only when all of these conditions hold:

- it has not already been claimed or revoked;
- fewer than 30 seconds have elapsed since the bridge offered it;
- the TTS voice exactly matches the voice reserved during STT;
- the normalized TTS language matches the language reserved during STT;
- the TTS request has no custom voice instructions; and
- the request is still in the same Home Assistant chat-session and prepared
  TTS context.

There is at most one outstanding handoff offer per bridge. A replacement or
unrelated incompatible speech request cleans the old resource before it can
start or publish another session.

The bridge stores only a SHA-256 digest of the ticket, compares it in constant
time, and never logs it. Outside the bearer-authenticated request/response
transport, the component keeps the raw ticket only in private in-memory task
context. Tickets must still be treated as bearer secrets: do not put them in
logs, diagnostics, automations, URLs, or user-visible state.

Before offering or claiming a session, the bridge discards unsent STT input,
drains benign late STT events, and rejects any unexpected assistant audio,
assistant output, remote close, or App Server failure. A watchdog invalidates
the offer if such activity appears while it is waiting. Expiry, cancellation,
replacement, component release, bridge shutdown, and successful use all stop
the realtime session and delete its thread exactly once.

No session is inferred from “the next request.” A direct `tts.speak` (including
one invoked from the same chat session), a different Home Assistant chat
session, another bridge client, a mismatched voice or language, or a request
with custom instructions does not inherit the retained session. An
incompatible speech request closes the offer and cold-starts its own session.
If reuse fails before any PCM has been returned, synthesis may cold-start once
within the original deadline; after the first PCM, it fails instead of risking
duplicate speech.

The released component omits the handoff request, so every STT and TTS operation
has a new remote context. Direct calls do the same.

## Bounded provider prewarming

The Compose deployment keeps the bridge listener, Codex App Server process,
OAuth state, and server media stack warm. The ThirdReality client additionally
holds at most one authenticated strict-v2 session: five minutes after startup
or a completed conversation, and ten seconds after a probable wake score. The
slot occupies the account's single speech lane, while device capture remains
closed until a real wake claims it and the ready cue reaches EOF.

Always-on remote prewarming would also:

- continuously send paced silent RTP to keep the WebRTC media path active;
- occupy the account's single admitted speech-session lane;
- need speculative ownership across satellites and chat sessions;
- add cancellation and cleanup work when the next request is incompatible; and
- consume an amount of ChatGPT subscription availability that App Server does
  not document as free or quota-neutral.

The official [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
provides usage and rate-limit observability, but no idle realtime-session cost
or lifetime guarantee. The implemented policy is therefore single-slot,
profile/configuration-bound, TTL-limited, and audio-empty before assignment. It
uses the exact production session rather than caching only a local offer, which
would not remove remote admission and negotiation. The dormant device-side
worker/standby design is retained as historical evidence only.

## ThirdReality safe performance settings

These settings affect device and Home Assistant satellite behavior, not the
bridge. Change one variable at a time, keep the original value, and repeat a
fixed phrase set before accepting it.

### Wake acknowledgement

Firmware `1.01.07` normally waits for the entire wake confirmation file before
it begins forwarding microphone audio. The measured stock cue was 0.946979 s;
the current pinned direct cue is the older 0.399592-second asset (about
0.400 seconds). Active native v2 uses that shorter file as a positive readiness
boundary, not as startup overlap. The accepted Okay Nabu wake immediately
queues the thinking/pulsing LED while capture stays closed. After the
server-owned peer/data channel are ready and exact strict-v2 `started` has
arrived, the device plays exactly once the root-owned
PCM16 mono 22,050 Hz file
`/usr/lib/python3.11/site-packages/sounds/wake_word_triggered_old.wav`, SHA-256
`6b25dd2abaf7537865222ca9fd6e14fbf723458526fb79bbe29d8261d1320724`.

All wake-tail, connecting, and cue-time PCM is discarded. Only cue EOF queues
the listening LED and opens capture, so test users must wait for the sound to
finish before speaking. The legacy local stop detector remains suspended for
the entire direct ownership window, so the wake tail cannot cancel signaling
and playback echo cannot end the live session. The sole spoken terminal control
is `end_conversation`. Missing EOF or cue failure is terminal after at most two
seconds; no Assist/Hermes fallback starts. This is not a cue-free path and it
has no 384 ms initial pre-roll or 64 KiB cold-start microphone backlog. There
is no initial rollover pre-roll. Once capture is live, the server retains up to
320 ms of recent already-resampled PCM and buffers subsequent speech for one
exactly-once feed to a replacement provider generation while the device
WebSocket stays open.

The active acoustic path uses native AEC3 with Adrian maintaining the reviewed
PulseAudio playback topology. The reference device's older v2 Adrian canaries
passed at 25%, and its historical v3 rollover canary passed at that
installation's qualified 60% setting. Neither result alone validates this
route: every installation and increase above its previously qualified values
needs its own physical double-talk and echo-rejection canaries. Native AEC3 is selected by
`capture_backend: "native_aec3"` in the secure realtime configuration; the
environment flag is only an explicit diagnostic override. The active reference
configuration uses a 10 dB native baseline, noise-limited adaptive microphone
gain, moderate noise suppression, 18 dB post-wake transport gain, and a 100%
sink/playback anchor. Before APM, channel 1 enters a click-free mix only for
strongly correlated, level-compatible frames; otherwise channel 0 passes
unchanged. Its saved starting output is implemented as non-amplifying software
attenuation below the physical 100% anchor.

### Finished speaking detection

The ThirdReality Home Assistant entity's **Finished speaking detection** select
was measured at approximately 700 ms of trailing silence in its default mode
and 250 ms in `aggressive` mode, a nominal 450 ms reduction. This is
device/Home Assistant satellite endpointing; Codex Voice does not set it.

`aggressive` is appropriate only after testing natural pauses, short commands,
numbers, names, and slow speech. Revert to the default if final words are
truncated or mid-sentence pauses end capture. A faster but incomplete
transcript is not a performance improvement.

### Microphone gain

The pinned v1.1.7 firmware starts PulseAudio at `S50`, opening PDM capture
before `S99ha-speaker` reads `/data/conf/sound.json` and writes `mic_gain`.
Controlled testing confirmed that this is a latch boundary, not just a stale
display: changing PDM Gain from 34/48 to 48/48 while PulseAudio held capture
open did not materially change recorded samples. Reopening capture at gain 0
dropped the same speaker-prompt peak from about 274 to 15; reopening it at gain
48 raised the peak to about 325 per channel.

The guarded deployment hook applies the configured percentage at `S49`, before
PulseAudio opens `hw:0,2`. On the measured ALSA control, standard percentage
mapping gives 30% = 14/48, 70% = 34/48, and 100% = 48/48. The earlier claim
that a stored value of 50 produced 34/48 was a stale-snapshot attribution;
34/48 corresponds to 70%. Treat the stored percentage as a device preference
and verify both the control and actual captured audio after each firmware or
boot change. Invalid or out-of-range settings are not clamped; the early hook
uses the vendor's 30% fail-safe.

The maximum setting is an acceptance candidate, not a universal
recommendation. Room acoustics and individual hardware vary. Changing the
stored preference requires ALSA capture to reopen. The early hook makes that
deterministic on the next controlled reboot; a separately managed PulseAudio
stop/start can latch it sooner. Restarting only the voice process cannot.

Test near, typical, and far speech plus a loud voice. Accept the higher gain
only when recognition improves without clipped peaks, elevated background
activation, echo-related failures, or degraded wake detection. Hardware
clipping cannot be repaired by the bridge's bounded adaptive normalization.
Use the bridge's privacy-safe numeric peak/RMS and adaptive-gain logs for
comparison; they never include speech or transcripts.

## Official v1.2 C++ firmware canary evaluation

Home Assistant reported `1.01.07` installed and `1.02.01` available on the
measured unit. Those display versions correspond to upstream firmware tags
v1.1.7 and v1.2.1.

The official [v1.2.0 release](https://github.com/thirdreality/voice-music-assistant/releases/tag/v1.2.0)
replaces the Python assistant with a native C++ binary and adds hardware-loopback
WebRTC AEC3. Its notes also describe a default 0.5 s continued-conversation
settle delay, unified voice/music volume behavior, and playback and memory
stability fixes. The [v1.2.1 release](https://github.com/thirdreality/voice-music-assistant/releases/tag/v1.2.1),
dated 2026-07-30, updates Sendspin, fixes DNS handling, and enables ADB over USB
and TCP.

### Production-device decision: no-go

The measured speaker must remain on its known-good 1.01.07 image. The vendor
images and tagged board source establish a single boot/system/recovery layout,
not two independently bootable slots: the board configuration leaves
[`CONFIG_AB_SYSTEM`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/sources/uboot/board/amlogic/configs/axg_s420_v1_trspk.h)
disabled, and the UBI configuration defines one dynamic
[`rootfs`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/fs/ubi/ubinize.cfg)
volume. The tree contains generic A/B support, but this board does not select
it. A flash of the only speaker is therefore not an A/B experiment.

The cached 1.1.7 file is a stock full-burn image, not a bit-for-bit read-back of
this device, and the configuration backup does not contain bootloader, boot,
recovery, rootfs/UBI, U-Boot environment, or device security state. Recovery
exposes neither SSH nor ADB, so TCP/5555 cannot rescue a system that fails
before the normal root filesystem boots. Until physical burn-mode entry and a
full 1.2.1-to-stock-1.1.7 downgrade have been rehearsed on identical spare
hardware, the production upgrade decision is **no-go**. Even a successful
stock-image downgrade is not exact restoration without this speaker's own
complete pre-flash read-backs.

Native C++ and lower memory use do not by themselves guarantee a faster Assist
turn. The v1.2.1
[`Satellite.cpp`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/package/thirdreality/linux-voice-assistant-cpp/src/satellite/Satellite.cpp)
still starts microphone streaming only after the wake cue callback. The
tagged v1.2.1 source vendors and installs the exact same cue blob as v1.1.7;
that asset measures 0.946979 s, so the upgrade alone does not remove that
gate. Its short-sound-safe
[`LibMpvPlayer.cpp`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/package/thirdreality/linux-voice-assistant-cpp/src/audio/LibMpvPlayer.cpp)
sets mpv's `audio-buffer` to `0.8`. That setting must be measured; it should not
be treated as an additive 0.8 s delay without evidence.

Only after those gates pass, run a paired comparison using a physically
separate canary while the known-good speaker remains untouched. Use the same
room, wake word, pipeline, voice, network path, command set, and playback
volume. Record at least:

- wake detection to microphone-stream start and the cue duration;
- utterance length and trailing-silence endpoint;
- STT completion and transcription accuracy/truncation;
- intent start to first TTS PCM and to first audible playback;
- TTS start to announcement finished;
- continued-conversation echo and settle behavior; and
- CPU, memory, audio underruns, disconnects, and recovery after restart.

Keep each raw run rather than reporting only the best result. Compare medians
and ranges, and return to the old image if accuracy, echo control, stability,
or tail latency regresses even when one median improves.

### Backup and rollback before a canary flash

Firmware flashing can erase configuration or make a device temporarily
unbootable. Before a v1.2 canary flash:

1. Require independently authenticated image provenance. After provenance is
   established, record SHA-256 hashes, byte sizes, source URLs, and acquisition
   dates in a private manifest. A locally calculated hash is only an identifier
   and later-corruption check; it does not authenticate the publisher. The
   v1.2.1
   [`UpdateEntity_Ota.cpp`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/package/thirdreality/linux-voice-assistant-cpp/src/entities/UpdateEntity_Ota.cpp)
   and
   [`OtaClient.cpp`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/package/thirdreality/linux-voice-assistant-cpp/src/tr/OtaClient.cpp)
   disable TLS peer and hostname verification for update metadata and image
   downloads.
2. Capture raw, verified read-backs of this exact device's bootloader, DTB,
   boot, recovery, rootfs/UBI, U-Boot environment, and complete `/data` before
   flashing. Record read-only secure-boot, lock, fuse, and anti-rollback state.
   Include `/data/conf`, every locally modified init script, and every modified
   audio asset. Treat Wi-Fi and service configuration as secrets; do not
   publish the archive or its host path.
3. On identical spare hardware, physically enter USB/burn recovery without
   Linux, ADB, or SSH; flash the exact candidate; then perform and verify the
   complete stock-image downgrade and restoration procedure. This rehearsal is
   mandatory, not optional.
4. Use stable power during flashing. After boot, verify the reported firmware,
   Home Assistant entities, wake/stop words, microphone, TTS, volume, network,
   and restart recovery before performance testing. The v1.2.0 release notes
   warn that the first v1.1-to-v1.2 OTA can leave a stale Home Assistant entity.
5. Do not flash the production speaker unless exact restoration from its own
   read-backs is proven. A stock 1.1.7 reflash proves only a downgrade, not
   recovery of the device's prior state. Restore version-specific configuration
   only to matching firmware; never blindly overlay one version's rootfs onto
   another.

### Harden root access after v1.2.1

The official v1.2.1
[`S55adbd`](https://github.com/thirdreality/voice-music-assistant/blob/v1.2.1/buildroot/board/thirdreality/trspk/rootfs/etc/init.d/S55adbd)
defaults `ADB_TCP_PORT` to `5555`. Its own warning states that the socket binds
to all interfaces while `adbd` runs as root with `ro.adb.secure=0`. Anyone who
can reach that port can therefore obtain an unauthenticated root shell.

Put the device on an isolated network while testing. If TCP ADB is required,
keep port 5555 enabled but restrict it at the network boundary to the specific
administration host or management network that needs it. Verify that the
approved host can reconnect after reboot and that other LAN/VLAN hosts cannot;
never expose the port to the internet. Because the daemon itself provides no
authentication and runs as root, a broad trusted-LAN rule is not an adequate
substitute for source restriction. Repeat both the allowed- and denied-source
checks after every firmware update.

For the measured device in this project, TCP ADB is an explicit normal-OS
remote-administration requirement: canary, reboot, and rollback procedures must
leave port 5555 enabled and verify that the administration path can reconnect.

This project's device requires TCP ADB for recovery. Do not stop `adbd`, clear
`ADB_TCP_PORT`, restart the ADB service as part of voice deployment, or disable
`S55adbd`. Keep port 5555 enabled and verify the designated administration host
can reconnect before and after every voice restart, reboot, rollback, and
firmware update. Apply protection at the network boundary instead.

The tagged board configuration also enables Dropbear SSH, and the official
v1.2.1 instructions publish a fixed root password. Do not leave that default
reachable from the LAN. From a trusted local or serial console, install a
unique credential and disable root password authentication if the shipped
Dropbear build and recovery design support it; otherwise disable the SSH boot
service or block TCP port 22 at both the device and network boundary. Verify
from another LAN host that the old credential no longer works and that any
intentionally closed port remains closed after reboot and every firmware
update. Preserve a tested local/serial recovery route before disabling both
remote administration services.
