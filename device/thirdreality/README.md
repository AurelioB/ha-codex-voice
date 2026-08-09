# ThirdReality v1.1.7 device overlay

This directory contains the guarded wake-latency patch and the Milestone 2
realtime turn-taking client for the Python-based ThirdReality
`1.01.07`/upstream `v1.1.7` image. The realtime client uses only the Python
standard library, runs inside the existing `linux_voice_assistant` process, and
does not add a daemon, replace firmware, or change Home Assistant.

The two wake phrases deliberately have different authority:

| Wake phrase | Route | Capability |
|---|---|---|
| Okay Nabu | Official Home Assistant Assist satellite flow | Local/remote STT, Conversation, TTS, and Home Assistant controls |
| Okay Computer | Direct realtime wire v2 to the Codex Voice bridge | Subscription-backed chat voice only; no Home Assistant tools |

Okay Nabu can preempt an active direct session and immediately reclaim the
microphone for the normal Home Assistant path. Saying the configured stop word
or otherwise stopping direct mode flushes local playback and tears down the
remote session. A later direct turn always creates a fresh WebSocket, Codex
thread, and realtime session; the provider does not offer reliable response
cancellation or truncation.

## Realtime behavior and bounds

[`realtime_client`](realtime_client) opens the authenticated v2 WebSocket,
sends 16 kHz mono PCM16 microphone frames, and plays 24 kHz mono PCM16 through
a fixed-argument `paplay` child. Explicit `speaking.started` and
`speaking.stopped` epochs own playback. In the released turn-taking mode,
microphone frames are gated for as long as remote or locally buffered output
can still be audible.

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

`full_duplex` accepts only `false` for this release; `true` fails configuration
loading. The v1.1.7 deployment has no active acoustic echo cancellation, so
listening while `paplay` is audible would feed the assistant's response back
into its microphone. True full duplex, measured AEC, and barge-in acceptance
are future milestones.

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
uses an LED-only acknowledgement on this hardware without active acoustic echo
cancellation. On the pinned single microphone thread, it pre-arms forwarding,
queues the pipeline start request, and ducks music without playing the local
wake cue. For Okay Computer only, its bounded RAM pre-roll recovers recorder
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
codex-realtime.example.json
README.md
LICENSE
```

The example contains documentation-only addresses and a placeholder—not a
credential. Generate a distinct `HA_CODEX_REALTIME_DEVICE_TOKEN` on the bridge
host, configure that same value only in the root-only device file, and never
put the token in the archive, repository, shell history, or diagnostics. The
device token is accepted only by `/v1/realtime` after a valid v2 negotiation;
it cannot enter legacy v1. Do not reuse the Home Assistant bridge token, a Home
Assistant access token, or Codex `auth.json`.

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
  "full_duplex": false
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
interrupt cleanup, timer interruption, serialized non-blocking LED execution,
newest-state overload coalescing, DBus timeout/nonzero handling, explicit
worker shutdown, and the atomic unknown-bytecode fail-closed path.

On the physical device, verify these independently:

1. Okay Nabu still completes Home Assistant commands, timers, announcements,
   reconnects, and repeated wakes.
2. Okay Computer starts direct chat, speaks a response, returns to listening,
   and responds to the stop word without replaying its own output.
3. A normal wake can preempt direct mode and then control Home Assistant.
4. A forced pre-ready connection failure enters the official Assist fallback
   without clipping the retained command prefix.
5. Repeated direct and standard turns leave one stable voice process, no
   `paplay` child or LED/duck state behind, and bounded memory.
6. TCP ADB remains reachable on port 5555 before deployment, after the service
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
removes the process-specific `PYTHONPATH` assignment and restarts only
`voice-assistant`, allowing untouched vendor bytecode to supply the original
behavior. Preserve the init-script and configuration backups until all physical
tests pass. Verify TCP ADB on port 5555 before and after either operation.

## Rejected global playback-cache experiment

An earlier candidate reduced module-level mpv network-cache constants. One
uncached `tts.speak` comparison observed about 3.21 seconds of stock player
state overhead versus 3.07 seconds with that candidate, but the responses were
not identical and the difference was noisy. Those globals also configure the
music player, so the change could trade a small unproven announcement gain for
internet-radio underruns. The shipped overlay preserves the vendor playback
cache values.
