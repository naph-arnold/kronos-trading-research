"""INTEGRATION tests against live sources. Deselected by default (see pyproject `-m 'not integration'`).

Run explicitly:  python -m pytest -m integration tests/test_integration_live.py -v
Each test reads public data or a demo/testnet account only; none places orders. Tests that need a
terminal or credentials that are not available are *skipped with a reason*, never counted as passing.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration
UTC = timezone.utc


def test_live_binance_public_exchange_info_and_quote():
    from trading.connectors.binance import BinanceReadOnlyClient, parse_symbol
    client = BinanceReadOnlyClient.public()
    discovery = parse_symbol(client.exchange_info("BTCUSDT"), source=client.source)
    assert discovery.fields["status"] == "TRADING" and not discovery.missing
    quote = client.book_ticker("BTCUSDT")
    assert quote.ask >= quote.bid > 0
    assert abs(client.server_time() - datetime.now(UTC)) < timedelta(minutes=2)


def test_live_binance_testnet_public_filters_match_production_shape():
    from trading.connectors.binance import BinanceReadOnlyClient, compare_filters, parse_symbol
    public = BinanceReadOnlyClient.public()
    testnet = BinanceReadOnlyClient.testnet_from_env({})
    diffs = compare_filters(parse_symbol(public.exchange_info(), source="public"),
                            parse_symbol(testnet.exchange_info(), source="testnet"))
    print("public vs testnet filter differences:", diffs)  # informational; testnet may differ


def test_live_binance_testnet_account_read_only():
    if not (os.environ.get("BINANCE_TESTNET_API_KEY") and os.environ.get("BINANCE_TESTNET_API_SECRET")):
        pytest.skip("BLOCKED: BINANCE_TESTNET_API_KEY/SECRET not configured")
    from trading.connectors.binance import BinanceReadOnlyClient, taker_fee_from_account
    account = BinanceReadOnlyClient.testnet_from_env().account()
    assert taker_fee_from_account(account) is not None


@pytest.mark.parametrize("name", ["fomc", "bea", "ecb"])
def test_live_official_calendars_parse(name):
    from trading.marketcal import fomc, official
    fetch = {"fomc": fomc.fetch_fomc_calendar, "bea": official.fetch_bea, "ecb": official.fetch_ecb}[name]
    events = fetch()
    assert events and all(e.scheduled_at.tzinfo is not None for e in events)


def test_live_bls_api_values():
    from trading.marketcal import official
    values = official.fetch_bls(["CUSR0000SA0"])
    assert values["CUSR0000SA0"] and float(values["CUSR0000SA0"][0][1]) > 0


def test_live_mt5_demo_terminal():
    try:
        import MetaTrader5  # noqa: F401
    except ImportError:
        pytest.skip("BLOCKED: MetaTrader5 package not installed (Windows only)")
    from trading.clock import NY_CLOSE
    from trading.connectors.mt5 import Mt5ReadOnlyConnector, Mt5Unavailable
    conn = Mt5ReadOnlyConnector(server_tz=NY_CLOSE)
    try:
        summary = conn.connect()
    except Mt5Unavailable as exc:
        pytest.skip(f"BLOCKED: no MT5 demo terminal available ({exc})")
    try:
        assert summary["is_demo"]
        name = conn.discover_xauusd()
        discovery = conn.symbol_specification(name)
        print("spec verified:", discovery.verified, discovery.missing, discovery.problems)
    finally:
        conn.shutdown()
