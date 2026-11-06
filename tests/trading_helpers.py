"""Shared fixtures for trading foundation tests (illustrative specs, not broker data)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from trading.breaker import CircuitBreaker
from trading.common import Market
from trading.config import load_demo_environment
from trading.instruments import BinanceSpotSpec, Mt5InstrumentSpec
from trading.marketdata import Quote
from trading.risk import RiskLimits
from trading.sessions import XAUUSD_NY_CLOSE_TEMPLATE
from trading.signals import TradeSignal
from trading.state import AccountSnapshot

NOW = datetime(2026, 11, 6, 14, 0, 5, tzinfo=timezone.utc)
BAR_CLOSE = datetime(2026, 11, 6, 14, 0, tzinfo=timezone.utc)
XAU_CALENDAR = XAUUSD_NY_CLOSE_TEMPLATE  # NOW (Fri 14:00 UTC) = 16:00 server, open
ENV = load_demo_environment({"EXECUTION_MODE": "demo"})


def xau_spec(**overrides) -> Mt5InstrumentSpec:
    values = dict(symbol="XAUUSD", broker_symbol="XAUUSD", account_currency="USD", contract_size=D("100"),
                  point=D("0.01"), tick_size=D("0.01"), tick_value_loss=D("1"), volume_min=D("0.01"),
                  volume_max=D("100"), volume_step=D("0.01"), stops_level_points=20,
                  margin_per_lot=D("2650"), commission_per_lot_round_trip=D("7"))
    values.update(overrides)
    return Mt5InstrumentSpec(**values)


def btc_spec(**overrides) -> BinanceSpotSpec:
    values = dict(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", tick_size=D("0.01"),
                  step_size=D("0.00001"), min_qty=D("0.00001"), max_qty=D("9000"), min_notional=D("5"),
                  taker_fee_rate=D("0.001"))
    values.update(overrides)
    return BinanceSpotSpec(**values)


def limits(market: Market, **overrides) -> RiskLimits:
    if market is Market.XAUUSD_MT5:
        values = dict(market=market, max_spread=D("0.50"), slippage_allowance=D("0.10"))
    else:
        values = dict(market=market, max_spread=D("1.00"), slippage_allowance=D("5"))
    values.update(overrides)
    return RiskLimits(**values)


def account(market: Market, **overrides) -> AccountSnapshot:
    values = dict(market=market, account_id="demo-1", is_demo=True,
                  currency="USD" if market is Market.XAUUSD_MT5 else "USDT",
                  equity=D("10000"), balance=D("10000"), available_funds=D("10000"),
                  day_start_equity=D("10000"), snapshot_at=NOW, reconciled=True)
    values.update(overrides)
    return AccountSnapshot(**values)


def xau_signal(**overrides) -> TradeSignal:
    values = dict(signal_id="xau-1", market="xauusd_mt5", symbol="XAUUSD", side="long",
                  entry_price="2650.00", stop_price="2645.00", take_profit="2660.00",
                  strategy_id="goldpilot", strategy_version="0.1.0", generated_at=BAR_CLOSE,
                  bar_close_time=BAR_CLOSE)
    values.update(overrides)
    return TradeSignal.create(**values)


def btc_signal(**overrides) -> TradeSignal:
    values = dict(signal_id="btc-1", market="btcusdt_binance_spot", symbol="BTCUSDT", side="long",
                  entry_price="60000.00", stop_price="59400.00", strategy_id="trend",
                  strategy_version="0.1.0", generated_at=BAR_CLOSE, bar_close_time=BAR_CLOSE)
    values.update(overrides)
    return TradeSignal.create(**values)


def xau_quote(bid="2649.90", ask="2650.10", time=NOW) -> Quote:
    return Quote.create(symbol="XAUUSD", bid=bid, ask=ask, time=time)


def btc_quote(bid="59999.99", ask="60000.01", time=NOW) -> Quote:
    return Quote.create(symbol="BTCUSDT", bid=bid, ask=ask, time=time)


def breaker(market: Market, **kwargs) -> CircuitBreaker:
    return CircuitBreaker(market, **kwargs)


def at(minutes: float = 0, seconds: float = 0) -> datetime:
    return NOW + timedelta(minutes=minutes, seconds=seconds)
