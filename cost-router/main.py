"""FastAPI wrapper around router.route_payment.

Endpoints:
  POST /route                        -> route a payment, cache the decision
  GET  /routing-trace/{payment_id}   -> return the cached decision
  GET  /health                       -> liveness + config-load status

Config is loaded once at startup via lifespan; trace cache is in-memory.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from router import load_connectors, route_payment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


class RouteRequest(BaseModel):
    payment_id: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)
    currency: str = Field(..., min_length=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_path = os.environ.get("ROUTING_RULES_PATH", "/config/routing_rules.json")
    connectors, err = load_connectors(config_path)
    if err:
        logger.error("[ROUTING] config_load_failed path=%s error=%s", config_path, err)
        app.state.connectors = None
        app.state.config_error = err
    else:
        logger.info(
            "[ROUTING] config_loaded path=%s connectors=%d",
            config_path, len(connectors),
        )
        app.state.connectors = connectors
        app.state.config_error = None
    app.state.config_path = config_path
    app.state.trace_cache: dict[str, dict[str, Any]] = {}
    yield


app = FastAPI(title="cost-router", lifespan=lifespan)


@app.post("/route")
async def route(req: RouteRequest):
    if app.state.connectors is None:
        return JSONResponse(
            status_code=503,
            content={
                "payment_id": req.payment_id,
                "error": "config_unavailable",
                "reason": app.state.config_error,
            },
        )

    result = route_payment(
        req.payment_id, req.amount, req.currency, app.state.connectors,
    )
    app.state.trace_cache[req.payment_id] = result

    if result["status"] == "error":
        return JSONResponse(
            status_code=422,
            content={
                "payment_id": result["payment_id"],
                "error": result["error"],
                "candidates_evaluated": result["candidates_evaluated"],
            },
        )

    return {
        "payment_id": result["payment_id"],
        "selected_connector": result["selected_connector"],
        "fee_percent": result["fee_percent"],
        "reason": result["reason"],
        "candidates_evaluated": result["candidates_evaluated"],
    }


@app.get("/routing-trace/{payment_id}")
async def trace(payment_id: str):
    cached = app.state.trace_cache.get(payment_id)
    if cached is None:
        return JSONResponse(
            status_code=404,
            content={"error": "trace_not_found", "payment_id": payment_id},
        )
    return cached


@app.get("/health")
async def health():
    if app.state.connectors is None:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "error": app.state.config_error},
        )
    return {
        "status": "ok",
        "config_path": app.state.config_path,
        "connectors_loaded": len(app.state.connectors),
    }
