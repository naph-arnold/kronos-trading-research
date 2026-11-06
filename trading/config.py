"""Demo-only execution environment guard.

Live trading is *structurally* unrepresentable here: `ExecutionMode` has no
live member, Binance endpoints are restricted to the Spot Testnet host, MT5
accounts must report the demo trade mode, and production-looking credential
variables in the process environment cause a hard failure.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping
from urllib.parse import urlparse


class LiveTradingForbidden(RuntimeError):
    """The process environment is not a verifiable demo environment."""


class ExecutionMode(str, Enum):
    DEMO = "demo"  # Intentionally the only member.


BINANCE_SPOT_TESTNET_HOSTS = frozenset({"testnet.binance.vision"})
BINANCE_SPOT_TESTNET_DEFAULT = "https://testnet.binance.vision"

# MetaTrader5 ACCOUNT_TRADE_MODE_* values: DEMO=0, CONTEST=1, REAL=2.
MT5_ACCOUNT_TRADE_MODE_DEMO = 0

# Variable names that conventionally hold production credentials. Their mere
# presence means the process could reach a live account, so we refuse to run.
FORBIDDEN_ENV_VARS = frozenset({
    "BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_SECRET_KEY",
    "BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET",
    "MT5_LIVE_LOGIN", "MT5_LIVE_PASSWORD", "MT5_REAL_LOGIN", "MT5_REAL_PASSWORD",
})

_FALSE = {"false", "0", "no", "off"}


@dataclass(frozen=True)
class DemoEnvironment:
    mode: ExecutionMode
    binance_base_url: str

    @property
    def live_trading_enabled(self) -> bool:
        return False


def verify_binance_endpoint(url: str) -> str:
    parsed = urlparse(url.strip())
    if (parsed.scheme != "https" or parsed.hostname not in BINANCE_SPOT_TESTNET_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise LiveTradingForbidden(f"Binance endpoint is not the Spot Testnet: {url!r}")
    return f"https://{parsed.hostname}"


def verify_mt5_demo_account(trade_mode: object) -> None:
    """`trade_mode` is MetaTrader5.account_info().trade_mode."""
    if type(trade_mode) is not int or trade_mode != MT5_ACCOUNT_TRADE_MODE_DEMO:
        raise LiveTradingForbidden(f"MT5 account is not a demo account (trade_mode={trade_mode!r})")


def load_demo_environment(env: Mapping[str, str] | None = None) -> DemoEnvironment:
    env = os.environ if env is None else env
    mode = env.get("EXECUTION_MODE")
    if mode != ExecutionMode.DEMO.value:
        raise LiveTradingForbidden("EXECUTION_MODE must be explicitly set to 'demo'")
    live_flag = env.get("LIVE_TRADING_ENABLED")
    if live_flag is not None and live_flag.strip().lower() not in _FALSE:
        raise LiveTradingForbidden("LIVE_TRADING_ENABLED must be false")
    legacy_mode = env.get("TRADING_MODE")
    if legacy_mode is not None and legacy_mode.strip().lower() not in ("paper", "demo"):
        raise LiveTradingForbidden("TRADING_MODE must be 'paper' or 'demo'")
    present = sorted(name for name in FORBIDDEN_ENV_VARS if env.get(name))
    if present:
        raise LiveTradingForbidden(f"Production credential variables present: {', '.join(present)}")
    base_url = verify_binance_endpoint(env.get("BINANCE_TESTNET_BASE_URL", BINANCE_SPOT_TESTNET_DEFAULT))
    return DemoEnvironment(mode=ExecutionMode.DEMO, binance_base_url=base_url)
