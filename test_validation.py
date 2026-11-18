"""Envelope and payload validation tests."""

import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from validation import (ProtocolError, parse_line,  # noqa: E402
                        valid_port, validate_payload)

# Examples copied from protocol_blueprint.md Section 4.
EXAMPLES = [
    b'{"msg_type":"CONNECT","player_id":null,"payload":{"alias":"Wade"},'
    b'"timestamp":1759546800}',
    b'{"msg_type":"LOBBY_WAIT","player_id":"PLAYER_1","payload":'
    b'{"players_connected":1,"message":"Waiting for Player 2..."},'
    b'"timestamp":1759546800}',
    b'{"msg_type":"GAME_START","player_id":"PLAYER_2","payload":'
    b'{"your_role":"PLAYER_2","your_alias":"Bob","opponent_alias":"Wade",'
    b'"first_player":"PLAYER_1","target_score":100},"timestamp":1759546830}',
    b'{"msg_type":"MOVE","player_id":"PLAYER_1","payload":{"action":"ROLL"},'
    b'"timestamp":1759546812}',
    b'{"msg_type":"STATE_UPDATE","player_id":null,"payload":{"active_player"'
    b':"PLAYER_1","scores":{"PLAYER_1":34,"PLAYER_2":41},"turn_total":5,'
    b'"final_turn":false,"last_event":{"player":"PLAYER_1","result":'
    b'"ROLLED","die":5,"points_banked":0}},"timestamp":1759546812}',
    b'{"msg_type":"ERROR","player_id":"PLAYER_2","payload":{"code":'
    b'"NOT_YOUR_TURN","detail":"It is PLAYER_1\'s turn."},'
    b'"timestamp":1759546813}',
    b'{"msg_type":"DISCONNECT","player_id":"PLAYER_2","payload":{"reason":'
    b'"user quit"},"timestamp":1759546900}',
    b'{"msg_type":"GAME_OVER","player_id":null,"payload":{"outcome":"WIN",'
    b'"winner":"PLAYER_1","final_scores":{"PLAYER_1":104,"PLAYER_2":97},'
    b'"reason":"PLAYER_2 busted on final turn"},"timestamp":1759546950}',
]


def code_for(line: bytes, sender_is_client: bool = True) -> str:
    try:
        validate_payload(parse_line(line), sender_is_client)
    except ProtocolError as exc:
        return exc.code
    return "OK"


class ValidationTests(unittest.TestCase):
    def test_blueprint_examples_pass(self):
        for line in EXAMPLES:
            self.assertEqual(code_for(line, sender_is_client=False), "OK",
                             line)

    def test_malformed(self):
        for line in (b"\xff\xfe", b"not json", b"[1,2]",
                     b'{"msg_type":"MOVE"}',
                     b'{"msg_type":"MOVE","player_id":null,"payload":null,'
                     b'"timestamp":1}',
                     b'{"msg_type":"MOVE","player_id":null,"payload":{},'
                     b'"timestamp":"now"}'):
            self.assertEqual(code_for(line), "MALFORMED_MESSAGE", line)

    def test_unknown_and_server_only_types(self):
        self.assertEqual(
            code_for(b'{"msg_type":"CHAT","player_id":null,"payload":{},'
                     b'"timestamp":1}'), "UNKNOWN_MSG_TYPE")
        self.assertEqual(code_for(EXAMPLES[7]), "UNKNOWN_MSG_TYPE")

    def test_bad_alias(self):
        for alias in (b'""', b'"has space"', b'"x234567890123456789"',
                      b'"ok\\n"', b"5"):
            line = (b'{"msg_type":"CONNECT","player_id":null,"payload":'
                    b'{"alias":' + alias + b'},"timestamp":1}')
            self.assertEqual(code_for(line), "BAD_ALIAS", alias)

    def test_invalid_action(self):
        for payload in (b"{}", b'{"action":"roll"}', b'{"action":"CHEAT"}'):
            line = (b'{"msg_type":"MOVE","player_id":"PLAYER_1","payload":'
                    + payload + b',"timestamp":1}')
            self.assertEqual(code_for(line), "INVALID_ACTION", payload)

    def test_extra_top_level_keys_ignored(self):
        self.assertEqual(
            code_for(b'{"msg_type":"MOVE","player_id":"PLAYER_1","payload":'
                     b'{"action":"HOLD"},"timestamp":1,"v":2}'), "OK")

    def test_port_range(self):
        self.assertEqual(valid_port("8080"), 8080)
        for bad in ("80", "1023", "65536", "abc", "-1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                valid_port(bad)


if __name__ == "__main__":
    unittest.main()
