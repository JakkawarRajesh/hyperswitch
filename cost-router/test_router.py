"""Tests for cost-router. Run from cost-router/:  pytest test_router.py -v"""
from __future__ import annotations

import importlib
import json
import logging

import pytest
from fastapi.testclient import TestClient

from router import load_connectors, route_payment


SAMPLE_CONNECTORS = [
    {"name": "razorpay", "fee_percent": 2.0, "min_success_rate": 0.90, "current_success_rate": 0.91},
    {"name": "stripe",   "fee_percent": 2.9, "min_success_rate": 0.95, "current_success_rate": 0.97},
    {"name": "adyen",    "fee_percent": 1.5, "min_success_rate": 0.92, "current_success_rate": 0.85},
]


# ---------- pure router.py tests ----------

def test_happy_path_picks_cheapest_above_floor():
    r = route_payment("pay_001", 5000, "INR", SAMPLE_CONNECTORS)
    assert r["status"] == "ok"
    assert r["selected_connector"] == "razorpay"
    assert r["fee_percent"] == 2.0
    assert r["reason"] == "cheapest_above_floor"


def test_candidate_statuses_pass_fail_correctly():
    r = route_payment("pay_002", 5000, "INR", SAMPLE_CONNECTORS)
    statuses = {c["name"]: c["status"] for c in r["candidates_evaluated"]}
    assert statuses == {"razorpay": "PASS", "stripe": "PASS", "adyen": "FAIL"}


def test_adyen_is_cheaper_but_fails_so_not_selected():
    # Adyen has fee=1.5% (cheapest) but sr=0.85 < floor=0.92.
    # Regression guard: ensure we never select on fee alone.
    r = route_payment("pay_003", 100, "USD", SAMPLE_CONNECTORS)
    assert r["selected_connector"] != "adyen"


def test_all_fail_returns_structured_error_not_crash():
    cons = [
        {"name": "a", "fee_percent": 1.0, "min_success_rate": 0.99, "current_success_rate": 0.50},
        {"name": "b", "fee_percent": 2.0, "min_success_rate": 0.99, "current_success_rate": 0.80},
    ]
    r = route_payment("pay_x", 1000, "USD", cons)
    assert r["status"] == "error"
    assert r["error"] == "no_connector_meets_floor"
    assert "selected_connector" not in r
    assert len(r["candidates_evaluated"]) == 2
    assert all(c["status"] == "FAIL" for c in r["candidates_evaluated"])


def test_tie_break_prefers_higher_current_success_rate():
    cons = [
        {"name": "low_sr",  "fee_percent": 2.0, "min_success_rate": 0.90, "current_success_rate": 0.91},
        {"name": "high_sr", "fee_percent": 2.0, "min_success_rate": 0.90, "current_success_rate": 0.99},
    ]
    r = route_payment("pay_tie", 1000, "USD", cons)
    assert r["selected_connector"] == "high_sr"


def test_empty_connector_list_returns_error():
    r = route_payment("pay_empty", 1, "USD", [])
    assert r["status"] == "error"
    assert r["error"] == "no_connectors_configured"


def test_decision_driven_by_config_not_hardcoded(tmp_path):
    # Different config -> different winner. Proves nothing is hardcoded.
    cfg = {"connectors": [
        {"name": "only_one", "fee_percent": 5.0, "min_success_rate": 0.5, "current_success_rate": 0.99},
    ]}
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(cfg))
    cons, err = load_connectors(str(p))
    assert err is None
    r = route_payment("pay_c", 100, "USD", cons)
    assert r["selected_connector"] == "only_one"
    assert r["fee_percent"] == 5.0


def test_load_connectors_missing_file():
    cons, err = load_connectors("/nonexistent/path.json")
    assert cons is None
    assert err.startswith("config_not_found")


def test_load_connectors_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not valid json {{{")
    cons, err = load_connectors(str(p))
    assert cons is None
    assert err.startswith("config_invalid_json")


def test_load_connectors_missing_required_fields(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"connectors": [{"name": "incomplete"}]}))
    cons, err = load_connectors(str(p))
    assert cons is None
    assert "missing_fields" in err


def test_load_connectors_wrong_type_for_numeric_field(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"connectors": [{
        "name": "x", "fee_percent": "two", "min_success_rate": 0.9, "current_success_rate": 0.95,
    }]}))
    cons, err = load_connectors(str(p))
    assert cons is None
    assert "fee_percent_not_number" in err


def test_three_log_lines_emitted_in_spec_format(caplog):
    with caplog.at_level(logging.INFO, logger="router"):
        route_payment("pay_log", 5000, "INR", SAMPLE_CONNECTORS)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[ROUTING] payment_id=pay_log amount=5000 currency=INR" in m for m in msgs)
    assert any(
        "candidates=" in m
        and "razorpay(fee=2.0%,sr=0.91,floor=0.90,PASS)" in m
        and "adyen(fee=1.5%,sr=0.85,floor=0.92,FAIL)" in m
        for m in msgs
    )
    assert any("selected=razorpay reason=cheapest_above_floor" in m for m in msgs)


# ---------- FastAPI integration tests ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """Fresh main.app with an isolated config file, per test."""
    cfg = {"connectors": SAMPLE_CONNECTORS}
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("ROUTING_RULES_PATH", str(p))
    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def test_post_route_success_200(client):
    r = client.post("/route", json={"payment_id": "pay_api_1", "amount": 5000, "currency": "INR"})
    assert r.status_code == 200
    body = r.json()
    assert body["payment_id"] == "pay_api_1"
    assert body["selected_connector"] == "razorpay"
    assert body["fee_percent"] == 2.0
    assert body["reason"] == "cheapest_above_floor"
    assert len(body["candidates_evaluated"]) == 3


def test_post_route_all_fail_returns_422(tmp_path, monkeypatch):
    cfg = {"connectors": [
        {"name": "x", "fee_percent": 1.0, "min_success_rate": 0.99, "current_success_rate": 0.50},
    ]}
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("ROUTING_RULES_PATH", str(p))
    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        r = c.post("/route", json={"payment_id": "pay_fail", "amount": 100, "currency": "USD"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "no_connector_meets_floor"
    assert body["payment_id"] == "pay_fail"
    # 422 body must be flat — not wrapped in {"detail": ...}
    assert "detail" not in body


def test_post_route_invalid_payload_returns_422(client):
    r = client.post("/route", json={"payment_id": "", "amount": -1, "currency": ""})
    assert r.status_code == 422


def test_trace_returns_cached_decision(client):
    client.post("/route", json={"payment_id": "pay_trace", "amount": 5000, "currency": "INR"})
    r = client.get("/routing-trace/pay_trace")
    assert r.status_code == 200
    body = r.json()
    assert body["selected_connector"] == "razorpay"


def test_trace_unknown_payment_returns_404(client):
    r = client.get("/routing-trace/unknown_payment")
    assert r.status_code == 404
    assert r.json()["error"] == "trace_not_found"


def test_health_ok_when_config_loaded(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["connectors_loaded"] == 3


def test_health_degraded_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTING_RULES_PATH", str(tmp_path / "does_not_exist.json"))
    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        r = c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
