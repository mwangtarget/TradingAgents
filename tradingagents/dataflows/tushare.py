"""Tushare market and fundamentals vendor integration.

This vendor is optional and intentionally lazy: if the package or token are
missing, the router treats it as "vendor unavailable" rather than crashing the
whole run. The data functions also raise the project-specific typed errors so
fallback routing can handle retries consistently.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import pandas as pd

from .errors import NoMarketDataError, VendorNotConfiguredError, VendorRateLimitError

logger = logging.getLogger(__name__)

try:  # pragma: no cover - dependency is optional at runtime
    import tushare as ts  # type: ignore
except Exception:  # pragma: no cover - handled below as a configured error
    ts = None


class TushareNotConfiguredError(VendorNotConfiguredError):
    """Raised when Tushare is selected but its token/package is missing."""


class TushareRateLimitError(VendorRateLimitError):
    """Raised when the Tushare API denies requests because of throttling."""


def get_api_token() -> str:
    """Retrieve the Tushare token from either of the common environment names."""
    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TUSHARE_API_KEY")
    if not token:
        raise TushareNotConfiguredError(
            "TUSHARE_TOKEN or TUSHARE_API_KEY environment variable is not set. "
            "Get a token at https://tushare.pro/ and export it before enabling tushare."
        )
    return token


def _require_api():
    if ts is None:
        raise TushareNotConfiguredError(
            "tushare package is not installed. Install it with `pip install tushare`."
        )
    return ts.pro_api(token=get_api_token())


def _coerce_date_column(df: pd.DataFrame, curr_date: str | None = None) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return df

    date_candidates = [
        "trade_date",
        "report_date",
        "end_date",
        "ann_date",
        "pub_date",
        "fiscal_date",
    ]
    for col in date_candidates:
        if col in df.columns:
            df = df.copy()
            df[col] = pd.to_datetime(df[col], format="%Y%m%d", errors="coerce")
            df = df[df[col].notna()].copy()
            if curr_date is not None:
                cutoff = pd.Timestamp(curr_date)
                df = df[df[col] <= cutoff].copy()
            if df.empty:
                return df
            return df.sort_values(col, ascending=False)
    return df


def _handle_api_error(exc: Exception, context: str):
    message = str(exc).lower()
    if any(token in message for token in ("rate limit", "too many", "429", "throttl")):
        raise TushareRateLimitError(f"Tushare rate limit exceeded while fetching {context}: {exc}") from exc
    if any(token in message for token in ("token", "auth", "permission", "apikey", "not authorized")):
        raise TushareNotConfiguredError(
            f"Tushare authentication failed while fetching {context}: {exc}"
        ) from exc
    raise exc


def _tushare_result(symbol: str, df: pd.DataFrame, *, date_label: str | None = None) -> str:
    if df is None or getattr(df, "empty", True):
        raise NoMarketDataError(symbol, symbol, "no rows returned by Tushare")

    csv = df.to_csv(index=False)
    header = f"# {date_label or 'Tushare data'} for {symbol}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """Return daily OHLCV data from Tushare for the requested window."""
    api = _require_api()
    ts_code = symbol.strip()
    try:
        df = api.daily(
            ts_code=ts_code,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
        )
    except Exception as exc:  # pragma: no cover - exercised through mocked API in tests
        _handle_api_error(exc, "daily market data")

    if df is None or getattr(df, "empty", True):
        raise NoMarketDataError(symbol, ts_code, f"no rows between {start_date} and {end_date}")

    df = df.copy()
    df = df.rename(columns={
        "trade_date": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "vol": "Volume",
        "amount": "Amount",
        "pct_chg": "PctChg",
        "turnover_rate": "TurnoverRate",
        "ts_code": "Ticker",
    })
    if "Date" in df.columns:
        df = df.sort_values("Date", ascending=True)
    return _tushare_result(symbol, df, date_label=f"Stock data from {start_date} to {end_date}")


VALID_TUSHARE_STATEMENT_METHODS = (
    "fina_indicator",
    "income",
    "balancesheet",
    "cashflow",
)


# ---------------------------------------------------------------------------
# Technical indicators (tushare-native, no yfinance dependency)
# ---------------------------------------------------------------------------

# Same indicator whitelist as the yfinance vendor so the LLM tool contract
# is identical across vendors.
_TUSHARE_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": (
        "50 SMA: A medium-term trend indicator. "
        "Usage: Identify trend direction and serve as dynamic support/resistance. "
        "Tips: It lags price; combine with faster indicators for timely signals."
    ),
    "close_200_sma": (
        "200 SMA: A long-term trend benchmark. "
        "Usage: Confirm overall market trend and identify golden/death cross setups. "
        "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
    ),
    "close_10_ema": (
        "10 EMA: A responsive short-term average. "
        "Usage: Capture quick shifts in momentum and potential entry points. "
        "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
    ),
    "macd": (
        "MACD: Computes momentum via differences of EMAs. "
        "Usage: Look for crossovers and divergence as signals of trend changes. "
        "Tips: Confirm with other indicators in low-volatility or sideways markets."
    ),
    "macds": (
        "MACD Signal: An EMA smoothing of the MACD line. "
        "Usage: Use crossovers with the MACD line to trigger trades. "
        "Tips: Should be part of a broader strategy to avoid false positives."
    ),
    "macdh": (
        "MACD Histogram: Shows the gap between the MACD line and its signal. "
        "Usage: Visualize momentum strength and spot divergence early. "
        "Tips: Can be volatile; complement with additional filters in fast-moving markets."
    ),
    "rsi": (
        "RSI: Measures momentum to flag overbought/oversold conditions. "
        "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
        "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
    ),
    "boll": (
        "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
        "Usage: Acts as a dynamic benchmark for price movement. "
        "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
    ),
    "boll_ub": (
        "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
        "Usage: Signals potential overbought conditions and breakout zones. "
        "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
    ),
    "boll_lb": (
        "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
        "Usage: Indicates potential oversold conditions. "
        "Tips: Use additional analysis to avoid false reversal signals."
    ),
    "atr": (
        "ATR: Averages true range to measure volatility. "
        "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
        "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
    ),
    "vwma": (
        "VWMA: A moving average weighted by volume. "
        "Usage: Confirm trends by integrating price action with volume data. "
        "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
    ),
    "mfi": (
        "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
        "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
        "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
    ),
}


def _fetch_tushare_ohlcv(symbol: str, curr_date: str, look_back_days: int) -> pd.DataFrame:
    """Fetch OHLCV from Tushare pro.daily() with enough history for indicator calculation.

    We fetch 5 years of data (same window as the yfinance vendor) so that
    long-range indicators like 200 SMA have sufficient warm-up data.
    """
    api = _require_api()
    ts_code = symbol.strip()

    curr_dt = pd.Timestamp(curr_date)
    # Fetch 5 years to ensure enough warm-up for 200 SMA etc.
    start_dt = curr_dt - pd.DateOffset(years=5)
    start_str = start_dt.strftime("%Y%m%d")
    end_str = curr_dt.strftime("%Y%m%d")

    try:
        df = api.daily(ts_code=ts_code, start_date=start_str, end_date=end_str)
    except Exception as exc:
        _handle_api_error(exc, "daily OHLCV for indicators")

    if df is None or getattr(df, "empty", True):
        raise NoMarketDataError(
            symbol, ts_code, f"no daily rows between {start_str} and {end_str}"
        )

    # Rename to stockstats-compatible columns
    df = df.rename(columns={
        "trade_date": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "vol": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d")
    df = df.sort_values("Date", ascending=True).reset_index(drop=True)
    # Filter to curr_date to prevent look-ahead
    df = df[df["Date"] <= curr_dt]
    return df[["Date", "Open", "High", "Low", "Close", "Volume"]]


def get_indicators(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int = 30,
) -> str:
    """Compute a technical indicator using Tushare OHLCV + stockstats.

    This is the tushare-native replacement for the yfinance indicator
    vendor.  It fetches daily data directly from ``tushare pro.daily()``
    (no yfinance dependency) and calculates indicators via ``stockstats``.
    """
    from stockstats import wrap as wrap_stockstats
    from dateutil.relativedelta import relativedelta

    indicator = indicator.strip().lower()
    if indicator not in _TUSHARE_INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. "
            f"Please choose from: {list(_TUSHARE_INDICATOR_DESCRIPTIONS.keys())}"
        )

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    # Fetch OHLCV directly from tushare (no yfinance)
    df = _fetch_tushare_ohlcv(symbol, curr_date, look_back_days)

    # Calculate indicator via stockstats
    stock_df = wrap_stockstats(df.copy())
    stock_df[indicator]  # triggers calculation

    # Build date → value dict
    stock_df["_date_str"] = stock_df["Date"].dt.strftime("%Y-%m-%d")
    value_map: dict[str, str] = {}
    for _, row in stock_df.iterrows():
        val = row[indicator]
        value_map[row["_date_str"]] = "N/A" if pd.isna(val) else str(val)

    # Build output string for the look-back window
    lines: list[str] = []
    current_dt = curr_date_dt
    while current_dt >= before:
        date_str = current_dt.strftime("%Y-%m-%d")
        lines.append(f"{date_str}: {value_map.get(date_str, 'N/A: Not a trading day (weekend or holiday)')}")
        current_dt -= relativedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + _TUSHARE_INDICATOR_DESCRIPTIONS.get(indicator, "No description available.")
    )


def _fetch_statement(ticker: str, method_name: str, curr_date: str | None = None) -> pd.DataFrame:
    api = _require_api()
    method_name = (method_name or "").strip()
    if method_name not in VALID_TUSHARE_STATEMENT_METHODS:
        valid = ", ".join(VALID_TUSHARE_STATEMENT_METHODS)
        message = (
            f"Unsupported Tushare statement method: '{method_name}'. "
            f"Valid methods are: {valid}. "
            f"This adapter expects the raw Tushare SDK method name, not the "
            f"project wrapper name (for example, use 'balancesheet' instead of 'balance_sheet', "
            f"or 'income' instead of 'get_income_statement')."
        )
        logger.error(message)
        raise ValueError(message)

    method = getattr(api, method_name, None)
    if method is None:
        message = (
            f"Tushare SDK does not expose method '{method_name}'. "
            f"Available adapter methods: {', '.join(VALID_TUSHARE_STATEMENT_METHODS)}. "
            f"Check that the installed tushare package version supports this interface."
        )
        logger.error(message)
        raise ValueError(message)

    try:
        df = method(ts_code=ticker)
    except Exception as exc:
        _handle_api_error(exc, method_name)

    if df is None or getattr(df, "empty", True):
        raise NoMarketDataError(ticker, ticker, f"no {method_name} rows returned")

    df = _coerce_date_column(df, curr_date)
    if getattr(df, "empty", False):
        raise NoMarketDataError(
            ticker,
            ticker,
            f"no {method_name} rows available on or before {curr_date}",
        )
    return df


def get_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    """Return a Tushare fundamentals snapshot as a JSON payload."""
    for method_name in ("fina_indicator", "income", "balancesheet", "cashflow"):
        try:
            df = _fetch_statement(ticker, method_name, curr_date)
            return df.to_json(orient="records", force_ascii=False)
        except NoMarketDataError:
            if method_name == "cashflow":
                raise
            continue
        except ValueError:
            if method_name == "cashflow":
                raise
            continue
    raise NoMarketDataError(ticker, ticker, "no fundamentals rows available from Tushare")


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str | None = None):
    """Fetch balance-sheet rows from Tushare and filter by the requested date."""
    df = _fetch_statement(ticker, "balancesheet", curr_date)
    return df.to_json(orient="records", force_ascii=False)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str | None = None):
    """Fetch cash-flow rows from Tushare and filter by the requested date."""
    df = _fetch_statement(ticker, "cashflow", curr_date)
    return df.to_json(orient="records", force_ascii=False)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str | None = None):
    """Fetch income-statement rows from Tushare and filter by the requested date."""
    df = _fetch_statement(ticker, "income", curr_date)
    return df.to_json(orient="records", force_ascii=False)
