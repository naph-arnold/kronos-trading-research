"""Binance Spot Testnet execution PREPARATION (Phase 3 is not authorized: nothing here can send an order).

What exists, so that a future, separately authorized adapter has tested building blocks:
* `build_entry_intent` — turns an APPROVED risk decision into a testnet order intent: quantity re-checked
  against LOT_SIZE (step, min, max) and NOTIONAL filters, stop price aligned to PRICE_FILTER tick, and a
  deterministic `newClientOrderId` derived from the signal's idempotency key (duplicate orders are rejected by
  the exchange and detectable on reconciliation).
* `reconcile` — compares the simulated/expected position ledger with the testnet account's balances.
* `detect_reset` — Binance resets the Spot Testnet periodically (balances restored, history wiped); a reset is
  recognised from the account snapshot and must be recorded as a cash flow, never as P&L.
* `submit` — always raises `ExecutionNotAuthorized`. There is deliberately no HTTP code in this module.

Endpoints, when Phase 3 is authorized, must be the Spot Testnet only (`trading.config.verify_binance_endpoint`);
production keys are refused by `trading.config.load_demo_environment`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from trading.common import Side, ValidationError, floor_to_step, is_multiple
from trading.instruments import BinanceSpotSpec
from trading.risk import RiskDecision
from trading.signals import TradeSignal

_CLIENT_ID = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")  # Binance newClientOrderId rule


class ExecutionNotAuthorized(PermissionError):
    """Order submission is disabled until Phase 3 is explicitly authorized."""


@dataclass(frozen=True)
class OrderIntent:
    venue: str
    symbol: str
    side: str
    type: str
    quantity: Decimal
    new_client_order_id: str
    protective_stop_price: Decimal
    take_profit_price: Decimal | None
    signal_id: str

    def as_params(self) -> dict:
        """Parameters a future testnet adapter would sign. Built for inspection only."""
        return {"symbol": self.symbol, "side": self.side, "type": self.type, "quantity": str(self.quantity),
                "newClientOrderId": self.new_client_order_id}


def client_order_id(signal: TradeSignal) -> str:
    cid = "krn-" + signal.idempotency_key
    if not _CLIENT_ID.match(cid):
        raise ValidationError("Client order id violates the venue format")
    return cid


def build_entry_intent(signal: TradeSignal, decision: RiskDecision, spec: BinanceSpotSpec) -> OrderIntent:
    if not decision.approved or decision.size <= 0:
        raise ValidationError("Only approved risk decisions can become order intents")
    if decision.signal_id != signal.signal_id or decision.dedup_key != signal.dedup_key:
        raise ValidationError("Decision does not belong to this signal")
    if signal.side is not Side.LONG:
        raise ValidationError("Spot is long/flat only")
    spec.validate()
    quantity = floor_to_step(decision.size, spec.step_size)
    if quantity != decision.size or not spec.min_qty <= quantity <= spec.max_qty:
        raise ValidationError(f"Quantity {decision.size} violates LOT_SIZE {spec.min_qty}/{spec.step_size}")
    if quantity * signal.entry_price < spec.min_notional:
        raise ValidationError(f"Notional below NOTIONAL filter {spec.min_notional}")
    for name in ("stop_price", "take_profit"):
        value = getattr(signal, name)
        if value is not None and not is_multiple(value, spec.tick_size):
            raise ValidationError(f"{name} not aligned to tick {spec.tick_size}")
    return OrderIntent("binance_spot_testnet", spec.symbol, "BUY", "MARKET", quantity, client_order_id(signal),
                       signal.stop_price, signal.take_profit, signal.signal_id)


def submit(intent: OrderIntent, *args, **kwargs):
    raise ExecutionNotAuthorized("Binance Spot Testnet order submission requires explicit Phase 3 authorization")


def _balances(account: dict) -> dict[str, Decimal]:
    out = {}
    for row in account.get("balances") or []:
        out[row["asset"]] = Decimal(row["free"]) + Decimal(row["locked"])
    return out


def reconcile(expected_base_qty: Decimal, account: dict, *, asset: str = "BTC",
              tolerance: Decimal = Decimal("0.00000001")) -> dict:
    """Expected open base-asset quantity (from the order/position ledger) versus the testnet account."""
    actual = _balances(account).get(asset, Decimal("0"))
    diff = actual - expected_base_qty
    return {"asset": asset, "expected": expected_base_qty, "actual": actual, "difference": diff,
            "reconciled": abs(diff) <= tolerance}


def detect_reset(previous: dict, current: dict, *, expected_change: dict[str, Decimal] | None = None,
                 tolerance: Decimal = Decimal("0.01")) -> str | None:
    """A testnet reset restores default balances and wipes history. Flag any balance change not explained by the
    ledger's expected change (fills, fees). Returns a reason (record as a cash flow + reconcile) or None."""
    expected_change = expected_change or {}
    before, after = _balances(previous), _balances(current)
    for asset in sorted(set(before) | set(after)):
        delta = after.get(asset, Decimal("0")) - before.get(asset, Decimal("0"))
        if abs(delta - expected_change.get(asset, Decimal("0"))) > tolerance:
            return f"Unexplained {asset} balance change {delta} (possible testnet reset)"
    if previous.get("updateTime") and current.get("updateTime") and current["updateTime"] < previous["updateTime"]:
        return "Account updateTime moved backwards (possible testnet reset)"
    return None
