"""Read-only Binance connectors for BTCUSDT research (Pipeline B).

Two strictly separated sources:
* Public market data (`https://data-api.binance.vision`): exchangeInfo, klines,
  bookTicker, server time. No credentials of any kind are sent.
* Spot Testnet (`https://testnet.binance.vision`, verified by `verify_binance_endpoint`):
  the same public endpoints plus *signed GET* account reads (balances, permissions,
  commission rates) using testnet-only keys from BINANCE_TESTNET_API_KEY/SECRET.

Only GET requests exist in this module. Keys are never logged, repr'd or returned.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from trading.common import ValidationError, to_decimal
from trading.config import BINANCE_SPOT_TESTNET_DEFAULT, verify_binance_endpoint
from trading.instruments import BinanceSpotSpec
from trading.marketdata import Quote

PUBLIC_DATA_URL = "https://data-api.binance.vision"
ALLOWED_PATHS = frozenset({"/api/v3/exchangeInfo", "/api/v3/time", "/api/v3/ticker/bookTicker", "/api/v3/klines",
                           "/api/v3/account"})
SIGNED_PATHS = frozenset({"/api/v3/account"})
ASSUMED_TAKER_FEE = Decimal("0.001")  # Binance spot base tier; used only when not verifiable

HttpGet = Callable[[str, Mapping[str, str]], tuple[int, bytes]]


def _urllib_get(url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
    request = Request(url, headers={"User-Agent": "kronos-trading-research/0.2", **headers}, method="GET")
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


class BinanceUnavailable(RuntimeError):
    pass


class BinanceReadOnlyClient:
    def __init__(self, base_url: str, *, http_get: HttpGet | None = None, api_key: str | None = None,
                 api_secret: str | None = None, attempts: int = 3):
        if base_url == PUBLIC_DATA_URL:
            if api_key or api_secret:
                raise ValidationError("Credentials must never be sent to the public market-data host")
            self.source = "binance_public_market_data"
        else:
            base_url = verify_binance_endpoint(base_url)
            self.source = "binance_spot_testnet"
        self.base_url = base_url
        self._get = http_get or _urllib_get
        self._key, self._secret = api_key, api_secret
        self.attempts = attempts

    def __repr__(self) -> str:
        return f"BinanceReadOnlyClient(source={self.source!r}, signed={'yes' if self._key else 'no'})"

    @classmethod
    def public(cls, **kwargs) -> "BinanceReadOnlyClient":
        return cls(PUBLIC_DATA_URL, **kwargs)

    @classmethod
    def testnet_from_env(cls, env: Mapping[str, str] | None = None, **kwargs) -> "BinanceReadOnlyClient":
        env = os.environ if env is None else env
        return cls(env.get("BINANCE_TESTNET_BASE_URL", BINANCE_SPOT_TESTNET_DEFAULT),
                   api_key=env.get("BINANCE_TESTNET_API_KEY") or None,
                   api_secret=env.get("BINANCE_TESTNET_API_SECRET") or None, **kwargs)

    def _request(self, path: str, params: dict | None = None, *, signed: bool = False):
        if path not in ALLOWED_PATHS or (path in SIGNED_PATHS) != signed:
            raise PermissionError(f"Path {path} not permitted for read-only research")
        params = dict(params or {})
        headers = {}
        if signed:
            if self.source != "binance_spot_testnet":
                raise PermissionError("Signed requests are only allowed against the Spot Testnet")
            if not (self._key and self._secret):
                raise BinanceUnavailable("Testnet API key/secret not configured")
            params.update(timestamp=int(time.time() * 1000), recvWindow=5000)
            query = urlencode(params)
            signature = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += f"&signature={signature}"
            headers["X-MBX-APIKEY"] = self._key
        else:
            query = urlencode(params)
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        for attempt in range(self.attempts):
            try:
                status, body = self._get(url, headers)
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.attempts - 1:
                    raise BinanceUnavailable(f"{self.source} unreachable: {type(exc).__name__}") from None
                time.sleep(min(2 ** attempt, 8))
                continue
            if status == 200:
                return json.loads(body)
            if status in (418, 429, 500, 502, 503, 504) and attempt < self.attempts - 1:
                time.sleep(min(2 ** attempt, 8))
                continue
            # Never echo the URL (it may carry a signature) or headers.
            raise BinanceUnavailable(f"{self.source} {path} returned HTTP {status}: {body[:200]!r}")
        raise BinanceUnavailable("Retries exhausted")

    # -- public ------------------------------------------------------------------------------
    def server_time(self) -> datetime:
        data = self._request("/api/v3/time")
        return datetime.fromtimestamp(int(data["serverTime"]) / 1000, timezone.utc)

    def exchange_info(self, symbol: str = "BTCUSDT") -> dict:
        data = self._request("/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = [s for s in data.get("symbols", []) if s.get("symbol") == symbol]
        if len(symbols) != 1:
            raise ValidationError(f"{symbol} not found in exchangeInfo")
        return symbols[0]

    def book_ticker(self, symbol: str = "BTCUSDT") -> Quote:
        """bookTicker has no timestamp; it is stamped with exchange server time fetched just before."""
        stamp = self.server_time()
        data = self._request("/api/v3/ticker/bookTicker", {"symbol": symbol})
        return Quote.create(symbol=symbol, bid=data["bidPrice"], ask=data["askPrice"], time=stamp)

    # -- signed testnet (read-only) ------------------------------------------------------------
    def account(self) -> dict:
        """Balances, permissions and commission rates. Returned without any credential material."""
        data = self._request("/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        return {k: data.get(k) for k in ("canTrade", "canWithdraw", "canDeposit", "accountType", "permissions",
                                         "commissionRates", "balances", "updateTime")}


@dataclass
class SymbolDiscovery:
    source: str
    retrieved_at: str
    fields: dict
    missing: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    fee_source: str = "assumed"

    @property
    def verified(self) -> bool:
        return not self.missing and not self.problems and self.fee_source != "assumed"

    def to_instrument_spec(self, *, allow_assumed_fee: bool = False) -> BinanceSpotSpec:
        if self.missing or self.problems or (self.fee_source == "assumed" and not allow_assumed_fee):
            raise ValidationError(f"Symbol specification unverified: missing={self.missing} "
                                  f"problems={self.problems} fee={self.fee_source}")
        f = self.fields
        spec = BinanceSpotSpec(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
                               tick_size=to_decimal(f["tick_size"], "tick_size", positive=True),
                               step_size=to_decimal(f["step_size"], "step_size", positive=True),
                               min_qty=to_decimal(f["min_qty"], "min_qty", positive=True),
                               max_qty=to_decimal(f["max_qty"], "max_qty", positive=True),
                               min_notional=to_decimal(f["min_notional"], "min_notional", positive=True),
                               taker_fee_rate=to_decimal(f["taker_fee_rate"], "taker_fee", nonnegative=True))
        spec.validate()
        return spec

    def to_record(self) -> dict:
        return {"source": self.source, "retrieved_at": self.retrieved_at, "verified": self.verified,
                "fee_source": self.fee_source, "fields": self.fields, "missing": self.missing,
                "problems": self.problems}


def parse_symbol(info: dict, *, source: str, taker_fee: Decimal | None = None,
                 fee_source: str = "assumed") -> SymbolDiscovery:
    filters = {f.get("filterType"): f for f in info.get("filters", [])}
    missing, problems = [], []
    price, lot = filters.get("PRICE_FILTER", {}), filters.get("LOT_SIZE", {})
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    fields = {
        "symbol": info.get("symbol"), "status": info.get("status"),
        "base_asset": info.get("baseAsset"), "quote_asset": info.get("quoteAsset"),
        "base_asset_precision": info.get("baseAssetPrecision"), "quote_precision": info.get("quoteAssetPrecision"),
        "tick_size": price.get("tickSize"), "min_price": price.get("minPrice"), "max_price": price.get("maxPrice"),
        "step_size": lot.get("stepSize"), "min_qty": lot.get("minQty"), "max_qty": lot.get("maxQty"),
        "min_notional": notional.get("minNotional"),
        "order_types": info.get("orderTypes"), "permissions": info.get("permissionSets") or info.get("permissions"),
        "taker_fee_rate": str(taker_fee if taker_fee is not None else ASSUMED_TAKER_FEE),
    }
    for key in ("tick_size", "step_size", "min_qty", "max_qty", "min_notional"):
        try:
            to_decimal(fields[key], key, positive=True)
        except ValidationError:
            missing.append(key)
    if fields["status"] != "TRADING":
        problems.append(f"Symbol status {fields['status']!r}")
    if (fields["base_asset"], fields["quote_asset"]) != ("BTC", "USDT"):
        problems.append("Unexpected base/quote assets")
    if fields["order_types"] and "STOP_LOSS_LIMIT" not in fields["order_types"]:
        problems.append("STOP_LOSS_LIMIT orders unavailable: exchange-side stop protection cannot be enforced")
    return SymbolDiscovery(source, datetime.now(timezone.utc).isoformat(), fields, missing, problems,
                           fee_source if taker_fee is not None else "assumed")


def compare_filters(public: SymbolDiscovery, testnet: SymbolDiscovery) -> list[str]:
    """Differences between production public filters and testnet filters (testnet may differ)."""
    keys = ("tick_size", "step_size", "min_qty", "max_qty", "min_notional", "status")
    return [f"{k}: public={public.fields.get(k)} testnet={testnet.fields.get(k)}"
            for k in keys if public.fields.get(k) != testnet.fields.get(k)]


def taker_fee_from_account(account: dict) -> Decimal | None:
    rates = account.get("commissionRates") or {}
    if rates.get("taker") is None:
        return None
    return to_decimal(rates["taker"], "taker commission", nonnegative=True)
