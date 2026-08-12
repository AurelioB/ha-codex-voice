"""Async child runtime for one isolated device PeerConnection."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
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

    def set_capture_gain_db(self, value: float) -> None:
        """Configure outbound microphone gain before offer creation."""
        ...

    async def set_answer(self, sdp: str) -> None:
        """Apply one remote SDP answer."""
        ...

    def feed_capture(self, value: CaptureAudio) -> None:
        """Queue one timestamped capture packet."""
        ...

    def commit_capture(self) -> None:
        """Freeze the ordered startup capture prefix."""
        ...

    def interrupt_response(self) -> None:
        """Fence local playback while provider server VAD interrupts output."""
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


@dataclass(slots=True)
class _PeerSlot:
    """One epoch-tagged peer and its local negotiation state."""

    epoch: int
    peer: PeerLike
    offer_created: bool = False
    answer_applied: bool = False
    capture_committed: bool = False
    capture_ready: bool = False
    stopped: bool = False
    failed: bool = False
    failure_reported: bool = False


class SidecarRuntime:
    """Drive one active peer plus one offer-warm standby from bounded IPC."""

    def __init__(
        self,
        transport: socket.socket,
        *,
        peer_factory: PeerFactory | None = None,
    ) -> None:
        """Adopt one child socket and construct its initial active peer."""
        self._transport = transport
        self._transport.setblocking(False)
        self._fatal = asyncio.Event()
        self._fatal_code: str | None = None
        self._shutdown = False
        if peer_factory is None:
            from .peer import DeviceWebRtcPeer  # noqa: PLC0415

            peer_factory = DeviceWebRtcPeer
        self._peer_factory = peer_factory
        self._next_peer_epoch = 1
        self._active = self._new_peer()
        self._standby: _PeerSlot | None = None
        self._standby_offer_task: asyncio.Task[None] | None = None

    def _new_peer(self, *, epoch: int | None = None) -> _PeerSlot:
        """Construct one fresh epoch-tagged peer in the imported child runtime."""
        peer_epoch = self._allocate_peer_epoch() if epoch is None else epoch
        peer = self._peer_factory(
            emit_lifecycle=lambda values: self._emit_lifecycle(peer_epoch, values),
            emit_playback=lambda value: self._emit_playback(peer_epoch, value),
            emit_capture_metrics=lambda values: self._emit_capture_metrics(
                peer_epoch,
                values,
            ),
            emit_state=lambda state: self._emit_state(peer_epoch, state),
            emit_fatal=lambda code: self._fail_peer(peer_epoch, code),
        )
        return _PeerSlot(peer_epoch, peer)

    def _allocate_peer_epoch(self) -> int:
        epoch = self._next_peer_epoch
        if epoch > 0xFFFFFFFF:
            raise RuntimeErrorCode("peer_epoch_exhausted")
        self._next_peer_epoch += 1
        return epoch

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
            # A peer callback can synchronously latch a fatal while the receive
            # task also completes. The first fatal always beats any readiness or
            # generic receive-loop classification from the same ordered action.
            if self._fatal_code is not None:
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
                await self._stop_owned_peers()
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
                active = self._active
                if (
                    active is None
                    or not active.offer_created
                    or active.stopped
                    or active.failed
                ):
                    raise RuntimeErrorCode("capture_outside_session")
                try:
                    active.peer.feed_capture(decoded)
                except Exception as exc:
                    raise RuntimeErrorCode("capture_rejected") from exc
                continue
            if not isinstance(decoded, ControlMessage):
                raise RuntimeErrorCode("packet_direction_invalid")
            await self._handle_control(decoded)

    async def _handle_control(self, message: ControlMessage) -> None:
        if message.type == "create_offer":
            active = self._active
            if active is None:
                try:
                    active = self._new_peer()
                except Exception as exc:
                    raise RuntimeErrorCode("offer_failed") from exc
                self._active = active
            if active.offer_created or active.stopped or active.failed:
                raise RuntimeErrorCode("offer_state_invalid")
            gain_db = message.values.get("direct_capture_gain_db", 0.0)
            assert isinstance(gain_db, (int, float)) and not isinstance(gain_db, bool)
            try:
                active.peer.set_capture_gain_db(float(gain_db))
                sdp = await active.peer.create_offer()
            except Exception as exc:
                raise RuntimeErrorCode("offer_failed") from exc
            active.offer_created = True
            self._send(encode_control("offer", sdp=sdp))
            return
        if message.type == "standby.create_offer":
            self._start_standby_offer(message)
            return
        if message.type == "standby.promote":
            epoch = message.values.get("peer_epoch")
            assert isinstance(epoch, int) and not isinstance(epoch, bool)
            await self._promote_standby(epoch)
            return
        if message.type == "set_answer":
            active = self._active
            if (
                active is None
                or not active.offer_created
                or active.answer_applied
                or active.stopped
                or active.failed
            ):
                raise RuntimeErrorCode("answer_state_invalid")
            sdp = message.values.get("sdp")
            assert isinstance(sdp, str)
            try:
                await active.peer.set_answer(sdp)
            except Exception as exc:
                raise RuntimeErrorCode("answer_failed") from exc
            active.answer_applied = True
            self._send(encode_control("answer.applied"))
            return
        if message.type == "capture.commit":
            active = self._active
            if (
                active is None
                or not active.answer_applied
                or active.capture_committed
                or active.stopped
                or active.failed
            ):
                raise RuntimeErrorCode("capture_commit_state_invalid")
            # Set the latch first: a zero-packet epoch can acknowledge
            # synchronously from inside commit_capture().
            active.capture_committed = True
            try:
                active.peer.commit_capture()
            except Exception as exc:
                raise RuntimeErrorCode("capture_commit_failed") from exc
            return
        if message.type == "response.interrupt":
            active = self._active
            if (
                active is None
                or not active.answer_applied
                or active.stopped
                or active.failed
            ):
                raise RuntimeErrorCode("interrupt_state_invalid")
            try:
                active.peer.interrupt_response()
            except Exception as exc:
                raise RuntimeErrorCode("interrupt_failed") from exc
            return
        if message.type == "stop":
            try:
                await self._stop_owned_peers()
            except Exception as exc:
                raise RuntimeErrorCode("stop_failed") from exc
            self._send(encode_control("stopped"))
            return
        if message.type == "shutdown":
            try:
                await self._stop_owned_peers()
            except Exception as exc:
                raise RuntimeErrorCode("stop_failed") from exc
            self._send(encode_control("shutdown.complete"))
            self._shutdown = True
            return
        raise RuntimeErrorCode("control_direction_invalid")

    def _start_standby_offer(self, message: ControlMessage) -> None:
        """Start one offer without pausing capture processing for the active peer."""
        active = self._active
        if (
            active is None
            or not active.offer_created
            or active.stopped
            or active.failed
        ):
            raise RuntimeErrorCode("standby_state_invalid")
        if self._standby is not None:
            raise RuntimeErrorCode("standby_state_invalid")

        epoch = self._allocate_peer_epoch()
        try:
            standby = self._new_peer(epoch=epoch)
            gain_db = message.values.get("direct_capture_gain_db", 0.0)
            assert isinstance(gain_db, (int, float)) and not isinstance(gain_db, bool)
            standby.peer.set_capture_gain_db(float(gain_db))
        except Exception:  # noqa: BLE001 - standby failure must preserve active media.
            self._send_standby_failed(epoch)
            return
        self._standby = standby
        task = asyncio.create_task(
            self._finish_standby_offer(standby),
            name="codex-device-webrtc-standby-offer",
        )
        self._standby_offer_task = task
        task.add_done_callback(self._standby_offer_done)

    async def _finish_standby_offer(self, standby: _PeerSlot) -> None:
        try:
            sdp = await standby.peer.create_offer()
            if self._standby is not standby:
                return
            if standby.failed or standby.stopped:
                self._standby = None
                with suppress(Exception):
                    await self._stop_peer(standby)
                return
            standby.offer_created = True
            self._send(
                encode_control(
                    "standby.offer",
                    sdp=sdp,
                    peer_epoch=standby.epoch,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - peer boundary maps to fixed IPC state.
            if self._standby is standby:
                self._standby = None
            with suppress(Exception):
                await self._stop_peer(standby)
            if not standby.failure_reported:
                standby.failure_reported = True
                self._send_standby_failed(standby.epoch)

    def _standby_offer_done(self, task: asyncio.Task[None]) -> None:
        if self._standby_offer_task is task:
            self._standby_offer_task = None
        if task.cancelled():
            return
        with suppress(Exception):
            task.result()

    async def _promote_standby(self, epoch: int) -> None:
        """Fence the current peer before making the exact warm epoch active."""
        standby = self._standby
        active = self._active
        if (
            active is None
            or standby is None
            or standby.epoch != epoch
            or not standby.offer_created
            or standby.failed
            or standby.stopped
        ):
            raise RuntimeErrorCode("standby_state_invalid")

        # Clearing active ownership is the callback fence. The ordered promote
        # acknowledgement is emitted only after stop() has drained the old peer,
        # so no retired lifecycle or playback callback can cross that barrier.
        self._active = None
        try:
            await self._stop_peer(active)
        except Exception as exc:
            raise RuntimeErrorCode("promotion_stop_failed") from exc
        if standby.failed or standby.stopped:
            self._standby = None
            with suppress(Exception):
                await self._stop_peer(standby)
            raise RuntimeErrorCode("standby_state_invalid")
        self._standby = None
        self._active = standby
        self._send(encode_control("standby.promoted", peer_epoch=epoch))

    async def _stop_peer(self, slot: _PeerSlot) -> None:
        if slot.stopped:
            return
        slot.stopped = True
        await slot.peer.stop()

    async def _stop_owned_peers(self) -> None:
        offer_task = self._standby_offer_task
        self._standby_offer_task = None
        if offer_task is not None and not offer_task.done():
            offer_task.cancel()
            await asyncio.gather(offer_task, return_exceptions=True)
        active = self._active
        standby = self._standby
        self._active = None
        self._standby = None
        slots = [slot for slot in (active, standby) if slot is not None]
        if not slots:
            return
        results = await asyncio.gather(
            *(self._stop_peer(slot) for slot in slots),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeErrorCode("stop_failed")

    def _emit_lifecycle(self, epoch: int, values: dict[str, str | int]) -> None:
        if not self._is_active_epoch(epoch):
            return
        self._send(encode_control("lifecycle", **values))

    def _emit_playback(self, epoch: int, value: PlaybackAudio) -> None:
        if not self._is_active_epoch(epoch):
            return
        self._send(encode_playback_audio(value))

    def _emit_capture_metrics(self, epoch: int, values: dict[str, int]) -> None:
        if not self._is_active_epoch(epoch):
            return
        # Optional diagnostics are deliberately lossy. A full parent socket
        # must not turn clipping telemetry into a fatal media-path failure.
        packet = encode_control("capture.metrics", **values)
        try:
            self._transport.send(packet)
        except (BlockingIOError, OSError):
            return

    def _emit_state(self, epoch: int, state: str) -> None:
        active = self._active
        if active is None or active.epoch != epoch or active.stopped or active.failed:
            return
        if state == "capture.ready":
            if (
                not active.capture_committed
                or active.capture_ready
                or self._fatal_code is not None
            ):
                if self._fatal_code is None:
                    self._fail("state_invalid")
                return
            active.capture_ready = True
            self._send(encode_control(state))
            return
        if state not in {"connected", "data.ready"}:
            self._fail("state_invalid")
            return
        self._send(encode_control(state))

    def _is_active_epoch(self, epoch: int) -> bool:
        active = self._active
        return (
            active is not None
            and active.epoch == epoch
            and not active.stopped
            and not active.failed
            and self._fatal_code is None
        )

    def _fail_peer(self, epoch: int, code: str) -> None:
        active = self._active
        if active is not None and active.epoch == epoch and not active.stopped:
            active.failed = True
            self._fail(code)
            return
        standby = self._standby
        if standby is None or standby.epoch != epoch or standby.stopped:
            return
        standby.failed = True
        if not standby.failure_reported:
            standby.failure_reported = True
            self._send_standby_failed(epoch)

    def _send_standby_failed(self, epoch: int) -> None:
        self._send(encode_control("standby.failed", peer_epoch=epoch))

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
