# ThirdReality v1.1.7 wake-latency overlay

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
wake cue; no microphone frame can be handled until those calls return. The
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

Treat `PYTHONPATH` as root-process code execution. Copy only the reviewed file
from a pinned repository commit, verify its SHA-256 after transfer, make the
directory and file root-owned, and deny group/other writes. For example, use a
mode-0755 directory and mode-0644 file. Never put a user-writable directory on
the root service's import path.

Copy the file to a dedicated device directory such as
`/data/conf/codex-python/sitecustomize.py`, then add that directory to
`PYTHONPATH` only for the `python3 -m linux_voice_assistant` process. Set
`PYTHONDONTWRITEBYTECODE=1` on the same launch line: this prevents a permissive
device umask from creating a group/world-writable `__pycache__` beneath a
root-process import path. Back up the exact init script before changing its
launch line.

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
`PYTHONPATH`/`PYTHONDONTWRITEBYTECODE` environment, source ownership/mode/hash,
absence of `__pycache__` in the import directory, and TCP ADB connection. A
physical wake must also show no `wake_word_triggered_old.wav` playback. Merely
importing `sitecustomize` in a separate probe process is not acceptance.

Python imports `sitecustomize` during process startup. The script validates all
compatibility guards before mutating the vendor class. An import failure or
unknown bytecode is non-destructive: Python reports the error or warning and
the original wake implementation remains installed.

## Acceptance and rollback

The repository test covers immediate request/duck/stream ordering, the first
post-wake microphone frame, VAD/run-end/disconnect/mute flag races during setup,
startup guards and exceptions, timer interruption, serialized non-blocking LED
execution, newest-state overload coalescing, DBus timeout/nonzero handling,
explicit worker shutdown, and the atomic unknown-bytecode fail-closed path.
On the physical device, additionally test immediate and delayed commands, short
and long speech, reconnects, continued conversation, timers, and repeated
wakes. Reject the overlay if it clips initial phonemes, plays or forwards cue
audio, leaks microphone audio after cancellation, fails to return to idle,
leaves stale LED state during repeated wakes, or destabilizes wake detection.

Rollback consists of removing the process-specific `PYTHONPATH` assignment and
restarting only `voice-assistant`; the untouched vendor bytecode then supplies
the original behavior. Preserve the init-script backup until all physical
tests pass. Verify TCP ADB on port 5555 before and after either operation.

## Rejected global playback-cache experiment

An earlier candidate reduced module-level mpv network-cache constants. One
uncached `tts.speak` comparison observed about 3.21 seconds of stock player
state overhead versus 3.07 seconds with that candidate, but the responses were
not identical and the difference was noisy. Those globals also configure the
music player, so the change could trade a small unproven announcement gain for
internet-radio underruns. The shipped overlay preserves the vendor playback
cache values.
