# Hyperswitch — cost-aware routing fork

This fork adds **`cost-router/`**, a standalone FastAPI microservice that picks the cheapest payment connector whose current success rate meets its configured floor. It runs alongside the standard Hyperswitch stack on port `8090`, exposes `POST /route` for routing decisions and `GET /routing-trace/{payment_id}` for inspecting cached traces, and emits a three-line `[ROUTING]` log per decision. Connector data is config-driven via `config/routing_rules.json` — nothing is hardcoded.

The upstream README is preserved in git history (see commit `4f50a8f6f` and earlier).

## Prerequisites

- **Docker Desktop** running (Linux containers; WSL2 backend on Windows)
- **Git**

## Quickstart

```bash
git clone https://github.com/JakkawarRajesh/hyperswitch
cd hyperswitch
git checkout cost-aware-routing
docker compose up -d
# Wait ~2 minutes for all services to become healthy
curl http://localhost:8080/health    # hyperswitch-server
curl http://localhost:8090/health    # cost-router
```

Both should return `200`. The `cost-router` health response includes `connectors_loaded: 3`.

## Test a payment

### Happy path — razorpay selected (cheapest above floor)

```bash
curl -sS -X POST http://localhost:8090/route \
  -H 'Content-Type: application/json' \
  -d '{"payment_id":"pay_001","amount":5000,"currency":"INR"}'
```

Expected response (`HTTP 200`):

```json
{
  "payment_id": "pay_001",
  "selected_connector": "razorpay",
  "fee_percent": 2.0,
  "reason": "cheapest_above_floor",
  "candidates_evaluated": [
    {"name": "razorpay", "fee_percent": 2.0, "current_success_rate": 0.91, "min_success_rate": 0.9, "status": "PASS"},
    {"name": "stripe",   "fee_percent": 2.9, "current_success_rate": 0.97, "min_success_rate": 0.95, "status": "PASS"},
    {"name": "adyen",    "fee_percent": 1.5, "current_success_rate": 0.85, "min_success_rate": 0.92, "status": "FAIL"}
  ]
}
```

Note that adyen has the lowest fee but fails its floor, so it is never selected.

### All-fail — no connector meets its floor

Drop every `current_success_rate` below its `min_success_rate` in `config/routing_rules.json`, then `docker compose restart cost-router`. Now:

```bash
curl -sS -X POST http://localhost:8090/route \
  -H 'Content-Type: application/json' \
  -d '{"payment_id":"pay_fail","amount":5000,"currency":"INR"}'
```

Returns `HTTP 422`:

```json
{
  "payment_id": "pay_fail",
  "error": "no_connector_meets_floor",
  "candidates_evaluated": [ /* all three with "status": "FAIL" */ ]
}
```

Restore the original values and `docker compose restart cost-router` to return to the happy path.

### Routing trace — inspect a past decision

```bash
curl http://localhost:8090/routing-trace/pay_001
```

Returns the cached decision dict for that `payment_id`, or `HTTP 404` if unknown.

## Run tests

```bash
cd cost-router
pip install -r requirements.txt
pytest test_router.py -v
```

19 tests cover happy path, all-fail, tie-break, config validation (missing file / bad JSON / wrong types), the three log lines verbatim, and FastAPI 200 / 422 / 404 / 503 paths.

## How routing works

For each request, every connector is marked **PASS** if `current_success_rate >= min_success_rate`, otherwise **FAIL**. The cheapest **PASS** connector by `fee_percent` wins; ties on fee are broken by higher `current_success_rate`. If no connector passes its floor, the service returns `HTTP 422` with `error: no_connector_meets_floor` — never crashes, always returns a structured response.

## Project structure

```
hyperswitch/
├── cost-router/
│   ├── router.py           # pure routing logic — no HTTP, no I/O beyond config load
│   ├── main.py             # FastAPI app: POST /route, GET /routing-trace/{id}, /health
│   ├── test_router.py      # 19 tests (pytest)
│   ├── requirements.txt    # fastapi, uvicorn, pydantic, pytest, httpx
│   ├── Dockerfile          # python:3.12-slim, exposes 8090
│   └── .gitignore
├── config/
│   └── routing_rules.json  # connector definitions: name, fee_percent, min/current SR
├── docker-compose.yml      # cost-router service joins router_net alongside hyperswitch-server
└── DECISIONS.md            # architecture rationale, what was skipped, follow-ups
```
