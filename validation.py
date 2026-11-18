"""Envelope and payload validation for CLI Pig.

Implements protocol_blueprint.md Section 2.5 (process_line steps 2-6),
Section 3 (envelope) and Section 4 (per-message payloads). Nothing here
reads game state, so these checks run without the server's room lock.
"""

from __future__ import annotations

import argparse
import json
import re

from protocol import ENCODING

ROLES = ("PLAYER_1", "PLAYER_2")
CLIENT_TYPES = frozenset({"CONNECT", "MOVE", "DISCONNECT"})
SERVER_TYPES = frozenset(
    {"LOBBY_WAIT", "GAME_START", "STATE_UPDATE", "ERROR", "GAME_OVER"}
)
ALL_TYPES = CLIENT_TYPES | SERVER_TYPES
ACTIONS = ("ROLL", "HOLD")
ALIAS_PATTERN = re.compile(r"[A-Za-z0-9_]{1,16}")   # used with fullmatch()
ERROR_CODES = frozenset({
    "MALFORMED_MESSAGE", "UNKNOWN_MSG_TYPE", "BAD_ALIAS",
    "ALREADY_CONNECTED", "PLAYER_ID_MISMATCH", "GAME_NOT_STARTED",
    "NOT_YOUR_TURN", "INVALID_ACTION", "LOBBY_FULL", "MESSAGE_TOO_LARGE",
})

MIN_PORT, MAX_PORT = 1024, 65535


class ProtocolError(Exception):
    """A message broke the protocol. ``code`` is a Section 4.6 error code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def valid_port(text: str) -> int:
    """argparse ``type=`` for -p: an integer in the unprivileged range."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{text}' is not a number (expected {MIN_PORT}-{MAX_PORT})"
        ) from None
    if not MIN_PORT <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(
            f"{port} is out of range (expected {MIN_PORT}-{MAX_PORT})"
        )
    return port


def is_valid_alias(alias: object) -> bool:
    """Blueprint Section 4.1: 1-16 letters, digits or underscores."""
    return isinstance(alias, str) and bool(ALIAS_PATTERN.fullmatch(alias))


# --- Small type predicates --------------------------------------------------
def _is_int(value: object) -> bool:
    # bool is a subclass of int in Python; JSON true/false must not pass.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_role(value: object) -> bool:
    return value in ROLES


def _is_scores(value: object) -> bool:
    return (isinstance(value, dict)
            and all(_is_int(value.get(role)) for role in ROLES))


def _is_last_event(value: object) -> bool:
    return (isinstance(value, dict)
            and _is_role(value.get("player"))
            and value.get("result") in ("TURN_START", "ROLLED",
                                        "BUSTED", "HELD")
            and (value.get("die") is None or _is_int(value.get("die")))
            and _is_int(value.get("points_banked")))


# Required payload fields for each server-to-client message (Section 4).
_SERVER_SCHEMAS = {
    "LOBBY_WAIT": {"players_connected": _is_int, "message": _is_str},
    "GAME_START": {
        "your_role": _is_role, "your_alias": _is_str,
        "opponent_alias": _is_str, "first_player": _is_role,
        "target_score": _is_int,
    },
    "STATE_UPDATE": {
        "active_player": _is_role, "scores": _is_scores,
        "turn_total": _is_int,
        "final_turn": lambda v: isinstance(v, bool),
        "last_event": _is_last_event,
    },
    "ERROR": {"code": lambda v: v in ERROR_CODES, "detail": _is_str},
    "GAME_OVER": {
        "outcome": lambda v: v in ("WIN", "DRAW", "FORFEIT"),
        "winner": lambda v: v is None or _is_role(v),
        "final_scores": _is_scores, "reason": _is_str,
    },
}


def parse_line(line: bytes) -> dict:
    """Turn one framed line into a validated envelope (steps 2-5)."""
    try:
        text = line.decode(ENCODING)                       # step 2
    except UnicodeDecodeError:
        raise ProtocolError("MALFORMED_MESSAGE",
                            "Message is not valid UTF-8.") from None
    try:
        msg = json.loads(text)                             # step 3
    except ValueError:
        raise ProtocolError("MALFORMED_MESSAGE",
                            "Message is not valid JSON.") from None
    if not isinstance(msg, dict):
        raise ProtocolError("MALFORMED_MESSAGE",
                            "Message must be a JSON object.")

    # Step 4: the four envelope keys and their types (Section 3).
    # Extra top-level keys are allowed and ignored.
    player_id = msg.get("player_id")
    if (not _is_str(msg.get("msg_type"))
            or "player_id" not in msg
            or not (player_id is None or _is_str(player_id))
            or not isinstance(msg.get("payload"), dict)
            or not _is_int(msg.get("timestamp"))):
        raise ProtocolError(
            "MALFORMED_MESSAGE",
            "Envelope needs msg_type, player_id, payload and timestamp.",
        )

    if msg["msg_type"] not in ALL_TYPES:                   # step 5
        raise ProtocolError("UNKNOWN_MSG_TYPE",
                            f"Unknown msg_type '{msg['msg_type']}'.")
    return msg


def validate_payload(msg: dict, sender_is_client: bool) -> None:
    """Check the payload against its Section 4 schema (step 6).

    ``sender_is_client`` is True on the server, where a server-only type
    arriving from a client is rejected as UNKNOWN_MSG_TYPE.
    """
    msg_type = msg["msg_type"]
    payload = msg["payload"]

    if msg_type in SERVER_TYPES:
        if sender_is_client:
            raise ProtocolError("UNKNOWN_MSG_TYPE",
                                f"{msg_type} is a server-only message.")
        for field, check in _SERVER_SCHEMAS[msg_type].items():
            if field not in payload or not check(payload[field]):
                raise ProtocolError(
                    "MALFORMED_MESSAGE",
                    f"{msg_type} payload has a bad '{field}' field.",
                )
    elif msg_type == "CONNECT":
        if not is_valid_alias(payload.get("alias")):
            raise ProtocolError(
                "BAD_ALIAS",
                "Alias must be 1-16 letters, digits or underscores.",
            )
    elif msg_type == "MOVE":
        if payload.get("action") not in ACTIONS:
            raise ProtocolError("INVALID_ACTION",
                                "Action must be ROLL or HOLD.")
    elif msg_type == "DISCONNECT":
        if not _is_str(payload.get("reason", "")):
            raise ProtocolError("MALFORMED_MESSAGE",
                                "DISCONNECT reason must be a string.")
