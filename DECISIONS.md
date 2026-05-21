# cost-router — Decisions

## What was built

A standalone HTTP service that picks a payment connector for each transaction
by filtering on a success-rate floor and ranking the survivors by fee.

- **`config/routing_rules.json`** — array of connectors with `name`, `fee_percent`,
  `min_success_rate`, `current_success_rate`. Single source of truth for routing inputs.
- **`cost-router/router.py`** — pure logic. `load_connectors(path)` and
  `route_payment(payment_id, amount, currency, connectors)`. No I/O beyond the
  config file read; no framework dependencies. Returns a dict; never raises.
- **`cost-router/main.py`** — FastAPI app exposing:
  - `POST /route` — routes a payment, caches the decision by `payment_id`.
  - `GET /routing-trace/{payment_id}` — returns the cached decision (404 if absent).
  - `GET /health` — reports config-load status.
- **`cost-router/test_router.py`** — 19 tests covering happy path, all-FAIL,
  tie-break, config validation, log-line format, and the FastAPI status codes
  (200 / 422 / 404 / 503).
- **Dockerfile + compose entry** — built from `python:3.12-slim`, joined to
  `router_net` so it can reach the Hyperswitch services if a future hop is added,
  exposes 8090 on the host.

## Routing rule

1. Mark each connector PASS if `current_success_rate >= min_success_rate`, else FAIL.
2. If no PASS: return HTTP 422 `{"error": "no_connector_meets_floor", ...}`.
3. Otherwise pick the PASS connector with the lowest `fee_percent`.
4. Tie-break on identical `fee_percent`: highest `current_success_rate` wins.

Implemented as one sort: `key=lambda c: (c.fee_percent, -c.current_success_rate)`.

## Why this architecture

- **Pure logic split from the web layer.** `router.py` knows nothing about
  HTTP, Pydantic, or FastAPI. Every interesting test exercises it directly,
  which keeps the suite fast and the failure messages local. The FastAPI tests
  exist only to verify the wire contract (status codes, response shape, caching).
- **`load_connectors` returns `(value, error)` instead of raising.** "Never
  crash, always return structured error" is the explicit ask; making the loader
  total means the wrapper can convert *any* failure mode into a 503 without a
  try/except funnel.
- **Config is loaded once at startup via FastAPI's lifespan.** Reading on every
  request would add disk I/O to the hot path; reading once keeps the routing
  decision a pure function of in-memory state. Trade-off: edits to
  `routing_rules.json` require a container restart (see "Skipped" below).
- **`JSONResponse` instead of `HTTPException` for the 422.** `HTTPException`
  wraps the body in `{"detail": ...}`. The spec specified a flat
  `{"payment_id", "error", "candidates_evaluated"}` body, so returning
  `JSONResponse` directly matches the contract. One test asserts `"detail" not in body`
  to lock this in.
- **In-memory trace cache.** Simple `dict[str, decision]` keyed by `payment_id`.
  Lost on restart, not shared across replicas — acceptable for a single-node
  service whose authoritative state is the log line, not the cache.
- **Log format is the contract.** `router.py` emits exactly the three
  `[ROUTING] ...` lines from the spec via `logging.info`. A test asserts the
  literal substrings (`razorpay(fee=2.0%,sr=0.91,floor=0.90,PASS)`) so reformatting
  is a breaking change.

## What was skipped

- **Hot reload of `routing_rules.json`.** No file watcher; updates need a
  container restart. A `POST /reload` endpoint or `mtime` check would close this.
- **Persistent trace storage.** Cache is process-local. A real deployment
  would write decisions to Postgres or a log aggregator.
- **Connector health probing.** `current_success_rate` is read from config
  as a static number. In production this would come from a rolling window
  fed by actual outcomes — out of scope here.
- **Auth.** The endpoints are open. Behind a private network in the compose
  setup; would need a token or mTLS to expose externally.
- **Metrics.** No Prometheus counters / histograms. Routing decisions could
  usefully emit `routing_decisions_total{selected, reason}`.
- **Integration with the Hyperswitch router itself.** This service decides
  *which* connector to call but doesn't hand the decision back to the
  Hyperswitch payment flow. That handoff is the obvious next piece of work.
- **Currency-aware routing.** `amount` and `currency` are logged but not
  used to filter. The connector config has no currency-eligibility field yet.

## What is hacky

- **Tests reload `main` via `importlib.reload`** inside fixtures so each one
  can use a different config file via `ROUTING_RULES_PATH`. Cleaner would
  be dependency injection — make `load_connectors` an FastAPI dependency
  the test can override. Reload works and is contained to the fixture.
- **Log format-string assertions are brittle.** The test asserts substrings
  like `razorpay(fee=2.0%,sr=0.91,floor=0.90,PASS)`. If `:.2f` formatting
  is ever changed to `:.3f`, the test breaks even though the decision is
  identical. Acceptable price for locking the spec'd format.
- **`Candidate` is a dataclass with a `to_dict()` method instead of using
  Pydantic.** Pydantic is already a dep (via FastAPI), so this is a tiny
  inconsistency. Kept the dataclass because `router.py` is supposed to be
  framework-free.
- **`load_connectors` returns `tuple[..., None]` rather than a result type.**
  Python doesn't ship a sum type; `(value, error)` is the closest idiomatic
  thing without pulling in `returns` or `result`.

## What I'd do with 4 more hours

1. **Wire the decision into the Hyperswitch payment flow.** Add a shim in
   `crates/router` that calls `POST /route` before connector selection and
   uses `selected_connector` (with a fast-path bypass if cost-router is
   unreachable). That's the actual product value; everything above is plumbing.
2. **Replace the static `current_success_rate` with a rolling-window probe.**
   Subscribe to payment outcomes from Redis pub/sub (already in the stack
   for `hyperswitch_invalidate`) and maintain a 5-minute rolling rate per
   connector. Persist to Postgres so restarts don't lose history.
3. **Add `/reload` and config hot-swap** so operators can adjust floors
   without a container restart. Validate before swap; reject on parse error.
4. **Currency / amount filters in the connector config.** Add optional
   `supported_currencies`, `min_amount`, `max_amount` fields and filter
   candidates accordingly before the PASS/FAIL step.
5. **Metrics + structured logs.** Replace the format-string log with a
   JSON line (already what Hyperswitch emits) and add Prometheus counters
   for selections, failures, and per-connector latencies.
6. **A `pytest -q` mode + GitHub Actions workflow** so the existing
   `cd cost-router && pytest test_router.py -v` runs on every PR.
