# CLI Pig — CS 457 network game

Two-player Pig over TCP. The server owns the die, the scores and the turn;
clients send `ROLL` / `HOLD` and draw what the server broadcasts. Rules use
"last licks": if Player 1 banks 100, Player 2 gets one final turn.

## Run

```
python3 server.py -p 8080
python3 client.py -i server.mccaulley.edu -p 8080     # terminal 2
python3 client.py -i server.mccaulley.edu -p 8080     # terminal 3
```

For local testing use `-i localhost`. `-h` on either program shows help.
Ports must be 1024–65535. `-a NAME` on the client skips the name prompt.
In a game type `r` to roll, `h` to hold, `quit` to leave.

Python 3.9+ and the standard library only.

## Files

| File | Purpose |
|---|---|
| `protocol.py` | Newline-delimited JSON framing (`LineBuffer`, `encode_message`) |
| `validation.py` | Envelope, payload and port checks |
| `game.py` | Game rules and server FSM, no sockets |
| `server.py` | Listening socket, reader/writer threads, room lock |
| `client.py` | DNS lookup, TCP connect, CLI board |
| `tests/` | `python3 -m unittest discover -s tests` |
| `docs/` | Design docs and the Sprint 3 execution log |

## Client exit codes

| Code | Meaning |
|---|---|
| 0 | Game finished or user quit |
| 1 | DNS lookup failed, connection lost, or lobby full |
| 2 | Bad arguments, or server refused / unreachable |
