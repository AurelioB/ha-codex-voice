# ThirdReality v1.1.7 wake-latency overlay

The Python-based ThirdReality `1.01.07`/upstream `v1.1.7` client waits for the
wake confirmation cue to finish before it asks Home Assistant to prepare the
Assist pipeline. On the measured device that leaves a 0.399592-second local
setup gap after wake detection, even with the shortened cue.

[`latency_sitecustomize/sitecustomize.py`](latency_sitecustomize/sitecustomize.py)
sends the pipeline start request immediately after wake detection, then keeps
microphone forwarding disabled until the cue reaches EOF. This overlaps Home
Assistant setup without sending the device's own cue into STT on hardware
without active acoustic echo cancellation.

The EOF callback carries a per-wake generation token and checks the current
pipeline, connection, and mute state. A callback from a cancelled, disconnected,
ended, or replaced wake cannot re-enable microphone streaming. The override is
applied only when SHA-256 hashes of both installed vendor code objects match the
tested build. A mismatch logs a warning and leaves all vendor behavior intact.

Wake setup is transactional: a send, duck, player, mute, or disconnect failure
rolls local pipeline and streaming flags back to idle and best-effort cancels a
start request that already reached Home Assistant. A two-second watchdog also
aborts the run if mpv never reports cue EOF (for example, because another sound
replaced playback), so later wake words cannot remain blocked behind a phantom
active pipeline. Microphone forwarding is explicitly disabled before every
start request.

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
launch line and restart only the `voice-assistant` service. Verify the process
command, source ownership/mode/hash, absence of `__pycache__` in the import
directory, and TCP ADB connection after restart.

Python imports `sitecustomize` during process startup. The script validates all
compatibility guards before mutating the vendor class. An import failure or
unknown bytecode is non-destructive: Python reports the error or warning and
the original wake implementation remains installed.

## Acceptance and rollback

The repository test covers normal cue completion, Home Assistant run end,
manual cancellation, disconnect and mute races, setup exceptions, a replaced
wake, missing EOF/watchdog cancellation, request ordering, and the
unknown-bytecode fail-closed path. On the physical device, additionally test
immediate and delayed commands, short and long speech, mute during the cue,
reconnects, continued conversation, timers, and repeated wakes. Reject the
overlay if it clips initial phonemes, forwards cue audio, leaks microphone audio
after cancellation, fails to return to idle, or destabilizes wake detection.

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
