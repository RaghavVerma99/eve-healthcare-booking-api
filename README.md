# EVE Diagnostics Booking API

Backend service for the EVE Healthcare SDE intern assignment: authentication, a
diagnostic centre/test catalogue, test bookings, and a simulated payment
gateway with an idempotent webhook receiver.

- **Stack:** FastAPI, async SQLAlchemy 2, Alembic, PostgreSQL (`asyncpg`),
  Redis cache, Celery worker, JWT auth, Pydantic v2
- **Interactive docs:** `/docs` (Swagger), `/redoc`
- **Tests:** 126 passing, run against SQLite by default or PostgreSQL with
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

## Endpoints

| Method   | Path                                        | Purpose                                       |
| -------- | ------------------------------------------- | --------------------------------------------- |
| `GET`    | `/health`                                   | Liveness plus database/cache health           |
| `POST`   | `/auth/signup`                              | Register and receive a token pair             |
| `POST`   | `/auth/login`                               | JSON login                                    |
| `POST`   | `/auth/token`                               | OAuth2 password flow (form encoded)           |
| `POST`   | `/auth/refresh`                             | Rotate a refresh token (revokes the family on reuse) |
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
| `DELETE` | `/bookings/{booking_id}`                    | Cancel alias                                  |
| `GET`    | `/bookings/{booking_id}/payments`           | Payment attempts for a booking                |
| `POST`   | `/payments/`                                | Simulated payment for a booking               |
| `POST`   | `/payments/webhook/`                        | Idempotent provider callback                  |
| `GET`    | `/payments/{payment_id}`                    | Payment detail (owner)                        |
| `GET`    | `/payments/webhook/{event_id}`              | Inspect a stored event (admin)                |

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

# 5. A provider retry of the same event is safe: replay it verbatim
curl -s -X POST $BASE/payments/webhook/ -H 'Content-Type: application/json' \
  -d "$(jq -c .webhook payment.json)" | jq '{event_id, duplicate, booking_status}'
# { "event_id": "evt_...", "duplicate": true, "booking_status": "CONFIRMED" }
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
- **Money is `NUMERIC(10,2)`** and handled as `Decimal` end to end. A partial
  unique index `uq_payments_single_success_per_booking`
  (`WHERE status = 'SUCCESS'`) makes a second successful payment for one
  booking impossible at the database level. That index is PostgreSQL-only
  (`ddl_if`); the service also enforces the invariant in application code.
- **Failed payments are not terminal.** A new `POST /payments/` retries the
  booking, so `FAILED` is a valid payable state and the attempt history is
  preserved.

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
4. Apply the transition inside the same transaction as the event write, so a
   crash cannot leave the event marked processed without the payment moving.

Verified behaviour:

- replaying one event three times yields one mutation and two `duplicate: true`
  responses
- ten concurrent deliveries of one event produce exactly one applied update
- a *new* event id for an already-settled payment is stored with
  `result: payment_already_applied` and changes nothing
- an invalid HMAC signature is rejected with `401` before any write

Set `WEBHOOK_REQUIRE_SIGNATURE=true` and sign the raw body with
`WEBHOOK_SIGNING_SECRET` to require `X-Signature: sha256=<hex hmac>`.

## Payments

No real gateway is involved. `POST /payments/` creates a `PENDING` payment,
calls the mock gateway, and feeds the result through the webhook processor, so
the normal path is exercised in tests and in local development.

`simulate` controls the outcome: `success` (default), `failure`,
`insufficient_funds`, `gateway_error`. Every attempt is stored, which makes the
retry-after-failure flow and its history visible via
`GET /bookings/{id}/payments`.

## Data model

`users`, `refresh_tokens`, `diagnostic_centres`, `diagnostic_tests`,
`centre_tests` (per-centre price and availability), `bookings`, `payments`,
`webhook_events`. UUID primary keys, timezone-aware UTC timestamps, and check
constraints on money, coordinates, durations, and status values.

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
make test                                        # SQLite in-memory, 126 tests
make lint                                        # ruff over app, tests, alembic

# PostgreSQL: the suite creates and drops its own schema, so point it at an
# empty database. docker compose only creates eve_diagnostics, so create the
# test database once:
#   createdb -U eve eve_diagnostics_test
make test-postgres                               # or override the target:
make test-postgres TEST_PG_URL=postgresql+asyncpg://eve@127.0.0.1:5432/eve_diagnostics_test
```

All 126 tests pass on **both** SQLite and PostgreSQL 18. Running them against
PostgreSQL matters: it exercises `SELECT ... FOR UPDATE`, the
`INSERT ... ON CONFLICT DO NOTHING` event claim, and the partial unique index,
none of which SQLite actually enforces. SQLite silently ignores `FOR UPDATE` and
has no partial indexes, so a SQLite-only pass would hide real concurrency bugs.

Coverage includes the full booking and payment state machines, concurrent
webhook delivery, signature verification, ownership and admin authorization,
token rotation and reuse detection, pagination, and cache invalidation.

Concurrency tests use a `concurrent_client` fixture that gives every request
its own session, mirroring production. Sharing one session would both break on
PostgreSQL and fake concurrency on SQLite, where all tasks share one
connection.

Verified against a live PostgreSQL instance:

- 12 concurrent deliveries of one event id: 1 applied, 11 `duplicate: true`,
  1 `webhook_events` row, 1 payment, booking `CONFIRMED`
- inserting a second `SUCCESS` payment directly in SQL is rejected by
  `uq_payments_single_success_per_booking`

## Background worker

Optional Celery worker for work that should not block a request:

```bash
make worker        # celery -A app.worker.celery_app worker -Q payments,default
```

Tasks: `bookings.expire_stale_pending` (age out unpaid bookings),
`payments.replay_webhook` (reprocess a failed event), `webhooks.dispatch`.
Redis backs both the cache and the broker. The API is fully functional without
the worker running.

## Configuration

Settings are environment variables with a `.env` file; see `.env.example`.
The values that matter most:

| Variable                    | Default                                       | Notes                                  |
| --------------------------- | --------------------------------------------- | -------------------------------------- |
| `DATABASE_URL`              | `postgresql+asyncpg://eve:eve@localhost/eve_diagnostics` | SQLite is also supported      |
| `JWT_SECRET_KEY`            | placeholder                                   | **Must** be replaced in production     |
| `WEBHOOK_SIGNING_SECRET`    | `mockpay-webhook-secret`                      | HMAC key for webhook signatures         |
| `WEBHOOK_REQUIRE_SIGNATURE` | `false`                                       | Turn on to enforce `X-Signature`        |
| `REDIS_URL`                 | `redis://localhost:6379/0`                    | Empty falls back to in-process cache    |
| `RATE_LIMIT_REQUESTS`       | `100`                                         | `0` disables rate limiting              |
| `LOG_JSON`                  | `true`                                        | JSON logs for ingestion                 |
| `SEED_ADMIN_EMAIL` / `_PASSWORD` | `admin@eve.health` / `Admin@12345`     | Only used by the seed script            |

## Assumptions

The assignment left several things open. These are the calls I made and why.

1. **A booking must be paid within a window.** Bookings start `PENDING` and are
   not auto-confirmed, because payment is what confirms them. A worker task
   (`bookings.expire_stale_pending`) can age out abandoned ones; nothing in the
   API schedules it, so unpaid bookings stay `PENDING` until something does.
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
   `booking_id`. An unrecognisable event is stored as `IGNORED` rather than
   rejected, so nothing is lost and a retry can still be processed.
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
10. **SQLite is a development convenience, PostgreSQL is the target.** The
    PostgreSQL-only partial index is the real double-capture guard, so PG is the
    only database where the full guarantee is enforced by the database itself.
11. **Rate limiting and caching are per process.** Correct for a single replica,
    documented as a limitation rather than silently assumed away.

## What I would improve with more time

Ordered by value, not by how interesting they are.

1. **Outbox pattern for payment initiation.** Today the payment row and the
   simulated event are written in one transaction, which is fine for an
   in-process mock. Against a real gateway, "create payment" and "receive
   webhook" cannot share a transaction, so a transactional outbox plus a
   delivery worker is the correct shape. This is the highest-value change here
   by a wide margin, and also the largest: a new table, a publisher, and a
   restructuring of the payment transaction. I would not attempt it as a
   timed "small change" exercise. A good live change in this codebase is
   refresh-token reuse detection, which is what I implemented.
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
   `kid` header so old keys can be retired gradually, is the fix. Note this
   replaces the "hash the token at rest" idea I had listed previously: since
   the raw token is never stored, that would have protected nothing. Token
   *families* now provide the session abstraction that item was reaching for.
9. **Contract tests for the webhook schema.** A small JSON Schema plus a
   provider-contract test suite would catch a provider-side change before it
   reaches production.

## Production notes

- Replace `JWT_SECRET_KEY` and `WEBHOOK_SIGNING_SECRET`; set
  `ENVIRONMENT=production`, `DEBUG=false`, and an explicit `CORS_ORIGINS`.
- The PostgreSQL partial unique index is the real guard against double capture;
  keep it when deploying to Postgres.
- Rate limiting and caching are per-process. For a multi-worker deployment,
  move both to Redis or a gateway so limits are global rather than per replica.
- `alembic upgrade head` is the only supported way to create the schema. The
  app never calls `create_all`, and the seed script refuses to run against an
  unmigrated database so migrations stay the single source of truth.
