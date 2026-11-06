"""End-to-end XAUUSD research runner on SYNTHETIC data (no MT5 needed)."""
from __future__ import annotations

import csv
import calendar as stdcal
import hashlib
import json
from argparse import Namespace

import pytest

from phase2_helpers import xau_m15
from research.phase2_xauusd import FIELDS, cmd_backtest, load_mt5_csv
from trading.common import ValidationError

SPEC_FIELDS = {"broker_symbol": "XAUUSD", "contract_size": 100.0, "tick_size": 0.01, "tick_value": 1.0,
               "tick_value_loss": 1.0, "point": 0.01, "volume_min": 0.01, "volume_max": 50.0, "volume_step": 0.01,
               "currency_profit": "USD", "currency_margin": "XAU", "account_currency": "USD",
               "stops_level_points": 0, "margin_per_lot": 2650.0, "commission_per_lot_round_trip": "7",
               "order_types": ["market", "sl", "tp"], "trade_mode": "full"}


def write_dataset(tmp_path, bars):
    path = tmp_path / "xau.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(FIELDS)
        for b in bars:
            server = stdcal.timegm((b.open_time.replace(tzinfo=None)).timetuple()) + 7200
            writer.writerow([b.open_time.isoformat(), server, b.open, b.high, b.low, b.close, int(b.volume), 20])
    manifest = {"schema_version": 1, "source": "SYNTHETIC-TEST", "interval": "15m", "rows": len(bars),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    path.with_suffix(".csv.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def write_config(tmp_path, dataset, *, missing=()):
    spec = tmp_path / "discovery.json"
    spec.write_text(json.dumps({"specification": {"symbol": "XAUUSD", "retrieved_at": "2026-11-06T00:00:00+00:00",
                                                  "fields": SPEC_FIELDS, "missing": list(missing),
                                                  "problems": []}}), encoding="utf-8")
    config = {"dataset": {"csv": str(dataset)}, "specification": str(spec), "server_tz": "ny_close",
              "holdout_fraction": 0.25, "walk_forward": {"train_bars": 1200, "test_bars": 600, "min_train_trades": 1},
              "warmup_bars": 400, "initial_equity": "10000", "costs": {"min_spread": "0.10",
                                                                       "slippage_allowance": "0.10"},
              "risk": {"risk_fraction": "0.005", "max_spread": "1.00"},
              "accept_calendar_differences": True,
              "strategies": {"goldpilot": {"strategy": "goldpilot", "fixed": {"h1_ema_fast": 10, "h1_ema_slow": 30},
                                           "grid": {"adx_min": [10, 15]}},
                             "orb_london": {"strategy": "orb", "fixed": {"session": "london", "min_range_atr": 0.1},
                                            "grid": {"range_minutes": [30]}, "max_holding_bars": 16}}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_backtest_runs_with_verified_spec_and_stays_experimental(tmp_path):
    dataset = write_dataset(tmp_path, xau_m15(3000))
    report = cmd_backtest(Namespace(config=str(write_config(tmp_path, dataset))))
    assert report["research_only"] and report["spread_statistics"]["median"] == "0.20"
    for item in report["strategies"].values():
        assert "not authorized" in item["classification"] or item["classification"].startswith("experimental")
        assert item["holdout"]["trades"] >= 0


def test_backtest_refuses_unverified_broker_spec(tmp_path):
    dataset = write_dataset(tmp_path, xau_m15(1500))
    with pytest.raises(ValidationError, match="unverified"):
        cmd_backtest(Namespace(config=str(write_config(tmp_path, dataset, missing=["commission"]))))


def test_tampered_dataset_rejected(tmp_path):
    dataset = write_dataset(tmp_path, xau_m15(200))
    with dataset.open("a", encoding="utf-8") as stream:
        stream.write("tampered\n")
    with pytest.raises(ValidationError, match="SHA-256"):
        load_mt5_csv(dataset)
