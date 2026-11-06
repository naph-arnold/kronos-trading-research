"""Official-source parsers (RECORDED FIXTURES), ingestion service, contradictions, market health."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from phase2_helpers import btc_h1
from trading.common import Market, ValidationError
from trading.health import MarketHealthMonitor, VenueWindow, combine_guards
from trading.marketcal import fomc, official
from trading.marketcal.events import EconomicEvent, EventStore
from trading.marketcal.holidays import build_xauusd_calendar, mark_broker_verified
from trading.marketcal.service import CalendarProvider, CalendarService, structured_file_provider
from trading.marketdata import Quote
from trading.replay import run_replay
from trading.sessions import BTCUSDT_ALWAYS_OPEN, XAUUSD_NY_CLOSE_TEMPLATE
from trading.signals import TradeSignal
from trading_helpers import ENV, btc_spec, limits

UTC = timezone.utc
DATA = Path(__file__).parent / "data"
RETRIEVED = datetime(2026, 11, 6, 12, tzinfo=UTC)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def fixture(name):
    return (DATA / name).read_text(encoding="utf-8")


# --- Official sources (recorded fixtures, not live data) ---------------------------------------

def test_bea_schedule_yields_headline_gdp_and_pce_only():
    events = official.parse_bea_schedule(fixture("bea_release_schedule_20261106.html"), retrieved_at=RETRIEVED)
    assert [(e.series, e.scheduled_at) for e in events] == [
        ("US_GDP", utc(2026, 11, 25, 13, 30)), ("US_PCE", utc(2026, 11, 25, 13, 30)),
        ("US_GDP", utc(2026, 12, 23, 13, 30)), ("US_PCE", utc(2026, 12, 23, 13, 30))]
    assert events[1].reference_period == "October 2026" and all(e.impact == "high" for e in events)
    assert all(e.provider == official.BEA_PROVIDER and e.retrieved_at == RETRIEVED for e in events)


def test_bea_year_inference_across_new_year():
    late = datetime(2026, 12, 20, tzinfo=UTC)
    assert official._infer_year(1, 15, late) == date(2027, 1, 15)
    assert official._infer_year(12, 1, late) == date(2026, 12, 1)


def test_ecb_decisions_on_final_meeting_day_at_1415_frankfurt():
    events = official.parse_ecb_calendar(fixture("ecb_meeting_calendar_20261106.html"), retrieved_at=RETRIEVED)
    assert events[0].event_id == "ecb-decision-2026-12-17" and events[0].scheduled_at == utc(2026, 12, 17, 13, 15)
    summer = [e for e in events if e.scheduled_at.month == 7]
    assert summer and summer[0].scheduled_at.hour == 12  # CEST
    assert all(e.currency == "EUR" and e.category == "central_bank" for e in events)


def test_bls_values_enrich_cpi_events_without_inventing_data():
    payload = json.loads(fixture("bls_api_cpi_20261106.json"))["response"]
    obs = {"CUSR0000SA0": official.parse_bls_observations(payload)["CUSR0000SA0"]}
    cpi = EconomicEvent(event_id="cpi-2026-09", name="US CPI", country="US", currency="USD",
                        scheduled_at=utc(2026, 10, 15, 12, 30), impact="high", provider="operator-file",
                        retrieved_at=RETRIEVED, updated_at=RETRIEVED, series="US_CPI", reference_period="2026-09")
    future = EconomicEvent(**{**cpi.__dict__, "event_id": "cpi-2026-10", "reference_period": "2026-10"})
    enriched = official.enrich_with_bls([cpi, future], obs, retrieved_at=RETRIEVED)
    assert enriched[0].actual == "336.298" and enriched[0].previous == "334.131" and enriched[0].status == "released"
    assert enriched[1].actual is None  # not published yet: left empty, never estimated
    with pytest.raises(ValidationError):
        official.parse_bls_observations({"status": "REQUEST_NOT_PROCESSED", "message": ["limit"]})


@pytest.mark.parametrize("parser", [official.parse_bea_schedule, official.parse_ecb_calendar])
def test_official_parsers_fail_closed_on_layout_change(parser):
    with pytest.raises(ValidationError):
        parser("<html>new layout</html>", retrieved_at=RETRIEVED)


# --- Ingestion service ---------------------------------------------------------------------------

def _fomc_provider():
    events = fomc.parse_fomc_calendar(fixture("fomc_calendar_2026.html"), retrieved_at=RETRIEVED)
    return CalendarProvider(fomc.PROVIDER, lambda: events, frozenset({"FOMC_DECISION"}), timedelta(days=14))


def test_refresh_records_success_failure_and_keeps_last_good():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("network down")
        return official.parse_bea_schedule(fixture("bea_release_schedule_20261106.html"), retrieved_at=RETRIEVED)

    store = EventStore()
    service = CalendarService(store, [_fomc_provider(),
                                      CalendarProvider(official.BEA_PROVIDER, flaky, frozenset({"US_GDP", "US_PCE"}),
                                                       timedelta(days=7))])
    first = service.refresh(RETRIEVED)
    assert all(r["ok"] for r in first.values())
    second = service.refresh(RETRIEVED + timedelta(days=1))
    assert not second[official.BEA_PROVIDER]["ok"]
    assert store.between(utc(2026, 11, 25), utc(2026, 11, 26))  # last good data kept
    status = service.status(RETRIEVED + timedelta(days=8))
    assert not status[official.BEA_PROVIDER]["fresh"] and "network down" in status[official.BEA_PROVIDER][
        "last_failure"]["error"]


def test_provider_attribution_and_series_trust_enforced():
    def impostor():
        return [EconomicEvent(event_id="x1", name="CPI", country="US", currency="USD", scheduled_at=RETRIEVED,
                              impact="high", provider="someone-else", retrieved_at=RETRIEVED, updated_at=RETRIEVED)]

    def overreach():
        return [EconomicEvent(event_id="x2", name="NFP", country="US", currency="USD", scheduled_at=RETRIEVED,
                              impact="high", provider="bea", retrieved_at=RETRIEVED, updated_at=RETRIEVED,
                              series="US_NFP")]

    service = CalendarService(EventStore(), [CalendarProvider("p", impostor, frozenset(), timedelta(days=1)),
                                             CalendarProvider("bea", overreach, frozenset({"US_GDP"}),
                                                              timedelta(days=1))])
    results = service.refresh(RETRIEVED)
    assert not results["p"]["ok"] and "attributed" in results["p"]["error"]
    assert not results["bea"]["ok"] and "not trusted" in results["bea"]["error"]


def test_guard_fails_closed_without_cpi_nfp_schedule_provider_then_allows(tmp_path):
    store = EventStore()
    service = CalendarService(store, [_fomc_provider()])
    service.refresh(RETRIEVED)
    guard = service.guard(Market.XAUUSD_MT5)
    assert "no verified calendar provider configured for US_CPI" in guard(RETRIEVED)
    crypto = service.guard(Market.BTCUSDT_BINANCE_SPOT)
    assert "US_CPI" in crypto(RETRIEVED)

    path = tmp_path / "cpi_nfp.json"
    path.write_text(json.dumps([EconomicEvent(
        event_id="cpi-2026-11-12", name="US CPI", country="US", currency="USD", scheduled_at=utc(2026, 11, 12, 13, 30),
        impact="high", provider="operator-file", retrieved_at=RETRIEVED, updated_at=RETRIEVED, category="inflation",
        series="US_CPI", reference_period="2026-10").to_json()]), encoding="utf-8")
    service.providers.append(structured_file_provider(path, provider="operator-file", series={"US_CPI"}))
    service.refresh(RETRIEVED)
    btc_guard = service.guard(Market.BTCUSDT_BINANCE_SPOT)
    assert btc_guard(RETRIEVED) is None
    assert "US CPI" in btc_guard(utc(2026, 11, 12, 13, 20))  # inside the CPI blackout
    assert "stale" in btc_guard(RETRIEVED + timedelta(days=20))  # FOMC provider max age 14 days


def test_contradictory_providers_reject_entries():
    store = EventStore()
    common = dict(name="US CPI", country="US", currency="USD", impact="high", retrieved_at=RETRIEVED,
                  updated_at=RETRIEVED, series="US_CPI")
    store.upsert([EconomicEvent(event_id="a", provider="vendor-a", scheduled_at=utc(2026, 11, 12, 13, 30), **common),
                  EconomicEvent(event_id="b", provider="vendor-b", scheduled_at=utc(2026, 11, 13, 13, 30), **common)])
    assert store.contradictions(utc(2026, 11, 11), utc(2026, 11, 14))
    service = CalendarService(store, [])
    guard = service.guard(Market.BTCUSDT_BINANCE_SPOT, required_series=())
    assert "contradictory calendar data for US_CPI" in guard(utc(2026, 11, 12))


# --- Holidays and session verification ------------------------------------------------------------

def test_calendar_verification_levels():
    cal, _ = build_xauusd_calendar(XAUUSD_NY_CLOSE_TEMPLATE, date(2026, 9, 1), date(2026, 10, 31))
    assert XAUUSD_NY_CLOSE_TEMPLATE.verification == "template" and cal.verification == "rule_based"
    from phase2_helpers import xau_m15
    bars = xau_m15(2500)  # SYNTHETIC bars generated inside the template sessions
    with pytest.raises(ValidationError, match="offset"):
        mark_broker_verified(cal, bars, offset_verified=False)
    assert mark_broker_verified(XAUUSD_NY_CLOSE_TEMPLATE, bars, offset_verified=True).verification == "broker_verified"
    with pytest.raises(ValidationError):
        mark_broker_verified(XAUUSD_NY_CLOSE_TEMPLATE, bars[:100], offset_verified=True)


def test_btc_not_blocked_by_traditional_market_holidays():
    christmas = utc(2026, 12, 25, 15)
    gold, _ = build_xauusd_calendar(XAUUSD_NY_CLOSE_TEMPLATE, date(2026, 12, 20), date(2026, 12, 31))
    assert not gold.is_open(christmas)
    assert BTCUSDT_ALWAYS_OPEN.tradeable_reason(christmas) is None
    assert BTCUSDT_ALWAYS_OPEN.tradeable_reason(utc(2026, 11, 7, 12)) is None  # Saturday


# --- Market health (9.4 / 9.5) -----------------------------------------------------------------------

def q(t, bid="100.00", ask="100.02"):
    return Quote.create(symbol="BTCUSDT", bid=bid, ask=ask, time=t)


def healthy_monitor(now):
    mon = MarketHealthMonitor(Market.BTCUSDT_BINANCE_SPOT)
    mon.record_binance_status("TRADING", now)
    for i in range(40):
        mon.record_quote(q(now - timedelta(seconds=40 - i)))
    return mon


def test_health_unknown_status_and_missing_data_fail_closed():
    now = RETRIEVED
    mon = MarketHealthMonitor(Market.BTCUSDT_BINANCE_SPOT)
    assert "unknown" in mon.guard(now)
    mon.record_binance_status("TRADING", now)
    assert "no market data" in mon.guard(now)
    assert healthy_monitor(now).guard(now) is None


def test_health_detects_suspension_stale_data_gaps_and_abnormal_spread():
    now = RETRIEVED
    mon = healthy_monitor(now)
    mon.record_binance_status("BREAK", now)
    assert "suspended" in mon.guard(now)
    mon = healthy_monitor(now)
    assert "stale" in mon.guard(now + timedelta(seconds=30))
    mon.record_quote(q(now + timedelta(minutes=5)))
    assert "gap in price updates" in mon.guard(now + timedelta(minutes=5))
    mon = healthy_monitor(now)
    mon.record_quote(q(now, bid="100.00", ask="100.50"))
    assert "abnormal spread" in mon.guard(now)
    assert any("missing price updates" in f for _, f in healthy_monitor(now).flags()) is False


def test_maintenance_and_unverified_session_change_windows():
    now = RETRIEVED
    mon = healthy_monitor(now)
    mon.add_window(VenueWindow(now - timedelta(minutes=5), now + timedelta(hours=1), "maintenance",
                               "Binance announcement", "ann-123"))
    assert "maintenance" in mon.guard(now)
    gold = MarketHealthMonitor(Market.XAUUSD_MT5)
    gold.record_mt5_trade_mode(4, now)
    gold.record_quote(Quote.create(symbol="XAUUSD", bid="2650.0", ask="2650.2", time=now))
    gold.add_window(VenueWindow(now + timedelta(days=2), now + timedelta(days=2, hours=3), "session_change",
                                "broker email", "msg-9", verified=False))
    assert "unverified session_change" in gold.guard(now)
    close_only = MarketHealthMonitor(Market.XAUUSD_MT5)
    close_only.record_mt5_trade_mode(3, now)
    close_only.record_quote(Quote.create(symbol="XAUUSD", bid="2650.0", ask="2650.2", time=now))
    assert "close_only" in combine_guards(lambda t: None, close_only.guard)(now)


def test_markets_are_isolated():
    now = RETRIEVED
    gold = MarketHealthMonitor(Market.XAUUSD_MT5)
    gold.record_mt5_trade_mode(0, now)
    assert gold.guard(now) and healthy_monitor(now).guard(now) is None


# --- Existing positions are still managed during blackouts ----------------------------------------

def test_open_position_keeps_stop_management_during_blackout():
    bars = btc_h1(40)

    def strategy(history):
        b = history[-1]
        if len(history) == 5:
            return TradeSignal.create(signal_id="s5", market="btcusdt_binance_spot", symbol="BTCUSDT", side="long",
                                      entry_price=b.close, stop_price=(b.close * D("0.97")).quantize(D("0.01")),
                                      strategy_id="t", strategy_version="1", generated_at=b.close_time,
                                      bar_close_time=b.close_time)
        return None

    blackout_from = bars[6].open_time
    result = run_replay(bars, strategy, instrument=btc_spec(), limits=limits(Market.BTCUSDT_BINANCE_SPOT),
                        environment=ENV, initial_equity=D("10000"), spread=D("0.02"), close_at_end=True,
                        event_guard=lambda now: "CPI" if now >= blackout_from else None)
    assert result["trades"] == 1
    trade = result["trade_ledger"][0]
    assert trade["exit_reason"] in ("stop", "stop_gap", "end_of_data") and trade["closed_at"] >= blackout_from
