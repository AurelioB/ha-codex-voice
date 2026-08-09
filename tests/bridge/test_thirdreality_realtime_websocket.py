from __future__ import annotations

import struct
from collections.abc import Iterator

import pytest

from device.thirdreality.realtime_client import websocket as websocket_module
from device.thirdreality.realtime_client.websocket import (
    Message,
    WebSocketClosed,
    WebSocketConnection,
    WebSocketError,
    _read_http_response,
    encode_client_frame,
)


def _server_frame(
    opcode: int,
    payload: bytes,
    *,
    final: bool = True,
    masked: bool = False,
) -> bytes:
    first = (0x80 if final else 0) | opcode
    length = len(payload)
    mask_bit = 0x80 if masked else 0
    if length < 126:
        header = bytes((first, mask_bit | length))
    elif length <= 0xFFFF:
        header = bytes((first, mask_bit | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, mask_bit | 127)) + struct.pack("!Q", length)
    if not masked:
        return header + payload
    key = b"mask"
    encoded = bytes(value ^ key[index % 4] for index, value in enumerate(payload))
    return header + key + encoded


class _MemoryTransport:
    def __init__(self) -> None:
        self.incoming = bytearray()
        self.sent = bytearray()
        self.closed = False

    def feed(self, value: bytes) -> None:
        self.incoming.extend(value)

    def settimeout(self, _timeout: float) -> None:
        pass

    def recv(self, size: int) -> bytes:
        value = bytes(self.incoming[:size])
        del self.incoming[:size]
        return value

    def sendall(self, value: bytes) -> None:
        self.sent.extend(value)

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _read_client_frame(peer: _MemoryTransport) -> tuple[int, bytes]:
    encoded = bytearray(peer.sent)
    peer.sent.clear()

    first, second = encoded[:2]
    del encoded[:2]
    assert first & 0x80
    assert second & 0x80
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", encoded[:2])[0]
        del encoded[:2]
    elif length == 127:
        length = struct.unpack("!Q", encoded[:8])[0]
        del encoded[:8]
    key = bytes(encoded[:4])
    del encoded[:4]
    payload = bytes(encoded[:length])
    assert len(payload) == length
    assert not encoded[length:]
    decoded = bytes(value ^ key[index % 4] for index, value in enumerate(payload))
    return first & 0x0F, decoded


@pytest.fixture
def socket_pair() -> Iterator[tuple[WebSocketConnection, _MemoryTransport]]:
    peer = _MemoryTransport()
    connection = WebSocketConnection(
        peer,  # type: ignore[arg-type]
        io_timeout_seconds=0.5,
        max_message_bytes=1_024,
    )
    try:
        yield connection, peer
    finally:
        connection.close()
        peer.close()


def test_client_binary_frame_is_final_masked_and_pcm_exact() -> None:
    encoded = encode_client_frame(0x2, b"\x01\x02\x03\x04", mask=b"abcd")

    assert encoded[:2] == b"\x82\x84"
    assert encoded[2:6] == b"abcd"
    assert encoded[6:] == bytes(
        value ^ b"abcd"[index % 4] for index, value in enumerate(b"\x01\x02\x03\x04")
    )


def test_client_frame_uses_canonical_extended_length() -> None:
    encoded = encode_client_frame(0x2, b"x" * 126, mask=b"abcd")

    assert encoded[:2] == b"\x82\xfe"
    assert encoded[2:4] == struct.pack("!H", 126)


def test_fragmented_text_allows_interleaved_ping_and_replies_with_pong(
    socket_pair: tuple[WebSocketConnection, _MemoryTransport],
) -> None:
    connection, peer = socket_pair
    peer.feed(
        _server_frame(0x1, b'{"ty', final=False)
        + _server_frame(0x9, b"check")
        + _server_frame(0x0, b'pe":"pong"}', final=True)
    )

    assert connection.receive_message() == Message("text", '{"type":"pong"}')
    assert _read_client_frame(peer) == (0xA, b"check")


def test_isolated_buffered_ping_returns_without_an_extra_socket_read(
    socket_pair: tuple[WebSocketConnection, _MemoryTransport],
) -> None:
    connection, peer = socket_pair
    peer.feed(_server_frame(0x9, b"check"))

    assert connection.receive_message() is None
    assert _read_client_frame(peer) == (0xA, b"check")


def test_binary_message_must_be_nonempty_and_pcm16_aligned(
    socket_pair: tuple[WebSocketConnection, _MemoryTransport],
) -> None:
    connection, peer = socket_pair
    peer.feed(_server_frame(0x2, b"odd"))

    with pytest.raises(WebSocketError, match="PCM16"):
        connection.receive_message()


def test_partial_frame_returns_without_waiting_and_resumes_exactly() -> None:
    peer = _MemoryTransport()
    connection = WebSocketConnection(
        peer,  # type: ignore[arg-type]
        io_timeout_seconds=10.0,
        max_message_bytes=1_024,
    )
    encoded = _server_frame(0x1, b'{"type":"pong"}')

    peer.feed(encoded[:3])
    assert connection.receive_message() is None
    peer.feed(encoded[3:])
    assert connection.receive_message() == Message("text", '{"type":"pong"}')


def test_http_upgrade_header_uses_one_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 10.0

    class SlowTransport:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.receives = 0

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def recv(self, _size: int) -> bytes:
            nonlocal now
            self.receives += 1
            now += 0.4
            return b"x"

    transport = SlowTransport()
    monkeypatch.setattr(websocket_module.time, "monotonic", lambda: now)

    with pytest.raises(TimeoutError, match="deadline expired"):
        _read_http_response(transport, deadline=11.0)  # type: ignore[arg-type]

    assert transport.receives == 3
    assert transport.timeouts == pytest.approx([1.0, 0.6, 0.2])


def test_server_frames_must_not_be_masked(
    socket_pair: tuple[WebSocketConnection, _MemoryTransport],
) -> None:
    connection, peer = socket_pair
    peer.feed(_server_frame(0x1, b"{}", masked=True))

    with pytest.raises(WebSocketError, match="must not be masked"):
        connection.receive_message()


def test_frame_bound_is_checked_before_payload_read(
    socket_pair: tuple[WebSocketConnection, _MemoryTransport],
) -> None:
    connection, peer = socket_pair
    peer.feed(bytes((0x82, 126)) + struct.pack("!H", 1_026))

    with pytest.raises(WebSocketError, match="exceeds its bound"):
        connection.receive_message()


def test_new_data_frame_during_fragmentation_is_rejected(
    socket_pair: tuple[WebSocketConnection, _MemoryTransport],
) -> None:
    connection, peer = socket_pair
    peer.feed(
        _server_frame(0x1, b"first", final=False) + _server_frame(0x2, b"\x00\x00")
    )

    with pytest.raises(WebSocketError, match="during fragmentation"):
        connection.receive_message()


def test_peer_close_is_validated_and_acknowledged(
    socket_pair: tuple[WebSocketConnection, _MemoryTransport],
) -> None:
    connection, peer = socket_pair
    payload = struct.pack("!H", 1000) + b"done"
    peer.feed(_server_frame(0x8, payload))

    with pytest.raises(WebSocketClosed, match="peer closed"):
        connection.receive_message()
    assert _read_client_frame(peer) == (0x8, payload)


@pytest.mark.parametrize("code", [1012, 1013, 1014])
def test_registered_service_close_codes_are_accepted(
    socket_pair: tuple[WebSocketConnection, _MemoryTransport],
    code: int,
) -> None:
    connection, peer = socket_pair
    payload = struct.pack("!H", code)
    peer.feed(_server_frame(0x8, payload))

    with pytest.raises(WebSocketClosed, match="peer closed"):
        connection.receive_message()
    assert _read_client_frame(peer) == (0x8, payload)


def test_outgoing_audio_rejects_empty_odd_and_oversized(
    socket_pair: tuple[WebSocketConnection, _MemoryTransport],
) -> None:
    connection, _peer = socket_pair

    for value in (b"", b"odd", b"x" * 1_026):
        with pytest.raises(WebSocketError):
            connection.send_binary(value)
