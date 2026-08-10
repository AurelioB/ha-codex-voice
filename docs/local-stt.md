# Reliable local speech-to-text

Codex Voice uses Home Assistant's provider composition for the production
Assist pipeline:

```text
microphone -> Wyoming faster-whisper STT -> Codex Voice Conversation
           -> Wyoming Piper TTS -> speaker
```

This is intentional. Codex App Server exposes ChatGPT OAuth realtime
conversation, but it does not expose a deterministic subscription-backed
transcription operation. A conversational WebRTC session can connect and
consume all input audio without returning a user transcript. Retrying that
same path is not an independent fallback.

Home Assistant's native Wyoming integration already provides the standard
streaming STT boundary, language negotiation, cancellation, and entity
lifecycle. Keeping the model runtime outside the HACS integration also avoids
installing architecture-specific CTranslate2 and ONNX Runtime wheels inside
Home Assistant.

## Home Assistant OS

Install the official Whisper app, add the discovered **Wyoming Protocol**
integration, and select its `stt.*` entity in the Assist pipeline. Keep the
Codex Voice Conversation entity selected, and use the Wyoming Piper entity for
TTS. See [reliable local text-to-speech](local-tts.md).

See the official [Wyoming integration
documentation](https://www.home-assistant.io/integrations/wyoming).

## External Linux host

Run this section from a full GitHub repository checkout. The HACS release ZIP
contains only the Home Assistant component, not the external service assets:

```bash
git clone https://github.com/AurelioB/ha-codex-voice.git
cd ha-codex-voice
```

The upstream `wyoming-faster-whisper` package can run beside the Codex Voice
bridge. Prefer a dedicated unprivileged service account. The example below uses
an isolated virtual environment, locks the complete validated Python stack, and
pins the model snapshot tested by this project:

```bash
python3 -m venv ~/.local/share/ha-codex-voice/wyoming-stt-venv
~/.local/share/ha-codex-voice/wyoming-stt-venv/bin/pip install \
  -r deploy/systemd/wyoming-stt-requirements.lock

~/.local/share/ha-codex-voice/wyoming-stt-venv/bin/python -c \
  'from pathlib import Path; from faster_whisper.utils import download_model; download_model("Systran/faster-whisper-base", output_dir=str(Path.home() / ".local/share/ha-codex-voice/models/faster-whisper-base"), revision="ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66")'
```

The model is downloaded once, addressed by its immutable revision, and then
kept resident by the service. The lock reflects the tested Linux x86-64
runtime; use the official Home Assistant app on unsupported architectures.

Upstream version 3.5.0 logs each recognized transcript at INFO. The supplied
runner raises only that logger to WARNING, retaining service readiness and
numeric model/VAD logs without writing utterance text to the journal.

Copy the supplied privacy runner, unit, and environment example:

```bash
install -Dm755 deploy/systemd/wyoming-faster-whisper-runner.py \
  ~/.local/share/ha-codex-voice/wyoming-stt-runner.py
install -Dm644 deploy/systemd/wyoming-faster-whisper.service \
  ~/.config/systemd/user/wyoming-faster-whisper.service
install -Dm600 deploy/systemd/local-stt.env.example \
  ~/.config/ha-codex-voice/local-stt.env
```

Edit `local-stt.env` so `HA_CODEX_STT_URI` binds an explicit LAN address that
Home Assistant can reach. Do not expose the unauthenticated Wyoming port to the
internet or an untrusted network. Start the service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now wyoming-faster-whisper.service
systemctl --user status wyoming-faster-whisper.service
```

A user service starts at boot only while that user's systemd manager is active.
For a dedicated service account, an administrator can enable lingering once:

```bash
loginctl show-user "$USER" -p Linger
sudo loginctl enable-linger "$USER"
```

Lingering intentionally permits that account's user services to run without an
interactive login. Do not enable it for a broad personal account when a
dedicated service account is available.

The supplied unit hides the service user's home with a temporary empty mount
and exposes only `~/.local/share/ha-codex-voice` read-only. It also hides other
processes, removes device access and capabilities, restricts address families,
and serializes faster-whisper inference to one native batch. Keep the virtual
environment, runner, and model beneath that isolated runtime directory.

In Home Assistant, add **Wyoming Protocol** with that host and port `10300`.
Create or edit an Assist pipeline with:

- Speech-to-text: the faster-whisper `stt.*` entity
- Speech-to-text language: a base language code such as `en` or `es`
- Conversation agent: the Codex Voice `conversation.*` entity
- Text-to-speech: the Wyoming Piper `tts.*` entity
- Mexican Spanish TTS voice: `es_MX-ald-medium`
- Prefer handling commands locally: enabled

The multilingual `base` model is the balanced default. `tiny` is faster but
less accurate; larger models improve some speech at substantially higher CPU
and memory cost. CPU `int8`, beam size 1, and a warm resident model favor voice
command latency.

Run a known-WAV smoke test with the Wyoming virtual environment. By default it
reports only whether non-empty text was received; `--show-transcript` is an
explicit opt-in for non-sensitive test audio.

```bash
~/.local/share/ha-codex-voice/wyoming-stt-venv/bin/python \
  scripts/smoke_wyoming_stt.py sample.wav \
  --uri tcp://192.168.1.10:10300 --language en
```

## Existing Codex STT entities

Upgrades do not delete existing Codex Voice STT subentries. They remain
available for explicit protocol diagnostics, but should not be selected in a
production Assist pipeline. New Codex Voice installations create the stable
Conversation subentry and the retained experimental Codex TTS subentry by
default.

Changing the integration default does not rewrite stored Assist pipelines or
stored subentry titles. After upgrading, edit every pipeline that referenced a
Codex Voice STT entity and explicitly select the Wyoming `stt.*` entity.

## Acceptance check

Before changing a physical satellite, verify all of these boundaries:

1. A known, non-sensitive WAV produces the expected Wyoming transcript on
   repeated warm runs.
2. Home Assistant reports `stt-start` followed promptly by `stt-end`.
3. The transcript reaches the Codex Voice conversation entity and a local HA
   command can call an exposed tool.
4. The Wyoming Piper TTS result plays on the satellite.
5. A failed local STT request returns an error without opening a Codex
   realtime session or silently switching providers.
6. The service journal contains timing/readiness information but no recognized
   utterance text.

Microphone audio for the STT stage stays between Home Assistant and the local
Wyoming host. ChatGPT OAuth remains confined to the Codex Voice bridge for
Conversation, direct realtime speech, and the optional experimental Codex TTS
path. Recommended Piper synthesis remains local.
