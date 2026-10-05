# AI Prompting & Constraint Strategy — CLI Pig

**Project:** CS 457 Multiplayer Network Game
**Student:** Wade McCaulley
**Sprint:** 1 — Protocol & FSM Design
**Tool:** Claude (Anthropic). Same prompts work in ChatGPT or Copilot Chat.

---

## 1. Strategy

Generic "write me a socket game server" prompts produce generic code: `recv(1024)` treated as one message, made-up message names, no EOF check. To avoid that, the AI never gets to invent anything about the wire format or game flow. The approach:

1. **The spec goes in first.** Every coding session starts with `protocol_blueprint.md` and `fsm_specification.md` pasted in full, followed by the system prompt below. The AI is told those documents are binding.
2. **One module per prompt.** Small, testable pieces (framing → validation → game logic → server loop → client) instead of "build the whole thing." Each piece is reviewed before the next is requested.
3. **Exact names and signatures.** Prompts name the function, its arguments, and its return values. The AI fills in bodies, not architecture.
4. **Tests prove conformance.** Every module prompt also asks for `unittest` tests built from the wire examples in the blueprint. If the generated code doesn't pass tests derived from my spec, it gets rejected, not patched by hand.
5. **Review checklist.** Every response is checked against Section 4 before it's committed.

---

## 2. System Prompt (used at the start of every session)

```
You are helping me implement a two-player networked Pig dice game for a
computer networks course. I am pasting two design documents:
protocol_blueprint.md and fsm_specification.md. They are the binding
specification. Follow them exactly.

Hard rules:
1. Python 3 standard library only: socket, threading, json, argparse, struct,
   random, re, time, logging, unittest. No asyncio, websockets, Twisted,
   pygame, or any third-party package.
2. Framing is newline-delimited JSON exactly as in blueprint Section 2. Every
   message is json.dumps(msg, separators=(",", ":")) + "\n", UTF-8 encoded,
   sent with sendall(). Never assume one recv() returns one message. Keep a
   per-connection bytearray buffer and split on b"\n".
3. Use only the 8 msg_type values in blueprint Section 4: CONNECT,
   LOBBY_WAIT, GAME_START, MOVE, STATE_UPDATE, ERROR, DISCONNECT, GAME_OVER.
   Use only the field names, types, and ERROR codes defined there. Do not
   add, rename, or drop fields.
4. Every envelope has exactly msg_type, player_id, payload, timestamp.
5. The server rolls the die. A client MOVE contains only "action": "ROLL" or
   "HOLD". Never put a die value in a client message.
6. Server states are exactly the names in fsm_specification.md: INIT,
   WAITING_FOR_PLAYERS, GAME_START, PLAYER_TURN, EVALUATE_MOVE,
   CHECK_WIN_DRAW, GAME_OVER, CLEANUP. Use an Enum with these names.
   Transitions must match the diagram and tables. Win/draw logic must match
   the last-licks table in Section 4 row by row.
7. recv() returning b"" means EOF. Handle it and break the loop. Wrap every
   recv() and sendall() in try/except for ConnectionResetError,
   BrokenPipeError, ConnectionAbortedError, TimeoutError, and OSError.
8. Bad input from a client must produce an ERROR message, never an uncaught
   exception. The server process must never crash because of a client.
9. No busy loops. Blocking recv() in per-client threads, or sleep() where a
   wait is needed.
10. PEP 8, type hints, docstrings, and comments on network/state logic.

If anything I ask for conflicts with the spec, or the spec is ambiguous,
stop and ask me. Do not guess and do not "improve" the protocol.
```

---

## 3. Module Prompts (in build order)

Each one is sent after the system prompt and both spec documents.

### 3.1 `protocol.py` — framing

```
Write protocol.py implementing blueprint Section 2 and the constants in
Section 7.

Required:
- MAX_MSG_BYTES = 4096, DELIMITER = b"\n"
- encode_message(msg: dict) -> bytes
    json.dumps with separators=(",", ":"), append "\n", encode UTF-8.
    Raise ValueError if the result exceeds MAX_MSG_BYTES.
- class LineBuffer:
    feed(self, chunk: bytes) -> list[bytes]
        Append chunk, return every complete line (without the "\n"),
        keep any trailing partial line. Skip empty lines.
    Raise FrameTooLargeError if the leftover partial line exceeds
    MAX_MSG_BYTES.
- send_message(sock, msg: dict) -> None  (encode_message + sendall)
- make_message(msg_type: str, player_id: str | None, payload: dict) -> dict
    fills in timestamp = int(time.time())

Also write tests/test_protocol.py with unittest covering:
- one full message in one chunk
- two messages coalesced in one chunk (use blueprint Section 2.6 example)
- one message fragmented across three chunks, including a split in the
  middle of a multi-byte UTF-8 character
- coalesced + fragmented: 2.5 messages, then the remaining 0.5
- a string value containing a newline round-trips (json escapes it)
- 4097 bytes with no delimiter raises FrameTooLargeError
```

### 3.2 `validation.py` — envelope and payload checks

```
Write validation.py implementing blueprint Sections 2.5 (process_line steps
2-6), 3, and 4.

- class ProtocolError(Exception) with attributes code and detail, where code
  is one of the ERROR codes in blueprint Section 4.6.
- parse_line(line: bytes) -> dict
    UTF-8 decode, json.loads, check it's a dict, check the four envelope
    keys and types, check msg_type is known. Raise ProtocolError with the
    exact code from the blueprint on any failure.
- validate_payload(msg: dict, sender_is_client: bool) -> None
    Check payload fields for the message type exactly as tabled in
    Section 4. If sender_is_client and msg_type is a server-only type,
    raise UNKNOWN_MSG_TYPE. Alias must match ^[A-Za-z0-9_]{1,16}$ else
    BAD_ALIAS. MOVE action must be "ROLL" or "HOLD" else INVALID_ACTION.

tests/test_validation.py: one passing case per message type using the JSON
examples in Section 4, plus one failing case per ERROR code that
validation is responsible for.
```

### 3.3 `game.py` — game state and FSM (no sockets)

```
Write game.py: the server FSM from fsm_specification.md with NO socket code.
It must be testable without a network.

- class ServerState(Enum) with exactly the 8 states in the spec.
- class PigGame:
    __init__(self, rng: random.Random | None = None)  # injectable for tests
    add_player(alias) -> list[Outbound]
    handle_move(role, action) -> list[Outbound]
    handle_disconnect(role, how) -> list[Outbound]   # idempotent
    reset() -> None                                   # CLEANUP
  Outbound is a dataclass (recipient: "PLAYER_1" | "PLAYER_2" | "ALL",
  message: dict). Methods return the messages to send; they never send.
- handle_move implements fsm_specification.md Section 3 exactly, and
  CHECK_WIN_DRAW implements the Section 4 table exactly. Comment each
  branch with its row number from that table.
- Invalid moves raise nothing; they return an Outbound ERROR to the sender
  and leave state unchanged.

tests/test_game.py with a seeded or stubbed RNG:
- each of the 7 rows in the last-licks table
- out-of-turn MOVE -> NOT_YOUR_TURN, state unchanged
- HOLD with turn_total 0 -> INVALID_ACTION
- disconnect in LOBBY_ONE -> LOBBY_EMPTY, no messages
- disconnect mid-game -> GAME_OVER FORFEIT to the other player
- second disconnect after GAME_OVER -> no messages
```

### 3.4 `server.py` — networking around `PigGame`

```
Write server.py using protocol.py, validation.py, and game.py.

- argparse: -p/--port required int; -h help. Bind to 0.0.0.0.
- INIT: SO_REUSEADDR, bind, listen. On OSError print a clear message and
  exit(1).
- Accept loop in the main thread; one daemon threading.Thread per client.
- Enable TCP keepalive on each accepted socket exactly as blueprint
  Section 6.3, guarded with hasattr for the TCP_KEEP* constants.
- One threading.Lock around every call into PigGame. Collect the Outbound
  list inside the lock, send it after releasing the lock.
- Client thread loop = blueprint Section 2.5 pseudocode plus Section 6.2
  and 6.3 exception handling. Every path out of the loop calls
  game.handle_disconnect exactly once (it's idempotent anyway).
- A third client gets ERROR LOBBY_FULL and is closed.
- Broadcast: wrap each sendall() separately; a failure on one recipient
  is reported to handle_disconnect after the loop finishes.
- After GAME_OVER: close both sockets, game.reset(), keep accepting.
- logging at INFO for state transitions, WARNING for disconnects.
```

### 3.5 `client.py`

```
Write client.py using protocol.py and validation.py. Follow the client FSM
in fsm_specification.md Section 6.

- argparse: -i/--host required (DNS name, e.g. server.mccaulley.edu),
  -p/--port required int, -h help.
- socket.create_connection((host, port)). Catch socket.gaierror ->
  "Could not resolve <host>", exit(2). Catch ConnectionRefusedError and
  TimeoutError -> clear message, exit(2).
- Prompt for alias, send CONNECT.
- One background thread reads from the socket with LineBuffer and puts
  parsed messages on a queue.Queue. The main thread reads stdin and
  renders. Use queue.get(timeout=...) - no spin loop.
- Render STATE_UPDATE like the CLI mockup in the SOW: scores, turn total,
  what happened. Prompt "[R]oll or [H]old?" only when active_player is me.
  Map r/h to MOVE ROLL/HOLD; "quit" sends DISCONNECT.
- Ctrl+C sends DISCONNECT then closes.
- Server EOF or socket exception -> "Connection to server lost.", exit(1).
- The client never computes scores or validates turns; it shows what the
  server sends.
```

---

## 4. Review Checklist (applied to every AI response before commit)

- [ ] Only standard-library imports.
- [ ] Every `msg_type`, field name, and error code appears in `protocol_blueprint.md`. (Checked with `grep` against the blueprint.)
- [ ] No `recv()` result is passed straight to `json.loads` — all go through `LineBuffer`.
- [ ] Every `recv()` loop has an `if not chunk:` EOF branch that exits the loop.
- [ ] Every `recv()` / `sendall()` is inside a `try` catching the listed exceptions.
- [ ] `sendall()` used, never bare `send()`.
- [ ] No die value is accepted from a client.
- [ ] State names match the FSM enum exactly; each `CHECK_WIN_DRAW` branch cites its table row.
- [ ] No `while True:` without a blocking call or timeout inside.
- [ ] Unit tests for the module pass.

If an item fails, the response is rejected and the prompt is re-sent with the failing rule quoted back to the AI, rather than hand-patching around generated code I don't understand.

---

## 5. Log of AI Sessions

Updated as implementation proceeds (Sprint 4).

| Date | Module | Tool | Result / changes needed |
|---|---|---|---|
| 2026-10-03 | Design docs (this sprint) | Claude | Drafted blueprint, FSM, and this strategy from my SOW decisions; reviewed and edited before commit. |
