import time
import pytest
from fastapi.testclient import TestClient
import main


@pytest.fixture(autouse=True)
def clear_cache():
    main._cache.update({"at": None, "data": None, "source": None, "error": None})
    yield
    main._cache.update({"at": None, "data": None, "source": None, "error": None})


def test_health_is_live_without_upstream():
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_normalize_rejects_missing_symbol():
    assert main.normalize({"lastPrice": 12}) is None


def test_normalize_never_divides_by_zero():
    item = main.normalize({"symbol": "TEST", "lastPrice": 12, "priceYesterday": 0})
    assert item is not None
    assert item["change_pct"] is None


def test_normalize_calculates_change_with_valid_reference():
    item = main.normalize({"symbol": "TEST", "lastPrice": 110, "priceYesterday": 100})
    assert item["change_pct"] == pytest.approx(10.0)


def test_normalize_rejects_infinite_numbers():
    item = main.normalize({"symbol": "TEST", "lastPrice": "inf", "priceYesterday": 10})
    assert item["last"] is None
    assert item["change_pct"] is None


def test_legacy_parser_empty_payload():
    assert main.parse_legacy_marketwatch("not a market watch") == []


def test_legacy_parser_parses_rows():
    # documented legacy layout: symbol/name, closing, last, volume, value, previous, EPS
    row = ["123", "isin", "ABC", "Company", "0", "0", "105", "110", "1", "200", "3000", "90", "120", "100", "5"]
    parsed = main.parse_legacy_marketwatch("header@header@" + ",".join(row) + ";")
    assert len(parsed) == 1
    assert parsed[0]["symbol"] == "ABC"
    assert parsed[0]["change_pct"] == pytest.approx(10.0)


def test_fundamentals_and_history_fail_closed():
    with TestClient(main.app) as client:
        hist = client.get("/api/history/ABC")
        fundamentals = client.get("/api/fundamentals/ABC")
    assert hist.status_code == 200 and hist.json()["status"] == "INSUFFICIENT DATA"
    assert fundamentals.status_code == 200 and fundamentals.json()["status"] == "INSUFFICIENT DATA"


@pytest.mark.asyncio
async def test_market_offline_does_not_invent_rows(monkeypatch):
    async def fake_get():
        return None, None, "UPSTREAM_UNAVAILABLE", "OFFLINE"
    monkeypatch.setattr(main, "get_market_data", fake_get)
    with TestClient(main.app) as client:
        response = client.get("/api/market")
    assert response.status_code == 503
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_signal_is_insufficient_without_data(monkeypatch):
    async def fake_get():
        return None, None, "UPSTREAM_UNAVAILABLE", "OFFLINE"
    monkeypatch.setattr(main, "get_market_data", fake_get)
    with TestClient(main.app) as client:
        response = client.get("/api/signals/ABC")
    assert response.status_code == 200
    assert response.json()["signal"] == "INSUFFICIENT DATA"


def test_limit_validation():
    with TestClient(main.app) as client:
        response = client.get("/api/market?limit=0")
    assert response.status_code == 422


def test_status_reports_offline_before_first_fetch():
    with TestClient(main.app) as client:
        response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["status"] == "OFFLINE"
