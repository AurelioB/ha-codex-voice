"""Async child runtime for one isolated device PeerConnection."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

from .protocol import (
    MAX_PACKET_BYTES,
    CaptureAudio,
    ControlMessage,
    PlaybackAudio,
    ProtocolError,
    decode_packet,
    encode_control,
    encode_playback_audio,
)


class PeerLike(Protocol):
    """WebRTC peer surface consumed by the IPC runtime."""

    async def create_offer(self) -> str:
        """Return one gathered device SDP offer."""
        ...

    async def set_answer(self, sdp: str) -> None:
        """Apply one remote SDP answer."""
        ...

    def feed_capture(self, value: CaptureAudio) -> None:
        """Queue one timestamped capture packet."""
        ...

    def cancel_response(self, response_id: str | None = None) -> None:
        """Send one provider response cancellation."""
        ...

    def interrupt_response(self, response_id: str | None = None) -> None:
        """Cancel and clear one provider WebRTC output buffer."""
        ...

    async def stop(self) -> None:
        """Release all media resources."""
        ...


PeerFactory = Callable[..., PeerLike]


class RuntimeErrorCode(RuntimeError):
    """Internal exception carrying only a fixed content-free error code."""

    def __init__(self, code: str) -> None:
        """Retain one allowlisted code without provider or user content."""
        super().__init__(code)
        self.code = code


class SidecarRuntime:
    """Drive one peer from bounded sequenced-packet IPC."""

    def __init__(
        self,
        transport: socket.socket,
        *,
        peer_factory: PeerFactory | None = None,
    ) -> None:
        """Adopt one child socket and construct its single peer."""
        self._transport = transport
        self._transport.setblocking(False)
        self._fatal = asyncio.Event()
        self._fatal_code: str | None = None
        self._shutdown = False
        self._offer_created = False
        self._answer_applied = False
        self._stopped = False
        if peer_factory is None:
            from .peer import DeviceWebRtcPeer  # noqa: PLC0415

            peer_factory = DeviceWebRtcPeer
        self._peer = peer_factory(
            emit_lifecycle=self._emit_lifecycle,
            emit_playback=self._emit_playback,
            emit_state=self._emit_state,
            emit_fatal=self._fail,
        )

    async def run(self) -> int:
        """Run until shutdown, peer failure, parent EOF, or protocol failure."""
        receive_task = asyncio.create_task(
            self._receive_loop(),
            name="codex-device-webrtc-ipc",
        )
        fatal_task = asyncio.create_task(
            self._fatal.wait(),
            name="codex-device-webrtc-fatal",
        )
        code = 0
        try:
            done, _ = await asyncio.wait(
                {receive_task, fatal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if fatal_task in done and self._fatal_code is not None:
                self._send_error(self._fatal_code)
                code = 1
            elif receive_task in done:
                try:
                    await receive_task
                except RuntimeErrorCode as exc:
                    self._send_error(exc.code)
                    code = 1
                except (OSError, ProtocolError):
                    self._send_error("protocol_error")
                    code = 1
        finally:
            for task in (receive_task, fatal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receive_task, fatal_task, return_exceptions=True)
            try:
                await self._peer.stop()
            except Exception:  # noqa: BLE001 - peer cleanup must not escape.
                code = 1
            self._transport.close()
        return code

    async def _receive_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._shutdown:
            packet = await loop.sock_recv(self._transport, MAX_PACKET_BYTES + 1)
            if not packet:
                self._shutdown = True
                return
            if len(packet) > MAX_PACKET_BYTES:
                raise RuntimeErrorCode("packet_too_large")
            decoded = decode_packet(packet)
            if isinstance(decoded, CaptureAudio):
                # Capture can queue behind a created offer while App Server
                # returns the answer. The bounded track/socket queues retain
                # wake audio without putting a second realtime clock in front
                # of the eventual RTP sender.
                if not self._offer_created or self._stopped:
                    raise RuntimeErrorCode("capture_outside_session")
                try:
                    self._peer.feed_capture(decoded)
                except Exception as exc:
                    raise RuntimeErrorCode("capture_rejected") from exc
                continue
            if not isinstance(decoded, ControlMessage):
                raise RuntimeErrorCode("packet_direction_invalid")
            await self._handle_control(decoded)

    async def _handle_control(self, message: ControlMessage) -> None:
        if message.type == "create_offer":
            if self._offer_created or self._stopped:
                raise RuntimeErrorCode("offer_state_invalid")
            try:
                sdp = await self._peer.create_offer()
            except Exception as exc:
                raise RuntimeErrorCode("offer_failed") from exc
            self._offer_created = True
            self._send(encode_control("offer", sdp=sdp))
            return
        if message.type == "set_answer":
            if not self._offer_created or self._answer_applied or self._stopped:
                raise RuntimeErrorCode("answer_state_invalid")
            sdp = message.values.get("sdp")
            assert isinstance(sdp, str)
            try:
                await self._peer.set_answer(sdp)
            except Exception as exc:
                raise RuntimeErrorCode("answer_failed") from exc
            self._answer_applied = True
            self._send(encode_control("answer.applied"))
            return
        if message.type == "response.cancel":
            if not self._answer_applied or self._stopped:
                raise RuntimeErrorCode("cancel_state_invalid")
            response_id = message.values.get("response_id")
            assert response_id is None or isinstance(response_id, str)
            try:
                self._peer.cancel_response(response_id)
            except Exception as exc:
                raise RuntimeErrorCode("cancel_failed") from exc
            return
        if message.type == "response.interrupt":
            if not self._answer_applied or self._stopped:
                raise RuntimeErrorCode("interrupt_state_invalid")
            response_id = message.values.get("response_id")
            assert response_id is None or isinstance(response_id, str)
            try:
                self._peer.interrupt_response(response_id)
            except Exception as exc:
                raise RuntimeErrorCode("interrupt_failed") from exc
            return
        if message.type == "stop":
            if not self._stopped:
                try:
                    await self._peer.stop()
                except Exception as exc:
                    raise RuntimeErrorCode("stop_failed") from exc
                self._stopped = True
            self._send(encode_control("stopped"))
            return
        if message.type == "shutdown":
            if not self._stopped:
                try:
                    await self._peer.stop()
                except Exception as exc:
                    raise RuntimeErrorCode("stop_failed") from exc
                self._stopped = True
            self._send(encode_control("shutdown.complete"))
            self._shutdown = True
            return
        raise RuntimeErrorCode("control_direction_invalid")

    def _emit_lifecycle(self, values: dict[str, str | int]) -> None:
        self._send(encode_control("lifecycle", **values))

    def _emit_playback(self, value: PlaybackAudio) -> None:
        self._send(encode_playback_audio(value))

    def _emit_state(self, state: str) -> None:
        if state not in {"connected", "data.ready"}:
            self._fail("state_invalid")
            return
        self._send(encode_control(state))

    def _fail(self, code: str) -> None:
        if self._fatal_code is None:
            self._fatal_code = code
            self._fatal.set()

    def _send_error(self, code: str) -> None:
        with suppress(Exception):
            self._send(encode_control("error", code=code))

    def _send(self, packet: bytes) -> None:
        try:
            sent = self._transport.send(packet)
        except BlockingIOError as exc:
            self._fail("output_backpressure")
            raise RuntimeErrorCode("output_backpressure") from exc
        except OSError as exc:
            self._fail("ipc_send_failed")
            raise RuntimeErrorCode("ipc_send_failed") from exc
        if sent != len(packet):
            self._fail("partial_packet")
            raise RuntimeErrorCode("partial_packet")


async def run_sidecar(
    transport: socket.socket,
    *,
    peer_factory: PeerFactory | None = None,
) -> int:
    """Construct and run one sidecar; kept separate for deterministic tests."""
    try:
        runtime = SidecarRuntime(transport, peer_factory=peer_factory)
    except Exception:  # noqa: BLE001 - peer boundary maps to fixed code.
        with suppress(Exception):
            transport.send(encode_control("error", code="peer_initialization_failed"))
        transport.close()
        return 1
    return await runtime.run()
