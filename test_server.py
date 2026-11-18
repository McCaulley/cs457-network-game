"""Integration tests: a real PigServer and raw TCP sockets on localhost."""

import json
import logging
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protocol import LineBuffer, encode_message, make_message  # noqa: E402
from server import PigServer  # noqa: E402

logging.disable(logging.CRITICAL)       # keep test output quiet


class ScriptedDie:
    def __init__(self, rolls):
        self.rolls = list(rolls)

    def randint(self, low, high):
        return self.rolls.pop(0) if self.rolls else 3


class Peer:
    """A bare-bones test client that speaks the wire protocol directly."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.buffer = LineBuffer()
        self.pending = []

    def send(self, msg_type, player_id, payload):
        self.sock.sendall(encode_message(
            make_message(msg_type, player_id, payload)))

    def recv(self):
        """Return the next message, or None once the server closes."""
        while not self.pending:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.pending += [json.loads(x) for x in self.buffer.feed(chunk)]
        return self.pending.pop(0)

    def expect(self, msg_type):
        msg = self.recv()
        assert msg is not None, f"connection closed, wanted {msg_type}"
        assert msg["msg_type"] == msg_type, msg
        return msg["payload"]

    def close(self):
        self.sock.close()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = PigServer(0)      # port 0: the OS picks a free port
        self.server.start()
        self.port = self.server.listen_sock.getsockname()[1]
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.peers = []

    def tearDown(self):
        for peer in self.peers:
            peer.close()
        self.server.close()

    def peer(self):
        self.peers.append(Peer(self.port))
        return self.peers[-1]

    def start_game(self, rolls=()):
        self.server.game.rng = ScriptedDie(rolls)
        one, two = self.peer(), self.peer()
        one.send("CONNECT", None, {"alias": "Wade"})
        one.expect("LOBBY_WAIT")
        two.send("CONNECT", None, {"alias": "Bob"})
        for peer, role in ((one, "PLAYER_1"), (two, "PLAYER_2")):
            self.assertEqual(peer.expect("GAME_START")["your_role"], role)
            peer.expect("STATE_UPDATE")
        return one, two

    def test_three_turns_broadcast_to_both(self):
        one, two = self.start_game([4, 6, 1, 5])
        script = [(one, "PLAYER_1", "ROLL"), (one, "PLAYER_1", "HOLD"),
                  (two, "PLAYER_2", "ROLL"), (two, "PLAYER_2", "ROLL"),
                  (one, "PLAYER_1", "ROLL"), (one, "PLAYER_1", "HOLD")]
        for sender, role, action in script:
            sender.send("MOVE", role, {"action": action})
            first = one.expect("STATE_UPDATE")
            self.assertEqual(first, two.expect("STATE_UPDATE"))
        self.assertEqual(first["scores"], {"PLAYER_1": 9, "PLAYER_2": 0})
        self.assertEqual(first["active_player"], "PLAYER_2")

    def test_fragmented_and_coalesced_frames(self):
        one, two = self.start_game([2, 3])
        roll = encode_message(make_message("MOVE", "PLAYER_1",
                                           {"action": "ROLL"}))
        for byte in roll:               # one message, one byte per segment
            one.sock.sendall(bytes([byte]))
            time.sleep(0.001)
        self.assertEqual(one.expect("STATE_UPDATE")["turn_total"], 2)
        hold = encode_message(make_message("MOVE", "PLAYER_1",
                                           {"action": "HOLD"}))
        one.sock.sendall(roll + hold)   # two messages in one segment
        self.assertEqual(one.expect("STATE_UPDATE")["turn_total"], 5)
        self.assertEqual(one.expect("STATE_UPDATE")["scores"]["PLAYER_1"], 5)

    def test_turn_enforcement_errors(self):
        one, two = self.start_game()
        two.send("MOVE", "PLAYER_2", {"action": "ROLL"})
        self.assertEqual(two.expect("ERROR")["code"], "NOT_YOUR_TURN")
        one.send("MOVE", "PLAYER_2", {"action": "ROLL"})
        self.assertEqual(one.expect("ERROR")["code"], "PLAYER_ID_MISMATCH")
        one.send("MOVE", "PLAYER_1", {"action": "HOLD"})
        self.assertEqual(one.expect("ERROR")["code"], "INVALID_ACTION")
        one.send("CONNECT", None, {"alias": "Again"})
        self.assertEqual(one.expect("ERROR")["code"], "ALREADY_CONNECTED")
        one.sock.sendall(b"garbage\n")
        self.assertEqual(one.expect("ERROR")["code"], "MALFORMED_MESSAGE")

    def test_lobby_errors(self):
        one = self.peer()
        one.send("MOVE", "PLAYER_1", {"action": "ROLL"})
        self.assertEqual(one.expect("ERROR")["code"], "GAME_NOT_STARTED")
        one.send("CONNECT", None, {"alias": "bad alias"})
        self.assertEqual(one.expect("ERROR")["code"], "BAD_ALIAS")
        one.send("CONNECT", None, {"alias": "Wade"})
        one.expect("LOBBY_WAIT")

    def test_third_client_rejected_game_unaffected(self):
        one, two = self.start_game([5])
        three = self.peer()
        three.send("CONNECT", None, {"alias": "Eve"})
        self.assertEqual(three.expect("ERROR")["code"], "LOBBY_FULL")
        self.assertIsNone(three.recv())             # server closed it
        one.send("MOVE", "PLAYER_1", {"action": "ROLL"})
        self.assertEqual(two.expect("STATE_UPDATE")["turn_total"], 5)

    def test_oversized_frame_closes_sender(self):
        one = self.peer()
        one.sock.sendall(b"x" * 5000)
        self.assertEqual(one.expect("ERROR")["code"], "MESSAGE_TOO_LARGE")
        self.assertIsNone(one.recv())

    def test_forfeit_then_new_game(self):
        one, two = self.start_game()
        two.close()                                 # EOF, no DISCONNECT
        over = one.expect("GAME_OVER")
        self.assertEqual((over["outcome"], over["winner"]),
                         ("FORFEIT", "PLAYER_1"))
        self.assertIsNone(one.recv())               # room cleaned up
        self.start_game()                           # server still usable

    def test_quit_message_names_reason(self):
        one, two = self.start_game()
        one.send("DISCONNECT", "PLAYER_1", {"reason": "user quit"})
        self.assertEqual(two.expect("GAME_OVER")["reason"],
                         "PLAYER_1 quit")

    def test_full_game_to_last_licks_win(self):
        one, two = self.start_game([6] * 17 + [6] * 17)
        for peer, role in ((one, "PLAYER_1"), (two, "PLAYER_2")):
            for _ in range(17):                     # 17 x 6 = 102
                peer.send("MOVE", role, {"action": "ROLL"})
                one.expect("STATE_UPDATE")
                two.expect("STATE_UPDATE")
            peer.send("MOVE", role, {"action": "HOLD"})
            if role == "PLAYER_1":
                self.assertTrue(one.expect("STATE_UPDATE")["final_turn"])
                two.expect("STATE_UPDATE")
        over = one.expect("GAME_OVER")
        self.assertEqual(over, two.expect("GAME_OVER"))
        self.assertEqual((over["outcome"], over["winner"]), ("DRAW", None))

    def test_simultaneous_moves_stay_consistent(self):
        one, two = self.start_game([3] * 400)
        barrier = threading.Barrier(2)

        def spam(peer, role):
            barrier.wait()
            for _ in range(100):
                peer.send("MOVE", role, {"action": "ROLL"})

        threads = [threading.Thread(target=spam, args=pair)
                   for pair in ((one, "PLAYER_1"), (two, "PLAYER_2"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # P1 is active throughout: 100 rolls applied, 100 rejected.
        updates = [one.expect("STATE_UPDATE") for _ in range(100)]
        self.assertEqual([u["turn_total"] for u in updates],
                         [3 * n for n in range(1, 101)])
        seen = []
        while len(seen) < 200:
            msg = two.recv()
            seen.append((msg["msg_type"], msg["payload"].get("turn_total")))
        self.assertEqual([t for kind, t in seen if kind == "STATE_UPDATE"],
                         [3 * n for n in range(1, 101)])
        self.assertEqual(sum(kind == "ERROR" for kind, _ in seen), 100)


if __name__ == "__main__":
    unittest.main()
