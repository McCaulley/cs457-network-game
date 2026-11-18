"""Wire framing for CLI Pig.

Implements protocol_blueprint.md Section 2 (newline-delimited JSON over TCP)
and the constants in Section 7. Both client.py and server.py import this
module so the two ends cannot disagree about how a message is framed.
"""

from __future__ import annotations

import json
import socket
import time

# --- Blueprint Section 7 constants -----------------------------------------
MAX_MSG_BYTES = 4096      # largest legal message, including the "\n"
RECV_CHUNK = 4096         # bytes requested per recv()
ENCODING = "utf-8"
DELIMITER = b"\n"         # the only 0x0A byte that ever appears on the wire


class FrameTooLargeError(Exception):
    """More than MAX_MSG_BYTES arrived without a complete, legal line."""


def make_message(msg_type: str, player_id: str | None, payload: dict) -> dict:
    """Build the four-key envelope from blueprint Section 3."""
    return {
        "msg_type": msg_type,
        "player_id": player_id,
        "payload": payload,
        "timestamp": int(time.time()),
    }


def encode_message(msg: dict) -> bytes:
    """Serialize one message to its wire form (blueprint Section 2.4).

    json.dumps() without ``indent`` never emits a raw newline, so the
    appended delimiter is the only one in the frame.
    """
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    data = line.encode(ENCODING)
    if len(data) > MAX_MSG_BYTES:
        raise ValueError(
            f"message is {len(data)} bytes; limit is {MAX_MSG_BYTES}"
        )
    return data


def send_message(sock: socket.socket, msg: dict) -> None:
    """Encode and send one message. sendall() retries partial writes."""
    sock.sendall(encode_message(msg))


class LineBuffer:
    """Per-connection receive buffer (blueprint Section 2.5).

    TCP is a byte stream: one recv() may hold several messages
    (coalescing) or only part of one (fragmentation). feed() accepts
    whatever arrived and returns only the complete lines, keeping any
    trailing partial line for the next call.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add ``chunk`` and return every complete line, delimiter removed.

        Blank lines (and a stray "\\r" from a "\\r\\n" sender) are skipped.
        Raises FrameTooLargeError for an oversized line or when the
        leftover partial line grows past MAX_MSG_BYTES with no delimiter.
        """
        self._buffer += chunk
        lines: list[bytes] = []
        while True:
            end = self._buffer.find(DELIMITER)
            if end < 0:
                break                       # no complete line left
            line = bytes(self._buffer[:end])
            del self._buffer[:end + 1]      # drop the line and its "\n"
            if len(line) + 1 > MAX_MSG_BYTES:
                raise FrameTooLargeError(len(line) + 1)
            line = line.rstrip(b"\r")
            if line:
                lines.append(line)
        if len(self._buffer) > MAX_MSG_BYTES:
            raise FrameTooLargeError(len(self._buffer))
        return lines
