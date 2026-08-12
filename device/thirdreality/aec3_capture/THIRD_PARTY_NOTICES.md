# Third-party notices

The hardware capture topology and 10 ms AEC ordering were informed by
ThirdReality's Apache-2.0-licensed `voice-music-assistant` v1.2.2 sources,
especially its `AudioCapture`, `WebRtcProcessor`, and `aec_loopback_test`
implementations. This module is a new, narrower implementation for the Python
v1.1.7 runtime and does not embed ThirdReality's replacement application.

Built artifacts dynamically include these Buildroot packages:

- `webrtc-audio-processing` 1.3, BSD-3-Clause. The build script copies its
  `COPYING` file into the artifact.
- `libabseil-cpp` 20230802.1, Apache-2.0. The build script copies its `LICENSE`
  file into the artifact.

The library dynamically uses the firmware's existing ALSA library. No ALSA
binary is bundled. Source and license information for the complete firmware is
available from ThirdReality's public `voice-music-assistant` v1.1.7 tree.
