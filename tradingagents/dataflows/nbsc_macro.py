"""NBSC (National Bureau of Statistics of China) macro vendor.

Fetches Chinese macroeconomic time series — CPI, PPI, GDP, PMI, M2/M1 money
supply, unemployment — from the NBS public data portal (data.stats.gov.cn)
via the `nbsc` package. No API key required.

Used by the news analyst to ground China macro commentary in actual numbers,
complementing FRED (which covers US macro). The routing layer treats this as
an optional macro_data vendor: if NBS is unreachable, the run degrades to
DATA_UNAVAILABLE rather than crashing.
"""
import logging
from datetime import datetime, timedelta

import nbsc

from .errors import NoMarketDataError

logger = logging.getLogger(__name__)

# Default trailing window when the caller does not specify one.
DEFAULT_LOOKBACK_DAYS = 365

# Rows cap for the rendered table.
MAX_ROWS = 40

# Friendly aliases → nbsc function names.  Anything not listed is matched
# case-insensitively against the keys; unknown indicators return guidance.
MACRO_INDICATORS = {
    # Inflation
    "cpi": "get_annual_inflation",
    "cpi_yoy": "get_annual_inflation",
    "cpi_mom": "get_recent_inflation",
    "inflation": "get_annual_inflation",
    "inflation_yoy": "get_annual_inflation",
    "inflation_mom": "get_recent_inflation",
    # PPI
    "ppi": "get_ppi_yoy",
    "ppi_yoy": "get_ppi_yoy",
    "ppi_mom": "get_ppi_mom",
    # GDP
    "gdp": "get_gdp_nominal",
    "gdp_nominal": "get_gdp_nominal",
    "gdp_real": "get_gdp_real",
    "gdp_index": "get_gdp_index",
    "gdp_qoq": "get_gdp_qoq_growth",
    # PMI
    "pmi": "get_manufacturing_pmi",
    "pmi_manufacturing": "get_manufacturing_pmi",
    "pmi_non_manufacturing": "get_non_manufacturing_pmi",
    "pmi_composite": "get_composite_pmi",
    # Money supply
    "m2": "get_m2",
    "m2_yoy": "get_m2_yoy",
    "m1": "get_m1",
    "m1_yoy": "get_m1_yoy",
    "money_supply": "get_m2",
    # Labor
    "unemployment": "get_unemployment_rate",
    "unemployment_rate": "get_unemployment_rate",
}


def _resolve_function(indicator: str):
    """Map a friendly alias to an nbsc function, or return None."""
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    func_name = MACRO_INDICATORS.get(key)
    if func_name is None:
        return None
    func = getattr(nbsc, func_name, None)
    return func


def _format_report(
    indicator: str,
    series_name: str,
    frequency: str,
    units: str,
    curr_date: str,
    look_back_days: int,
    points: list[tuple[str, float]],
) -> str:
    """Build a markdown report mirroring FRED's output style."""
    header = (
        f"## NBS China: {series_name}\n"
        f"- Units: {units}\n"
        f"- Frequency: {frequency}\n"
        f"- Window: ~{look_back_days} days ending {curr_date}\n"
    )

    if not points:
        return header + (
            f"\nNo observations for {indicator} in this window. "
            f"The series may report less frequently than the window "
            f"(try a longer look_back_days)."
        )

    first_date, first_val = points[0]
    last_date, last_val = points[-1]
    try:
        delta = last_val - first_val
        base = first_val
        pct = f" ({delta / base * 100:+.2f}%)" if base != 0 else ""
        summary = (
            f"\n**Latest:** {last_val} ({last_date}) | "
            f"**Change over window:** {delta:+.2f}{pct} "
            f"from {first_val} ({first_date})\n"
        )
    except (TypeError, ZeroDivisionError):
        summary = f"\n**Latest:** {last_val} ({last_date})\n"

    shown = points
    note = ""
    if len(points) > MAX_ROWS:
        shown = points[-MAX_ROWS:]
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(points)} observations)_\n"

    table = (
        "\n| Date | Value |\n| --- | --- |\n"
        + "\n".join(f"| {d} | {v} |" for d, v in shown)
        + "\n"
    )

    return header + summary + note + table


def _series_metadata(func_name: str) -> tuple[str, str, str]:
    """Return (series_name, frequency, units) for a given nbsc function."""
    meta = {
        "get_annual_inflation": ("CPI Year-on-Year", "Monthly", "% (decimal, 0.01=1%)"),
        "get_recent_inflation": ("CPI Month-on-Month", "Monthly", "% (decimal)"),
        "get_gdp_nominal": ("GDP Nominal", "Quarterly", "100M CNY (亿元)"),
        "get_gdp_real": ("GDP Real (constant prices)", "Quarterly", "100M CNY"),
        "get_gdp_index": ("GDP Index (same period last year=100)", "Quarterly", "Index"),
        "get_gdp_qoq_growth": ("GDP Quarter-on-Quarter Growth", "Quarterly", "%"),
        "get_manufacturing_pmi": ("Manufacturing PMI", "Monthly", "% (50=expansion threshold)"),
        "get_non_manufacturing_pmi": ("Non-Manufacturing PMI", "Monthly", "%"),
        "get_composite_pmi": ("Composite PMI Output Index", "Monthly", "%"),
        "get_m2": ("M2 Money Supply (end-of-period)", "Monthly", "100M CNY"),
        "get_m2_yoy": ("M2 Year-on-Year Growth", "Monthly", "%"),
        "get_m1": ("M1 Money Supply (end-of-period)", "Monthly", "100M CNY"),
        "get_m1_yoy": ("M1 Year-on-Year Growth", "Monthly", "%"),
        "get_unemployment_rate": ("Urban Survey Unemployment Rate", "Monthly", "%"),
        "get_ppi_yoy": ("PPI Year-on-Year (same month last year=100)", "Monthly", "Index"),
        "get_ppi_mom": ("PPI Month-on-Month (preceding month=100)", "Monthly", "Index"),
    }
    return meta.get(func_name, ("China Macro Indicator", "", ""))


def _filter_by_window(
    series, curr_date: str, look_back_days: int
) -> list[tuple[str, float]]:
    """Filter a pandas Series to the look-back window and return (date, value) pairs."""
    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=look_back_days)

    points = []
    for idx, val in series.items():
        # nbsc returns pandas Series with PeriodIndex or DatetimeIndex
        try:
            if hasattr(idx, "to_timestamp"):
                dt = idx.to_timestamp()
            else:
                dt = datetime.strptime(str(idx), "%Y-%m-%d")
        except (ValueError, TypeError):
            # Try quarterly format like "2026Q2"
            s = str(idx)
            if "Q" in s:
                try:
                    year, q = s.split("Q")
                    dt = datetime(int(year), int(q) * 3, 1)
                except (ValueError, IndexError):
                    continue
            else:
                continue

        if start_dt <= dt <= end_dt:
            try:
                points.append((str(idx), float(val)))
            except (ValueError, TypeError):
                points.append((str(idx), val))
    return points


def get_macro_data(
    indicator: str,
    curr_date: str,
    look_back_days: int | None = None,
) -> str:
    """Fetch a Chinese macroeconomic series from NBS as a formatted markdown report.

    Args:
        indicator: A friendly alias (e.g. "cpi", "pmi", "gdp", "m2",
            "unemployment", "ppi") or a known variation.
        curr_date: The as-of date (yyyy-mm-dd). Bounds the observation window.
        look_back_days: Trailing window length; None uses DEFAULT_LOOKBACK_DAYS.

    Returns:
        A markdown report with the series name, units, frequency, the latest
        value, the change over the window, and a recent observation table.
    """
    if look_back_days is None:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    func = _resolve_function(indicator)
    if func is None:
        known = ", ".join(sorted(MACRO_INDICATORS.keys()))
        return (
            f"NBS China: '{indicator}' is not a known indicator. "
            f"Available aliases: {known}"
        )

    # Resolve the alias to the function name for metadata lookup
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    func_name = MACRO_INDICATORS.get(key, "")
    series_name, frequency, units = _series_metadata(func_name)

    # nbsc functions take a start year string; we derive it from the look-back window
    start_year = str(
        (datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)).year
    )

    try:
        series = func(start_year)
    except Exception as e:
        logger.warning("NBS China: failed to fetch %s: %s", indicator, e)
        return (
            f"NBS China: failed to fetch '{indicator}' — {e}. "
            f"The NBS portal (data.stats.gov.cn) may be temporarily unavailable."
        )

    if series is None or len(series) == 0:
        return (
            f"NBS China: no data returned for '{indicator}'. "
            f"The NBS portal may be temporarily unavailable or the series "
            f"has no data starting from {start_year}."
        )

    points = _filter_by_window(series, curr_date, look_back_days)

    return _format_report(
        indicator=indicator,
        series_name=series_name,
        frequency=frequency,
        units=units,
        curr_date=curr_date,
        look_back_days=look_back_days,
        points=points,
    )
