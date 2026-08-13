# Development workflow

Use three loops, in this order: focused pytest tests for the fastest feedback,
the disposable local Home Assistant container for runtime integration checks,
and SSH sync to the real Home Assistant host for production validation. The
local container is an inner-loop convenience; it is not a substitute for the
production-host check.

## Fastest loop: pytest

Install the development dependencies once:

```bash
uv sync --extra test --extra lint
```

Run only the smallest relevant test while editing:

```bash
uv run pytest tests/component/test_config_flow.py
```

Bridge and standalone-script tests do not need the Home Assistant pytest
plugin. Select the exact regression or test class during the edit/deploy loop;
do not run the whole bridge suite after every change:

```bash
uv run pytest -p no:homeassistant \
  tests/bridge/test_thirdreality_realtime_session.py::test_full_duplex_user_start_quarantines_tail_after_speaking_stop
```

Use three validation tiers:

1. **Edit loop:** one to five directly affected tests and Ruff only on changed
   files.
2. **Pre-deploy:** the affected test file or a narrow `-k` selection, followed
   by the physical canary that exercises the reported behavior.
3. **Release:** the complete component and bridge suites, full Ruff, hassfest,
   and HACS validation.

The complete 1,000-plus-test suite is a release gate, not an inner-loop gate.
Do not delete behavior coverage merely to shorten the edit loop; select less of
it until a release candidate exists. Pytest remains the fastest automated loop
because it avoids container startup and onboarding entirely.

## On-demand realtime session report

The diagnostic reporter is a separate process and adds no work to the bridge,
speaker capture callback, or media path. It reads existing rotated Docker and
device syslog records only when invoked:

```bash
UV_CACHE_DIR=/tmp/codex-uv-cache uv run python \
  scripts/report_realtime_session.py \
  --live --adb-serial 192.168.8.42:5555 --since 4h --latest 3
```

Use `--json` for machine-readable output. For an offline report, supply
`--bridge-log PATH --device-log PATH`. Exact transcript text appears only when
the bridge's explicit testing switch `HA_CODEX_REALTIME_LOG_TRANSCRIPTS=true`
was active; the reporter itself never enables logging or records audio.

## Private voice lab

The voice lab is a separate, dependency-free CLI for explicitly consented wake
and speaker-enrollment recordings. It is not imported by the bridge or device,
so installing it adds no media-path work. Initialize a private dataset outside
the repository, or use the ignored `.voice-lab/` path for local experiments:

```bash
uv run python scripts/voice_lab.py --root .voice-lab init
uv run python scripts/voice_lab.py --root .voice-lab add sample.wav \
  --kind wake-positive --phrase "Okay Nabu" --outcome miss \
  --speaker-id owner --provenance physical-test \
  --session-id kitchen-20260813-a --consent
uv run python scripts/voice_lab.py --root .voice-lab verify
uv run python scripts/voice_lab.py --root .voice-lab list
uv run python scripts/voice_lab.py --root .voice-lab export-wake \
  --phrase "Okay Nabu" \
  --output .voice-lab/artifacts/okay-nabu-training.json
```

Imports must be mono PCM16 WAV at 16 kHz. The CLI canonicalizes the file,
deduplicates by PCM SHA-256, measures duration/peak/RMS, and keeps the dataset
directory at mode `0700` with files at `0600`. Use `remove SAMPLE_ID` to delete
an imported recording. Do not place real recordings, embeddings, or trained
models in Git.

`export-wake` includes matching hit/miss positives plus wake-negative and
background samples. It deterministically splits by `session_id` (falling back
to provenance), never by individual clip, so near-duplicate audio from one
recording session cannot leak between train and validation. The output remains
inside the private lab and is the handoff to a microWakeWord trainer. This
repository intentionally does not disguise dataset preparation as model
training: the referenced Voice PE project also uses the external community
microWakeWord trainer. A trained JSON/TFLite pair must be calibrated and pass
the physical acceptance matrix before setting `personalized_wake_config_path`.

## Optional speaker identification

Speaker identity is host-side and disabled unless the optional Compose override
is selected. Download the exact TitaNet model into a private directory and
verify its deployment lock:

```bash
install -d -m 700 .speaker-identity/models .speaker-identity/profiles
curl -fL \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx \
  -o .speaker-identity/models/nemo_en_titanet_large.onnx
printf '%s  %s\n' \
  d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77 \
  .speaker-identity/models/nemo_en_titanet_large.onnx | sha256sum --check
```

Import explicitly consented natural speech as `speaker-enrollment`, then build
one private centroid from at least five usable three-second chunks spanning
several recordings, distances, and sessions:

```bash
docker build -f deploy/docker/SpeakerIdentity.Dockerfile \
  -t ha-codex-speaker-identity:1.13.4 .
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/.speaker-identity/models:/models:ro" \
  -v "$PWD/.speaker-identity/profiles:/profiles" \
  -v "$PWD/private-enrollment-recordings:/recordings:ro" \
  ha-codex-speaker-identity:1.13.4 \
  --model /models/nemo_en_titanet_large.onnx \
  --model-sha256 d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77 \
  enroll owner /recordings/recording-1.wav /recordings/recording-2.wav \
  /recordings/recording-3.wav --profiles /profiles
```

Set `HA_CODEX_SPEAKER_IDENTITY_TOKEN` to a distinct long random value and set
`HA_CODEX_SPEAKER_IDENTITY_MODEL_HOST` and
`HA_CODEX_SPEAKER_PROFILES_HOST` to those absolute paths. Start the bridge and
worker together:

```bash
docker compose --env-file .codex-voice/compose.env \
  -f compose.yaml -f compose.speaker-identity.yaml \
  up --detach --build
```

The worker binds only to host loopback. The bridge copies one bounded
five-second post-wake window, never blocks PCM on inference, and appends only a
confident match as advisory context. Calibrate thresholds with held-out
household and visitor recordings before relying on personalization. A match is
not biometric authentication and must not authorize locks, alarms, purchases,
or account changes.

## Inner loop: local Home Assistant

The standard-library helper needs Docker but no Python packages from this
project. It always uses the pinned
`ghcr.io/home-assistant/home-assistant:2026.8.1` image and publishes Home
Assistant only on `127.0.0.1:18123`. It never exposes the development instance
on a LAN interface. The helper targets POSIX hosts, including Linux and WSL;
it is not a native-Windows Docker wrapper.

First check that every component Python file compiles and that the component
package imports against the pinned image:

```bash
python3 scripts/dev_home_assistant.py check
```

`check` uses a one-shot, network-disabled, read-only container. It does not
start Home Assistant and therefore does not require completing onboarding.

Start the interactive instance and open <http://127.0.0.1:18123>:

```bash
python3 scripts/dev_home_assistant.py up
```

Without a dedicated local token, `up` waits only for the frontend so onboarding
remains possible and reports that Core lifecycle is unverified. After
onboarding, create a long-lived token from the local development user's profile
and export it only in the development shell:

```bash
export HA_CODEX_DEV_HASS_TOKEN="replace-with-the-local-development-token"
```

Do not reuse a production Home Assistant token. With this variable set, `up`
and `restart` poll the authenticated local `/api/config` response and return
only when Core reports `RUNNING`. `restart` requires the variable and validates
it before changing container state, so it cannot report a frontend-only false
ready result.

The helper bind-mounts `custom_components/codex_voice` read-only at
`/config/custom_components/codex_voice`. Home Assistant's writable local
configuration and database stay in the ignored `.ha-dev/` directory. Do not
put production backups, credentials, or other reusable secrets there. The
helper rejects a symlinked state path and requires an existing directory to be
owned by the current user with mode `0700`.

After changing component code, restart Home Assistant so Python modules are
loaded again:

```bash
python3 scripts/dev_home_assistant.py restart
python3 scripts/dev_home_assistant.py logs --follow
```

For an end-to-end local component test, run a second bridge on its dedicated
development port. The helper validates the owned container's exact
`host.docker.internal:host-gateway` mapping, confirms that it resolves to the
attached Docker bridge gateway, and refuses an unsafe, mismatched, or occupied
address:

```bash
export HA_CODEX_BRIDGE_HOST="$(python3 scripts/dev_home_assistant.py bridge-host)"
export HA_CODEX_BRIDGE_PORT=18787
uv run --extra bridge python -m bridge
```

Keep the normal bridge token and file-backed Codex login environment described
in the bridge setup. In the local Home Assistant config flow, use
`http://host.docker.internal:18787` with that bridge token. The development
bridge binds only the Docker gateway, not `0.0.0.0`; Home Assistant remains
published only at `127.0.0.1:18123`. The mandatory bearer token still protects
the bridge from other local containers on Docker's default bridge network.

All commands are:

| Command | Purpose |
| --- | --- |
| `up [--timeout SECONDS]` | Create or start the pinned instance and wait for its frontend availability probe |
| `restart [--timeout SECONDS]` | Restart the existing instance and require authenticated Core state `RUNNING` |
| `check` | Compile and import the component without onboarding |
| `status` | Show the owned container's Docker state |
| `bridge-host` | Print the validated gateway address for a bridge on port 18787 |
| `logs [--tail LINES] [--follow]` | Print recent logs, optionally following them |
| `down` | Remove the container while preserving `.ha-dev/` state |

Startup waits are bounded to 60 seconds by default and can never exceed five
minutes. Without `HA_CODEX_DEV_HASS_TOKEN`, the `/manifest.json` probe proves
only that the local frontend is answering. With the token, the bounded
authenticated probe requires Core state `RUNNING`. The helper records ownership
labels and validates the pinned image and `/init` entrypoint, exact read-only
and read-write mounts, bridge-only networking, loopback port publication,
host-gateway mapping, and restart policy before reuse. `up` and `restart`
gracefully recreate a drifted but owned container while preserving `.ha-dev/`;
`status`, `logs`, and `bridge-host` reject drift instead of presenting it as
trusted. `down` gives Home Assistant ten seconds to stop, removes the container
without forced or volume removal, and deliberately keeps `.ha-dev/`. Use the
Home Assistant UI or a deliberate local filesystem action when a clean
onboarding state is required.

## Production loop: SSH sync

After pytest and the local container pass, validate on the actual Home
Assistant deployment with `scripts/deploy_home_assistant_ssh.py`. This is the
production loop: it stages and verifies the component over SSH. Passing
`--restart` additionally restarts Home Assistant and waits for its authenticated
API to return.

On Home Assistant OS, install and start the official **Terminal & SSH** app
(`core_ssh`). Create a dedicated key on the development host:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ha-codex-voice -C ha-codex-voice-deploy
```

In the app configuration, add only the contents of
`~/.ssh/ha-codex-voice.pub` to `authorized_keys`, leave password authentication
disabled, and expose the chosen TCP port only on the trusted administration
network. Restart the app, then verify its host-key fingerprint through a
trusted channel before accepting it into `known_hosts`. Do not copy the private
key into Home Assistant.

Never place a password or Home Assistant token in a command line or repository
file. Run the helper with `--help` for the current flags. A normal deployment
uses the key path above by default:

```bash
python3 scripts/deploy_home_assistant_ssh.py \
  --host homeassistant.local
```

To restart after deployment, export `HASS_URL` and `HASS_TOKEN` in the calling
environment and add `--restart`. `HASS_URL` must identify the same Home
Assistant instance reached by `--host`. The token is sent only in the Home
Assistant REST authorization header and is redacted from dry-run output. A file
sync without `--restart` does not activate changed Python; restart manually from
the UI or run `ha core restart` in the Terminal & SSH shell before validation.

The restart path first requires authenticated `/api/config` state `RUNNING`.
Home Assistant may close or time out the restart POST after accepting it; the
helper treats that response as ambiguous and succeeds only after observing a
non-running/unavailable transition followed by `RUNNING`. A confirmed success
response retains a bounded grace fallback for very fast restarts. Authentication
and HTTP failures remain terminal and token values are never printed.

The helper packages only the integration, uploads a bounded archive, verifies
its size and SHA-256 digest on the host, and uses a random per-run upload and
staging path outside `/config/custom_components`. An atomic remote lock rejects
overlapping deployments before either can move the active integration. The
helper then preserves the prior integration at
`/config/.codex_voice-deploy-previous` and moves the staged version into place.
That backup is intentionally outside `custom_components`, so Home Assistant
cannot mistake it for a second integration. A failed final move is rolled back;
an abrupt host failure between directory moves can still require the manual
recovery below.

After a restart, inspect **Settings → System → Logs** for `codex_voice` errors
and run a text request through the intended Assist pipeline. API readiness alone
does not prove that a configured entry or its bridge connection works.

To roll back before another deployment overwrites the single backup, open the
Terminal & SSH shell, verify both paths are directories, and run:

```bash
set -eu
cd /config
[ -d .codex_voice-deploy-previous ] && [ ! -L .codex_voice-deploy-previous ]
if [ -e custom_components/codex_voice ] || \
   [ -L custom_components/codex_voice ]; then
  [ -d custom_components/codex_voice ] && \
    [ ! -L custom_components/codex_voice ]
  test ! -e .codex_voice-deploy-failed
  mv custom_components/codex_voice .codex_voice-deploy-failed
fi
mv .codex_voice-deploy-previous custom_components/codex_voice
```

This handles both an ordinary rollback and the crash window where the active
path is temporarily absent. Restart Home Assistant and inspect the logs again.
The explicit type and existence checks reject symlinks and fail closed if
`.codex_voice-deploy-failed` already exists; rename or remove that known failed
copy deliberately before retrying. If an uncatchable process or host failure
also leaves `/config/.codex_voice-deploy-lock`, verify that no deployment is
running and remove that empty directory with `rmdir` before retrying.

Use this production loop only after the cheaper checks. A successful local
import does not prove the production host's configuration, integrations,
network access, or persisted entries are compatible with the change.
