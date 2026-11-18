#!/usr/bin/env python3
"""CLI Pig game client.

Usage:
    python3 client.py -h
    python3 client.py -i server.mccaulley.edu -p 8080

A thin view of the server's game (client FSM, fsm_specification.md
Section 6). The client never computes scores or decides turns; it draws
whatever the last message said and sends ROLL / HOLD when asked.

Threads: one reads the socket, one reads the keyboard, and both put events
on a single queue. The main thread takes events off that queue one at a
time, so a GAME_OVER can be shown even while the user is at a prompt.
"""

from __future__ import annotations

import argparse
import queue
import socket
import sys
import threading
from enum import Enum

from protocol import (FrameTooLargeError, LineBuffer, RECV_CHUNK,
                      make_message, send_message)
from validation import (MAX_PORT, MIN_PORT, ProtocolError, is_valid_alias,
                        parse_line, valid_port, validate_payload)

CONNECT_TIMEOUT = 10        # seconds to wait for the TCP handshake
EXIT_OK = 0
EXIT_ERROR = 1              # DNS failure, lost connection, lobby full
EXIT_CONNECT = 2            # server refused or unreachable
RULE = "-" * 46


class ClientState(Enum):
    """Client FSM states that follow CONNECTING."""

    JOINING = "JOINING"     # CONNECT sent, waiting for the server's answer
    LOBBY = "LOBBY"         # seated as Player 1, waiting for an opponent
    IN_GAME = "IN_GAME"
    FINISHED = "FINISHED"


def say(text: str = "") -> None:
    """Print and flush, so output is live even when piped to a log file."""
    print(text, flush=True)


# --- Startup: arguments, DNS, TCP connect -----------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line. -h is added by argparse."""
    parser = argparse.ArgumentParser(
        description="CLI Pig game client. Connects to a Pig server and "
                    "plays one two-player game.",
        epilog="example: python3 client.py -i server.mccaulley.edu -p 8080",
    )
    parser.add_argument(
        "-i", "--host", required=True, metavar="HOSTNAME", dest="hostname",
        help="server DNS hostname or IP address (required)",
    )
    parser.add_argument(
        "-p", "--port", type=valid_port, required=True, metavar="PORT",
        help=f"server TCP port ({MIN_PORT}-{MAX_PORT}, required)",
    )
    parser.add_argument(
        "-a", "--alias", metavar="NAME",
        help="player name, 1-16 letters/digits/underscores "
             "(asked for if omitted)",
    )
    args = parser.parse_args(argv)
    if args.alias is not None and not is_valid_alias(args.alias):
        parser.error("-a/--alias must be 1-16 letters, digits or "
                     "underscores")
    return args


def resolve_host(hostname: str) -> str:
    """Resolve the server hostname through DNS, or exit cleanly.

    No IP address is hard-coded anywhere: whatever -i names is looked up
    here. A failed lookup prints advice and exits with status 1 instead of
    a traceback.
    """
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        say(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        say("Please check your Cisco router DNS configuration "
            "('ip dns server' / 'ip host') or verify domain spelling.")
        sys.exit(EXIT_ERROR)
    except UnicodeError:
        # Raised for names no DNS label can hold (empty or over-long).
        say(f"[ERROR] '{hostname}' is not a valid DNS hostname.")
        sys.exit(EXIT_ERROR)


def open_connection(server_ip: str, port: int,
                    hostname: str) -> socket.socket:
    """Open the TCP connection, or exit cleanly if the server is down."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect((server_ip, port))
    except OSError as exc:      # refused, timed out, no route to host
        sock.close()
        say(f"[ERROR] Could not connect to {hostname} ({server_ip}) "
            f"port {port}: {exc}")
        say("Check that server.py is running and the port is correct.")
        sys.exit(EXIT_CONNECT)
    sock.settimeout(None)       # gameplay uses plain blocking recv()
    return sock


def ask_alias() -> str:
    """Prompt until the user gives an alias the protocol allows."""
    while True:
        try:
            alias = input("Enter your player name: ").strip()
        except (EOFError, KeyboardInterrupt):
            say()
            sys.exit(EXIT_OK)
        if is_valid_alias(alias):
            return alias
        say("Names are 1-16 letters, digits or underscores. Try again.")


# --- Background threads ------------------------------------------------------
def network_reader(sock: socket.socket, events: queue.Queue) -> None:
    """Receive loop: frame, validate and queue every server message."""
    buffer = LineBuffer()
    while True:
        try:
            chunk = sock.recv(RECV_CHUNK)
        except OSError:
            break                           # reset, abort or local close
        if not chunk:
            break                           # b"" is EOF: server sent FIN
        try:
            lines = buffer.feed(chunk)      # handles split/merged messages
        except FrameTooLargeError:
            break
        for line in lines:
            try:
                msg = parse_line(line)
                validate_payload(msg, sender_is_client=False)
            except ProtocolError as exc:
                events.put(("bad", exc.detail))
                continue
            events.put(("net", msg))
    events.put(("closed", None))


def keyboard_reader(events: queue.Queue) -> None:
    """Queue each line the user types."""
    for line in sys.stdin:
        events.put(("key", line.strip()))
    events.put(("eof", None))               # stdin closed: treat as quit


# --- The client -------------------------------------------------------------
class PigClient:
    """Renders server messages and turns keystrokes into MOVE messages."""

    def __init__(self, sock: socket.socket, alias: str) -> None:
        self.sock = sock
        self.alias = alias
        self.events: queue.Queue = queue.Queue()
        self.state = ClientState.JOINING
        self.role: str | None = None        # assigned by GAME_START
        self.names: dict[str, str] = {}     # role -> alias
        self.target = 100
        self.my_turn = False                # from the last STATE_UPDATE
        self.can_move = False               # True while a move is wanted
        self.need_alias = False             # True after ERROR BAD_ALIAS

    def run(self) -> int:
        """Main loop. Returns the process exit code."""
        threads = ((network_reader, (self.sock, self.events)),
                   (keyboard_reader, (self.events,)))
        for target, args in threads:
            threading.Thread(target=target, args=args, daemon=True).start()
        try:
            self._send("CONNECT", {"alias": self.alias})
            while True:
                try:
                    # The timeout keeps Ctrl+C responsive on every OS.
                    kind, data = self.events.get(timeout=0.5)
                except queue.Empty:
                    continue
                code = self._dispatch(kind, data)
                if code is not None:
                    return code
        except KeyboardInterrupt:
            say()
            return self._quit("user pressed Ctrl+C")
        except OSError:
            say("\nConnection to server lost.")
            return EXIT_ERROR
        finally:
            self.sock.close()

    def _dispatch(self, kind: str, data) -> int | None:
        """Handle one event. A return value ends the client."""
        if kind == "net":
            return self._on_message(data)
        if kind == "key":
            return self._on_key(data)
        if kind == "eof":
            return self._quit("input closed")
        if kind == "bad":
            say(f"[warning] Ignored a bad message from the server: {data}")
            return None
        # kind == "closed"
        if self.state is ClientState.FINISHED:
            return EXIT_OK
        say("\nConnection to server lost.")
        return EXIT_ERROR

    # --- Sending -----------------------------------------------------------
    def _send(self, msg_type: str, payload: dict) -> None:
        send_message(self.sock, make_message(msg_type, self.role, payload))

    def _quit(self, reason: str) -> int:
        """Tell the server why we are leaving, then exit (blueprint 6.1)."""
        try:
            self._send("DISCONNECT", {"reason": reason})
        except OSError:
            pass                            # already gone; nothing to tell
        say("Goodbye.")
        return EXIT_OK

    # --- Keyboard ----------------------------------------------------------
    def _on_key(self, text: str) -> int | None:
        word = text.lower()
        if word in ("q", "quit", "exit"):
            return self._quit("user quit")

        if self.need_alias:
            if is_valid_alias(text):
                self.alias, self.need_alias = text, False
                self._send("CONNECT", {"alias": text})
            else:
                say("Names are 1-16 letters, digits or underscores.")
                say("Enter a new player name: ")
        elif self.state is not ClientState.IN_GAME:
            say("Waiting for the game to start. Type 'quit' to leave.")
        elif not self.can_move:
            say(f"Not your turn. Waiting for {self._opponent()}...")
        elif word in ("r", "roll"):
            self.can_move = False           # one move per prompt
            self._send("MOVE", {"action": "ROLL"})
        elif word in ("h", "hold"):
            self.can_move = False
            self._send("MOVE", {"action": "HOLD"})
        else:
            say("Type r to roll, h to hold, or quit to leave.")
            self._prompt()
        return None

    # --- Server messages ---------------------------------------------------
    def _on_message(self, msg: dict) -> int | None:
        payload = msg["payload"]
        msg_type = msg["msg_type"]

        if msg_type == "LOBBY_WAIT":
            self.state = ClientState.LOBBY
            say(f"Joined as {self.alias}. {payload['message']}")
            say("(type 'quit' to leave)")
        elif msg_type == "GAME_START":
            self.state = ClientState.IN_GAME
            self.role = payload["your_role"]
            other = "PLAYER_2" if self.role == "PLAYER_1" else "PLAYER_1"
            self.names = {self.role: payload["your_alias"],
                          other: payload["opponent_alias"]}
            self.target = payload["target_score"]
            say(f"\nGame on! You are {self.role} ({self.names[self.role]}) "
                f"against {self.names[other]}.")
            say(f"First to bank {self.target} wins. Rolling a 1 loses "
                "your turn total.")
        elif msg_type == "STATE_UPDATE":
            self._draw_board(payload)
        elif msg_type == "ERROR":
            return self._on_error(payload)
        elif msg_type == "GAME_OVER":
            self._draw_result(payload)
            self.state = ClientState.FINISHED
            return EXIT_OK
        return None

    def _on_error(self, payload: dict) -> int | None:
        say(f"[server] {payload['detail']}")
        if payload["code"] == "LOBBY_FULL":
            say("Try again when the current game has finished.")
            return EXIT_ERROR
        if payload["code"] == "BAD_ALIAS":
            self.need_alias = True
            say("Enter a new player name: ")
        elif (self.state is ClientState.IN_GAME
              and payload["code"] in ("INVALID_ACTION", "NOT_YOUR_TURN")):
            # The move was rejected, so ask again if it is still our turn.
            self.can_move = self.my_turn
            if self.can_move:
                self._prompt()
        return None

    # --- Rendering ---------------------------------------------------------
    def _opponent(self) -> str:
        for role, name in self.names.items():
            if role != self.role:
                return name
        return "Player 2"

    def _label(self, role: str) -> str:
        name = self.names.get(role, role)
        return f"{name} (you)" if role == self.role else name

    def _prompt(self) -> None:
        say("*** Your Turn ***  [R]oll or [H]old? ")

    def _draw_board(self, payload: dict) -> None:
        """Redraw the whole board from one full-snapshot STATE_UPDATE."""
        event = payload["last_event"]
        active = payload["active_player"]
        who = self.names.get(event["player"], event["player"])
        if event["result"] == "TURN_START":
            headline = f"{who} starts."
        elif event["result"] == "ROLLED":
            headline = f"{who} rolled a {event['die']}."
        elif event["result"] == "BUSTED":
            headline = f"{who} rolled a 1 and BUSTED. Turn total lost."
        else:
            headline = f"{who} held and banked {event['points_banked']}."

        say()
        say("=" * len(RULE))
        say(f" PIG  |  first to {self.target}  |  {headline}")
        say(RULE)
        for role in ("PLAYER_1", "PLAYER_2"):
            marker = "->" if role == active else "  "
            say(f" {marker} {self._label(role):<24}"
                f"{payload['scores'][role]:>4}")
        say(RULE)
        say(f" Turn total ({self.names.get(active, active)}): "
            f"{payload['turn_total']}")
        if payload["final_turn"]:
            say(f" FINAL TURN: {self.names.get('PLAYER_2', 'Player 2')} "
                "gets one last turn to tie or win.")
        say("=" * len(RULE))

        self.my_turn = active == self.role
        self.can_move = self.my_turn
        if self.my_turn:
            self._prompt()
        else:
            say(f"Waiting for {self._opponent()}...")

    def _draw_result(self, payload: dict) -> None:
        scores = payload["final_scores"]
        say()
        say("=" * len(RULE))
        say(" GAME OVER")
        say(RULE)
        for role in ("PLAYER_1", "PLAYER_2"):
            say(f"    {self._label(role):<24}{scores[role]:>4}")
        say(RULE)
        if payload["outcome"] == "DRAW":
            say(" It's a draw!")
        elif payload["winner"] == self.role:
            say(" You win!")
        else:
            say(f" {self._label(payload['winner'])} wins.")
        say(f" ({payload['outcome']}: {payload['reason']})")
        say("=" * len(RULE))


def main() -> int:
    """Entry point. Returns the process exit code."""
    args = parse_args()

    # Client FSM: RESOLVING -> CONNECTING -> JOINING. DNS is checked first
    # so a bad hostname fails before the user is asked for anything.
    server_ip = resolve_host(args.hostname)
    alias = args.alias or ask_alias()
    sock = open_connection(server_ip, args.port, args.hostname)
    say(f"Connected to {args.hostname} ({server_ip}) port {args.port}.")
    return PigClient(sock, alias).run()


if __name__ == "__main__":
    sys.exit(main())
