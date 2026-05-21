"""Pure routing logic — no FastAPI, no I/O beyond config file load.

Public surface:
  load_connectors(path) -> (connectors, error)
  route_payment(payment_id, amount, currency, connectors) -> decision dict

The decision dict is the single source of truth: main.py serializes it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = {"name", "fee_percent", "min_success_rate", "current_success_rate"}


@dataclass
class Candidate:
    name: str
    fee_percent: float
    current_success_rate: float
    min_success_rate: float
    status: str  # "PASS" or "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fee_percent": self.fee_percent,
            "current_success_rate": self.current_success_rate,
            "min_success_rate": self.min_success_rate,
            "status": self.status,
        }


def load_connectors(config_path: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Read and validate the routing rules file.

    Returns (connectors, None) on success or (None, error_code) on any failure.
    Never raises — graceful degradation per spec.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, f"config_not_found: {config_path}"
    except PermissionError:
        return None, f"config_permission_denied: {config_path}"
    except json.JSONDecodeError as e:
        return None, f"config_invalid_json: {e}"
    except OSError as e:
        return None, f"config_read_error: {e}"

    if not isinstance(data, dict) or "connectors" not in data:
        return None, "config_missing_connectors_key"

    connectors = data["connectors"]
    if not isinstance(connectors, list):
        return None, "config_connectors_not_list"
    if not connectors:
        return None, "config_connectors_empty"

    for i, c in enumerate(connectors):
        if not isinstance(c, dict):
            return None, f"config_connector_{i}_not_object"
        missing = REQUIRED_FIELDS - c.keys()
        if missing:
            return None, f"config_connector_{i}_missing_fields: {sorted(missing)}"
        for num_field in ("fee_percent", "min_success_rate", "current_success_rate"):
            if not isinstance(c[num_field], (int, float)) or isinstance(c[num_field], bool):
                return None, f"config_connector_{i}_field_{num_field}_not_number"
        if not isinstance(c["name"], str) or not c["name"]:
            return None, f"config_connector_{i}_name_invalid"

    return connectors, None


def _format_candidate_log(c: Candidate) -> str:
    return (
        f"{c.name}(fee={c.fee_percent}%,"
        f"sr={c.current_success_rate:.2f},"
        f"floor={c.min_success_rate:.2f},"
        f"{c.status})"
    )


def route_payment(
    payment_id: str,
    amount: float,
    currency: str,
    connectors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pick the cheapest connector whose current success rate meets its floor.

    Tie-break on identical fee_percent: higher current_success_rate wins.
    All-FAIL: returns status="error" with error="no_connector_meets_floor".
    Empty connectors: returns status="error" with error="no_connectors_configured".
    """
    logger.info(
        "[ROUTING] payment_id=%s amount=%s currency=%s",
        payment_id, amount, currency,
    )

    if not connectors:
        logger.info("[ROUTING] candidates=[]")
        logger.info("[ROUTING] selected=none reason=no_connectors_configured")
        return {
            "status": "error",
            "payment_id": payment_id,
            "error": "no_connectors_configured",
            "candidates_evaluated": [],
        }

    candidates = [
        Candidate(
            name=c["name"],
            fee_percent=c["fee_percent"],
            current_success_rate=c["current_success_rate"],
            min_success_rate=c["min_success_rate"],
            status="PASS" if c["current_success_rate"] >= c["min_success_rate"] else "FAIL",
        )
        for c in connectors
    ]

    logger.info(
        "[ROUTING] candidates=[%s]",
        ", ".join(_format_candidate_log(c) for c in candidates),
    )

    passers = [c for c in candidates if c.status == "PASS"]
    if not passers:
        logger.info("[ROUTING] selected=none reason=no_connector_meets_floor")
        return {
            "status": "error",
            "payment_id": payment_id,
            "error": "no_connector_meets_floor",
            "candidates_evaluated": [c.to_dict() for c in candidates],
        }

    # Cheapest fee first; tie-break by higher current_success_rate.
    passers.sort(key=lambda c: (c.fee_percent, -c.current_success_rate))
    selected = passers[0]

    logger.info(
        "[ROUTING] selected=%s reason=cheapest_above_floor",
        selected.name,
    )

    return {
        "status": "ok",
        "payment_id": payment_id,
        "selected_connector": selected.name,
        "fee_percent": selected.fee_percent,
        "reason": "cheapest_above_floor",
        "candidates_evaluated": [c.to_dict() for c in candidates],
    }
