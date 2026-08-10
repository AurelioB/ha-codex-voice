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

Run the smallest relevant component test while editing, then the full component
suite:

```bash
uv run pytest tests/component/test_config_flow.py
uv run pytest tests/component
```

Bridge and standalone-script tests do not need the Home Assistant pytest
plugin:

```bash
uv run pytest -p no:homeassistant tests/bridge
```

Before handing off a change, run Ruff as described in the repository
`README.md`. Pytest remains the fastest loop because it avoids container
startup and onboarding entirely.

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

All commands are:

| Command | Purpose |
| --- | --- |
| `up [--timeout SECONDS]` | Create or start the pinned instance and wait for its frontend availability probe |
| `restart [--timeout SECONDS]` | Restart the existing instance and wait for the same availability probe |
| `check` | Compile and import the component without onboarding |
| `status` | Show the owned container's Docker state |
| `logs [--tail LINES] [--follow]` | Print recent logs, optionally following them |
| `down` | Remove the container while preserving `.ha-dev/` state |

Startup waits are bounded to 60 seconds by default and can never exceed five
minutes. The `/manifest.json` probe proves that the local frontend is answering;
it is not an authenticated API or component-functionality test. The helper
records ownership labels and validates the pinned image, exact read-only and
read-write mounts, bridge-only networking, loopback port publication, and
restart policy before reuse. `up` and `restart` gracefully recreate a drifted
but owned container while preserving `.ha-dev/`; `status` and `logs` reject
drift instead of presenting it as trusted. `down` gives Home Assistant ten
seconds to stop, removes the container without forced or volume removal, and
deliberately keeps `.ha-dev/`. Use the Home Assistant UI or a deliberate local
filesystem action when a clean onboarding state is required.

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
