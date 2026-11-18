#!/usr/bin/env python3
"""CLI Pig game server.

Usage:
    python3 server.py -h
    python3 server.py -p 8080

Networking and concurrency around the socket-free PigGame (game.py),
following sprint2/concurrency_design.md:

* The main thread only accepts connections.
* Each connection gets a reader thread (blocking recv loop) and a writer
  thread (drains that connection's outbox queue).
* One lock, room_lock, guards all shared room state. Handlers change the
  state and queue the resulting messages while holding it; the writer
  threads do the actual sendall() outside it. That fixes message order
  without letting one slow client stall the other.
"""

from __future__ import annotations

import argparse
import logging
import queue
import socket
import sys
import threading
from dataclasses import dataclass, field

from game import ALL, Outbound, PigGame, ServerState
from protocol import (FrameTooLargeError, LineBuffer, RECV_CHUNK,
                      encode_message, make_message)
from validation import (MAX_PORT, MIN_PORT, ProtocolError, parse_line,
                        valid_port, validate_payload)

log = logging.getLogger("pig.server")

BIND_ADDRESS = "0.0.0.0"    # all interfaces, as the assignment requires
LISTEN_BACKLOG = 5
JOIN_TIMEOUT = 15           # seconds a new socket has to send CONNECT
CLOSE = object()            # outbox sentinel: "flush, then close the socket"


@dataclass(eq=False)        # eq=False: connections compare by identity
class Connection:
    """Everything the server knows about one accepted socket."""

    sock: socket.socket
    addr: tuple
    outbox: queue.Queue = field(default_factory=queue.Queue)
    buffer: LineBuffer = field(default_factory=LineBuffer)  # reader only
    role: str | None = None     # PLAYER_1 / PLAYER_2 once joined
    closed: bool = False        # set once, under room_lock

    @property
    def name(self) -> str:
        """Label for log lines."""
        return self.role or f"{self.addr[0]}:{self.addr[1]}"


class PigServer:
    """One listening socket and one game room."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.game = PigGame()
        self.room_lock = threading.Lock()
        self.players: dict[str, Connection] = {}    # role -> Connection
        self.listen_sock: socket.socket | None = None

    # --- FSM INIT ----------------------------------------------------------
    def start(self) -> None:
        """Create, bind and listen. Raises OSError if the bind fails."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Lets the server restart right away instead of waiting out
        # TIME_WAIT on the old listening socket.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((BIND_ADDRESS, self.port))
            sock.listen(LISTEN_BACKLOG)
        except OSError:
            sock.close()
            raise
        self.listen_sock = sock
        log.info("Listening on %s:%d", BIND_ADDRESS, self.port)
        with self.room_lock:
            self.game.listening()           # INIT -> WAITING_FOR_PLAYERS

    def serve_forever(self) -> None:
        """Accept loop. It never reads, writes or touches room state."""
        while True:
            try:
                sock, addr = self.listen_sock.accept()
            except OSError as exc:
                log.error("accept() failed: %s", exc)
                return
            enable_keepalive(sock)
            sock.settimeout(JOIN_TIMEOUT)   # lifted once the client joins
            conn = Connection(sock, addr)
            log.info("Accepted TCP connection from %s", conn.name)
            # Daemon threads: Ctrl+C on the server exits without joining.
            for target in (self._writer_loop, self._reader_loop):
                threading.Thread(target=target, args=(conn,),
                                 daemon=True).start()

    def close(self) -> None:
        """Stop listening."""
        if self.listen_sock is not None:
            self.listen_sock.close()

    # --- Per-connection threads --------------------------------------------
    def _reader_loop(self, conn: Connection) -> None:
        """Blocking receive loop (blueprint Section 2.5)."""
        joined = False
        while True:
            try:
                chunk = conn.sock.recv(RECV_CHUNK)
            except OSError as exc:
                # Covers RST, abort, keepalive timeout and the join timeout
                # (ConnectionResetError, TimeoutError, ... are OSErrors).
                self.drop(conn, "network", str(exc) or type(exc).__name__)
                return
            if not chunk:
                # b"" is EOF: the peer sent FIN. Without this return the
                # loop would spin on recv() returning b"" forever.
                self.drop(conn, "eof", "peer closed the connection")
                return

            try:
                lines = conn.buffer.feed(chunk)
            except FrameTooLargeError:
                self._enqueue_error(conn, "MESSAGE_TOO_LARGE",
                                    "No newline within 4096 bytes.")
                self.drop(conn, "network", "oversized frame")
                return

            for line in lines:
                try:
                    # Stateless checks: no lock needed.
                    msg = parse_line(line)
                    validate_payload(msg, sender_is_client=True)
                except ProtocolError as exc:
                    self._enqueue_error(conn, exc.code, exc.detail)
                    continue
                self.handle(conn, msg)

            if not joined and conn.role is not None:
                # Seated players may think as long as they like; from here
                # a dead peer is caught by EOF, RST or TCP keepalive.
                joined = True
                try:
                    conn.sock.settimeout(None)
                except OSError:
                    pass                    # socket already closed

    def _writer_loop(self, conn: Connection) -> None:
        """Send queued frames in order; the only thread that sends."""
        while True:
            item = conn.outbox.get()        # blocks; no busy loop
            if item is CLOSE:
                break
            try:
                conn.sock.sendall(item)
            except OSError as exc:          # BrokenPipe, reset, timeout
                self.drop(conn, "network", f"send failed: {exc}")
                break
        try:
            # shutdown() wakes this connection's reader out of recv();
            # close() alone does not do that on Linux.
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.sock.close()

    # --- Message handling (the critical section) ---------------------------
    def handle(self, conn: Connection, msg: dict) -> None:
        """Apply one validated client message as one FSM step."""
        msg_type = msg["msg_type"]
        with self.room_lock:
            if conn.closed:
                return                      # stale thread from a past game
            if msg_type == "CONNECT":
                self._join(conn, msg["payload"]["alias"])
            elif msg_type == "MOVE":
                self._move(conn, msg)
            # DISCONNECT changes nothing here; drop() below does the work.
            if self.game.state is ServerState.CLEANUP:
                self._cleanup_room()
        if msg_type == "DISCONNECT":
            self.drop(conn, "quit", msg["payload"].get("reason", ""))

    def _join(self, conn: Connection, alias: str) -> None:
        """CONNECT handling. Caller holds room_lock."""
        if conn.role is not None:
            self._enqueue_error(conn, "ALREADY_CONNECTED",
                                "This connection has already joined.")
        elif self.game.state is not ServerState.WAITING_FOR_PLAYERS:
            # Third client: reject without touching room state.
            self._enqueue_error(conn, "LOBBY_FULL",
                                "A game is already in progress.")
            conn.closed = True
            conn.outbox.put(CLOSE)
            log.info("Rejected %s: lobby full", conn.name)
        else:
            role, out = self.game.add_player(alias)
            conn.role = role
            self.players[role] = conn
            log.info("%s:%d joined as %s (alias '%s')",
                     conn.addr[0], conn.addr[1], role, alias)
            self._route(out)

    def _move(self, conn: Connection, msg: dict) -> None:
        """MOVE handling in the order of concurrency design Section 5.1."""
        action = msg["payload"]["action"]
        log.info("RX %s MOVE %s", conn.name, action)
        if (conn.role is None
                or self.game.state is not ServerState.PLAYER_TURN):
            self._enqueue_error(conn, "GAME_NOT_STARTED",
                                "The game has not started yet.")
        elif msg["player_id"] != conn.role:
            # Trust the socket, not the message: no spoofing the opponent.
            self._enqueue_error(conn, "PLAYER_ID_MISMATCH",
                                f"This connection is {conn.role}.")
        else:
            # NOT_YOUR_TURN and INVALID_ACTION are decided by the FSM.
            self._route(self.game.handle_move(conn.role, action))

    def _route(self, outbound: list[Outbound]) -> None:
        """Encode each message once and queue it. Caller holds room_lock.

        Encoding under the lock freezes the bytes at this state; queueing
        under the lock fixes their order for both players.
        """
        for item in outbound:
            data = encode_message(item.message)
            if item.recipient == ALL:
                targets = list(self.players.values())
            else:
                targets = [self.players.get(item.recipient)]
            for target in targets:
                if target is not None and not target.closed:
                    target.outbox.put(data)
            log.info("TX %s %s", item.recipient, describe(item.message))

    def _enqueue_error(self, conn: Connection, code: str,
                       detail: str) -> None:
        """Queue an ERROR for one client. Safe with or without the lock."""
        payload = {"code": code, "detail": detail}
        conn.outbox.put(encode_message(
            make_message("ERROR", conn.role, payload)))
        log.info("TX %s ERROR %s", conn.name, code)

    def _cleanup_room(self) -> None:
        """FSM CLEANUP. Caller holds room_lock.

        CLOSE goes in behind anything already queued, so each writer sends
        GAME_OVER before it closes its socket.
        """
        for conn in self.players.values():
            conn.closed = True
            conn.outbox.put(CLOSE)
        self.players.clear()
        self.game.reset()                   # -> WAITING_FOR_PLAYERS

    # --- Teardown ----------------------------------------------------------
    def drop(self, conn: Connection, how: str, why: str = "") -> None:
        """Single exit path for every way a connection can end.

        Idempotent: the reader and the writer may both report the same
        dead socket, and a stale thread may report one after a reset.
        """
        with self.room_lock:
            if conn.closed:
                return
            conn.closed = True
            log.warning("Lost %s (%s) %s", conn.name, how, why)
            # Look the player up by Connection object, not role string:
            # after a reset the same role may belong to someone new.
            if conn.role and self.players.get(conn.role) is conn:
                self._route(self.game.handle_disconnect(conn.role, how))
                del self.players[conn.role]
                if self.game.state is ServerState.CLEANUP:
                    self._cleanup_room()
            conn.outbox.put(CLOSE)


def enable_keepalive(sock: socket.socket) -> None:
    """Detect silently dead links without kicking idle players.

    Values from blueprint Section 6.3: probe after 30 s idle, every 10 s,
    dead after 3 misses. The TCP_KEEP* names are Linux-specific, so each
    is checked before use.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10),
                        ("TCP_KEEPCNT", 3)):
        if hasattr(socket, name):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)


def describe(msg: dict) -> str:
    """One-line summary of an outbound message for the server log."""
    payload = msg["payload"]
    if msg["msg_type"] == "STATE_UPDATE":
        event = payload["last_event"]
        scores = payload["scores"]
        die = f" die={event['die']}" if event["die"] is not None else ""
        return (f"STATE_UPDATE {event['player']} {event['result']}{die} "
                f"turn_total={payload['turn_total']} "
                f"scores={scores['PLAYER_1']}-{scores['PLAYER_2']} "
                f"active={payload['active_player']}")
    if msg["msg_type"] == "GAME_OVER":
        return (f"GAME_OVER {payload['outcome']} winner={payload['winner']} "
                f"({payload['reason']})")
    if msg["msg_type"] == "ERROR":
        return f"ERROR {payload['code']}"
    return msg["msg_type"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line. -h is added by argparse."""
    parser = argparse.ArgumentParser(
        description="CLI Pig game server. Listens on all interfaces "
                    "(0.0.0.0) and hosts one two-player game at a time.",
        epilog="example: python3 server.py -p 8080",
    )
    parser.add_argument(
        "-p", "--port", type=valid_port, required=True, metavar="PORT",
        help=f"TCP port to listen on ({MIN_PORT}-{MAX_PORT}, required)",
    )
    return parser.parse_args(argv)


def main() -> int:
    """Entry point. Returns the process exit code."""
    args = parse_args()
    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s [%(levelname)s] %(message)s")
    server = PigServer(args.port)
    try:
        server.start()
    except OSError as exc:
        print(f"[ERROR] Cannot listen on port {args.port}: {exc}",
              file=sys.stderr)
        print("Is another server already using this port?", file=sys.stderr)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
