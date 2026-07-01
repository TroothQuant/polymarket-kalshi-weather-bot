"""Gamma-window fix (2026-07-01): newest-events-first ordering + stale-date skip,
so an unresolved-event backlog can't push the current day's event past limit=10."""
import asyncio
from datetime import date, timedelta

import backend.data.weather_markets as wm


def _q(d):
    return {"question": f"Will the highest temperature in NYC be 90F or higher on {d.strftime('%B %d, %Y')}?",
            "outcomePrices": '["0.30", "0.70"]', "clobTokenIds": '["ytok", "ntok"]',
            "conditionId": "0xABC"}


def test_parse_skips_stale_keeps_current():
    stale = date.today() - timedelta(days=5)
    future = date.today() + timedelta(days=1)
    assert wm._parse_polymarket_weather(_q(stale), "slug") is None          # past date dropped
    assert wm._parse_polymarket_weather(_q(future), "slug") is not None      # current/future kept
    assert wm._parse_polymarket_weather(_q(date.today()), "slug") is not None  # today kept


def test_fetch_requests_newest_first():
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return []

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            captured.update(params or {})
            return _Resp()

    orig = wm.httpx.AsyncClient
    wm.httpx.AsyncClient = lambda *a, **k: _Client()
    try:
        asyncio.run(wm.fetch_polymarket_weather_markets(["nyc"]))
    finally:
        wm.httpx.AsyncClient = orig
    assert captured.get("order") == "id"
    assert str(captured.get("ascending")).lower() == "false"
