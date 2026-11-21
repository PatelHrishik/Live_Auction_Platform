# Live Auction Platform: Bidding Core

A Django service that runs live auctions: it accepts or rejects each bid,
extends an auction by 30 seconds when a bid arrives in its final 30 seconds,
stays correct when many people bid at the same moment, and closes with exactly
one winner. Watchers get every price change pushed to them live.

- **Stack:** Python 3.11+, Django 5.1+ (tested on 6.0), SQLite by default, PostgreSQL optional. No other dependencies.
- **Tests:** 39 tests, including multi-threaded bid storms on real separate DB connections.
- **Accounts:** sign up as a **seller** (creates auctions) or a **bidder** (places bids). Login uses Django sessions.
- **Demo:** a browser page with live price, countdown and bidding. Log in as a seller in one window and as bidders in others (use a private window for each extra account).

---

## Quick start

```bash
cd auction
python -m venv .venv && .venv\Scripts\activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py test auctions                      # 39 tests
python manage.py runserver                          # http://127.0.0.1:8000, then "Create an account"
```

Optional, in a second terminal, to record results as soon as auctions end
(correctness does not depend on this; see [How correctness is guaranteed](#how-correctness-is-guaranteed)):

```bash
python manage.py close_auctions --loop
```

Load test against the running server (standard library only). It first signs up
one seller and `--bidders` bidder accounts with random names:

```bash
python scripts/bid_storm.py --bids 1000 --workers 100
# 300 bids in 3.31s: {'ACCEPTED': 10, 'BID_TOO_LOW': 282, 'ALREADY_LEADING': 8}
# OK: no lost updates, prices strictly increasing, final state matches history
```

---

## Design note: 50,000 auctions, 200,000 watchers on one

1. **Each auction has exactly one owner, chosen by sharding on its ID.** Auction IDs are spread across a set of bidding nodes with consistent hashing. Every bid for an auction is routed to the node that owns it, and that node handles them one at a time in memory, like an actor. There are no distributed locks. One owner can handle well over 100k bid checks per second, which covers the rush in the final ten seconds. 50,000 auctions spread over N nodes means each node owns about 50,000/N small actors.
2. **A bid is stored durably before anyone is told it was accepted.** Each accepted bid is appended to a log partitioned by auction ID (Kafka or Redis Streams) and written to Postgres before the bidder gets `accepted`. If an owner node dies, a new owner takes over the auction's lease, with a fencing token so the old owner can no longer write, and rebuilds state by replaying the log. Accepted bids are never lost, and two nodes can never both accept bids for the same auction.
3. **Updates fan out in layers, not from the owner.** The owner publishes each state change once to pub/sub. A layer of stateless WebSocket/SSE gateways subscribes and pushes to clients. At about 20k connections per gateway, 200,000 watchers need about 10 gateways plus spare capacity. Watchers can be added without adding any load to bidding.
4. **Messages are versioned snapshots, batched under load.** Every message is the full auction state with a strictly increasing `version`. Clients drop anything older than what they already have, so an out-of-order or repeated message can never show an old price. Gateways send each auction at most about 10 times per second (latest state wins), so 5,000 bids in the last ten seconds become about 100 updates per watcher, not 5,000.
5. **Hopeless bids are rejected before they reach the owner.** Gateways cache the latest price and reject bids that are clearly too low or arrive after the auction ended, so the owner only sees bids that might win. Per-user rate limits and idempotency keys (already implemented here) handle retries and abuse during the closing rush.
6. **The owner's clock decides, and the close goes through the same queue as bids.** Clients never send timestamps. A bid counts if the owner processes it before `ends_at`, and the 30-second extension is computed from the owner's clock. The close is an event in the same ordered stream as the bids, so the winner is simply the leader at the moment the stream closes. It is recorded once, persisted, and then announced. The countdown on screen is only for display: each snapshot carries `server_time` so clients can correct for their own clock being off.

**How this repo maps to that design:** here the database row lock plays the
role of the single owner (point 1), the database commit is the durable store
(point 2), and the SSE endpoint is a single-node gateway that polls instead of
subscribing (points 3 and 4). The client-facing contract (versioned snapshots,
reason codes, idempotency keys) is the one the scaled-out design would keep.

---

## Rules and assumptions

The brief leaves details open on purpose. These are the decisions made:

| Question | Decision |
|---|---|
| Whose clock counts? | **The server's only.** A bid is timestamped when it is processed, after acquiring the auction lock. Clients never send times. |
| Valid first bid | `amount >= starting_price` |
| Valid later bid | `amount >= current_price + min_increment` |
| Two equal bids at once | The first one processed wins. The second fails `BID_TOO_LOW` because it doesn't beat the new price. |
| Leader bids again | Rejected (`ALREADY_LEADING`), so nobody can raise the price against themselves. |
| Late-bid extension | If a bid is accepted with **≤ 30s** left, `ends_at = now + 30s`, so there are always at least 30s after the last bid for someone to respond. A bid with more than 30s left doesn't change `ends_at`. (The window is `AUCTION_EXTENSION_SECONDS` in settings.) |
| When exactly does it end? | Bids are accepted while `now < ends_at`. A bid at exactly `ends_at` is rejected. |
| No bids at all | The auction closes with `winner = null`. |
| Money | Integers in minor units (cents), so no floating-point errors. Floats are rejected with a 400. |
| Retries | Optional `Idempotency-Key` header (or `idempotency_key` field). Re-sending an accepted bid returns the original result (200, `duplicate: true`). Reusing a key for a *different* bid is rejected. Rejected bids aren't stored, so retrying one simply re-evaluates it. |
| Identity | Comes from the logged-in session. The bidder is always the session user; a `bidder_id` in the request body is ignored. |
| Roles | Each account is a `SELLER` or a `BIDDER` (`Profile.role`, chosen at sign-up). Only sellers create auctions; only bidders bid. Users made with `createsuperuser` count as bidders. |

---

## How correctness is guaranteed

All state changes live in [`auctions/services.py`](auctions/services.py). It rests on three rules:

**1. One writer at a time per auction.** Every bid and every close runs inside
a transaction that first locks the auction row (`SELECT ... FOR UPDATE`).
Concurrent bids on one auction queue up and run one after another. Each reads
the state the previous bid left behind, so a bid can never overwrite another's
accepted update ("lost update"), and there can never be two leaders.

- PostgreSQL: a real row lock, so different auctions proceed in parallel.
- SQLite (default): `FOR UPDATE` is a no-op there, so transactions are opened
  with `BEGIN IMMEDIATE` (taking the write lock up front) and WAL mode lets
  readers continue. It's correct, but less parallel. See `config/settings.py`.

**2. The clock decides, not a timer.** Every bid checks `now >= ends_at` itself,
inside the lock. Once the end time passes, *nothing* can change the leader, so
the winner is fixed at that instant, whether or not any close job has run.
The close job (`close_auctions`), the read endpoints and late bids only
*record* the result that is already fixed. A delayed or crashed closer can
never let a late bid through.

**3. The close is atomic and happens once.** Closing takes the same lock,
re-checks `status == OPEN and now >= ends_at`, and writes `winner`,
`final_price`, `closed_at` in one transaction. Any later attempt sees
`CLOSED` and does nothing. A database constraint also requires every
closed auction to have a `closed_at`.

On top of that, every accepted bid is stored in an append-only `Bid` table with a
per-auction `sequence` (1, 2, 3…, unique), so the full history can be
audited and the auction rebuilt from it.

### What the tests prove

| File | What it covers |
|---|---|
| `tests/test_rules.py` | Every rule with a frozen clock: starting price, increment, ties, self-outbid, the exact end instant, extension at 31s / 10s / 1ms left, a 10-round last-second bidding war, no-bid close, close idempotency, idempotency keys. |
| `tests/test_concurrency.py` | **Real threads, separate DB connections, all released at once by a barrier:** a 400-bid storm with no lost updates (history contiguous, prices strictly rising by ≥ increment, final state = last bid); 20 equal bids at once, exactly one accepted; a last-second burst that extends correctly and ends with one winner; 20 late bids racing 10 close workers, giving exactly one close and an unchanged winner; 15 concurrent retries with one key, giving one bid. |
| `tests/test_api.py` | HTTP contract: status codes, error bodies, validation, idempotency header, extension through the API, SSE stream delivering the final result. Auth: login/logout/sign-up, role checks (401/403), bidder taken from the session, CSRF enforced. |

The concurrency suite was run three times in a row with every test passing each time. `scripts/bid_storm.py` checks the same
invariants end-to-end over HTTP against a running server.

---

## API

Base URL `/api`. All bodies are JSON. Money is in cents.

**Auth:** reads (`GET`) are public. Writes (`POST`) need a session cookie from
`/login/` or `/signup/` plus the `X-CSRFToken` header (value of the `csrftoken`
cookie). Not logged in gives `401 NOT_AUTHENTICATED`; wrong role gives
`403 FORBIDDEN`.

### Create an auction: `POST /api/auctions` (sellers only)
```json
{ "title": "Vintage camera", "starting_price": 1000, "min_increment": 100, "duration_seconds": 300 }
```
`201` returns the auction object (below).

### Get an auction: `GET /api/auctions/{id}`  ·  List: `GET /api/auctions`
```json
{
  "id": 1, "title": "Vintage camera", "seller": "sam", "status": "OPEN",
  "starting_price": 1000, "min_increment": 100,
  "current_price": 1500, "leader": "alice", "min_next_bid": 1600, "bid_count": 3,
  "original_ends_at": "...", "ends_at": "...", "remaining_ms": 21400,
  "winner": null, "final_price": null, "closed_at": null,
  "version": 3, "server_time": "..."
}
```

### Place a bid: `POST /api/auctions/{id}/bids` (bidders only)
```bash
curl -X POST http://127.0.0.1:8000/api/auctions/1/bids \
  -b "sessionid=...; csrftoken=..." -H "X-CSRFToken: ..." \
  -H "Content-Type: application/json" -H "Idempotency-Key: 7f3c..." \
  -d '{"amount": 1600}'
```

| Status | Meaning | Body |
|---|---|---|
| `201` | Accepted | `{accepted, duplicate:false, extended, bid, auction}` |
| `200` | Retry of an already-accepted bid | same, with `duplicate: true` |
| `409` | Rejected by a rule | `{error, message, auction, min_required?}` where `error` ∈ `BID_TOO_LOW`, `ALREADY_LEADING`, `AUCTION_CLOSED`, `IDEMPOTENCY_CONFLICT` |
| `400` | Malformed input | `{error: "INVALID_REQUEST", message}` |
| `401` | Not logged in | `{error: "NOT_AUTHENTICATED", message}` |
| `403` | Wrong role (seller bidding, bidder creating) | `{error: "FORBIDDEN", message}` (a missing CSRF token also gives `403`, with Django's HTML error page) |
| `404` | Unknown auction | `{error: "NOT_FOUND", message}` |

### Bid history: `GET /api/auctions/{id}/bids`
The latest 100 accepted bids, newest first.

### Live updates: `GET /api/auctions/{id}/stream` (Server-Sent Events)
```
id: 4
event: auction
data: { ...auction object... }
```
Sent whenever `version` changes. The stream ends after the `CLOSED` event.
Clients should ignore any event whose `version` is lower than the one they
hold. Use `server_time` to correct the countdown for client clock drift.

---

## Project layout

```
auction/
├── config/settings.py            DB setup that makes bidding safe (read the comments)
├── auctions/
│   ├── services.py               ★ all auction rules + locking: start here
│   ├── models.py                 Profile (seller/bidder role), Auction (live state + result), Bid (append-only history)
│   ├── forms.py                  sign-up form with role choice
│   ├── clock.py                  single source of time; patched in tests
│   ├── views.py                  JSON API + SSE stream (thin)
│   ├── urls.py
│   ├── admin.py                  read-only views of state (changes must go through services)
│   ├── management/commands/close_auctions.py
│   ├── templates/base.html       shared layout + styles
│   ├── templates/registration/   login + sign-up pages
│   ├── templates/auctions/index.html   live auction page (seller and bidder views)
│   └── tests/                    test_rules · test_concurrency · test_api
└── scripts/bid_storm.py          HTTP load + invariant check
```

---

## Trade-offs and what I'd do next

- **SQLite serializes all writes.** That's fine for a demo. With `POSTGRES_DB` set (and
  `psycopg` installed), locks are per auction row and auctions don't block each other.
  Beyond one database: the single-owner, log-backed design in the note above.
- **The SSE stream polls the database every 250 ms per watcher** and holds one
  worker thread per connection. That's simple and correct, but doesn't scale. Next step:
  Redis pub/sub published after commit, plus an async (ASGI) gateway.
- **The dev server (`runserver`) handled about 90 bids/s on Windows** in the storm test.
  That number reflects the dev server and SQLite, not the design. Use gunicorn/uvicorn and Postgres
  for real throughput.
- **Not built (out of scope for the core):** password reset, reserve prices,
  scheduled start times, proxy (automatic) bidding, payments.
