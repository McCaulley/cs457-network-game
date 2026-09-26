# CS 457 Project Statement of Work (SOW) & Protocol Specification Template

**Student Name:** Wade McCaulley
**Date:** 2026-09-20
**Course:** CS 457 - Computer Networks
**Target Server Domain:** `server.mccaulley.edu`

---

## 1. Game Selection & Scope (Sprint 0)

### 1.1 Game Overview
- **Chosen Game:** Pig: two-player press-your-luck dice game
- **Player Capacity:** 2 Players (Simulated via 2 CML Client nodes)
- **Game Summary:** Players alternate turns. On your turn you may roll a single six-sided die as many times as you want. Each roll of 2–6 adds to a running turn total, which is at risk and not yet yours. Rolling a 1 wipes out the turn total and ends your turn immediately. Choosing to hold moves the turn total into your permanent banked score and passes the turn. The first player to 100 points wins.

The die is rolled by the server, never the client. This is the core reason the game suits a client-server architecture: the server is the sole source of randomness and the sole authority on score, so a modified or malicious client cannot fabricate a favorable roll. Clients only ever send an intent (`ROLL` or `HOLD`) and render whatever state the server returns.

### 1.2 Core Game Rules & Win/Draw Conditions
- **Turn Mechanics:** Roles are assigned by connection order where the first client to complete a `CONNECT` handshake becomes `PLAYER_1` and moves first. After the second becomes `PLAYER_2`. The server tracks `active_player` field and rejects any `MOVE` whose `player_id` does not match it, replying with an `ERROR` message rather than mutating state. A turn ends when the player rolls a 1 (bust) or sends `HOLD`; the server then flips `active_player` and broadcasts the new state. Turn order is therefore enforced entirely server-side — clients are not trusted to take turns politely.

- **Victory Condition:** A player wins by reaching a banked score of 100 or more, subject to the last-licks rule below. Only banked points count; an unbanked turn total is never a winning score.

- **Draw/Tie Condition:** This project uses the *last licks* variant. When a player first reaches 100 or more banked points, the opponent is granted one final turn. After that final turn resolves:
  - If the opponent's banked score exceeds the leader's, the opponent wins.
  - If the opponent's banked score is lower, the leader wins.
  - If the two banked scores are exactly equal, the game is a **draw**.

  Standard Pig has no possible tie, since play stops the instant a player crosses 100. Last licks was chosen deliberately so the finite state machine has a genuine draw branch to model and the implementation has a real tie path to test.

---

## 2. Application-Layer Messaging Protocol Blueprint (Sprint 1 Deliverable)

### 2.1 Message Transport & Serialization Format
- **Transport Protocol:** TCP
- **Serialization Format:** JSON (UTF-8 encoded)
- **Framing Mechanism:** Newline-delimited (`\n`) JSON payloads. Each message is a single JSON object on one line. The receiver buffers bytes until it sees a newline, parses that line, and retains any partial remainder for the next `recv()` — this handles TCP's stream semantics, where one `recv()` may return a partial message or several concatenated messages.

### 2.2 Message Schema Definitions

#### Message Types:
1. `CONNECT` (Client -> Server): Request to join the game room. Carries a display name.
2. `LOBBY_WAIT` (Server -> Client): Acknowledges the join, assigns `player_id`, notes the server is waiting for the second player.
3. `GAME_START` (Server -> Clients): Game initiated; confirms role assignment and which player moves first.
4. `MOVE` (Client -> Server): Player action. Payload carries `action`, which is either `ROLL` or `HOLD`.
5. `STATE_UPDATE` (Server -> Clients): Broadcast of full authoritative state — both banked scores, current turn total, active player, the result of the last die roll, and whether last licks is in effect.
6. `GAME_OVER` (Server -> Clients): Win or draw notification with final banked scores.
7. `ERROR` (Server -> Client): Out-of-turn move, unknown action, or malformed packet.

#### Example JSON Protocol Schema:

Client move:
```json
{
  "msg_type": "MOVE",
  "player_id": "Player_1",
  "payload": {
    "action": "ROLL"
  },
  "timestamp": 1727000000
}
```

Server state broadcast:
```json
{
  "msg_type": "STATE_UPDATE",
  "player_id": "SERVER",
  "payload": {
    "scores": { "Player_1": 34, "Player_2": 41 },
    "turn_total": 11,
    "active_player": "Player_1",
    "last_roll": 6,
    "last_event": "ROLLED",
    "last_licks": false
  },
  "timestamp": 1727000001
}
```

`last_event` is one of `ROLLED`, `BUSTED`, `HELD`, or `TURN_START`, and tells the client what text to print above the scoreboard.

---

### 2.3 Game State Machine (FSM) Design (Sprint 1 Deliverable)

Top-level flow:

`INIT` -> `WAITING_FOR_PLAYERS` -> `PLAYER_TURN` -> `EVALUATE_MOVE` -> `CHECK_WIN_DRAW` -> (`LAST_LICKS` ->) `GAME_OVER` -> `CLEANUP`

Detail on the turn cycle, which is where Pig has more structure than a simple grid game:

- `PLAYER_TURN` — turn total reset to 0; server awaits a `MOVE` from `active_player` only.
- `EVALUATE_MOVE` — branches on the action:
  - `ROLL` and die is 2–6: add to turn total, broadcast, return to `PLAYER_TURN` with the same active player.
  - `ROLL` and die is 1: discard turn total, broadcast bust, advance to `CHECK_WIN_DRAW`.
  - `HOLD`: add turn total to banked score, broadcast, advance to `CHECK_WIN_DRAW`.
- `CHECK_WIN_DRAW` — if the player who just finished has banked 100+ and last licks has not yet been played, set the last-licks flag and pass the turn for one final round. If last licks has already resolved, compare banked scores and route to `GAME_OVER` as a win or a draw. Otherwise return to `PLAYER_TURN` with the opponent active.
- `CLEANUP` — close sockets, free the room, log the result.

A full transition diagram will be submitted in Sprint 2.

---

## 3. Game Behavior & Server Concurrency Architecture (Sprint 2 Deliverable)

### 3.1 Server Concurrency Strategy
- **Architecture Choice:** Multi-threading (`threading.Thread`). One thread per connected client handles blocking `recv()`, plus the main thread running the `accept()` loop. With a fixed two-player room this is simpler to reason about than `selectors`, and it avoids the tight-spin-loop problem since every thread is blocked on I/O rather than polling.
- **Synchronization Logic:** All mutable game state — banked scores, turn total, active player, last-licks flag, and the connected-client list — lives in a single `GameState` object guarded by one `threading.Lock`. A client thread acquires the lock for the whole read-modify-broadcast sequence of a move, so the die roll, score update, and turn flip are atomic. A single coarse lock is sufficient here and eliminates the main race risk: both players sending a `MOVE` at the same instant and both being evaluated against a stale `active_player`.

### 3.2 State & Score Synchronization Across Clients
- **Turn Enforcement:** Before processing any `MOVE`, the server compares the message's `player_id` against `active_player` while holding the lock. A mismatch produces an `ERROR` reply to that client only, and game state is left untouched. Clients also grey out their input prompt when it is not their turn, but this is a convenience, not a control — the server assumes clients may lie.
- **Score & Board Synchronization:** After every state change the server broadcasts one identical `STATE_UPDATE` to both clients. Pig has no hidden information, so a single broadcast payload serves both players and there is no per-client filtering. Clients are stateless renderers: they hold no authoritative copy of the score and simply redraw the scoreboard from whatever the most recent `STATE_UPDATE` contained. This keeps the two screens consistent by construction and makes Sprint 5 reconnection straightforward — a rejoining client just needs one fresh `STATE_UPDATE` to be fully caught up.

---

## 4. Coding & AI Implementation Plan (Sprint 3)

- **Permitted AI Tools:** Claude, GitHub Copilot
- **AI Prompting & Constraint Strategy:** The protocol blueprint and FSM in Sections 2 and 3 are written first and treated as fixed. AI prompts will paste the relevant schema and state definitions directly into context and ask for code that conforms to them, rather than asking for "a dice game over sockets" and accepting whatever protocol the model invents. Generated code will be reviewed against the message-type list and FSM transitions before being committed, with particular attention to two failure modes AI tools are prone to here: dropping the newline framing logic in favor of assuming one `recv()` equals one message, and placing authority (the die roll, the win check) on the client side.
- **Implementation Risk Management:** Modules will be split early — transport, protocol parsing, and game state — so each can be tested in isolation and a problem in one does not block the others. Development order is server logic first with a scripted test client, then the interactive client, then CML deployment. Local `127.0.0.1` prototyping will be used through Sprint 4, keeping CML work focused on the topology rather than debugging application logic. Buffer time is reserved before the Sprint 5 deadline for link-impairment testing, which is the hardest piece to estimate.

---

## 5. CML Multi-Subnet Topology & Wireshark Deployment Plan (Sprint 4 & 5 Deliverable)

### 5.1 Subnet & Router Design
- **Subnet A (Client 1):** `192.168.10.0/24` (Interface `Gi0/1` on Router R1)
- **Subnet B (Client 2):** `192.168.11.0/24` (Interface `Gi0/2` on Router R1)
- **Subnet C (Game Server):** `192.168.20.0/24` (Interface `Gi0/1` on Router R2)
- **Router Backbone:** `10.0.0.0/30` (Interface `Gi0/0` on R1 <-> `Gi0/0` on R2)

### 5.2 DHCP Pools & DNS Configuration Plan
- **Router R1 DHCP Pool 1 (`CLIENT1_POOL`):** Leases `192.168.10.10` - `192.168.10.50`, gateway `192.168.10.1`, DNS `10.0.0.2`.
- **Router R1 DHCP Pool 2 (`CLIENT2_POOL`):** Leases `192.168.11.10` - `192.168.11.50`, gateway `192.168.11.1`, DNS `10.0.0.2`.
- **Router R2 Authoritative DNS:** Configured with `ip dns server` and static host mapping `ip host server.mccaulley.edu 192.168.20.100`.

### 5.3 Deployment Strategy & Wireshark Trace Capture
- **CML Deployment Strategy:** Deploy `server.py` onto the Subnet C node (`192.168.20.100`, statically addressed) behind Router R2, and `client.py` onto the Subnet A and Subnet B nodes behind Router R1. Clients are launched with `-i server.mccaulley.edu -p <port>` so that DNS resolution is exercised on every run rather than bypassed with a hardcoded IP.
- **Cisco Infrastructure Configuration:** Router R1 DHCP pools (`CLIENT1_POOL`, `CLIENT2_POOL`) and Router R2 authoritative DNS (`ip host server.mccaulley.edu 192.168.20.100`).
- **Wireshark Trace Capture Plan:** Capture the DHCP DORA exchange on a client-side link (`dhcp_negotiation.pcap`) and the DNS query/response resolution of `server.mccaulley.edu` (`dns_lookup.pcap`).

---

## 6. Repository

- **Public Git Repository:** https://github.com/McCaulley/cs457-network-game
