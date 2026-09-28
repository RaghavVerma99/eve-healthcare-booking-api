# EVE Diagnostics Booking API

Backend service for the EVE Healthcare SDE intern assignment: authentication, a
diagnostic centre/test catalogue, test bookings, and a simulated payment
gateway with an idempotent webhook receiver.

- **Stack:** FastAPI, async SQLAlchemy 2, Alembic, PostgreSQL (`asyncpg`),
  Redis cache, Celery worker, JWT auth, Pydantic v2
- **Interactive docs:** `/docs` (Swagger), `/redoc`
- **Tests:** 150 passing, run against SQLite by default or PostgreSQL with
  `TEST_DATABASE_URL`
- **Requires:** Python 3.11+ (developed on 3.14), PostgreSQL 14+ or SQLite 3.35+

## Quick start

### Docker (whole stack: Postgres, Redis, migrations, seed, API, worker)

```bash
cp .env.example .env
make compose-up            # or: docker compose up --build
```

The API is served on <http://localhost:8000>. `docker compose` runs migrations
and the seed script before the API starts, so the catalogue is populated on
first boot.

### Local development

```bash
make dev-install                       # python3 -m venv .venv + pip install
cp .env.example .env                   # point DATABASE_URL at your Postgres
make migrate                           # alembic upgrade head
make seed                              # 3 centres, 5 tests, one admin user
make run                               # uvicorn app.main:app --reload
```

`make help` lists every target.

**No PostgreSQL or Docker available?** Switch the database URL to SQLite and the
same commands work unchanged — migrations and the seed script both handle it:

```bash
cp .env.example .env
sed -i 's|^DATABASE_URL=.*|DATABASE_URL="sqlite+aiosqlite:///./dev.db"|' .env
make migrate && make seed && make run
```

### Seeded credentials

| Role  | Email              | Password      |
| ----- | ------------------ | ------------- |
| Admin | `admin@eve.health` | `Admin@12345` |

Admin accounts can create and edit centres, tests, and per-centre offerings.
Regular users can browse the catalogue, book tests, and pay for bookings.

## Architecture

```
app/
  main.py            app factory, lifespan, OpenAPI
  api/
    deps.py          auth/ownership dependencies, pagination
    middleware.py    request context, structured logging, rate limiting
    router.py        route table
    routers/         auth, catalogue, bookings, payments, system
  core/              settings, exception types, exception handlers, security, logging
  db/                engine/session, base mixins, seed script
  models/            SQLAlchemy models and state machines
  schemas/           Pydantic request/response models
  services/          all business logic and transaction boundaries
  worker.py          Celery app
alembic/versions/    schema migrations
tests/               150 tests
```

Requests flow **router → service → model**. Routers do no business logic; they
parse, authorise, call one service function, and commit. Services own the
transaction boundaries and are the only place that mutates payment or booking
state. That split is what makes the state machines testable without HTTP, and
it is why `POST /payments/` and the webhook route can share one code path.

### The decision that shaped the persistence layer

`Booking` eager-loads `user`, `test`, and `centre` (`lazy="joined"`), because
almost every booking response needs them and avoiding a second round trip is
worth the join. That has a consequence that is easy to miss: **any
`SELECT ... FOR UPDATE` on a booking now contains outer joins, and PostgreSQL
rejects `FOR UPDATE` on the nullable side of an outer join.**

So `SELECT bookings.* FOR UPDATE` fails on PostgreSQL with
`cannot be applied to the nullable side of an outer join`, while SQLite ignores
`FOR UPDATE` entirely and passes. A SQLite-only test run cannot catch this.

The fix is `with_for_update(of=Booking)`, which scopes the lock to the booking
row and leaves the joined rows alone. It is applied wherever a payment or
refresh token is locked:

- `app/services/payment_service.py` — serialises concurrent callbacks for one
  booking, so a double capture is impossible
- `app/services/auth_service.py` — serialises token rotation for one user

`of=` is also what keeps the lock narrow. Without it PostgreSQL would lock the
shared centre and user rows too, so two unrelated bookings at the same centre
would queue behind each other. The same tests run green on SQLite and fail on
PostgreSQL if this is removed, which is why the suite is verified on both.

### Idempotency keys

`POST /bookings/` and `POST /payments/` accept an optional `idempotency_key`
(1–64 characters, whitespace trimmed). Presenting a key that has already been
used **by the same user** returns the original resource instead of creating a
duplicate, so a client that retries after a timeout cannot double-book or
double-charge.

Presenting a key that belongs to a **different user** is rejected with `403`
`idempotency_key_conflict`, which stops one user from reading or overwriting
another's resource by guessing keys. On payments an admin is exempt from that
check, consistent with admin access to other users' bookings.

Be aware that the key is matched on the key alone, not on a fingerprint of the
request body. Reusing a key with a *different* body therefore returns the
original booking rather than a conflict. Comparing a request fingerprint and
rejecting the mismatch is the stricter behaviour and is listed under *What I
would improve with more time*.

This is separate from webhook idempotency, which is enforced by the
`webhook_events` claim rather than by a client-supplied key, and needs no
cooperation from the caller.

## Endpoints

| Method   | Path                                        | Purpose                                       |
| -------- | ------------------------------------------- | --------------------------------------------- |
| `GET`    | `/health`                                   | Liveness plus database/cache health           |
| `POST`   | `/auth/signup`                              | Register and receive a token pair             |
| `POST`   | `/auth/login`                               | JSON login                                    |
| `POST`   | `/auth/token`                               | OAuth2 password flow (form encoded)           |
| `POST`   | `/auth/refresh`                             | Rotate a refresh token                       |
| `POST`   | `/auth/logout`                              | Revoke a refresh token                        |
| `GET`    | `/auth/me`                                  | Current user                                  |
| `GET`    | `/auth/config`                              | Non-sensitive runtime settings                |
| `GET`    | `/centres/`                                 | Paginated centre search (`city`, `search`)    |
| `POST`   | `/centres/`                                 | Create a centre (admin)                       |
| `GET`    | `/centres/{centre_id}`                      | Centre detail                                 |
| `PATCH`  | `/centres/{centre_id}`                      | Update a centre (admin)                       |
| `GET`    | `/centres/{centre_id}/tests`                | Centre offerings with centre-specific prices  |
| `PUT`    | `/centres/{centre_id}/tests`                | Replace centre offerings (admin)              |
| `DELETE` | `/centres/{centre_id}/tests/{test_id}`      | Stop offering a test (admin)                  |
| `GET`    | `/tests/`                                   | Test catalogue search                         |
| `POST`   | `/tests/`                                   | Create a test (admin)                         |
| `GET`    | `/tests/{test_id}`                          | Test detail                                   |
| `PATCH`  | `/tests/{test_id}`                          | Update a test (admin)                         |
| `POST`   | `/bookings/`                                | Book a test at a centre                       |
| `GET`    | `/bookings/`                                | Current user's bookings                       |
| `GET`    | `/bookings/{booking_id}`                    | Booking detail                                |
| `POST`   | `/bookings/{booking_id}/cancel`             | Cancel a booking                              |
| `GET`    | `/bookings/{booking_id}/payments`           | Payment attempts for a booking                |
| `POST`   | `/payments/`                                | Simulated payment for a booking               |
| `POST`   | `/payments/webhook/`                        | Idempotent provider callback                  |
| `GET`    | `/payments/{payment_id}`                    | Payment detail (owner)                        |
| `GET`    | `/payments/webhook/{event_id}`              | Inspect a stored event (admin)                |
| `POST`   | `/payments/webhook/{event_id}/replay`      | Re-queue a stored event (admin)              |

### Walkthrough

```bash
BASE=http://localhost:8000

# 1. Sign up
TOKEN=$(curl -s -X POST $BASE/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"Str0ngPass","full_name":"Me"}' \
  | jq -r .tokens.access_token)

# 2. Find a centre and what it offers
CENTRE=$(curl -s "$BASE/centres/?city=Bengaluru" -H "Authorization: Bearer $TOKEN" | jq -r .items[0].id)
TEST=$(curl -s "$BASE/centres/$CENTRE/tests" -H "Authorization: Bearer $TOKEN" | jq -r .items[0].test_id)

# 3. Book
BOOKING_ID=$(curl -s -X POST $BASE/bookings/ -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"test_id\":\"$TEST\",\"centre_id\":\"$CENTRE\",\"appointment_at\":\"2026-10-01T09:30:00+05:30\"}" \
  | jq -r .id)

# 4. Pay. "simulate" is one of: success (default), failure,
#    insufficient_funds, gateway_error. The response echoes the webhook event
#    that the simulated provider delivered, so save it.
curl -s -X POST $BASE/payments/ -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"booking_id\":\"$BOOKING_ID\",\"simulate\":\"success\"}" > payment.json
jq '{payment: .payment.status, booking: .booking_status, event: .webhook.event_id}' payment.json
# { "payment": "SUCCESS", "booking": "CONFIRMED", "event": "evt_..." }

# 5. A provider retry of the same event is safe: replay it verbatim.
#    Signatures are required by default, so sign the exact bytes you send.
#    The key is read from your .env, so it cannot drift from what the server
#    actually loaded.
export WEBHOOK_SIGNING_SECRET=$(grep '^WEBHOOK_SIGNING_SECRET=' .env | cut -d= -f2- | tr -d '"')
BODY=$(jq -c .webhook payment.json)
SIG=$(printf '%s' "$BODY" | python3 -c \
  'import hashlib,hmac,os,sys; print(hmac.new(os.environ["WEBHOOK_SIGNING_SECRET"].encode(), sys.stdin.buffer.read(), hashlib.sha256).hexdigest())')

curl -s -X POST $BASE/payments/webhook/ -H 'Content-Type: application/json' \
  -H "X-Signature: sha256=$SIG" -d "$BODY" | jq '{event_id, duplicate, booking_status, payment_id}'
# { "event_id": "evt_...", "duplicate": true, "booking_status": "CONFIRMED", "payment_id": "..." }
```

`X-Signature` takes a bare hex digest or the `sha256=<hex>` prefix, either way
round, and the signature must cover the exact bytes sent, so sign the string you
pass to `-d` rather than re-serialising the JSON.

Set `WEBHOOK_REQUIRE_SIGNATURE=false` to drop the header while experimenting;
the receiver is unauthenticated, so the default is on.

### Failure and retry

A declined payment leaves the booking `FAILED` and payable again, so a user
retries the same booking rather than rebooking. Every attempt is kept.

```bash
# Book again, then fail the payment
BOOKING_ID=$(curl -s -X POST $BASE/bookings/ -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"test_id\":\"$TEST\",\"centre_id\":\"$CENTRE\",\"appointment_at\":\"2026-10-02T09:30:00+05:30\"}" \
  | jq -r .id)

curl -s -X POST $BASE/payments/ -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"booking_id\":\"$BOOKING_ID\",\"simulate\":\"insufficient_funds\"}" \
  | jq '{payment: .payment.status, failure: .payment.failure_code, booking: .booking_status}'
# { "payment": "FAILED", "failure": "insufficient_funds", "booking": "FAILED" }

# Retry the same booking; it transitions FAILED -> PENDING -> SUCCESS
curl -s -X POST $BASE/payments/ -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"booking_id\":\"$BOOKING_ID\",\"simulate\":\"success\"}" \
  | jq '{payment: .payment.status, booking: .booking_status}'
# { "payment": "SUCCESS", "booking": "CONFIRMED" }

# Both attempts are visible, newest first
curl -s "$BASE/bookings/$BOOKING_ID/payments" -H "Authorization: Bearer $TOKEN" \
  | jq '[.[] | {status, failure_code, created_at}]'
# [ { "status": "SUCCESS", "failure_code": null, "created_at": "..." },
#   { "status": "FAILED", "failure_code": "insufficient_funds", ... } ]

# A confirmed booking cannot be cancelled by its owner
curl -s -X POST $BASE/bookings/$BOOKING_ID/cancel -H "Authorization: Bearer $TOKEN" \
  | jq .error
# { "code": "booking_not_cancellable", ... }
```

## Authentication and session security

Signup and login both return an access token and a refresh token. The access
token is a short-lived signed JWT; the refresh token is longer-lived, single-use,
and tracked server-side so it can be rotated and revoked.

### What is stored, and what that buys

| Stored | Never stored |
| --- | --- |
| `jti` (random `uuid4` hex), `family_id`, `user_id`, `expires_at`, `revoked_at`, `replaced_by_jti`, `user_agent` | the refresh token itself, the password |

Decisions behind that:

- **The raw refresh token is never persisted.** Only the `jti` is, so a database
  leak yields material that cannot be presented as a credential. This is also
  why I did *not* add hashing of the token at rest: there is no plaintext token
  in the table to hash. A hash of a value that is never stored adds a lookup
  indirection without removing a risk, so it would be security theatre.
- **Authorization never trusts a token claim.** The access token does carry
  `is_admin`, but only as display metadata. `get_current_user`
  (`app/api/deps.py`) reloads the user on every request and `get_current_admin`
  checks `user.is_admin` on that row. Deactivating a user or stripping admin
  therefore takes effect on the next request instead of waiting out the token
  lifetime. This costs one query per request, which is the right trade on an
  admin surface.
- **Token types are enforced.** `access` and `refresh` are distinguished by the
  `type` claim, and every entry point requires the type it expects, so a refresh
  token cannot be replayed as a bearer credential.
- **Passwords** are bcrypt at cost 12 via passlib, compared in constant time, and
  never logged.

### Rotation, families, and reuse detection

Every `POST /auth/refresh` revokes the presented token and issues a successor
carrying the same `family_id`. A family is one login's rotation chain.

Presenting a token that was **already rotated past** is the signature of a stolen
token being replayed, or a client racing its own rotation. The response is `401`
`refresh_token_reuse_detected`, and every live token in that family is revoked,
so attacker and honest user are both signed out and the user re-authenticates.

Replaying a token revoked by an explicit `POST /auth/logout` is deliberately
**not** escalated to reuse detection. Such a token has no successor, and "logged
out, then retried" is a client quirk rather than a compromise; burning every
session over it would be a self-inflicted denial of service.
`replaced_by_jti` is what distinguishes the two cases and is the only reason
that column exists.

Blast radius is one family: a second login for the same user is a separate family
and is unaffected.

### One subtlety worth flagging

The request-scoped session is rolled back on any exception
(`app/db/session.py`). The family revocation is therefore committed *inside*
`_revoke_token_family` before the `401` is raised. Without that, the rollback
would silently discard the security response and the attacker would keep a
working token while the request appeared to have worked. This is the one place
where the service layer commits instead of leaving it to the router, and the
reason is worth more than the layering purity it costs.

Verified against PostgreSQL 18:

- rotating then replaying the original returns `refresh_token_reuse_detected`
- the successor issued by that rotation is revoked in the same response, which
  is what proves the commit survived the `401`
- a second, independent login still returns `200` from `/auth/refresh`
- replaying a logged-out token still returns the plain `refresh_token_revoked`
- the `family_id` backfill on upgrade seeds each pre-existing token as its own
  family, so enabling detection can never revoke an unrelated live session

## Domain model and state machines

`bookings.status` and `payments.status` are validated by explicit transition
maps in `app/models/enums.py`. An illegal transition raises a conflict instead
of silently mutating state.

```
Booking                        Payment
PENDING   -> CONFIRMED          PENDING -> SUCCESS
PENDING   -> FAILED             PENDING -> FAILED
PENDING   -> CANCELLED          FAILED  -> PENDING -> SUCCESS
FAILED    -> CONFIRMED          SUCCESS -> REFUNDED
FAILED    -> CANCELLED
CONFIRMED -> CANCELLED  (refund only)
CANCELLED -> terminal
```

Design decisions worth calling out:

- **A confirmed booking cannot be cancelled by the user** (`booking_not_cancellable`).
  Cancellation is only allowed while `PENDING` or `FAILED`; after a successful
  payment the money is captured, so the refund path owns cancellation.
- **The booking amount is snapshotted** from `centre_tests.price` at booking
  time, so later catalogue price changes never alter an existing booking.
- **A centre slot can hold one live booking, enforced by the database.**
  `create_booking` reads for a conflicting slot first, so the common case gets a
  clear `409 appointment_slot_taken` instead of a constraint error. That read
  cannot see a concurrent transaction that has not committed, so it is advisory
  only: two simultaneous requests for the same slot can both pass it. The
  partial unique index `uq_bookings_active_slot`
  (`WHERE status IN ('PENDING','CONFIRMED')`) is the authoritative guard, and
  the losing request surfaces as the same `409` from an `IntegrityError` caught
  in a savepoint. Because the predicate covers only live statuses, cancelling or
  failing a booking releases its slot for rebooking.
- **Money is `NUMERIC(10,2)`** and handled as `Decimal` end to end. A partial
  unique index `uq_payments_single_success_per_booking`
  (`WHERE status = 'SUCCESS'`) makes a second successful payment for one
  booking impossible at the database level. That index is PostgreSQL-only
  (`ddl_if`); the service also enforces the invariant in application code.
- **Failed payments are not terminal.** A new `POST /payments/` retries the
  booking, so `FAILED` is a valid payable state and the attempt history is
  preserved.
- **Cancelling is not deleting.** `DELETE /bookings/{id}` returns `405`; the
  only way to end a booking early is `POST /bookings/{id}/cancel`. A `DELETE`
  that quietly cancelled would report `200` for a request that destroyed
  nothing, which is worse than an honest `405`.

## Webhook idempotency

`POST /payments/webhook/` is the single mutation path for payment outcomes, and
`POST /payments/` calls the same internal function, so both routes behave
identically.

1. Claim the event with `INSERT ... ON CONFLICT DO NOTHING` on the
   `webhook_events.event_id` primary key. A conflicting insert means another
   request owns the event.
2. On conflict, re-read the row under `SELECT ... FOR UPDATE`. If it already
   succeeded, return `duplicate: true` and touch nothing. If the stored attempt
   failed, the event is retryable and is reprocessed.
3. Resolve the target payment by `payment_id`, then
   `provider + provider_payment_id`, then `booking_reference`, then
   `booking_id` (newest attempt first).
4. Check the amount and currency the provider settled against the ones we
   charged, when it sends them.
5. Apply the transition inside the same transaction as the event write, so a
   crash cannot leave the event marked processed without the payment moving.

### What a repeat delivery is allowed to do

A duplicate must not mutate state, but its acknowledgement still has to be
honest. The ack names the payment **the caller just asked about**, resolved from
the request body, not the one the first delivery happened to reference. If a
provider recycled an `event_id` across two payments, the second ack describes the
second payment — including the fact that it is still `FAILED` — and the collision
is logged as `webhook_event_id_reused` at `ERROR`. Reporting the first delivery's
booking would have told the provider its new payment was settled when it was not.

`WebhookAck` therefore carries `payment_id` alongside `booking_status`, and a
replay whose body cannot be resolved at all still acks `200` from the stored row
rather than turning into a `404`.

### Signature verification

`webhook_events.signature_valid` is tri-state, and the distinction matters:

| Value  | Meaning                                              |
| ------ | ---------------------------------------------------- |
| `true` | a signature verified (or the event came from the in-process gateway) |
| `false`| a signature was presented and **rejected**; parked in `FAILED`, never applied |
| `null` | signature checking was not enabled for this delivery |

Collapsing "not checked" into `false` would make every unsigned delivery look
like an attack in the admin inspector, and would permanently block the replay
task from re-processing it.

`X-Signature` accepts a bare hex digest or the conventional `sha256=<hex>`
prefix, in any case, and is compared with `hmac.compare_digest`.

Verified behaviour:

- replaying one event three times yields one mutation and two `duplicate: true`
  responses
- five concurrent deliveries of one event, each with its own session, produce
  exactly one applied update
- a *new* event id for an already-settled payment is stored with
  `result: payment_already_applied` and changes nothing, including the stored
  `provider_payment_id`
- an invalid HMAC signature is rejected with `401` before any write
- a settlement quoting the wrong amount or currency is `422` and the payment
  keeps its previous status

## Payments

No real gateway is involved. `POST /payments/` creates a `PENDING` payment,
calls the mock gateway, and feeds the result through the webhook processor, so
the normal path is exercised in tests and in local development.

`simulate` controls the outcome: `success` (default), `failure`,
`insufficient_funds`, `gateway_error`. Every attempt is stored, which makes the
retry-after-failure flow and its history visible via
`GET /bookings/{id}/payments`.

The provider is not trusted to be right about the invoice. `apply_payment_outcome`
already short-circuits a settled payment, then checks the amount and currency the
event quotes against what was charged — a mismatch is `422 amount_mismatch` or
`422 currency_mismatch` and the payment keeps its previous status. Recording the
provider's `provider_payment_id` happens only after those checks, so a second
event cannot overwrite the reference the first one stored.

## Data model

Eight tables, UUID primary keys, timezone-aware UTC timestamps throughout.

```
users ──< bookings ──< payments ──< webhook_events
  │          │                        (via payment_id)
  └──< refresh_tokens
centres ──< centre_tests >── tests        (per-centre price + availability)
```

| Table | Notable columns | Notes |
| --- | --- | --- |
| `users` | `email` unique, `hashed_password`, `is_admin`, `is_active` | email normalised to lowercase |
| `refresh_tokens` | `jti` unique, `family_id`, `revoked_at`, `replaced_by_jti` | token itself is never stored |
| `diagnostic_centres` | `latitude`/`longitude`, `city`, `is_active` | coordinates and status check-constrained |
| `diagnostic_tests` | `base_price`, `duration_minutes` | `base_price` and duration check-constrained |
| `centre_tests` | `price`, `is_available` | composite PK `(centre_id, test_id)`; per-centre override of `base_price` |
| `bookings` | `reference` unique, `amount`, `status`, `appointment_at`, `idempotency_key` | `amount` snapshotted from `centre_tests.price` |
| `payments` | `booking_id`, `amount`, `currency`, `status`, `provider_payment_id`, `failure_code` | `NUMERIC(10,2)`; `Decimal` end to end |
| `webhook_events` | `event_id` PK, `event_type`, `payload`, `result`, `signature_valid` | claim target for idempotency; `signature_valid` is nullable so "not checked" is distinct from "rejected" |

Indexes exist for the access patterns that matter: a user's bookings by recency
(`ix_bookings_user_created`), a user's non-terminal bookings
(`ix_bookings_user_status`), a centre's schedule
(`ix_bookings_centre_appointment`), catalogue availability
(`ix_centre_tests_test_available`), and active tokens per user
(`ix_refresh_tokens_user_active`). Two partial unique indexes carry invariants
that application checks cannot enforce on their own:

| Index | Table | Predicate | Enforces |
| --- | --- | --- | --- |
| `uq_bookings_active_slot` | `bookings` | `status IN ('PENDING','CONFIRMED')` | one live booking per centre per slot |
| `uq_payments_single_success_per_booking` | `payments` | `status = 'SUCCESS'` | at most one captured payment per booking |

The first exists on both PostgreSQL and SQLite, so the double-booking guarantee
is testable in the default suite; the second is PostgreSQL-only (`ddl_if`)
because SQLite's partial-index support is not relied on for a payment invariant,
and the service enforces it in application code as well. Check constraints cover
money, coordinates, durations, and status values, so bad data is rejected by the
database and not only by Pydantic.

Migrations live in `alembic/versions/`, and `alembic check` reports no drift on
either database. `alembic/env.py` skips indexes declared for another dialect, so
the PostgreSQL-only partial index is not reported as missing when checking
SQLite. `(centre_id, test_id)` on `centre_tests` is the composite primary key
rather than a separate unique constraint, because PostgreSQL collapses a unique
constraint with the same columns into the primary key index anyway.

`7c1f4a9b2d38` adds `refresh_tokens.family_id`. It is added nullable, backfilled
from `jti`, then made `NOT NULL`, because SQLite cannot alter a column to
`NOT NULL` in place; `batch_alter_table` recreates the table there and is a
no-op on PostgreSQL. The upgrade, downgrade, and re-upgrade paths were all
executed against both databases, with pre-existing rows in place.

## Error format

Every failure uses one envelope, with a `request_id` that also appears in the
`X-Request-ID` response header and in the structured logs:

```json
{
  "error": { "code": "booking_not_cancellable", "message": "...", "details": null },
  "request_id": "0f0f...",
  "timestamp": "2026-09-27T15:52:22Z"
}
```

`400` validation, `401` auth, `403` ownership/admin, `404` missing, `409` state
or uniqueness conflicts, `422` request validation, `429` rate limited.

Two `401` codes on `/auth/refresh` are deliberately distinct:
`refresh_token_revoked` for a token that was logged out or already spent, and
`refresh_token_reuse_detected` for a token that was rotated past and has come
back, which additionally revokes the whole token family. See
*Authentication and session security*.

## Testing

```bash
make test                                        # SQLite in-memory, 150 tests
make lint                                        # ruff over app, tests, alembic

# PostgreSQL: the suite creates and drops its own schema, so point it at an
# empty database. docker compose only creates eve_diagnostics, so create the
# test database once:
#   createdb -U eve eve_diagnostics_test
make test-postgres                               # or override the target:
make test-postgres TEST_PG_URL=postgresql+asyncpg://eve@127.0.0.1:5432/eve_diagnostics_test
```

All 150 tests pass on **both** SQLite and PostgreSQL. Running them against
PostgreSQL matters: it exercises `SELECT ... FOR UPDATE`, the
`INSERT ... ON CONFLICT DO NOTHING` event claim, and
`uq_payments_single_success_per_booking`, none of which SQLite enforces. SQLite
silently ignores `FOR UPDATE` and cannot host that payment-specific index, so a
SQLite-only pass would hide real concurrency bugs. `uq_bookings_active_slot` is
declared for both dialects precisely so the double-booking guarantee *is* covered
by the default run.

Coverage includes the full booking and payment state machines, concurrent
webhook delivery, signature verification, amount and currency reconciliation,
slot contention, ownership and admin authorization, token rotation and reuse
detection, pagination, and cache invalidation.

Concurrency tests use a `concurrent_client` fixture that gives every request
its own session, mirroring production. Sharing one session would both break on
PostgreSQL and fake concurrency on SQLite, where all tasks share one
connection. The shared-session `client` fixture revives its session after a
request that failed mid-write, which is what production gets for free from
closing a session per request.

Verified against a live PostgreSQL instance:

- 12 concurrent deliveries of one event id: 1 applied, 11 `duplicate: true`,
  1 `webhook_events` row, 1 payment, booking `CONFIRMED`
- inserting a second `SUCCESS` payment directly in SQL is rejected by
  `uq_payments_single_success_per_booking`

The double-booking guard is verified on both dialects, since
`uq_bookings_active_slot` is declared for each: with the advisory pre-check
deliberately blinded, a second insert for a slot that already holds a live
booking is still refused with `409 appointment_slot_taken`, and the centre keeps
exactly one booking at that time.

## Background worker

The Celery worker and beat handle work that should not block a request:

```bash
make worker        # celery -A app.worker.celery_app worker -Q payments,default
make beat          # celery -A app.worker.celery_app beat
```

| Task | Trigger | Does |
| --- | --- | --- |
| `bookings.expire_stale_pending` | beat, every 15 min | cancels `PENDING` bookings whose appointment is more than 24 h past, freeing the slot |
| `auth.purge_expired_refresh_tokens` | beat, every 6 h | deletes refresh tokens that expired more than a day ago |
| `payments.replay_webhook` | `POST /payments/webhook/{event_id}/replay` (admin) | re-queues a stored event, for one parked in `FAILED` |

`expire_stale_pending` selects with `FOR UPDATE SKIP LOCKED`, so it never blocks
on or races a payment that is confirming the same booking. Replay is safe to
trigger repeatedly: the processor is idempotent, so replaying a settled event is
a no-op. If the broker is unreachable the endpoint returns `503
queue_unavailable` rather than pretending the work was queued.

Redis backs both the cache and the broker. The API is fully functional without
the worker or beat running — only the recurring maintenance is deferred.

## Configuration

Settings are environment variables with a `.env` file; see `.env.example`.
The values that matter most:

| Variable                    | Default                                       | Notes                                  |
| --------------------------- | --------------------------------------------- | -------------------------------------- |
| `DATABASE_URL`              | `postgresql+asyncpg://eve:eve@localhost/eve_diagnostics` | SQLite is also supported      |
| `JWT_SECRET_KEY`            | placeholder                                   | **Must** be replaced in production     |
| `WEBHOOK_SIGNING_SECRET`    | `mockpay-webhook-secret`                      | HMAC key for webhook signatures         |
| `WEBHOOK_REQUIRE_SIGNATURE` | `true`                                        | Set `false` only for local experiments  |
| `REDIS_URL`                 | `redis://localhost:6379/0`                    | Empty falls back to in-process cache    |
| `RATE_LIMIT_REQUESTS`       | `100`                                         | `0` disables rate limiting              |
| `RATE_LIMIT_TRUST_PROXY_HEADERS` | `false`                                   | Only behind a proxy that overwrites the header |
| `CORS_ORIGINS`              | empty (deny all cross-origin)                 | Comma-separated allowlist, or `*`        |
| `LOG_JSON`                  | `true`                                        | JSON logs for ingestion                 |
| `SEED_ADMIN_EMAIL` / `_PASSWORD` | `admin@eve.health` / `Admin@12345`     | Only used by the seed script            |

## Assumptions

The assignment left several things open. These are the calls I made and why.

1. **A booking must be paid within a window.** Bookings start `PENDING` and are
   not auto-confirmed, because payment is what confirms them.
   `bookings.expire_stale_pending` ages out abandoned ones on a 15-minute beat
   schedule, so an unpaid slot frees itself instead of blocking the calendar
   forever. The API alone does not run it; without the worker, bookings stay
   `PENDING` until it does.
2. **`FAILED` is retryable, `CANCELLED` is terminal.** A declined payment should
   not force the user to rebook, so a failed booking can be paid again and the
   attempt history is kept. Cancellation ends the lifecycle.
3. **A user may not cancel a `CONFIRMED` booking.** Money is captured, so
   cancellation is a refund concern, not a booking concern. I read the suggested
   `CANCELLED` state as reachable via refund, which the webhook path handles.
4. **Price is a property of the centre, not the test.** A test has a base price;
   each centre may override it. Bookings snapshot the resolved price so later
   price changes never rewrite history.
5. **Webhook `data` shape is not specified, so the receiver is flexible.** The
   target payment is resolved by `payment_id`, then
   `provider` + `provider_payment_id`, then `booking_reference`, then
   `booking_id`. An event naming no payment we can resolve is a `404
   payment_not_found` and nothing is written. I originally stored those as
   `IGNORED`, but that status had no writer outside the branch that rejected
   them, and `404` is the truthful answer: we were not asked about something we
   hold. The event is not lost either, because a provider retry that carries a
   resolvable payload will be stored under a new `event_id` and processed
   normally.
6. **Only the three required event types are accepted**
   (`payment.succeeded`, `payment.failed`, `payment.refunded`). Unknown event
   types are a `422` rather than being silently swallowed.
7. **Amounts are trusted from the server, never the client.** The payment amount
   is read from the booking, and currency is fixed to `INR`.
8. **Signup returns a token pair.** The brief only requires signup and login, but
   not making a new user call login immediately is poor UX.
9. **Refresh tokens are persisted, not stateless.** That is what makes rotation
   and revocation possible. Only the `jti` is stored, never the token, and each
   rotation inherits a `family_id` so that reuse of a superseded token can burn
   exactly that login chain. See *Authentication and session security*.
10. **SQLite is a development convenience, PostgreSQL is the target.** On
    PostgreSQL both guards are enforced by the database: the partial unique
    index on live booking slots and the one on captured payments. SQLite hosts
    the slot index too, so that guarantee is covered by the default test run;
    it cannot host the payment index, so that one is application-enforced there.
11. **Rate limiting and caching are per process.** Correct for a single replica,
    documented as a limitation rather than silently assumed away. The window
    key is the peer address, not a forwarded header, because a client can set
    `X-Forwarded-For` itself; enable `RATE_LIMIT_TRUST_PROXY_HEADERS` only
    behind a proxy that overwrites it.


## What I would improve with more time

Ordered by value, not by how interesting they are.

1. **Outbox pattern for payment initiation.** Today the payment row and the
   simulated event are written in one transaction, which is fine for an
   in-process mock. Against a real gateway, "create payment" and "receive
   webhook" cannot share a transaction, so a transactional outbox plus a
   delivery worker is the correct shape. This is the highest-value change here
   by a wide margin, and also the largest: a new table, a publisher, and a
   restructuring of the payment transaction, so it is a poor choice for a
   timed "small change" exercise. Refresh-token reuse detection is a
   better-sized example of the same kind of reasoning.
2. **Outsourced auth.** Password hashing and JWT rotation are hand-rolled here
   deliberately, to show the mechanics. In production this should be a proven
   library or an identity provider, with key rotation and JWKS.
3. **Distributed rate limiting and caching.** Both are in-process today; Redis
   is already wired up for the broker, so moving both there is a small change.
4. **A real time-slot model.** A booking currently stores one `appointment_at`.
   A centre should have bookable slots with capacity, and concurrent bookings
   for the last slot should conflict on a partial unique index.
5. **Migrated test suite as the default.** The suite runs on both databases, but
   CI should always run PostgreSQL since it is the target and the only place the
   locking and partial-index behaviour is real.
6. **Email verification, password reset, and MFA** on top of the existing auth.
7. **Observability beyond logs.** Correlation IDs already exist; adding request
   metrics, latency histograms, and tracing would make the bonus points real.
8. **Signing key rotation.** Tokens are signed with a single symmetric
   `JWT_SECRET_KEY`, so there is no `kid` and no way to roll a key without
   invalidating every session. Asymmetric signing with a published JWKS, and a
   `kid` header so old keys can be retired gradually, is the fix. Note that
   hashing the token at rest is deliberately *not* on this list: the raw token
   is never stored, so there is nothing to hash. Token *families* already
   provide the session abstraction.
9. **Contract tests for the webhook schema.** A small JSON Schema plus a
   provider-contract test suite would catch a provider-side change before it
   reaches production.
10. **Request fingerprinting for idempotency keys.** Keys are matched on the key
    alone, so a reused key with a different body returns the original resource
    instead of `409`. Storing a hash of the canonical request and rejecting a
    mismatch would make reuse explicit rather than silently returning the old
    resource.

## Production notes

- Replace `JWT_SECRET_KEY` and `WEBHOOK_SIGNING_SECRET`; set
  `ENVIRONMENT=production`, `DEBUG=false`, and an explicit `CORS_ORIGINS`.
  Cross-origin requests are denied unless `CORS_ORIGINS` names them, and
  `allow_credentials` is only enabled for an explicit allowlist because the
  CORS spec forbids credentials alongside a `*` origin.
- The PostgreSQL partial unique index is the real guard against double capture;
  keep it when deploying to Postgres.
- Rate limiting and caching are per-process. For a multi-worker deployment,
  move both to Redis or a gateway so limits are global rather than per replica.
- `alembic upgrade head` is the only supported way to create the schema. The
  app never calls `create_all`, and the seed script refuses to run against an
  unmigrated database so migrations stay the single source of truth.
