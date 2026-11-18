"""Game rules and server FSM for CLI Pig. No socket code lives here.

Implements fsm_specification.md: the eight server states, EVALUATE_MOVE
(Section 3) and the last-licks CHECK_WIN_DRAW table (Section 4).

Every public method returns a list of Outbound messages for the caller to
deliver; nothing in this module sends anything. PigGame is not thread-safe
by itself: server.py calls it only while holding its room lock.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from enum import Enum

from protocol import make_message

log = logging.getLogger("pig.game")

P1, P2 = "PLAYER_1", "PLAYER_2"
ALL = "ALL"                 # Outbound recipient meaning "both players"
TARGET_SCORE = 100


class ServerState(Enum):
    """The eight server states from fsm_specification.md Section 2."""

    INIT = "INIT"
    WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
    GAME_START = "GAME_START"
    PLAYER_TURN = "PLAYER_TURN"
    EVALUATE_MOVE = "EVALUATE_MOVE"
    CHECK_WIN_DRAW = "CHECK_WIN_DRAW"
    GAME_OVER = "GAME_OVER"
    CLEANUP = "CLEANUP"


@dataclass(frozen=True)
class Outbound:
    """One message to deliver: recipient is PLAYER_1, PLAYER_2 or ALL."""

    recipient: str
    message: dict


def other(role: str) -> str:
    """Return the opposing role."""
    return P2 if role == P1 else P1


class PigGame:
    """One game room: scores, turn state and the FSM that guards them."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()   # injectable so tests can script
        self.state = ServerState.INIT
        self.aliases: dict[str, str] = {}   # role -> alias, in join order
        self._reset_scores()

    # --- State helpers -----------------------------------------------------
    def _reset_scores(self) -> None:
        self.scores = {P1: 0, P2: 0}        # banked points only
        self.turn_total = 0                 # active player's unbanked points
        self.active_player = P1
        self.final_turn = False             # P1 reached 100; P2 on last licks

    def _go(self, new_state: ServerState) -> None:
        """Apply one FSM transition and log it."""
        log.info("FSM %s -> %s", self.state.value, new_state.value)
        self.state = new_state

    def _error(self, role: str, code: str, detail: str) -> list[Outbound]:
        payload = {"code": code, "detail": detail}
        return [Outbound(role, make_message("ERROR", role, payload))]

    def _state_update(self, player: str, result: str, die: int | None,
                      banked: int) -> Outbound:
        """Full snapshot of the room, identical for both players."""
        payload = {
            "active_player": self.active_player,
            "scores": dict(self.scores),    # copy: a snapshot, not a view
            "turn_total": self.turn_total,
            "final_turn": self.final_turn,
            "last_event": {"player": player, "result": result,
                           "die": die, "points_banked": banked},
        }
        return Outbound(ALL, make_message("STATE_UPDATE", None, payload))

    # --- Lifecycle ---------------------------------------------------------
    def listening(self) -> None:
        """INIT -> WAITING_FOR_PLAYERS once the socket is bound."""
        self._go(ServerState.WAITING_FOR_PLAYERS)

    def reset(self) -> None:
        """CLEANUP -> WAITING_FOR_PLAYERS: ready for a new pair."""
        self.aliases.clear()
        self._reset_scores()
        self._go(ServerState.WAITING_FOR_PLAYERS)

    def add_player(self, alias: str) -> tuple[str, list[Outbound]]:
        """Seat a player. Returns the assigned role and messages to send.

        The caller must check the state is WAITING_FOR_PLAYERS first; a
        full lobby is the server's LOBBY_FULL case and never reaches here.
        """
        if self.state is not ServerState.WAITING_FOR_PLAYERS:
            raise RuntimeError("add_player called while a game is running")

        if P1 not in self.aliases:
            # LOBBY_EMPTY -> LOBBY_ONE
            self.aliases[P1] = alias
            payload = {"players_connected": 1,
                       "message": "Waiting for Player 2..."}
            return P1, [Outbound(P1, make_message("LOBBY_WAIT", P1, payload))]

        # LOBBY_ONE -> GAME_START -> PLAYER_TURN
        self.aliases[P2] = alias
        self._go(ServerState.GAME_START)
        self._reset_scores()
        out = []
        for role in (P1, P2):               # each client gets its own copy
            payload = {
                "your_role": role,
                "your_alias": self.aliases[role],
                "opponent_alias": self.aliases[other(role)],
                "first_player": P1,
                "target_score": TARGET_SCORE,
            }
            out.append(Outbound(role, make_message("GAME_START", role,
                                                   payload)))
        out.append(self._state_update(P1, "TURN_START", None, 0))
        self._go(ServerState.PLAYER_TURN)
        return P2, out

    # --- Moves -------------------------------------------------------------
    def handle_move(self, role: str, action: str) -> list[Outbound]:
        """Apply one MOVE. Invalid moves return an ERROR and change nothing.

        EVALUATE_MOVE and CHECK_WIN_DRAW both run inside this one call, so
        the server's lock makes them a single atomic step.
        """
        if self.state is not ServerState.PLAYER_TURN:
            return self._error(role, "GAME_NOT_STARTED",
                               "The game has not started yet.")
        if role != self.active_player:
            return self._error(role, "NOT_YOUR_TURN",
                               f"It is {self.active_player}'s turn.")

        self._go(ServerState.EVALUATE_MOVE)

        if action not in ("ROLL", "HOLD"):
            self._go(ServerState.PLAYER_TURN)
            return self._error(role, "INVALID_ACTION",
                               "Action must be ROLL or HOLD.")
        if action == "HOLD" and self.turn_total == 0:
            self._go(ServerState.PLAYER_TURN)
            return self._error(role, "INVALID_ACTION",
                               "You must roll at least once before holding.")

        if action == "ROLL":
            die = self.rng.randint(1, 6)    # the server owns the die
            if die != 1:
                # Rolled 2-6: same player keeps the turn.
                self.turn_total += die
                self._go(ServerState.PLAYER_TURN)
                return [self._state_update(role, "ROLLED", die, 0)]
            # Bust: the unbanked points are lost and the turn ends.
            self.turn_total = 0
            return self._check_win_draw(role, "BUSTED", 1, 0)

        # HOLD: bank the turn total and end the turn.
        banked = self.turn_total
        self.scores[role] += banked
        self.turn_total = 0
        return self._check_win_draw(role, "HELD", None, banked)

    def _check_win_draw(self, player: str, result: str, die: int | None,
                        banked: int) -> list[Outbound]:
        """End-of-turn rules. Row numbers refer to FSM spec Section 4."""
        self._go(ServerState.CHECK_WIN_DRAW)
        p1_score, p2_score = self.scores[P1], self.scores[P2]

        if player == P1:
            # Row 1: P1 reached the target, so P2 gets one final turn.
            # Row 2: otherwise play simply passes to P2.
            if p1_score >= TARGET_SCORE:
                self.final_turn = True
            return self._next_turn(player, result, die, banked)

        if not self.final_turn:
            if p2_score >= TARGET_SCORE:
                # Row 3: both players have had equal turns; P2 wins.
                return self._game_over("WIN", P2,
                                       f"{P2} reached {TARGET_SCORE}")
            # Row 4: no result yet; back to P1.
            return self._next_turn(player, result, die, banked)

        if p2_score > p1_score:
            # Row 5: P2 passed P1 on the final turn.
            return self._game_over("WIN", P2,
                                   f"{P2} beat {P1} on the final turn")
        if p2_score == p1_score:
            # Row 6: exact tie on the final turn.
            return self._game_over("DRAW", None,
                                   f"{P2} tied {P1} on the final turn")
        # Row 7: P2 fell short, whether by holding early or by busting.
        how = "busted" if result == "BUSTED" else "held short"
        return self._game_over("WIN", P1, f"{P2} {how} on final turn")

    def _next_turn(self, player: str, result: str, die: int | None,
                   banked: int) -> list[Outbound]:
        """CHECK_WIN_DRAW -> PLAYER_TURN with the other player active."""
        self.active_player = other(player)
        self._go(ServerState.PLAYER_TURN)
        return [self._state_update(player, result, die, banked)]

    def _game_over(self, outcome: str, winner: str | None, reason: str,
                   recipient: str = ALL) -> list[Outbound]:
        """Build GAME_OVER and move through GAME_OVER to CLEANUP."""
        self._go(ServerState.GAME_OVER)
        payload = {"outcome": outcome, "winner": winner,
                   "final_scores": dict(self.scores), "reason": reason}
        message = make_message("GAME_OVER", None, payload)
        self._go(ServerState.CLEANUP)       # server closes sockets, resets
        return [Outbound(recipient, message)]

    # --- Departures --------------------------------------------------------
    def handle_disconnect(self, role: str, how: str) -> list[Outbound]:
        """A seated player left (blueprint Section 6.4). Idempotent."""
        if self.state is ServerState.WAITING_FOR_PLAYERS:
            # LOBBY_ONE -> LOBBY_EMPTY: nobody else to tell.
            self.aliases.pop(role, None)
            return []
        if self.state is ServerState.PLAYER_TURN and role in self.aliases:
            # Mid-game: the remaining player wins by forfeit.
            verb = "quit" if how == "quit" else "disconnected"
            return self._game_over("FORFEIT", other(role), f"{role} {verb}",
                                   recipient=other(role))
        return []                           # game already over: ignore
