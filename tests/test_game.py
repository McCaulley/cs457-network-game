"""FSM tests with a scripted die: every last-licks row plus edge cases."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game import P1, P2, PigGame, ServerState  # noqa: E402


class ScriptedDie:
    """Stands in for random.Random; returns the given rolls in order."""

    def __init__(self, *rolls):
        self.rolls = list(rolls)

    def randint(self, low, high):
        return self.rolls.pop(0)


def started(*rolls, p1=0, p2=0, final=False, active=P1):
    game = PigGame(rng=ScriptedDie(*rolls))
    game.listening()
    game.add_player("Wade")
    game.add_player("Bob")
    game.scores = {P1: p1, P2: p2}
    game.final_turn = final
    game.active_player = active
    return game


def payload(out):
    return out[-1].message["payload"]


class LobbyTests(unittest.TestCase):
    def test_join_sequence(self):
        game = PigGame()
        game.listening()
        role, out = game.add_player("Wade")
        self.assertEqual((role, out[0].message["msg_type"]),
                         (P1, "LOBBY_WAIT"))
        role, out = game.add_player("Bob")
        self.assertEqual(role, P2)
        self.assertEqual([o.message["msg_type"] for o in out],
                         ["GAME_START", "GAME_START", "STATE_UPDATE"])
        self.assertEqual(payload(out)["last_event"]["result"], "TURN_START")
        self.assertIs(game.state, ServerState.PLAYER_TURN)

    def test_disconnect_in_lobby(self):
        game = PigGame()
        game.listening()
        game.add_player("Wade")
        self.assertEqual(game.handle_disconnect(P1, "eof"), [])
        self.assertEqual(game.aliases, {})
        self.assertEqual(game.add_player("New")[0], P1)


class MoveTests(unittest.TestCase):
    def test_roll_keeps_turn(self):
        game = started(5)
        out = game.handle_move(P1, "ROLL")
        self.assertEqual(out[0].recipient, "ALL")
        self.assertEqual(payload(out)["turn_total"], 5)
        self.assertEqual(payload(out)["active_player"], P1)

    def test_bust_loses_turn_total(self):
        game = started(6, 1)
        game.handle_move(P1, "ROLL")
        out = game.handle_move(P1, "ROLL")
        self.assertEqual(payload(out)["last_event"]["result"], "BUSTED")
        self.assertEqual(payload(out)["scores"], {P1: 0, P2: 0})
        self.assertEqual(payload(out)["active_player"], P2)

    def test_out_of_turn(self):
        game = started()
        out = game.handle_move(P2, "ROLL")
        self.assertEqual((out[0].recipient, payload(out)["code"]),
                         (P2, "NOT_YOUR_TURN"))
        self.assertEqual(game.active_player, P1)

    def test_hold_with_zero(self):
        game = started()
        self.assertEqual(payload(game.handle_move(P1, "HOLD"))["code"],
                         "INVALID_ACTION")
        self.assertIs(game.state, ServerState.PLAYER_TURN)

    def test_bad_action(self):
        game = started()
        self.assertEqual(payload(game.handle_move(P1, "CHEAT"))["code"],
                         "INVALID_ACTION")

    def test_move_in_lobby(self):
        game = PigGame()
        game.listening()
        game.add_player("Wade")
        self.assertEqual(payload(game.handle_move(P1, "ROLL"))["code"],
                         "GAME_NOT_STARTED")


class LastLicksTests(unittest.TestCase):
    """One test per row of fsm_specification.md Section 4."""

    def test_row1_p1_reaches_target(self):
        game = started(4, p1=96)
        game.handle_move(P1, "ROLL")
        out = game.handle_move(P1, "HOLD")
        self.assertTrue(payload(out)["final_turn"])
        self.assertEqual(payload(out)["active_player"], P2)
        self.assertIs(game.state, ServerState.PLAYER_TURN)

    def test_row2_p1_below_target(self):
        game = started(4)
        game.handle_move(P1, "ROLL")
        out = game.handle_move(P1, "HOLD")
        self.assertFalse(payload(out)["final_turn"])
        self.assertEqual(payload(out)["scores"][P1], 4)
        self.assertEqual(payload(out)["last_event"]["points_banked"], 4)

    def test_row3_p2_reaches_target(self):
        game = started(5, p1=50, p2=95, active=P2)
        game.handle_move(P2, "ROLL")
        out = game.handle_move(P2, "HOLD")
        self.assertEqual((payload(out)["outcome"], payload(out)["winner"]),
                         ("WIN", P2))
        self.assertIs(game.state, ServerState.CLEANUP)

    def test_row4_p2_below_target(self):
        game = started(5, active=P2)
        game.handle_move(P2, "ROLL")
        out = game.handle_move(P2, "HOLD")
        self.assertEqual(payload(out)["active_player"], P1)

    def test_row5_p2_beats_p1(self):
        game = started(4, 5, p1=102, p2=95, final=True, active=P2)
        game.handle_move(P2, "ROLL")
        game.handle_move(P2, "ROLL")
        out = game.handle_move(P2, "HOLD")
        self.assertEqual((payload(out)["outcome"], payload(out)["winner"]),
                         ("WIN", P2))
        self.assertEqual(payload(out)["final_scores"], {P1: 102, P2: 104})

    def test_row6_draw(self):
        game = started(6, p1=100, p2=94, final=True, active=P2)
        game.handle_move(P2, "ROLL")
        out = game.handle_move(P2, "HOLD")
        self.assertEqual((payload(out)["outcome"], payload(out)["winner"]),
                         ("DRAW", None))

    def test_row7_p2_busts(self):
        game = started(1, p1=104, p2=97, final=True, active=P2)
        out = game.handle_move(P2, "ROLL")
        self.assertEqual((payload(out)["outcome"], payload(out)["winner"]),
                         ("WIN", P1))
        self.assertEqual(payload(out)["reason"],
                         "PLAYER_2 busted on final turn")

    def test_row7_p2_holds_short(self):
        game = started(2, p1=104, p2=97, final=True, active=P2)
        game.handle_move(P2, "ROLL")
        out = game.handle_move(P2, "HOLD")
        self.assertEqual(payload(out)["winner"], P1)


class DisconnectTests(unittest.TestCase):
    def test_mid_game_forfeit(self):
        game = started()
        out = game.handle_disconnect(P2, "eof")
        self.assertEqual(out[0].recipient, P1)
        self.assertEqual((payload(out)["outcome"], payload(out)["winner"]),
                         ("FORFEIT", P1))
        self.assertIs(game.state, ServerState.CLEANUP)

    def test_second_disconnect_ignored(self):
        game = started()
        game.handle_disconnect(P2, "eof")
        self.assertEqual(game.handle_disconnect(P1, "eof"), [])

    def test_reset(self):
        game = started()
        game.handle_disconnect(P2, "quit")
        game.reset()
        self.assertIs(game.state, ServerState.WAITING_FOR_PLAYERS)
        self.assertEqual(game.add_player("Next")[0], P1)


if __name__ == "__main__":
    unittest.main()
