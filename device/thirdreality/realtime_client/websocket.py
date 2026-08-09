"""Small, strict RFC 6455 client used by the embedded voice process."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HTTP_HEADER_BYTES = 16 * 1024
_MAX_TEXT_BYTES = 16 * 1024
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class WebSocketError(ConnectionError):
    """Raised for a transport or RFC 6455 protocol failure."""


class WebSocketClosed(WebSocketError):
    """Raised after a peer close frame or EOF."""


@dataclass(frozen=True, slots=True)
class Message:
    """One complete application message or pong control."""

    kind: str
    data: str | bytes


class WebSocketConnection:
    """One client-side WebSocket with strict masking and message bounds."""

    def __init__(
        self,
        transport: socket.socket,
        *,
        initial_data: bytes = b"",
        io_timeout_seconds: float,
        max_message_bytes: int,
    ) -> None:
        """Wrap an upgraded socket and any bytes read past its HTTP headers."""
        self._transport = transport
        self._transport.settimeout(io_timeout_seconds)
        self._buffer = bytearray(initial_data)
        self._max_message_bytes = max_message_bytes
        self._fragment_opcode: int | None = None
        self._fragment = bytearray()
        self._close_sent = False
        self._closed = False

    @classmethod
    def connect(
        cls,
        *,
        url: str,
        connect_address: str,
        token: str,
        connect_timeout_seconds: float,
        io_timeout_seconds: float,
        max_message_bytes: int,
        ssl_context: ssl.SSLContext | None = None,
    ) -> WebSocketConnection:
        """Open and validate an HTTP Upgrade without using ambient proxies."""
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme not in {"ws", "wss"} or host is None:
            raise WebSocketError("invalid WebSocket URL")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        family = socket.AF_INET6 if ":" in connect_address else socket.AF_INET
        address: tuple[Any, ...]
        if family == socket.AF_INET6:
            address = (connect_address, port, 0, 0)
        else:
            address = (connect_address, port)

        raw = socket.socket(family, socket.SOCK_STREAM)
        transport: socket.socket = raw
        deadline = time.monotonic() + connect_timeout_seconds
        try:
            _set_remaining_timeout(raw, deadline)
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            raw.connect(address)
            if parsed.scheme == "wss":
                context = ssl_context or ssl.create_default_context()
                _set_remaining_timeout(raw, deadline)
                transport = context.wrap_socket(raw, server_hostname=host)
            _set_remaining_timeout(transport, deadline)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = _upgrade_request(parsed, host, port, key, token)
            transport.sendall(request)
            header, initial_data = _read_http_response(
                transport,
                deadline=deadline,
            )
            _validate_upgrade_response(header, key)
            return cls(
                transport,
                initial_data=initial_data,
                io_timeout_seconds=io_timeout_seconds,
                max_message_bytes=max_message_bytes,
            )
        except Exception:
            try:
                transport.close()
            finally:
                if transport is not raw:
                    raw.close()
            raise

    @property
    def transport(self) -> socket.socket:
        """Return the underlying socket for read readiness polling."""
        return self._transport

    def pending(self) -> int:
        """Return one when a complete buffered frame can be consumed."""
        metadata = self._frame_metadata()
        if metadata is None:
            return 0
        _final, _opcode, header_size, length = metadata
        return int(len(self._buffer) >= header_size + length)

    def send_json(self, value: dict[str, Any]) -> None:
        """Send one bounded JSON object."""
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
        if len(encoded) > min(_MAX_TEXT_BYTES, self._max_message_bytes):
            raise WebSocketError("outgoing JSON message exceeds its bound")
        self._send_frame(_OP_TEXT, encoded)

    def send_binary(self, value: bytes) -> None:
        """Send one non-empty, sample-aligned binary audio message."""
        if not value or len(value) % 2:
            raise WebSocketError("outgoing binary audio must be PCM16 aligned")
        if len(value) > self._max_message_bytes:
            raise WebSocketError("outgoing binary message exceeds its bound")
        self._send_frame(_OP_BINARY, value)

    def send_ping(self, payload: bytes) -> None:
        """Send a bounded ping payload."""
        if len(payload) > 125:
            raise WebSocketError("ping payload is too large")
        self._send_frame(_OP_PING, payload)

    def send_close(self, code: int = 1000) -> None:
        """Send at most one close frame."""
        if self._close_sent or self._closed:
            return
        self._send_frame(_OP_CLOSE, struct.pack("!H", code))
        self._close_sent = True

    def receive_message(self) -> Message | None:
        """Consume available bytes without waiting for a partial frame.

        The session thread calls this only after readiness polling. Reading at
        most once per call keeps interrupt/stop checks responsive even if a
        peer deliberately trickles one WebSocket frame.
        """
        read_attempted = False
        consumed_frame = False
        while True:
            final, opcode, payload = self._read_frame()
            if final is None:
                if read_attempted or consumed_frame:
                    return None
                self._receive_some()
                read_attempted = True
                continue
            assert opcode is not None and payload is not None
            consumed_frame = True
            if opcode >= _OP_CLOSE:
                control = self._handle_control(final, opcode, payload)
                if control is not None:
                    return control
                continue
            if opcode == _OP_CONTINUATION:
                if self._fragment_opcode is None:
                    raise WebSocketError("unexpected continuation frame")
                self._append_fragment(payload)
                if not final:
                    continue
                message_opcode = self._fragment_opcode
                complete = bytes(self._fragment)
                self._fragment_opcode = None
                self._fragment.clear()
                return self._decode_message(message_opcode, complete)
            if opcode not in {_OP_TEXT, _OP_BINARY}:
                raise WebSocketError("unsupported WebSocket opcode")
            if self._fragment_opcode is not None:
                raise WebSocketError("new data frame arrived during fragmentation")
            if final:
                return self._decode_message(opcode, payload)
            self._fragment_opcode = opcode
            self._fragment.clear()
            self._append_fragment(payload)

    def close(self) -> None:
        """Close the socket without allowing cleanup errors to escape."""
        if self._closed:
            return
        self._closed = True
        with suppress(OSError):
            self._transport.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            self._transport.close()

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WebSocketClosed("WebSocket is closed")
        self._transport.sendall(encode_client_frame(opcode, payload))

    def _read_frame(
        self,
    ) -> tuple[bool | None, int | None, bytes | None]:
        metadata = self._frame_metadata()
        if metadata is None:
            return None, None, None
        final, opcode, header_size, length = metadata
        total_size = header_size + length
        if len(self._buffer) < total_size:
            return None, None, None
        payload = bytes(self._buffer[header_size:total_size])
        del self._buffer[:total_size]
        return final, opcode, payload

    def _frame_metadata(self) -> tuple[bool, int, int, int] | None:
        if len(self._buffer) < 2:
            return None
        first, second = self._buffer[:2]
        final = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketError("WebSocket extensions are not supported")
        opcode = first & 0x0F
        if second & 0x80:
            raise WebSocketError("server WebSocket frames must not be masked")
        length_code = second & 0x7F
        header_size = 2
        if length_code == 126:
            if len(self._buffer) < 4:
                return None
            length = struct.unpack("!H", self._buffer[2:4])[0]
            header_size = 4
            if length < 126:
                raise WebSocketError("non-canonical WebSocket frame length")
        elif length_code == 127:
            if len(self._buffer) < 10:
                return None
            encoded_length = self._buffer[2:10]
            header_size = 10
            if encoded_length[0] & 0x80:
                raise WebSocketError("invalid WebSocket frame length")
            length = struct.unpack("!Q", encoded_length)[0]
            if length <= 0xFFFF:
                raise WebSocketError("non-canonical WebSocket frame length")
        else:
            length = length_code
        if opcode >= _OP_CLOSE:
            if not final or length > 125:
                raise WebSocketError("invalid fragmented control frame")
        elif length > self._max_message_bytes:
            raise WebSocketError("WebSocket frame exceeds its bound")
        return final, opcode, header_size, length

    def _handle_control(
        self, final: bool, opcode: int, payload: bytes
    ) -> Message | None:
        if not final:
            raise WebSocketError("control frames must not be fragmented")
        if opcode == _OP_PING:
            self._send_frame(_OP_PONG, payload)
            return None
        if opcode == _OP_PONG:
            return Message("pong", payload)
        if opcode != _OP_CLOSE:
            raise WebSocketError("unsupported WebSocket control opcode")
        if len(payload) == 1:
            raise WebSocketError("invalid close payload")
        if len(payload) >= 2:
            code = struct.unpack("!H", payload[:2])[0]
            if not _valid_close_code(code):
                raise WebSocketError("invalid close status code")
            try:
                payload[2:].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WebSocketError("close reason must be UTF-8") from exc
        if not self._close_sent:
            self._send_frame(_OP_CLOSE, payload)
            self._close_sent = True
        raise WebSocketClosed("peer closed WebSocket")

    def _append_fragment(self, payload: bytes) -> None:
        if len(self._fragment) + len(payload) > self._max_message_bytes:
            raise WebSocketError("fragmented WebSocket message exceeds its bound")
        self._fragment.extend(payload)

    def _decode_message(self, opcode: int, payload: bytes) -> Message:
        if opcode == _OP_BINARY:
            if not payload or len(payload) % 2:
                raise WebSocketError("binary audio must be non-empty PCM16")
            return Message("binary", payload)
        if len(payload) > min(_MAX_TEXT_BYTES, self._max_message_bytes):
            raise WebSocketError("text WebSocket message exceeds its bound")
        try:
            return Message("text", payload.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise WebSocketError("text WebSocket message must be UTF-8") from exc

    def _receive_some(self) -> None:
        maximum_buffer = self._max_message_bytes + 16 * 1024
        remaining = maximum_buffer - len(self._buffer)
        if remaining <= 0:
            raise WebSocketError("WebSocket receive buffer exceeded its bound")
        try:
            chunk = self._transport.recv(min(4_096, remaining))
        except (TimeoutError, BlockingIOError, ssl.SSLWantReadError):
            return
        if not chunk:
            raise WebSocketClosed("WebSocket transport reached EOF")
        self._buffer.extend(chunk)


def encode_client_frame(
    opcode: int, payload: bytes, *, mask: bytes | None = None
) -> bytes:
    """Encode one final, masked client frame."""
    if opcode not in {_OP_TEXT, _OP_BINARY, _OP_CLOSE, _OP_PING, _OP_PONG}:
        raise WebSocketError("unsupported outgoing WebSocket opcode")
    if opcode >= _OP_CLOSE and len(payload) > 125:
        raise WebSocketError("control payload is too large")
    masking_key = mask if mask is not None else os.urandom(4)
    if len(masking_key) != 4:
        raise WebSocketError("masking key must contain four bytes")
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
    masked = bytes(
        value ^ masking_key[index % 4] for index, value in enumerate(payload)
    )
    return header + masking_key + masked


def _upgrade_request(
    parsed: Any,
    host: str,
    port: int,
    key: str,
    token: str,
) -> bytes:
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    if "\r" in target or "\n" in target:
        raise WebSocketError("invalid WebSocket request target")
    try:
        target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise WebSocketError("WebSocket request target must be ASCII encoded") from exc
    host_header = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "wss" else 80
    if port != default_port:
        host_header = f"{host_header}:{port}"
    lines = [
        f"GET {target} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        f"Authorization: Bearer {token}",
        "User-Agent: ha-codex-voice-thirdreality/1",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


def _read_http_response(
    transport: socket.socket,
    *,
    deadline: float,
) -> tuple[bytes, bytes]:
    response = bytearray()
    while b"\r\n\r\n" not in response:
        if len(response) >= _MAX_HTTP_HEADER_BYTES:
            raise WebSocketError("WebSocket upgrade headers are too large")
        _set_remaining_timeout(transport, deadline)
        chunk = transport.recv(min(4_096, _MAX_HTTP_HEADER_BYTES - len(response)))
        if not chunk:
            raise WebSocketError("WebSocket upgrade reached EOF")
        response.extend(chunk)
    marker = response.index(b"\r\n\r\n") + 4
    return bytes(response[:marker]), bytes(response[marker:])


def _set_remaining_timeout(transport: Any, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("WebSocket connection deadline expired")
    transport.settimeout(remaining)


def _validate_upgrade_response(header: bytes, key: str) -> None:
    try:
        lines = header.decode("iso-8859-1").split("\r\n")
    except UnicodeDecodeError as exc:  # pragma: no cover - ISO-8859-1 is total
        raise WebSocketError("invalid WebSocket upgrade response") from exc
    status = lines[0].split(" ", 2)
    if len(status) < 2 or status[0] != "HTTP/1.1" or status[1] != "101":
        raise WebSocketError("WebSocket upgrade was rejected")
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line:
            continue
        if line[0].isspace() or ":" not in line:
            raise WebSocketError("malformed WebSocket upgrade header")
        name, value = line.split(":", 1)
        headers.setdefault(name.strip().casefold(), []).append(value.strip())
    if "websocket" not in _header_tokens(headers, "upgrade"):
        raise WebSocketError("WebSocket upgrade response is missing Upgrade")
    if "upgrade" not in _header_tokens(headers, "connection"):
        raise WebSocketError("WebSocket upgrade response is missing Connection")
    accept_values = headers.get("sec-websocket-accept", [])
    expected = base64.b64encode(
        hashlib.sha1(key.encode("ascii") + _GUID, usedforsecurity=False).digest()
    ).decode("ascii")
    if accept_values != [expected]:
        raise WebSocketError("WebSocket upgrade response has an invalid accept key")
    if "sec-websocket-extensions" in headers:
        raise WebSocketError("WebSocket extensions were not requested")
    if "sec-websocket-protocol" in headers:
        raise WebSocketError("WebSocket subprotocol was not requested")


def _header_tokens(headers: dict[str, list[str]], name: str) -> set[str]:
    return {
        token.strip().casefold()
        for value in headers.get(name, [])
        for token in value.split(",")
        if token.strip()
    }


def _valid_close_code(code: int) -> bool:
    if code in {
        1000,
        1001,
        1002,
        1003,
        1007,
        1008,
        1009,
        1010,
        1011,
        1012,
        1013,
        1014,
    }:
        return True
    return 3000 <= code <= 4999
