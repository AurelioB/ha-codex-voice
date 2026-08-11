# ThirdReality WebRTC dependency runtime

The target is the ThirdReality `1.01.07`/upstream `v1.1.7` aarch64 Buildroot
Linux image with Python 3.11 and glibc 2.28 or newer. It is not an Android
runtime.

The direct-media client runs `aiortc` in exactly two reusable isolated Python
processes so the vendor voice process never imports its native libraries. One
process owns the active PeerConnection while the other prepares a fresh standby;
rollover stops the retired PeerConnection, recycles its process as standby,
and alternates the two roles. Exactly those two prewarmed process slots are
used; an absent or invalid standby terminates the outer session instead of
cold-launching a replacement or allocating a third process. Each
sidecar runs with UID/GID 65534, no supplementary groups, a minimal fixed
environment, and a umask of 077. Each process uses only the device standard
library, reviewed sidecar source, and a root-owned
but sidecar-readable dependency runtime. It receives no Home Assistant token,
Codex OAuth credential, prompt, transcript, or other application secret through
argv or the environment. No long-lived application credential or prompt,
transcript/model text, tool data, or raw provider data-channel payload crosses
IPC. Offer/answer SDP necessarily does cross IPC and contains ephemeral ICE
credentials and DTLS negotiation material.

`-I -S`, explicit import paths, the separate processes, and their unprivileged
identities isolate dependency loading and prevent either sidecar from reading the
root-owned mode-0600 realtime configuration or staging archive. This is still
not a general filesystem, syscall, or network sandbox: treat the reviewed
sidecar and every pinned native wheel as trusted device code.

The lock, archive builder, installer, manifest verification, import/version
smoke test, and SDP-offer smoke test are deterministic local gates. They do not
by themselves establish that RTP, playback, AEC, or barge-in has passed on a
physical speaker; the device acceptance matrix remains a separate requirement.

[`webrtc-runtime.lock.txt`](webrtc-runtime.lock.txt) is the complete,
hash-locked Python 3.11 dependency set for `aarch64-manylinux_2_28`. Its direct
requirements are `aiortc==1.15.0` and `av==17.1.0`. Builds accept wheels only,
use the public PyPI index, disable keyring and ambient `uv` configuration, and
verify every wheel hash. Standard proxy and CA variables may be forwarded to
`uv`; application tokens and credentials are not.

The exact aiortc and PyAV pins are compatibility and media-correctness
boundaries for offer creation, SDP handling, RTP capture timestamps, decoding,
and resampling. Production interruption does not reuse a peer or reset private
receiver queues, jitter buffers, or resampler state: every rollover constructs
a fresh `RTCPeerConnection` in the other prewarmed process slot. Any dependency
version change therefore requires the full automated suite and physical
double-interruption canary before deployment. That canary must also prove that
continuous speech cannot retire the replacement peer, seven detector-quiet
64 ms callbacks are insufficient, eight consecutive quiet callbacks rearm, and
only a later two-frame speech edge triggers another rollover.

## Build

Run this from a clean checkout on a trusted build host with `uv` installed:

```sh
python3 scripts/install_thirdreality_webrtc_runtime.py build \
  --lock device/thirdreality/webrtc-runtime.lock.txt \
  --output /tmp/codex-webrtc-aarch64-py311.tar.gz
```

The command prints the archive SHA-256. Record that value separately from the
archive. The gzip and tar metadata are normalized, so the same inputs produce
the same bytes. The bundle contains only `site-packages/` and a manifest with
the size and SHA-256 of every installed file.

To regenerate the reviewed lock after intentionally changing
[`webrtc-runtime.in`](webrtc-runtime.in), use:

```sh
uv pip compile device/thirdreality/webrtc-runtime.in \
  --output-file device/thirdreality/webrtc-runtime.lock.txt \
  --generate-hashes --only-binary :all: \
  --python-version 3.11 \
  --python-platform aarch64-manylinux_2_28 \
  --no-annotate --no-header
```

Review every version change and run the full sidecar tests before replacing
the checked-in lock.

## Device install

Copy the archive and this installer to root-controlled staging paths on the
device. Then pass the SHA-256 printed by the trusted build as a literal value:

```sh
python3 /data/conf/install_thirdreality_webrtc_runtime.py install \
  --archive /data/conf/codex-webrtc-aarch64-py311.tar.gz \
  --archive-sha256 REPLACE_WITH_REVIEWED_SHA256 \
  --target-link /data/conf/codex-webrtc \
  --python /usr/bin/python3
```

Installation must run as root. Keep the root-owned archive and installer at
mode 0600. The installer rejects links and special files, enforces bounded
archive and extracted sizes, verifies the manifest and every file, and
requires enough free space for a staged copy plus filesystem headroom. On a
real root install, both the staged and selected-release smoke checks run the
device interpreter with `-I -S` as UID/GID 65534, import the exact `aiortc`,
`av`, and `pylibsrtp` versions, and create an SDP offer containing audio and
data-channel media sections. Any failure leaves the active link unchanged.

Verified releases live under
`/data/conf/.codex-webrtc-releases/<archive-sha256>`. The releases parent,
release root, dependency directories, and all nested directories are
root-owned mode 0755; the manifest and dependency files are root-owned mode
0644. These permissions make the secret-free code/runtime readable and
traversable by UID/GID 65534 without making it writable. The final
`/data/conf/codex-webrtc` relative symlink swap is atomic. Previous releases
remain available for explicit rollback; the installer never deletes them.
The sidecar resolves the link and independently requires root ownership with
no group/world write access. The mode-0600 configuration and archive remain
outside this readable runtime tree.

This procedure does **not** restart PulseAudio or the voice service, alter
speaker volume, change realtime configuration, or change/disable ADB TCP port
5555. Review and perform any service restart as a separate deployment step.
