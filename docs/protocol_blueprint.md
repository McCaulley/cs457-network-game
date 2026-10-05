# Application Protocol Blueprint — CLI Pig

**Project:** CS 457 Multiplayer Network Game
**Student:** Wade McCaulley
**Server:** `server.mccaulley.edu` (192.168.20.100), TCP port set by `-p`
**Sprint:** 1 — Protocol & FSM Design

This document is the single source of truth for every byte that crosses the wire between `client.py` and `server.py`. Code must match this document; if they disagree, the code is wrong.

---

## 1. Design Summary

| Item | Choice |
|---|---|
| Transport | TCP (`socket.SOCK_STREAM`) |
| Serialization | JSON, UTF-8 encoded |
| Framing | Newline-delimited: every message ends with exactly one `\n` (0x0A) |
| Max message size | 4096 bytes including the `\n` |
| Authority | Server owns the die, the scores, and the turn. Clients only send intent (`ROLL` / `HOLD`). |

**Why newline-delimited JSON:** it's simple to implement, the delimiter can't collide with the payload (Section 2.1), and each message shows up as one readable line in a Wireshark capture.

---

## 2. Framing Rule — Option A: Newline-Delimited JSON (`\n` Framing)

### 2.1 Framing Rule

Every JSON object is UTF-8 encoded and terminated by a single newline character `\n` (0x0A). The receiver adds incoming bytes to a stream buffer until it finds a `\n`, extracts the complete line, and deserializes the JSON object.

**Why a framing rule is needed:** TCP is a continuous byte stream with no built-in message boundaries. Messages sent back to back may arrive in a single `recv()` chunk (**coalescing**), or one message may be split across several chunks (**fragmentation**). The `\n` terminator gives the receiver a deterministic boundary, so it never assumes one `recv()` equals one message.

**Why the delimiter can't collide with the payload:** `json.dumps()` (with no `indent` argument) never emits a raw newline. A newline inside a string value is escaped to the two characters `\` `n`. So the only 0x0A byte on the wire is the terminator.

### 2.2 Wire Stream Example (Continuous Stream)

Raw bytes as they appear on the wire; each `\n` is the single byte 0x0A.

```
{"msg_type":"CONNECT","player_id":null,"payload":{"alias":"Wade"},"timestamp":1759546800}\n{"msg_type":"MOVE","player_id":"PLAYER_1","payload":{"action":"ROLL"},"timestamp":1759546812}\n
```

Hex view of the boundary between two messages (end of one `MOVE`, start of the next):
```
... 22 7d 2c 22 74 69 6d 65 73 74 61 6d 70 22 3a 31 37 35 39 35 34 36 38 31 32 7d 0a 7b 22 6d 73 67 ...
     " }  ,  "  t  i  m  e  s  t  a  m  p  "  :  1  7  5  9  5  4  6  8  1  2  } \n  {  "  m  s  g
```

### 2.3 JSON Schema / Structure Specification (MOVE)

Shown indented for readability. On the wire it is sent as one line, followed by `\n`.

```json
{
  "msg_type": "MOVE",
  "player_id": "PLAYER_1",
  "payload": {
    "action": "ROLL"
  },
  "timestamp": 1759546812
}
```

| Field | Type | Rules |
|---|---|---|
| `msg_type` | string | `"MOVE"` |
| `player_id` | string | `"PLAYER_1"` or `"PLAYER_2"` |
| `payload.action` | string | `"ROLL"` or `"HOLD"` |
| `timestamp` | integer | Unix epoch seconds |

The common envelope is defined in Section 3, and the schema for every message type is in Section 4.

### 2.4 Sender Logic

1. Build a Python dict matching one of the schemas in Section 4.
2. `line = json.dumps(msg, separators=(",", ":")) + "\n"`
3. `data = line.encode("utf-8")`
4. If `len(data) > 4096`, it's a bug — raise, don't send.
5. `sock.sendall(data)` — never `send()`, which can write only part of the buffer.

### 2.5 Receiver Extraction Logic

Each connection gets its own receive buffer (`bytearray`):

```
loop:
    chunk = sock.recv(4096)
    if chunk == b"":                    # EOF — peer closed (see Section 6)
        handle disconnect; stop
    buffer += chunk
    while b"\n" in buffer:              # may run 0, 1, or many times
        line, _, buffer = buffer.partition(b"\n")
        process_line(line)              # decode UTF-8, json.loads, validate
    if len(buffer) > 4096:              # no newline after 4 KB = garbage or attack
        send ERROR MESSAGE_TOO_LARGE; close connection
```

Bytes after the last `\n` stay in the buffer and are completed by the next `recv()`.

`process_line` steps, in order. Any failure sends `ERROR` and the connection stays open (except where noted):

1. Empty line (`b""`) → ignored silently (tolerates stray `\r\n` / blank lines).
2. UTF-8 decode fails → `ERROR MALFORMED_MESSAGE`.
3. `json.loads` fails, or result isn't a JSON object → `ERROR MALFORMED_MESSAGE`.
4. Missing/wrong-type envelope field (Section 3) → `ERROR MALFORMED_MESSAGE`.
5. Unknown `msg_type` → `ERROR UNKNOWN_MSG_TYPE`.
6. Payload fails its schema (Section 4) → `ERROR` with the code listed for that message.
7. Otherwise → hand to the game state machine.

### 2.6 Coalescing & Fragmentation Example

**`recv()` #1 — coalescing:** two full messages and the start of a third arrive together.
```
{"msg_type":"STATE_UPDATE",...,"timestamp":1759546813}\n{"msg_type":"STATE_UPDATE",...,"timestamp":1759546815}\n{"msg_type":"GAME_O
```
The receiver extracts both complete `STATE_UPDATE` messages. `{"msg_type":"GAME_O` has no `\n` yet, so it stays in the buffer.

**`recv()` #2 — fragmentation:** the rest of the third message arrives.
```
VER","player_id":null,"payload":{"outcome":"WIN","winner":"PLAYER_1",...},"timestamp":1759546815}\n
```
The buffer now holds a full line ending in `\n`, so the `GAME_OVER` message is extracted and the buffer is empty again.

---

## 3. Message Envelope (common to every message)

Every message is one JSON object with exactly these four top-level keys:

| Key | Type | Rules |
|---|---|---|
| `msg_type` | string | One of the 8 types in Section 4. Uppercase. |
| `player_id` | string or `null` | `"PLAYER_1"` or `"PLAYER_2"`. `null` in `CONNECT` (no role yet) and in server broadcasts that aren't addressed to one player. |
| `payload` | object | Type-specific fields, Section 4. Use `{}` if there are none. Never `null`. |
| `timestamp` | integer | Unix epoch seconds at send time. Informational only — logging and Wireshark correlation. The server never makes game decisions on it. |

Extra top-level keys are ignored (lets the protocol grow without breaking older clients). Missing keys or wrong types are rejected.

**Server-side identity rule:** the server knows which role belongs to which socket from the `CONNECT` handshake. A client's `player_id` field must match its socket's assigned role; a mismatch returns `ERROR PLAYER_ID_MISMATCH`. A client can't impersonate the other player by changing a string.

---

## 4. Message Types

| # | `msg_type` | Direction | Purpose |
|---|---|---|---|
| 1 | `CONNECT` | Client → Server | Join the game room with an alias |
| 2 | `LOBBY_WAIT` | Server → Client | Tell Player 1 the server is waiting for Player 2 |
| 3 | `GAME_START` | Server → each client | Game begins; tells each client its role |
| 4 | `MOVE` | Client → Server | Active player chooses `ROLL` or `HOLD` |
| 5 | `STATE_UPDATE` | Server → both clients | Scores, turn total, whose turn, what just happened |
| 6 | `ERROR` | Server → one client | Rejected message; game state unchanged |
| 7 | `DISCONNECT` | Client → Server | Player is quitting on purpose |
| 8 | `GAME_OVER` | Server → both clients | Final outcome: win, draw, or forfeit |

### 4.1 `CONNECT` (Client → Server)

Sent once, immediately after the TCP connection opens.

| Payload field | Type | Rules |
|---|---|---|
| `alias` | string | 1–16 chars, letters, digits, `_` only |

```json
{"msg_type":"CONNECT","player_id":null,"payload":{"alias":"Wade"},"timestamp":1759546800}
```

Server responses:
- First player in → assigned `PLAYER_1`, gets `LOBBY_WAIT`.
- Second player in → assigned `PLAYER_2`, both get `GAME_START`.
- Room already has two players → `ERROR LOBBY_FULL`, then server closes the socket.
- Bad alias → `ERROR BAD_ALIAS`; client may resend `CONNECT`.
- Duplicate `CONNECT` from an already-joined socket → `ERROR ALREADY_CONNECTED`.

### 4.2 `LOBBY_WAIT` (Server → Client)

| Payload field | Type | Rules |
|---|---|---|
| `players_connected` | integer | Always `1` when sent |
| `message` | string | Human-readable status for the CLI |

```json
{"msg_type":"LOBBY_WAIT","player_id":"PLAYER_1","payload":{"players_connected":1,"message":"Waiting for Player 2..."},"timestamp":1759546800}
```

### 4.3 `GAME_START` (Server → each client, sent individually)

Each client gets its own copy so `your_role` is correct for the recipient.

| Payload field | Type | Rules |
|---|---|---|
| `your_role` | string | `"PLAYER_1"` or `"PLAYER_2"` |
| `your_alias` | string | Echo of the recipient's alias |
| `opponent_alias` | string | The other player's alias |
| `first_player` | string | Always `"PLAYER_1"` |
| `target_score` | integer | `100` |

```json
{"msg_type":"GAME_START","player_id":"PLAYER_2","payload":{"your_role":"PLAYER_2","your_alias":"Bob","opponent_alias":"Wade","first_player":"PLAYER_1","target_score":100},"timestamp":1759546830}
```

A `STATE_UPDATE` with `last_event.result = "TURN_START"` follows immediately so both screens draw the opening board.

### 4.4 `MOVE` (Client → Server)

| Payload field | Type | Rules |
|---|---|---|
| `action` | string | `"ROLL"` or `"HOLD"` — nothing else |

```json
{"msg_type":"MOVE","player_id":"PLAYER_1","payload":{"action":"ROLL"},"timestamp":1759546812}
```

There is no die value in a `MOVE`. The client asks; the server rolls.

Rejections (state unchanged, client stays connected):

| Condition | Error code |
|---|---|
| Game hasn't started (still in lobby) | `GAME_NOT_STARTED` |
| Sender isn't `active_player` | `NOT_YOUR_TURN` |
| `action` missing or not `ROLL`/`HOLD` | `INVALID_ACTION` |
| `HOLD` with turn total of 0 | `INVALID_ACTION` (must roll at least once) |

### 4.5 `STATE_UPDATE` (Server → both clients)

Sent after every state change: turn start, every roll, bust, hold. Both clients get the identical message (`player_id` is `null`). Pig has no hidden information, so one broadcast keeps both screens in sync.

| Payload field | Type | Rules |
|---|---|---|
| `active_player` | string | Whose turn it is **now** (after this event) |
| `scores` | object | `{"PLAYER_1": int, "PLAYER_2": int}` — banked scores |
| `turn_total` | integer | Unbanked points for `active_player` right now |
| `final_turn` | boolean | `true` when Player 1 has reached 100 and Player 2 is on last licks |
| `last_event` | object | What just happened — see below |

`last_event`:

| Field | Type | Rules |
|---|---|---|
| `player` | string | Who acted |
| `result` | string | `"TURN_START"`, `"ROLLED"`, `"BUSTED"`, or `"HELD"` |
| `die` | integer or `null` | 1–6 for `ROLLED` / `BUSTED`; `null` otherwise |
| `points_banked` | integer | Points added on `HELD`; `0` otherwise |

Example — Player 1 rolls a 5:
```json
{"msg_type":"STATE_UPDATE","player_id":null,"payload":{"active_player":"PLAYER_1","scores":{"PLAYER_1":34,"PLAYER_2":41},"turn_total":5,"final_turn":false,"last_event":{"player":"PLAYER_1","result":"ROLLED","die":5,"points_banked":0}},"timestamp":1759546812}
```

Example — Player 1 then busts; turn passes:
```json
{"msg_type":"STATE_UPDATE","player_id":null,"payload":{"active_player":"PLAYER_2","scores":{"PLAYER_1":34,"PLAYER_2":41},"turn_total":0,"final_turn":false,"last_event":{"player":"PLAYER_1","result":"BUSTED","die":1,"points_banked":0}},"timestamp":1759546814}
```

### 4.6 `ERROR` (Server → one client)

| Payload field | Type | Rules |
|---|---|---|
| `code` | string | One of the codes below |
| `detail` | string | Human-readable explanation for the CLI |

```json
{"msg_type":"ERROR","player_id":"PLAYER_2","payload":{"code":"NOT_YOUR_TURN","detail":"It is PLAYER_1's turn."},"timestamp":1759546813}
```

| Code | Meaning | Connection after |
|---|---|---|
| `MALFORMED_MESSAGE` | Not UTF-8, not JSON, or bad envelope | Stays open |
| `UNKNOWN_MSG_TYPE` | `msg_type` not in this spec, or a server-only type sent by a client | Stays open |
| `BAD_ALIAS` | Alias fails Section 4.1 rules | Stays open |
| `ALREADY_CONNECTED` | Second `CONNECT` on the same socket | Stays open |
| `PLAYER_ID_MISMATCH` | `player_id` doesn't match the socket's role | Stays open |
| `GAME_NOT_STARTED` | `MOVE` sent while in the lobby | Stays open |
| `NOT_YOUR_TURN` | `MOVE` from the inactive player | Stays open |
| `INVALID_ACTION` | Bad `action` value, or `HOLD` with 0 turn total | Stays open |
| `LOBBY_FULL` | Third client tried to join | **Server closes** |
| `MESSAGE_TOO_LARGE` | 4096 bytes received with no `\n` | **Server closes** |

`player_id` on an `ERROR` is the recipient's role, or `null` if the client hasn't joined yet.

### 4.7 `DISCONNECT` (Client → Server)

Sent when the user types `quit` (or presses Ctrl+C — the client catches `KeyboardInterrupt` and sends this before closing).

| Payload field | Type | Rules |
|---|---|---|
| `reason` | string | Optional free text, e.g. `"user quit"`. Default `""`. |

```json
{"msg_type":"DISCONNECT","player_id":"PLAYER_2","payload":{"reason":"user quit"},"timestamp":1759546900}
```

The server doesn't reply to the sender. It handles the departure per Section 6.

### 4.8 `GAME_OVER` (Server → both clients)

| Payload field | Type | Rules |
|---|---|---|
| `outcome` | string | `"WIN"`, `"DRAW"`, or `"FORFEIT"` |
| `winner` | string or `null` | Winning role; `null` on `DRAW` |
| `final_scores` | object | `{"PLAYER_1": int, "PLAYER_2": int}` |
| `reason` | string | Human-readable, e.g. `"PLAYER_2 disconnected"` |

```json
{"msg_type":"GAME_OVER","player_id":null,"payload":{"outcome":"WIN","winner":"PLAYER_1","final_scores":{"PLAYER_1":104,"PLAYER_2":97},"reason":"PLAYER_2 busted on final turn"},"timestamp":1759546950}
```

```json
{"msg_type":"GAME_OVER","player_id":null,"payload":{"outcome":"FORFEIT","winner":"PLAYER_1","final_scores":{"PLAYER_1":34,"PLAYER_2":41},"reason":"PLAYER_2 disconnected"},"timestamp":1759546901}
```

After sending `GAME_OVER`, the server closes both sockets and resets for a new game (FSM `CLEANUP`).

---

## 5. Full Session Example

```
C1 → S  {"msg_type":"CONNECT","player_id":null,"payload":{"alias":"Wade"},"timestamp":1759546800}
S → C1  {"msg_type":"LOBBY_WAIT","player_id":"PLAYER_1","payload":{"players_connected":1,"message":"Waiting for Player 2..."},"timestamp":1759546800}
C2 → S  {"msg_type":"CONNECT","player_id":null,"payload":{"alias":"Bob"},"timestamp":1759546830}
S → C1  {"msg_type":"GAME_START","player_id":"PLAYER_1","payload":{"your_role":"PLAYER_1","your_alias":"Wade","opponent_alias":"Bob","first_player":"PLAYER_1","target_score":100},"timestamp":1759546830}
S → C2  {"msg_type":"GAME_START","player_id":"PLAYER_2","payload":{"your_role":"PLAYER_2","your_alias":"Bob","opponent_alias":"Wade","first_player":"PLAYER_1","target_score":100},"timestamp":1759546830}
S → C*  {"msg_type":"STATE_UPDATE","player_id":null,"payload":{"active_player":"PLAYER_1","scores":{"PLAYER_1":0,"PLAYER_2":0},"turn_total":0,"final_turn":false,"last_event":{"player":"PLAYER_1","result":"TURN_START","die":null,"points_banked":0}},"timestamp":1759546830}
C2 → S  {"msg_type":"MOVE","player_id":"PLAYER_2","payload":{"action":"ROLL"},"timestamp":1759546831}
S → C2  {"msg_type":"ERROR","player_id":"PLAYER_2","payload":{"code":"NOT_YOUR_TURN","detail":"It is PLAYER_1's turn."},"timestamp":1759546831}
C1 → S  {"msg_type":"MOVE","player_id":"PLAYER_1","payload":{"action":"ROLL"},"timestamp":1759546835}
S → C*  {"msg_type":"STATE_UPDATE",...,"turn_total":4,...,"last_event":{"player":"PLAYER_1","result":"ROLLED","die":4,"points_banked":0}}
C1 → S  {"msg_type":"MOVE","player_id":"PLAYER_1","payload":{"action":"HOLD"},"timestamp":1759546838}
S → C*  {"msg_type":"STATE_UPDATE","player_id":null,"payload":{"active_player":"PLAYER_2","scores":{"PLAYER_1":4,"PLAYER_2":0},"turn_total":0,"final_turn":false,"last_event":{"player":"PLAYER_1","result":"HELD","die":null,"points_banked":4}},"timestamp":1759546838}
   ... play continues ...
S → C*  GAME_OVER
S       closes both sockets → CLEANUP → WAITING_FOR_PLAYERS
```

---

## 6. Connection Termination & Socket Lifecycle

Every way a connection can end lands in the same server function, `handle_disconnect(role, how)`, so cleanup logic lives in one place.

### 6.1 Graceful: application-layer `DISCONNECT`

1. Client sends `DISCONNECT`, then calls `sock.close()`.
2. Closing starts the TCP FIN handshake (FIN → ACK, FIN → ACK).
3. Server reads the `DISCONNECT` line, calls `handle_disconnect(role, "quit")`.
4. Server's next `recv()` on that socket returns `b""` (EOF from the FIN). The handler is idempotent, so the second trigger does nothing.

The point of the `DISCONNECT` message: the server learns *why* the player left, and can say so in `GAME_OVER.reason`.

### 6.2 Clean close without `DISCONNECT`: TCP FIN / EOF

If the client process exits normally (or the user closes the terminal), the OS sends FIN without an application message.

**The 0-byte rule:** when the peer closes, `recv()` does **not** raise — it returns `b""`. That is EOF. Every receive loop must check for it:

```python
chunk = sock.recv(4096)
if not chunk:                       # b"" = peer sent FIN
    handle_disconnect(role, "eof")
    break                           # without this, recv() returns b"" forever → 100% CPU spin
```

### 6.3 Abrupt: TCP RST, crashes, dead links

| Event | What the server sees |
|---|---|
| Client killed (`kill -9`), host reboots | Next `recv()` / `sendall()` raises `ConnectionResetError` (RST) |
| Server sends to an already-closed peer | `BrokenPipeError` (EPIPE) |
| Windows-style abort | `ConnectionAbortedError` |
| Link cut in CML, no packets at all | Nothing — the socket sits silent. Detected by TCP keepalive (below), which then raises `TimeoutError` / `OSError` on the next read |

Every `recv()` and `sendall()` is wrapped:

```python
try:
    chunk = sock.recv(4096)
except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError, OSError) as e:
    log.warning("lost %s: %s", role, e)
    handle_disconnect(role, "network")
    break
```

A failure while **broadcasting** to one client must not stop the broadcast to the other: each `sendall()` in a broadcast is wrapped individually.

**Silent link failure:** a pulled cable produces no FIN and no RST, and a waiting player is legitimately idle, so a plain `recv()` timeout would wrongly kick someone thinking about their move. Instead the server enables TCP keepalive on each accepted socket:

```python
sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)   # probe after 30 s idle
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)  # every 10 s
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)     # dead after 3 misses
```

A dead peer is detected in about 60 seconds without a single application byte. (`TCP_KEEPIDLE` etc. are Linux constants; the CML Ubuntu/Alpine nodes support them. The code checks `hasattr` before using them.)

### 6.4 What `handle_disconnect` does, by game phase

| Phase when it happens | Server action |
|---|---|
| Lobby, only Player 1 connected | Remove Player 1, close socket, lobby back to empty. No message sent (nobody to tell). |
| During play | Remaining player gets `GAME_OVER` with `outcome:"FORFEIT"`, `winner` = remaining player. Then `CLEANUP`. |
| Already in `GAME_OVER` / `CLEANUP` | Ignored — game already ended. |
| Socket never sent `CONNECT` | Close socket, no state change. |

### 6.5 Client side

- Server EOF or socket exception → client prints `Connection to server lost.` and exits with code 1. No crash, no traceback.
- DNS failure on `-i` (`socket.gaierror`) → prints `Could not resolve <hostname>` and exits with code 2.
- Connection refused → prints the host/port it tried and exits with code 2.

### 6.6 Planned change for Sprint 5

Sprint 5 adds reconnect-without-state-loss. At that point the "during play" row changes from immediate forfeit to a grace period (planned: 60 s) during which the dropped player can reconnect with a `CONNECT` carrying a session token. This blueprint will be revised then; Sprint 1 behavior is immediate forfeit.

---

## 7. Server-Side Constants

| Constant | Value |
|---|---|
| `MAX_MSG_BYTES` | 4096 |
| `RECV_CHUNK` | 4096 |
| `TARGET_SCORE` | 100 |
| `ALIAS_PATTERN` | `^[A-Za-z0-9_]{1,16}$` |
| `ENCODING` | `utf-8` |
| `DELIMITER` | `b"\n"` |
