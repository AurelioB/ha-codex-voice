# ThirdReality v1.1.7 device overlay

This directory contains the guarded wake-latency patch and the Milestone 2
realtime client for the Python-based ThirdReality
`1.01.07`/upstream `v1.1.7` image. The realtime client uses only the Python
standard library, runs inside the existing `linux_voice_assistant` process, and
does not add a daemon, replace firmware, or change Home Assistant.

The two wake phrases deliberately have different authority:

| Wake phrase | Route | Capability |
|---|---|---|
| Okay Nabu | Official Home Assistant Assist satellite flow | Local/remote STT, Conversation, TTS, and Home Assistant controls |
| Okay Computer | Direct realtime wire v2 to the Codex Voice bridge | Subscription voice over untrusted audio/control wire; optional Home Assistant tools only through a separate HA-owned broker |

Okay Nabu can preempt an active direct session and immediately reclaim the
microphone for the normal Home Assistant path. Saying the configured stop word,
starting the normal Home Assistant path, mute/disconnect, or otherwise releasing
the vendor owner flushes local playback and tears down the remote session. A
later wake after any such teardown creates a fresh WebSocket and realtime
session. In opt-in full-duplex mode, bounded local AEC-filtered speech detection
can flush playback before provider VAD for natural barge-in without releasing
that owner. An explicit interrupt preserves
the same socket only when the bridge returns an authoritative acknowledgement
that remote cancellation succeeded; timeout or ambiguous cancellation closes
the session.

## Realtime behavior and bounds

[`realtime_client`](realtime_client) opens the authenticated v2 WebSocket,
sends 16 kHz mono PCM16 microphone frames, and plays 24 kHz mono PCM16 through
a fixed-argument `paplay` child. Explicit `speaking.started` and
`speaking.stopped` epochs own playback. In the default turn-taking mode,
microphone frames are gated for as long as remote or locally buffered output
can still be audible. Opt-in full duplex keeps microphone submission continuous
only after a startup preflight verifies the exact echo-cancel source, sink, and
loaded PulseAudio module.

The default input and pre-ready fallback buffers are each 64 KiB: 2.048 seconds
at 16 kHz mono PCM16. Once a cold handshake completes, queued input is sent at
no more than 2× capture rate while more than one captured frame remains, then
returns to realtime pacing. This bounded catch-up preserves accepted audio and
removes a permanent startup offset; it does **not** remove the cold
App Server/WebRTC handshake or provider-generation latency. If startup fails or
either pre-ready buffer fills, the overlay re-enters the official Home
Assistant wake path and replays the retained prefix on the pinned microphone
thread. A capacity or protocol failure after v2 is ready ends the direct
session safely instead of silently dropping audio.

Wake activation occurs after the pinned recorder callback has already handled
the triggering frame. While the satellite is idle, connected, and unmuted, the
overlay therefore retains only the newest six 2,048-byte recorder frames in
RAM: 384 ms, or 12 KiB. An Okay Computer wake atomically takes that direct-only
pre-roll and seeds both the realtime input queue and its one-time Assist
fallback copy. An Okay Nabu wake discards it, so the official Assist path still
receives only post-wake audio. Stop, mute, disconnect, and teardown also clear
the buffer without persisting or logging it.

Pre-roll never consumes the client's reserved 32 KiB (1.024 s) of live
post-wake capacity. It is trimmed from the oldest samples or omitted when a
smaller legal input or fallback queue needs that headroom; the default 64 KiB
queues retain all 12 KiB. This allowance is part of the existing queue bounds,
not additional unbounded storage.

The default device output queue is 48 KiB, about 1.024 seconds at 24 kHz mono
PCM16. The bridge separately caps only v2 live input at 2,250 ms; its finite STT
adapters retain their whole-utterance capacity. These are safety bounds, not a
latency promise.

`full_duplex` remains `false` by default. Setting it to `true` also requires
explicit safe `pulse_aec_source` and `pulse_aec_sink` names and permits only the
allowlisted `pulse_aec_method` values `webrtc`, `speex`, or `adrian`. Omitting
the method defaults to WebRTC for compatibility; the client never probes or
falls back to another engine. Before it connects to the bridge, the client
requires those exact PulseAudio defaults and a loaded `module-echo-cancel` with
the expected raw hardware masters, endpoint names, configured method, and
master format. It also requires an uncorked native capture stream owned by the
current voice-process PID—including the already-open vendor recorder—to use
that AEC source, and requires every live AEC sink channel to be at or below
`aec_test_volume_percent`; output `paplay` is pinned to the same sink. A failed
or mismatched check ends startup before any microphone audio leaves the device.
Every full-duplex `paplay` child also receives a fixed linear stream-volume cap
derived from that percentage, so a new response cannot start above the canary
ceiling established by configuration. The sink-volume check runs again before
every `speaking.started`, not only during startup; a raised channel fails the
response closed. The guard compares raw PulseAudio units to the exact linear
ceiling rather than trusting the rounded displayed percent.

While verified full duplex is active, capture remains continuous during
playback. Two consecutive 64 ms AEC-filtered microphone frames that meet the
bounded peak and sustained-energy checks request a local flush; the network
thread terminates the owned `paplay` child and quarantines already-in-flight
PCM for that exact output epoch. A provider
`input_audio_buffer.speech_started` event independently reinforces the same
local boundary. Neither signal itself claims remote cancellation or tears down
the session. The client separately negotiates
`same_session_interrupt_ack: true`, accepts the bridge's sanitized
`response.cancelled` event, and reuses the socket only for the explicit
`fresh_session_required: false` / `remote_cancelled: true` stopped
acknowledgement. The older safe fallback acknowledgement still closes it.

The observed stock `1.01.07`/v1.1.7 PulseAudio build rejects both WebRTC and
Speex because those engines are not compiled in. Adrian loads with the pinned
`hw:0,2`/`hw:0,1` masters, `use_master_format=1`, and creates the expected
16 kHz mono source and sink. The reference device passed a bounded canary at
25%: 5.531 seconds of assistant playback caused no false interrupt across 86
microphone frames (maximum peak 2 and integer RMS 0), while staged double-talk
stopped local output in 141 ms and continued on the same session. That result
does not qualify another installation. Install and qualify the static
PulseAudio AEC assets in [`deploy`](deploy) first. The initial acoustic canary must use the
configured `aec_test_volume_percent`, which defaults to 25 and is hard-limited
to 1–25. Do not exceed 25% until echo rejection and early, middle, and late
double-talk barge-in pass on that physical device. Speex is available only on
a different firmware build that actually compiles that engine.

## Wake-latency patch

The Python-based ThirdReality `1.01.07`/upstream `v1.1.7` client starts the wake
confirmation cue but waits for cue EOF before it asks Home Assistant to prepare
the Assist pipeline. After starting that asynchronous cue, its ThirdReality
wrapper performs a synchronous LED DBus call on the microphone thread with a
two-second timeout. In three 2026-08-09 human baselines, Home Assistant VAD
began 1.37, 2.46, and 3.27 seconds after pipeline start. Those end-to-end values
include the combined device and Home Assistant path; they do not isolate the
LED call by itself.

[`latency_sitecustomize/sitecustomize.py`](latency_sitecustomize/sitecustomize.py)
uses an LED-only acknowledgement and does not play wake audio; this cue-free
path remains independent of whether the optional full-duplex AEC topology is
enabled. On the pinned single microphone thread, it pre-arms forwarding, queues
the pipeline start request, and ducks music without playing the local wake cue.
For Okay Computer only, its bounded RAM pre-roll recovers recorder
frames handled before that direct owner exists; Okay Nabu explicitly discards
the same history. The
ThirdReality LED command is queued on one daemon worker, so the DBus subprocess
can no longer block microphone capture. Commands are serialized and bounded by
the vendor's two-second timeout. If the bounded queue fills, stale pending
animations are coalesced into the newest state; timed-out children are reaped.
Both the vendor base class and the pinned ThirdReality subclass are patched
directly. This prevents a later base-method rebinding during device startup from
restoring the cue gate while retaining the guarded LED behavior.

The override is applied atomically only when SHA-256 hashes of all four
installed vendor code objects match the tested build: the base wake and cue-EOF
methods plus the ThirdReality wake wrapper and LED helper. A mismatch logs a
warning and leaves both vendor modules intact.

Wake setup is transactional: a send or duck failure rolls local pipeline and
streaming flags back to idle and best-effort queues a cancellation if a start
request was attempted. Pre-arming occurs on the same microphone thread that
forwards audio, so it cannot leak a frame before wake setup returns. The overlay
rechecks the armed state after both external calls, and never sets it again;
pinned VAD/STT-end, mute, disconnect, and run-end teardown therefore wins any
startup race.

The overlay does not replace vendor modules, change Home Assistant, modify the
wake audio file, reboot the speaker, or enable/disable USB or TCP ADB.

## Deployment contract

Published releases attach `thirdreality-realtime.zip` with this layout:

```text
latency_sitecustomize/
  sitecustomize.py
realtime_client/
  __init__.py
  config.py
  session.py
  websocket.py
deploy/
  README.md
  prepare_pulseaudio_aec.py
  pulse/
    codex-echo-cancel.pa
    codex-echo-cancel-speex.pa
    codex-echo-cancel-adrian.pa
codex-realtime.example.json
README.md
LICENSE
```

The example contains documentation-only addresses and a placeholder—not a
credential. Generate a distinct `HA_CODEX_REALTIME_DEVICE_TOKEN` on the bridge
host, configure that same value only in the root-only device file, and never
put the token in the archive, repository, shell history, or diagnostics. The
device token is accepted only by `/v1/realtime` after a valid v2 negotiation;
it cannot enter legacy v1 or `/v1/home-assistant/tools`. Do not reuse the Home
Assistant bridge token, a Home Assistant access token, or Codex `auth.json`.

The device never receives tool schemas, tool calls, results, or a Home
Assistant credential. Realtime home control is disabled by default. To enable
it, explicitly select **Provide Home Assistant tools to realtime voice** on
exactly one Codex Voice Conversation subentry and choose only the Home Assistant
LLM APIs that should be exposed. The integration uses its primary bridge token
to maintain a separate broker connection, defaults that authority's response
locale to `es-MX`, and executes every correlated call inside Home Assistant.
Zero, multiple, disconnected, or changed authorities fail closed; nothing is
added to the device configuration or v2 frames.

Treat `PYTHONPATH` as root-process code execution. Copy only the reviewed file
and package from a pinned repository commit or release asset, verify SHA-256
after transfer, make the directory and source files root-owned, and deny
group/other writes. Use a mode-0755 directory and mode-0644 Python files. Never
put a user-writable directory on the root service's import path.

Copy `latency_sitecustomize/sitecustomize.py` from the archive to the root of a
dedicated import directory and copy `realtime_client/` beside it. The installed
layout should be:

```text
/data/conf/codex-python/
  sitecustomize.py
  realtime_client/
    __init__.py
    config.py
    session.py
    websocket.py
```

Add `/data/conf/codex-python` to `PYTHONPATH` only for the
`python3 -m linux_voice_assistant` process. Set
`PYTHONDONTWRITEBYTECODE=1` on the same launch line: this prevents a permissive
device umask from creating a group/world-writable `__pycache__` beneath a
root-process import path. Back up the exact init script before changing its
launch line.

Create `/data/conf/codex-realtime.json` as a root-owned regular file with mode
0600. Start from [`codex-realtime.example.json`](codex-realtime.example.json),
replace the documentation address and placeholder token, then explicitly set
`enabled` to `true`. A minimal active configuration is:

```json
{
  "enabled": true,
  "url": "ws://192.0.2.10:8787/v1/realtime",
  "connect_address": "192.0.2.10",
  "token": "REPLACE_WITH_DISTINCT_REALTIME_DEVICE_TOKEN",
  "wake_phrase": "okay computer",
  "voice": "cove",
  "prompt": "Responde en español latinoamericano de México salvo que el usuario pida explícitamente otro idioma. Usa un acento mexicano natural, estable y claro; mantén separados el idioma y el acento, y no cambies de idioma solo por el acento del usuario. Sé conciso.",
  "full_duplex": false,
  "aec_test_volume_percent": 25
}
```

Use `wss` with normal certificate validation whenever the path is not a
source-restricted trusted LAN. `connect_address` must be the numeric address to
which the client connects; the URL supplies the HTTP Host/TLS server name.
Unknown keys, symlinks, non-root ownership, group/other access, unsafe URLs,
and invalid bounds fail closed. `max_message_bytes` ranges from 2,048 through
65,536 bytes (the example and default use 65,536) and bounds WebSocket payload
bytes, excluding framing overhead; its minimum therefore carries exactly one
fixed 2,048-byte recorder frame. An absent or explicitly disabled config
leaves the direct client inactive while retaining the guarded wake-latency
patch.

To qualify full duplex, first follow [`deploy/README.md`](deploy/README.md).
The pinned PulseAudio server starts with `--disallow-module-loading`, and its
`default.pa.d` include occurs before the raw hardware masters; dynamic module
loading or a naive drop-in cannot establish the required startup order. The
guarded helper dry-runs by default, backs up the root config, and appends an
exact fail-closed block after the `hw:0,2` capture and `hw:0,1` playback
masters. Its `--aec-method` allowlist is `webrtc`, `speex`, and `adrian`; an
omitted flag means WebRTC and never triggers a fallback. The stock v1.1.7 image
must use `--aec-method adrian`. The helper never restarts services, changes
volume, or changes ADB. Once its static and physical canaries pass, add all five
settings together:

```json
{
  "full_duplex": true,
  "pulse_aec_source": "codex_echo_cancel_source",
  "pulse_aec_sink": "codex_echo_cancel_sink",
  "pulse_aec_method": "adrian",
  "aec_test_volume_percent": 25
}
```

PulseAudio object names must start with an ASCII letter, contain only ASCII
letters, digits, `.` or `_`, and be at most 128 characters. Supplying an AEC
route or method while full duplex is disabled, omitting either route while it
is enabled, selecting a method outside the three-value allowlist, or setting the
canary volume outside 1–25 fails configuration loading. Omitting the method in
full duplex selects WebRTC rather than detecting or substituting an available
engine. The shipped disabled configuration example intentionally omits the AEC
routes and method so changing only `enabled` cannot activate unqualified full
duplex.

Optional `voice` and `prompt` settings are sent only in the v2 start message.
Voice names start with an ASCII letter, contain 1–64 ASCII letters, digits,
`_`, or `-`, and are normalized to lowercase. Prompts are printable, non-empty
text up to 1,024 characters. Their
actual compact, ASCII-escaped start message must fit `max_message_bytes`, so a
small custom message bound may require a shorter prompt. Prompt text and the
bearer token are omitted from the configuration object's diagnostic
representation. The example selects the existing `cove` realtime voice and
keeps language and accent as separate instructions: Spanish is the default
language unless explicitly changed, while the voice keeps a stable, natural
Mexican accent instead of switching language merely because of the user's
accent. Omitting both settings preserves the provider defaults.

Enable both installed detector identifiers—`okay_nabu` and
`okay_computer`—in the device's existing sound configuration without replacing
its other settings. Keep Okay Nabu as the normal Home Assistant wake phrase and
make `wake_phrase` match Okay Computer. Do not assign the same phrase to both
paths.

The v1.1.7 unified init script keeps its supervision functions in a long-lived
shell. Editing the script does not update the function already held by that
monitor: a voice-only restart can briefly launch the overlay and then be
replaced by the stale monitor without `PYTHONPATH`. Refresh the unified monitor
once through the device's normal service manager (or reboot during an approved
maintenance window), then start the voice child from the updated definition.
When invoking the service manager through ADB, ensure its background monitor is
detached from the controlling shell rather than relying on a short-lived remote
shell job.

Verify the *long-lived* process PID after at least one monitor interval, its
`PYTHONPATH`/`PYTHONDONTWRITEBYTECODE` environment, source and config
ownership/mode/hash, absence of `__pycache__` in the import directory, and TCP
ADB connection. A physical wake must also show no
`wake_word_triggered_old.wav` playback. Merely importing `sitecustomize` in a
separate probe process is not acceptance.

Python imports `sitecustomize` during process startup. The script validates all
compatibility guards before mutating the vendor class. An import failure or
unknown bytecode is non-destructive: Python reports the error or warning and
the original wake implementation remains installed.

## Acceptance and rollback

The repository tests cover immediate request/duck/stream ordering, the first
post-wake microphone frame, VAD/run-end/disconnect/mute flag races during setup,
startup guards and exceptions, bounded direct-only pre-roll, normal-wake
discard, 32 KiB live headroom, normal-wake preemption, pre-ready fallback and
replay, strict v2 framing, bounded pacing and queues, output-epoch playback,
PulseAudio AEC preflight and explicit sink routing, continuous full-duplex
capture, speech-start local flush and late-frame quarantine, authoritative
same-socket interrupt acknowledgement, guarded static-AEC installation and
rollback, interrupt cleanup, timer interruption, serialized non-blocking LED
execution, newest-state overload coalescing, DBus timeout/nonzero handling,
explicit worker shutdown, and the atomic unknown-bytecode fail-closed path.

On the physical device, verify these independently:

1. Okay Nabu still completes Home Assistant commands, timers, announcements,
   reconnects, and repeated wakes.
2. Okay Computer starts direct voice, speaks a response, returns to listening,
   and responds to the stop word without replaying its own output.
3. With realtime authority disabled, Okay Computer cannot control Home
   Assistant. With exactly one deliberately opted-in Conversation authority,
   it can invoke only a reviewed test entity from the selected LLM API, while
   the device still receives no tool schema, call, result, or primary token.
4. A normal wake can preempt direct mode and then control Home Assistant.
5. A forced pre-ready connection failure enters the official Assist fallback
   without clipping the retained command prefix.
6. Repeated direct and standard turns leave one stable voice process, no
   `paplay` child or LED/duck state behind, and bounded memory.
7. At no more than the configured 25% canary volume, speak over early, middle,
   and late response audio from near and far positions. Playback must stop at
   speech detection, the command must be recognized once, and response audio
   must not appear as user speech. Repeat after an idle gap and reconnect.
   Raising any AEC sink channel above the configured ceiling between responses
   must fail the next response, and the spawned `paplay` stream must remain
   pinned at or below 25%.
8. A deliberately missing AEC module or mismatched default source/sink must fail
   direct startup before sending microphone audio, while Okay Nabu remains
   usable after rollback to `full_duplex: false`.
9. TCP ADB remains reachable on port 5555 before deployment, after the service
   restart, and after any reboot. The overlay and its procedures must never
   stop `adbd`, blank its TCP-port setting, or remove that recovery path.

One post-deployment physical regression canary replayed the exact previously
failing sample with 308 ms of silence between wake phrase and command. The
device-local input playback ran from 21:52:39 through 21:52:42, the bridge
completed its handshake in 1.308 s, and device-local answer playback began at
21:52:45. The command was captured and answered end to end. This single
controlled run validates the specific clipping regression; it is not a latency
distribution or benchmark.

Reject the overlay if it clips initial phonemes, plays or forwards cue audio,
leaks microphone audio after cancellation, echoes its own response, fails to
return to idle, leaves stale player/LED/duck state, destabilizes wake detection,
or breaks the standard Home Assistant path.

To disable only direct mode, set `enabled` to `false` in the root-only config
and restart `voice-assistant`; the latency patch remains active. Full rollback
first sets `full_duplex` to `false`, removes the exact managed PulseAudio tail
with the guarded deploy helper, and performs the controlled PulseAudio/service
restart. Removing the process-specific `PYTHONPATH` assignment and restarting
only `voice-assistant` then allows untouched vendor bytecode to supply the
original behavior. Preserve the PulseAudio, init-script, and configuration
backups until all physical tests pass. Verify TCP ADB on port 5555 before and
after every operation.

## Rejected global playback-cache experiment

An earlier candidate reduced module-level mpv network-cache constants. One
uncached `tts.speak` comparison observed about 3.21 seconds of stock player
state overhead versus 3.07 seconds with that candidate, but the responses were
not identical and the difference was noisy. Those globals also configure the
music player, so the change could trade a small unproven announcement gain for
internet-radio underruns. The shipped overlay preserves the vendor playback
cache values.
