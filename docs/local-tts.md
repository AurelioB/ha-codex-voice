# Reliable local text-to-speech

The recommended production Assist pipeline keeps staged Home Assistant
provider boundaries while moving speech rendering off the experimental Codex
realtime compatibility adapter:

```text
microphone -> Wyoming faster-whisper STT -> Codex Voice Conversation
           -> Wyoming Piper TTS -> speaker
```

For Mexican Spanish, the validated voice is `es_MX-ald-medium`. Piper runs
locally, so synthesis does not open a Codex realtime thread, occupy the
subscription speech lane, or send the response text to a speech provider on
the internet. “Okay Computer” is intentionally different: the optional direct
route continues to use Codex realtime conversation and speech.

## Home Assistant OS

Install the official Piper add-on (shown as an app in current Home Assistant
UI), select `es_MX-ald-medium`, and add its discovered **Wyoming Protocol**
integration. Then select the resulting `tts.*` entity and voice in the Assist
pipeline. See the official [Piper integration
documentation](https://www.home-assistant.io/integrations/piper/).

On some virtualized x86-64 Home Assistant OS installations, the current Piper
add-on may require the virtual CPU to expose x86-64-v2 instructions. A guest
using a generic or older emulated CPU model can therefore fail even when the
physical host is capable. This caveat is limited to affected x86-64
virtualization setups; it is not a claim that every Piper installation or
architecture requires x86-64-v2.

When appropriate, expose a compatible host/guest CPU model and retry the
official add-on. If changing the VM CPU contract is undesirable or unavailable,
run the external Wyoming service below. Home Assistant uses the same native
Wyoming provider contract for either deployment.

## External Linux host

Run this section from a full repository checkout. HACS installs only the Home
Assistant component and does not include the external service assets:

```bash
git clone https://github.com/AurelioB/ha-codex-voice.git
cd ha-codex-voice
```

The supplied lock installs `wyoming-piper==2.3.1` and its validated dependency
set in an isolated virtual environment:

```bash
python3 -m venv ~/.local/share/ha-codex-voice/wyoming-tts-venv
~/.local/share/ha-codex-voice/wyoming-tts-venv/bin/pip install \
  -r deploy/systemd/wyoming-piper-requirements.lock
```

Install the pinned Mexican-Spanish voice before starting the service. The
installer fetches files from one immutable upstream revision into temporary
files, checks their exact sizes and SHA-256 digests, and only then atomically
places them in the model directory:

```bash
install -d -m 700 ~/.local/share/ha-codex-voice/models/piper
~/.local/share/ha-codex-voice/wyoming-tts-venv/bin/python \
  scripts/install_locked_piper_voice.py \
  --lock deploy/systemd/wyoming-piper-model.lock.json \
  --target-dir ~/.local/share/ha-codex-voice/models/piper
```

Copy the model lock, private runner, user service, and environment example:

```bash
install -Dm644 deploy/systemd/wyoming-piper-model.lock.json \
  ~/.local/share/ha-codex-voice/wyoming-piper-model.lock.json
install -Dm755 deploy/systemd/wyoming-piper-runner.py \
  ~/.local/share/ha-codex-voice/wyoming-piper-runner.py
install -Dm644 deploy/systemd/wyoming-piper.service \
  ~/.config/systemd/user/wyoming-piper.service
install -Dm600 deploy/systemd/local-tts.env.example \
  ~/.config/ha-codex-voice/local-tts.env
```

Installing the model before startup is required because the hardened unit
verifies and bind-mounts that exact directory read-only. Edit `local-tts.env`
before starting the service. Keep
`HA_CODEX_TTS_VOICE=es_MX-ald-medium`, and change the default
`HA_CODEX_TTS_URI` from loopback to an explicit trusted LAN address that Home
Assistant can reach, for example `tcp://192.168.1.10:10200`. The private runner
sets ONNX Runtime's inference thread counts explicitly, avoiding automatic CPU
affinity on restricted or asymmetric CPU sets. Start with
`HA_CODEX_TTS_THREADS=4`; tune it to the physical performance cores available
to the service if needed. Values outside `1..64` are rejected. The runner also
fails closed unless the installed `wyoming-piper` version is exactly the
reviewed `2.3.1`, both locked model files pass verification, and the configured
voice matches `es_MX-ald-medium`. It advertises only that reviewed voice and
rejects attempts to download another model through Wyoming.

Start and inspect the user service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now wyoming-piper.service
systemctl --user status wyoming-piper.service
```

A user service starts at boot only while that user's systemd manager is active.
For a dedicated service account, an administrator can enable lingering once:

```bash
loginctl show-user "$USER" -p Linger
sudo loginctl enable-linger "$USER"
```

Use a dedicated unprivileged service account when practical. Do not expose the
unauthenticated Wyoming listener to the internet or an untrusted network.

Run the fixed-text smoke test from the repository checkout. It prints only
timing and PCM metadata; it does not print or save the synthesized phrase or
audio:

```bash
~/.local/share/ha-codex-voice/wyoming-tts-venv/bin/python \
  scripts/smoke_wyoming_tts.py --uri tcp://192.168.1.10:10200
```

## Home Assistant pipeline

In Home Assistant, add **Wyoming Protocol** with the external host and port
`10200`. Piper may also appear as a discovered integration. Create or edit the
Assist pipeline with:

- Speech-to-text: the Wyoming faster-whisper `stt.*` entity
- Speech-to-text language: `es`
- Conversation agent: the Codex Voice `conversation.*` entity
- Conversation and pipeline language: `es-MX`
- Text-to-speech: the Wyoming Piper `tts.*` entity
- Text-to-speech language: `es_MX`
- Text-to-speech voice: `es_MX-ald-medium`
- Prefer handling commands locally: enabled

The Codex Voice TTS entity remains installed for explicit experiments and
diagnostics; do not select it in the recommended production pipeline. Changing
the recommendation does not rewrite an existing stored Assist pipeline, so
edit each pipeline that previously selected Codex Voice TTS.

## Measured host boundary

On the measured i5-13600K host, the repository smoke probe issued two
cold-restart and five warm same-text Wyoming requests to
`es_MX-ald-medium`. It measures the first non-empty PCM `AudioChunk`, not the
preceding `AudioStart` metadata:

| Observation | Time |
|---|---:|
| Cold requests to first PCM, range of two | 0.714–0.956 s |
| Cold complete synthesis, range of two | 0.824–1.072 s |
| Warm time to first PCM, median of five | 0.028 s |
| Warm time to first PCM, maximum of five | 0.044 s |
| Warm complete synthesis, median of five | 0.116 s |

The five warm first-PCM observations were 0.025, 0.024, 0.044, 0.028, and 0.035
seconds. The generated audio ranged from 2.949 to 3.367 seconds. Three
controlled Codex TTS requests for the same text reached first audio in 2.898,
1.671, and 2.025 seconds: a 2.025-second median. Piper's 0.028-second warm
median was about 72 times faster at this provider boundary. This comparison is
a host-side provider probe, not an end-to-end speaker benchmark.

In a separate physical Home Assistant call, the ThirdReality `media_player`
entity entered `playing` 0.018097 seconds after the call and returned to `idle`
at 3.564543 seconds. Entity state is not an audible-onset instrument, so
satellite buffering, audio-device startup, and actual first sound remain
unmeasured.

### Physical Spanish pipeline canary

A controlled self-acoustic canary then exercised the physical ThirdReality
speaker and microphone, wake detector, Home Assistant VAD, local
faster-whisper, Codex Conversation, local Piper, response playback, and return
to idle. The generated non-sensitive request was recognized with the intended
Spanish words, the response was non-empty and labeled `es-MX`, and the run
reported no errors.

| Interval from pipeline start | Time |
|---|---:|
| VAD start | 1.626 s |
| VAD end | 5.936 s |
| STT end | 6.590 s |
| Codex Conversation duration | 1.734 s |
| Satellite responding | 8.324 s |
| Satellite idle | 13.919 s |

Recognition completed 0.653 seconds after VAD end. Home Assistant created the
streaming TTS result in 0.000315 seconds; that metadata boundary is not Piper's
first PCM or the first audible sound. The separate Wyoming measurements above
remain the provider-side PCM evidence.

## Acceptance check

Before making Piper the default on a physical satellite, verify all of these
boundaries:

1. Repeated non-sensitive test text returns non-empty audio from the Wyoming
   service, including after a service restart.
2. Home Assistant discovers or connects to the service and exposes a Piper
   `tts.*` entity with `es_MX-ald-medium`.
3. The Assist pipeline uses faster-whisper for STT, Codex Voice for
   Conversation, and Piper for TTS.
4. “Okay Nabu” plays the complete response and the satellite returns to idle.
5. Names, numbers, units, and home-control confirmations are intelligible in
   the deployment room.
6. Stopping Piper produces a bounded TTS error without switching silently to
   Codex TTS or a billed API.
7. “Okay Computer,” when installed, still takes its separate direct Codex
   realtime route.

Piper receives the response text that Home Assistant asks it to synthesize but
does not receive the ChatGPT credential, bridge token, microphone audio, or
Home Assistant access token. Restrict TCP port `10200` to Home Assistant and
review service logs before deciding what operational retention is acceptable.
