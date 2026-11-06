"""Read-only MT5 and Binance connectors (fakes; no terminal, network or credentials needed)."""
from __future__ import annotations

import calendar
import json
from collections import namedtuple
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from trading.clock import NY_CLOSE, ServerClockError
from trading.common import ValidationError
from trading.config import LiveTradingForbidden
from trading.connectors.binance import (PUBLIC_DATA_URL, BinanceReadOnlyClient, BinanceUnavailable,
                                        compare_filters, parse_symbol, taker_fee_from_account)
from trading.connectors.mt5 import Mt5ReadOnlyConnector, Mt5Unavailable, ReadOnlyMt5, spread_statistics

UTC = timezone.utc
Account = namedtuple("Account", "login trade_mode currency server company leverage equity balance margin_free "
                                "trade_allowed")
Terminal = namedtuple("Terminal", "connected build trade_allowed")
Symbol = namedtuple("Symbol", "name currency_base currency_profit")
Tick = namedtuple("Tick", "time time_msc bid ask")


def server_epoch(*args) -> int:
    return calendar.timegm(datetime(*args).timetuple())


class FakeMt5:
    """Minimal stand-in for the MetaTrader5 module."""

    TIMEFRAME_M15, TIMEFRAME_H1, ORDER_TYPE_BUY = 15, 16385, 0

    def __init__(self, *, trade_mode=0, symbols=("XAUUSD", "EURUSD"), connected=True, symbol_info=None,
                 rates=None, tick=None):
        self.trade_mode, self.connected = trade_mode, connected
        self.symbols = [Symbol(n, "XAU" if n.startswith(("XAU", "GOLD")) else "EUR", "USD") for n in symbols]
        self.info = symbol_info
        self.rates = rates or []
        self.tick = tick
        self.calls = []
        self.shutdown_called = False

    def initialize(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        return True

    def last_error(self):
        return (1, "ok")

    def shutdown(self):
        self.shutdown_called = True

    def account_info(self):
        return Account(123, self.trade_mode, "USD", "Broker-Demo", "Broker Ltd", 100, 10000.0, 10000.0, 9000.0, True)

    def terminal_info(self):
        return Terminal(self.connected, 4000, True)

    def symbols_get(self):
        return self.symbols

    def symbol_select(self, name, enable):
        return True

    def symbol_info(self, name):
        return self.info

    def symbol_info_tick(self, name):
        return self.tick

    def order_calc_margin(self, order_type, name, volume, price):
        return 2650.0

    def order_calc_profit(self, order_type, name, volume, price_open, price_close):
        contract = (self.info or {}).get("trade_contract_size", 100.0)
        return (price_close - price_open) * volume * contract

    def copy_rates_range(self, name, timeframe, start, end):
        self.calls.append(("copy_rates_range", start, end))
        return self.rates

    def order_send(self, request):  # must never be reachable
        raise AssertionError("order function reached")


FULL_INFO = {"trade_contract_size": 100.0, "trade_tick_size": 0.01, "trade_tick_value": 1.0,
             "trade_tick_value_loss": 1.0, "point": 0.01, "digits": 2, "volume_min": 0.01, "volume_max": 50.0,
             "volume_step": 0.01, "currency_base": "XAU", "currency_profit": "USD", "currency_margin": "XAU",
             "order_mode": 127, "filling_mode": 3, "trade_stops_level": 0, "trade_freeze_level": 0, "spread": 18,
             "spread_float": True, "trade_mode": 4, "description": "Gold vs US Dollar", "path": "Metals\\XAUUSD"}


def connect(fake, commission=D("7")):
    conn = Mt5ReadOnlyConnector(server_tz=NY_CLOSE, module=fake, commission_per_lot_round_trip=commission)
    summary = conn.connect()
    return conn, summary


def test_proxy_allows_reads_and_blocks_everything_else():
    proxy = ReadOnlyMt5(FakeMt5())
    assert proxy.account_info().trade_mode == 0 and proxy.TIMEFRAME_M15 == 15
    for name in ("order_" + "send", "order_check", "positions_close", "anything_new"):
        with pytest.raises(PermissionError):
            getattr(proxy, name)
    with pytest.raises(PermissionError):
        proxy.x = 1


def test_connect_requires_demo_and_shuts_down_otherwise():
    conn, summary = connect(FakeMt5())
    assert summary["is_demo"] and "password" not in json.dumps(summary)
    real = FakeMt5(trade_mode=2)
    with pytest.raises(LiveTradingForbidden):
        connect(real)
    assert real.shutdown_called


def test_disconnected_terminal_is_unavailable():
    with pytest.raises(Mt5Unavailable):
        connect(FakeMt5(connected=False))
    fake = FakeMt5()
    conn, _ = connect(fake)
    fake.connected = False
    with pytest.raises(Mt5Unavailable):
        conn.discover_xauusd()


def test_credentials_never_in_repr():
    conn = Mt5ReadOnlyConnector(server_tz=NY_CLOSE, module=FakeMt5(), login=1, password="s3cret", server="x")
    assert "s3cret" not in repr(conn)


@pytest.mark.parametrize("symbols,expected", [(("XAUUSD", "EURUSD"), "XAUUSD"), (("XAUUSD.m", "EURUSD"), "XAUUSD.m"),
                                              (("GOLD", "EURUSD"), "GOLD")])
def test_symbol_discovery(symbols, expected):
    conn, _ = connect(FakeMt5(symbols=symbols))
    assert conn.discover_xauusd() == expected


def test_ambiguous_symbol_fails_closed():
    conn, _ = connect(FakeMt5(symbols=("XAUUSD.a", "XAUUSD.b")))
    with pytest.raises(ValidationError):
        conn.discover_xauusd()


def test_complete_spec_becomes_instrument_spec():
    fake = FakeMt5(symbol_info=FULL_INFO, tick=Tick(0, server_epoch(2026, 11, 6, 16) * 1000, 2650.0, 2650.18))
    conn, _ = connect(fake)
    discovery = conn.symbol_specification("XAUUSD")
    assert discovery.verified, (discovery.missing, discovery.problems)
    spec = discovery.to_instrument_spec()
    assert spec.tick_value_loss == D("1.0") and spec.margin_per_lot == D("2650.0")
    assert set(discovery.fields["order_types"]) >= {"market", "sl", "tp"}


def test_missing_commission_or_restricted_symbol_is_unverified():
    fake = FakeMt5(symbol_info=FULL_INFO, tick=Tick(0, 1, 2650.0, 2650.18))
    conn, _ = connect(fake, commission=None)
    discovery = conn.symbol_specification("XAUUSD")
    assert not discovery.verified and any("commission" in m for m in discovery.missing)
    with pytest.raises(ValidationError):
        discovery.to_instrument_spec()
    restricted = FakeMt5(symbol_info={**FULL_INFO, "trade_mode": 3, "trade_tick_value_loss": 0},
                         tick=Tick(0, 1, 2650.0, 2650.18))
    conn2, _ = connect(restricted)
    d2 = conn2.symbol_specification("XAUUSD")
    assert "trade_tick_value_loss" in d2.missing and any("close_only" in p for p in d2.problems)


def test_history_converts_server_time_and_preserves_raw():
    rows = [{"time": server_epoch(2026, 11, 6, 16, 0), "open": 2650.0, "high": 2651.0, "low": 2649.0,
             "close": 2650.5, "tick_volume": 120, "spread": 18},
            {"time": server_epoch(2026, 11, 6, 16, 15), "open": 2650.5, "high": 2652.0, "low": 2650.0,
             "close": 2651.5, "tick_volume": 90, "spread": 25}]
    fake = FakeMt5(rates=rows)
    conn, _ = connect(fake)
    bars = conn.history("XAUUSD", "15m", datetime(2026, 11, 6, 14, tzinfo=UTC), datetime(2026, 11, 6, 15, tzinfo=UTC))
    assert bars[0].bar.open_time == datetime(2026, 11, 6, 14, tzinfo=UTC)
    assert bars[0].server_time == rows[0]["time"] and bars[1].spread_points == 25
    _, start, end = fake.calls[-1]
    assert start.hour == 16 and end.hour == 17  # request bounds sent in server time
    stats = spread_statistics(bars, D("0.01"))
    assert stats["max"] == "0.25"


def test_history_with_ambiguous_server_time_rejected():
    rows = [{"time": server_epoch(2026, 11, 1, 8, 30), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
             "tick_volume": 1, "spread": 1}]
    conn, _ = connect(FakeMt5(rates=rows))
    with pytest.raises(ServerClockError):
        conn.history("XAUUSD", "15m", datetime(2026, 11, 1, 5, tzinfo=UTC), datetime(2026, 11, 1, 7, tzinfo=UTC))


def test_server_time_verification_and_stale_quote():
    now = datetime(2026, 11, 6, 14, 0, 1, tzinfo=UTC)
    fresh = Tick(0, server_epoch(2026, 11, 6, 16) * 1000, 2650.0, 2650.2)
    conn, _ = connect(FakeMt5(tick=fresh))
    assert conn.verify_server_time("XAUUSD", now=now)["offset_hours"] == 2
    assert conn.quote("XAUUSD", now=now).spread == D("0.2")
    wrong = Tick(0, server_epoch(2026, 11, 6, 17) * 1000, 2650.0, 2650.2)  # broker actually on +3
    conn2, _ = connect(FakeMt5(tick=wrong))
    with pytest.raises(ServerClockError):
        conn2.verify_server_time("XAUUSD", now=now)
    with pytest.raises(ValidationError, match="Stale"):
        conn.quote("XAUUSD", now=datetime(2026, 11, 6, 15, tzinfo=UTC))


# --- Binance ------------------------------------------------------------------------------

EXCHANGE_INFO = {"symbols": [{
    "symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "baseAssetPrecision": 8,
    "quoteAssetPrecision": 8, "orderTypes": ["LIMIT", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"],
    "filters": [{"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000.00", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000.00000", "stepSize": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]}]}


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        for path, response in self.responses.items():
            if path in url:
                status, body = response() if callable(response) else response
                return status, json.dumps(body).encode()
        return 404, b"{}"


def test_public_exchange_info_parsed_without_credentials():
    http = FakeHttp({"/api/v3/exchangeInfo": (200, EXCHANGE_INFO)})
    client = BinanceReadOnlyClient.public(http_get=http)
    discovery = parse_symbol(client.exchange_info(), source=client.source)
    assert discovery.fields["tick_size"] == "0.01" and discovery.fields["min_notional"] == "5.00000000"
    assert not discovery.verified and discovery.fee_source == "assumed"
    with pytest.raises(ValidationError):
        discovery.to_instrument_spec()
    assert discovery.to_instrument_spec(allow_assumed_fee=True).taker_fee_rate == D("0.001")
    assert all("X-MBX-APIKEY" not in h for _, h in http.calls)
    assert http.calls[0][0].startswith(PUBLIC_DATA_URL)


def test_credentials_rejected_for_public_host_and_production_host_rejected():
    with pytest.raises(ValidationError):
        BinanceReadOnlyClient(PUBLIC_DATA_URL, api_key="k", api_secret="s")
    with pytest.raises(LiveTradingForbidden):
        BinanceReadOnlyClient("https://api." + "binance.com")


def test_only_read_paths_and_signed_account_on_testnet():
    http = FakeHttp({"/api/v3/account": (200, {"canTrade": True, "canWithdraw": False, "balances": [],
                                               "commissionRates": {"maker": "0.001", "taker": "0.001"}})})
    client = BinanceReadOnlyClient.testnet_from_env({"BINANCE_TESTNET_API_KEY": "key123",
                                                     "BINANCE_TESTNET_API_SECRET": "sec456"}, http_get=http)
    account = client.account()
    url, headers = http.calls[0]
    assert url.startswith("https://testnet.binance.vision/api/v3/account?") and "signature=" in url
    assert headers["X-MBX-APIKEY"] == "key123" and "sec456" not in url
    assert taker_fee_from_account(account) == D("0.001")
    assert "key123" not in repr(client) and "sec456" not in json.dumps(account)
    with pytest.raises(PermissionError):
        client._request("/api/v3/order", {})
    with pytest.raises(PermissionError):
        BinanceReadOnlyClient.public(http_get=http)._request("/api/v3/account", signed=True)


def test_missing_testnet_keys_and_http_errors_do_not_leak():
    client = BinanceReadOnlyClient.testnet_from_env({}, http_get=FakeHttp({}))
    with pytest.raises(BinanceUnavailable, match="not configured"):
        client.account()
    failing = BinanceReadOnlyClient.testnet_from_env({"BINANCE_TESTNET_API_KEY": "k", "BINANCE_TESTNET_API_SECRET":
                                                      "s"}, http_get=FakeHttp({"/api/v3/account": (401, {})}),
                                                     attempts=1)
    with pytest.raises(BinanceUnavailable) as err:
        failing.account()
    assert "signature" not in str(err.value)


def test_filter_comparison_and_trading_status():
    public = parse_symbol(EXCHANGE_INFO["symbols"][0], source="public")
    halted = parse_symbol({**EXCHANGE_INFO["symbols"][0], "status": "BREAK"}, source="testnet")
    diffs = compare_filters(public, halted)
    assert any(d.startswith("status") for d in diffs)
    assert any("BREAK" in p for p in halted.problems)


def test_misreported_tick_value_resolved_conservatively_from_terminal_pnl():
    """Mirrors MetaQuotes-Demo XAUUSD (2026-11-06): trade_tick_value 0.1 but 1 lot x 1.00 = 100 USD."""
    info = {**FULL_INFO, "trade_tick_value": 0.1, "trade_tick_value_loss": 0.1, "trade_calc_mode": 4}
    conn, _ = connect(FakeMt5(symbol_info=info, tick=Tick(0, 1, 4075.47, 4075.60)))
    discovery = conn.symbol_specification("XAUUSD")
    check = discovery.fields["tick_value_check"]
    assert not check["consistent"] and check["used"] == pytest.approx(1.0)
    assert check["sources"]["reported_tick_value_loss"] == 0.1
    assert discovery.to_instrument_spec().tick_value_loss == D("1.0")


def test_commission_assumption_only_when_commission_is_the_sole_gap():
    conn, _ = connect(FakeMt5(symbol_info=FULL_INFO, tick=Tick(0, 1, 2650.0, 2650.18)), commission=None)
    discovery = conn.symbol_specification("XAUUSD")
    with pytest.raises(ValidationError):
        discovery.to_instrument_spec()
    spec = discovery.to_instrument_spec(commission_assumption=D("7"))
    assert spec.commission_per_lot_round_trip == D("7") and not discovery.verified
    restricted = FakeMt5(symbol_info={**FULL_INFO, "trade_mode": 3}, tick=Tick(0, 1, 2650.0, 2650.18))
    conn2, _ = connect(restricted, commission=None)
    with pytest.raises(ValidationError):
        conn2.symbol_specification("XAUUSD").to_instrument_spec(commission_assumption=D("0"))
