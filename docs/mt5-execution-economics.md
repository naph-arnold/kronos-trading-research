# XAUUSD MT5 execution economics (2026-11-09)

**Sources:**
- the broker's recorded `symbol_info` (MetaQuotes-Demo), plus the runners' read-only specification events;
- broker-tick M1 bars for 2025-05 → 2026-11 (`research/xau_execution_economics.py`), which record the **minimum**
  spread per minute and are therefore optimistic.

**MetaQuotes-Demo is not assumed to represent the broker that would be used for real money.** Every figure must be
re-measured on that broker's own specification, ticks and fills.

## 1. Verified contract specification (MetaQuotes-Demo XAUUSD)
| Field | Value |
| --- | --- |
| Contract size, tick size | 100 oz, 0.01 |
| Value per point per lot | 1.00 USD (verified with `order_calc_profit`; the reported `tick_value` 0.1 is wrong) |
| Volume | min 0.01, step 0.01, max 100 |
| Order types | market, limit, stop, stop-limit, SL, TP, close-by |
| Filling modes | FOK and IOC (no RETURN) |
| Stops level, freeze level | 0 points |
| Spread | floating; recorded minimum-per-minute median 0.13, p90 0.19 |
| Margin | about 4,100 USD per lot (1:100) |
| Swap | long −12.6 USD/lot/night, short −4.6; triple Wednesday |
| Commission | **not exposed by MT5**; unverified until a real demo fill |

## 2. Spread by UTC hour (minimum spread per minute; median / p90)
| Hours (UTC) | Spread |
| --- | --- |
| Cheapest: 12–19 | 0.10–0.12 / 0.17–0.18 |
| Asia: 00–06 | 0.14–0.15 / 0.20–0.23 |
| London: 07–11 | 0.13–0.14 / 0.18–0.19 |
| Widest: 20–23 (rollover) | 0.16 / 0.34–0.37 |

## 3. Market-order round trip and the required gross edge
- Round trip per lot = spread × 100 + 2 × 0.10 slippage × 100 + commission, which comes to **33–40 USD per lot**
  (commission 0–7). That is 0.33–0.40 USD per ounce.
- As a share of risk, at a stop of s USD the cost is 0.33–0.40 ÷ s:

  | Stop | Cost in R |
  | --- | --- |
  | 2 USD | 0.17–0.20 R |
  | 5 USD | 0.07–0.08 R |
  | 12 USD | 0.03 R |

- A strategy needs at least that much **gross** edge per trade just to break even. The break-even win rate at reward
  ratio k is (1 + c) ÷ (1 + k), where c is the cost in R. For example, a 5 USD stop at 1:1 needs about 54%.
- **Minimum lot:** 0.01 lot loses 1 USD per 1.00 move. The 0.5% budget (15 USD at 3,000; 50 USD at 10,000) therefore
  caps the stop at 15 USD or 50 USD at the minimum size, and sizing above the minimum moves in 0.01-lot steps.
- **Swap:** holding a 0.01-lot long overnight costs about 0.13 USD a night, negligible for intraday trading, but
  scalping strategies should close before the 21:00 UTC rollover anyway (spreads widen too).

## 4. Market vs limit entries (seeded random times; mechanics only, no strategy)
A limit order placed `offset` × ATR(1m) better than the market, with an expiry:

| Offset | Expiry | Fill rate | Mean 15-min drift after a limit fill | Same, market entry |
| --- | --- | --- | --- | --- |
| 0.25 ATR | 5 min | 84% | −0.104 ATR | −0.045 ATR |
| 0.5 ATR | 15 min | 83% | −0.121 ATR | −0.052 ATR |
| 1.0 ATR | 15 min | 68% | −0.098 ATR | −0.028 ATR |

**Adverse selection:** limit orders fill disproportionately when price keeps moving against them. The drift after a
fill is worse by 0.06–0.07 ATR, which roughly equals the spread a limit saves (about 0.07 ATR). The broker supports
limit orders, but they offer **no free cost saving**; they would need strategy-specific evidence. This estimate uses
bid OHLC plus the minute's spread, with no queue or partial fills.

## 5. Still unknown until a demo fill (cannot be estimated honestly from bars)
- the commission actually charged per deal;
- real slippage against the requested price under FOK;
- latency from signal to fill;
- the rejection rate (requotes and price-changed return codes).

These are the purpose of the single-order mechanics trial (`docs/mt5-demo-execution.md` §3 and §5).
