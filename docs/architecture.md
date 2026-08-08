# Architecture

## Standard Assist pipeline

```text
ThirdReality Assist satellite
  -> Home Assistant Assist pipeline
     -> Codex Voice STT entity
     -> Codex Voice Conversation entity
     -> Codex Voice TTS entity
  -> authenticated HTTP/WebSocket bridge API
  -> local bridge process
  -> Codex App Server over JSON-RPC/stdio
  -> Codex-managed ChatGPT login
```

The component is a normal Home Assistant integration with one parent config
entry and Conversation, STT, and TTS config subentries. This makes all three
providers selectable in the official Assist pipeline. The existing
ThirdReality satellite needs no firmware change for this turn-based mode.

Conversation turns use stable App Server thread and turn methods. Selected Home
Assistant LLM tools are advertised as dynamic tools. When Codex requests a tool,
the component executes it through Home Assistant's LLM API; the bridge has no
direct home-control authority.

## Subscription audio adapter

Codex 0.146.0 does not expose independent subscription-backed STT or TTS RPCs.
The bridge therefore creates short-lived realtime-conversation sessions:

1. Create an `aiortc` peer with a paced audio track and `oai-events` data
   channel.
2. Send the SDP offer to `thread/realtime/start` with realtime v3 and WebRTC
   transport.
3. Apply the answer returned by App Server.
4. Send or receive RTP audio and observe transcript events.
5. Stop and dispose of the session on success, timeout, cancellation, or error.

The active outbound track is required even for synthesis: a transceiver-only
offer negotiates successfully but the subscription service does not deliver
remote audio until outbound RTP activates the media path. Native media cadence
is 48 kHz, 16-bit PCM, 960 samples per 20 ms frame. Incoming WebRTC audio is
decoded from 48 kHz stereo, downmixed and resampled to 24 kHz mono PCM, and
returned to Home Assistant as WAV.

App Server's raw realtime WebSocket route is not used. In the pinned release it
requires API-key authentication, while the WebRTC call-creation path works with
Codex-managed ChatGPT OAuth.

## Realtime client mode

The bridge's `/v1/realtime` WebSocket is a project-owned LAN protocol, not the
App Server transport. A capable client sends PCM/control messages to the bridge;
the bridge relays them through its internal WebRTC peer and returns transcripts
and PCM output. This separates the ThirdReality firmware protocol from the
version-coupled Codex protocol.

Stock ThirdReality firmware can use only the standard Assist path. Full duplex,
continuous listening, and barge-in require the firmware audio-stream extension
described in [the ThirdReality plan](../thirdreality-codex-realtime-plan.md).
