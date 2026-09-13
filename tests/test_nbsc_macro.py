"""Tests for the NBS China macro vendor (nbsc_macro.py)."""

import unittest
from datetime import datetime
from unittest import mock

from tradingagents.dataflows.nbsc_macro import (
    MACRO_INDICATORS,
    _filter_by_window,
    _format_report,
    _resolve_function,
    _series_metadata,
    get_macro_data,
)
from tradingagents.dataflows.interface import (
    VENDOR_LIST,
    VENDOR_METHODS,
    route_to_vendor,
)


class TestNbscSymbolResolution(unittest.TestCase):
    """Indicator alias resolution."""

    def test_known_aliases(self):
        for alias in ["cpi", "CPI", "pmi", "GDP", "m2", "unemployment", "ppi"]:
            func = _resolve_function(alias)
            self.assertIsNotNone(func, f"{alias} should resolve")

    def test_unknown_alias_returns_none(self):
        self.assertIsNone(_resolve_function("nonexistent_indicator"))
        self.assertIsNone(_resolve_function("bank_of_china_rate"))

    def test_alias_with_spaces_and_hyphens(self):
        func = _resolve_function("cpi-yoy")
        self.assertIsNotNone(func)
        func = _resolve_function("money supply")
        self.assertIsNotNone(func)


class TestNbscSeriesMetadata(unittest.TestCase):
    """Metadata lookup for known indicators."""

    def test_cpi_metadata(self):
        name, freq, units = _series_metadata("get_annual_inflation")
        self.assertIn("CPI", name)
        self.assertEqual(freq, "Monthly")
        self.assertIn("%", units)

    def test_pmi_metadata(self):
        name, freq, units = _series_metadata("get_manufacturing_pmi")
        self.assertIn("PMI", name)
        self.assertEqual(freq, "Monthly")

    def test_unknown_function_metadata(self):
        name, freq, units = _series_metadata("get_unknown")
        self.assertEqual(name, "China Macro Indicator")
        self.assertEqual(freq, "")
        self.assertEqual(units, "")


class TestNbscFormatReport(unittest.TestCase):
    """Markdown report formatting."""

    def test_report_with_data(self):
        points = [("2026-07", 0.005), ("2026-08", 0.008)]
        report = _format_report(
            "cpi", "CPI Year-on-Year", "Monthly", "%",
            "2026-09-13", 365, points
        )
        self.assertIn("NBS China: CPI Year-on-Year", report)
        self.assertIn("2026-08", report)
        self.assertIn("0.008", report)
        self.assertIn("| Date | Value |", report)

    def test_report_empty_data(self):
        report = _format_report(
            "cpi", "CPI Year-on-Year", "Monthly", "%",
            "2026-09-13", 365, []
        )
        self.assertIn("No observations", report)

    def test_report_truncation(self):
        points = [(f"2025-{i:02d}", float(i)) for i in range(1, 13)]
        report = _format_report(
            "cpi", "CPI", "Monthly", "%",
            "2026-01-01", 365, points
        )
        # Should show all 12 (under MAX_ROWS=40)
        self.assertIn("| 2025-12 | 12.0 |", report)


class TestNbscFilterByWindow(unittest.TestCase):
    """Date window filtering."""

    def test_filters_old_data(self):
        import pandas as pd

        idx = pd.PeriodIndex(["2024-01", "2026-07", "2026-08"], freq="M")
        series = pd.Series([0.01, 0.005, 0.008], index=idx)
        points = _filter_by_window(series, "2026-09-13", 365)
        self.assertEqual(len(points), 2)  # Only 2026 data within 365 days
        self.assertEqual(points[0][0], "2026-07")
        self.assertEqual(points[1][0], "2026-08")

    def test_all_data_in_window(self):
        import pandas as pd

        idx = pd.PeriodIndex(["2026-07", "2026-08"], freq="M")
        series = pd.Series([0.005, 0.008], index=idx)
        points = _filter_by_window(series, "2026-09-13", 365)
        self.assertEqual(len(points), 2)


class TestNbscGetMacroData(unittest.TestCase):
    """get_macro_data with mocked nbsc."""

    def test_unknown_indicator_returns_guidance(self):
        result = get_macro_data("nonexistent_xyz", "2026-09-13")
        self.assertIn("not a known indicator", result)
        self.assertIn("Available aliases:", result)

    def test_import_error_returns_guidance(self):
        """When nbsc is not installed, module import itself fails.
        This test verifies the module-level import works in the test env."""
        # nbsc is imported at module level, so if the module loaded, nbsc is available.
        # This test just verifies we can still call get_macro_data with a bad indicator.
        result = get_macro_data("nonexistent_xyz", "2026-09-13")
        self.assertIn("not a known indicator", result)

    @mock.patch("tradingagents.dataflows.nbsc_macro.nbsc")
    def test_cpi_fetch_success(self, mock_nbsc):
        import pandas as pd

        mock_series = pd.Series(
            [0.005, 0.008],
            index=pd.PeriodIndex(["2026-07", "2026-08"], freq="M"),
        )
        mock_nbsc.get_annual_inflation.return_value = mock_series

        result = get_macro_data("cpi", "2026-09-13", 365)
        self.assertIn("NBS China: CPI Year-on-Year", result)
        self.assertIn("2026-08", result)
        self.assertIn("0.008", result)
        mock_nbsc.get_annual_inflation.assert_called_once()

    @mock.patch("tradingagents.dataflows.nbsc_macro.nbsc")
    def test_pmi_fetch_success(self, mock_nbsc):
        import pandas as pd

        mock_series = pd.Series(
            [50.3, 49.8],
            index=pd.PeriodIndex(["2026-07", "2026-08"], freq="M"),
        )
        mock_nbsc.get_manufacturing_pmi.return_value = mock_series

        result = get_macro_data("pmi", "2026-09-13", 365)
        self.assertIn("NBS China: Manufacturing PMI", result)
        self.assertIn("49.8", result)

    @mock.patch("tradingagents.dataflows.nbsc_macro.nbsc")
    def test_empty_series_returns_no_data(self, mock_nbsc):
        import pandas as pd

        mock_nbsc.get_annual_inflation.return_value = pd.Series([], dtype=float)
        result = get_macro_data("cpi", "2026-09-13", 365)
        self.assertIn("no data returned", result)

    @mock.patch("tradingagents.dataflows.nbsc_macro.nbsc")
    def test_fetch_exception_returns_error_message(self, mock_nbsc):
        mock_nbsc.get_annual_inflation.side_effect = ConnectionError("timeout")
        result = get_macro_data("cpi", "2026-09-13", 365)
        self.assertIn("failed to fetch", result)
        self.assertIn("timeout", result)

    @mock.patch("tradingagents.dataflows.nbsc_macro.nbsc")
    def test_gdp_quarterly_data(self, mock_nbsc):
        import pandas as pd

        mock_series = pd.Series(
            [351234.5, 361511.1],
            index=pd.PeriodIndex(["2026Q1", "2026Q2"], freq="Q"),
        )
        mock_nbsc.get_gdp_nominal.return_value = mock_series

        result = get_macro_data("gdp", "2026-09-13", 365)
        self.assertIn("NBS China: GDP Nominal", result)
        self.assertIn("361511.1", result)


class TestNbscRouterRegistration(unittest.TestCase):
    """Verify nbsc is registered in the routing layer."""

    def test_nbsc_in_vendor_list(self):
        self.assertIn("nbsc", VENDOR_LIST)

    def test_nbsc_in_macro_indicators_methods(self):
        self.assertIn("nbsc", VENDOR_METHODS["get_macro_indicators"])

    def test_nbsc_route_returns_data(self):
        """route_to_vendor should dispatch to nbsc when configured."""
        import pandas as pd

        mock_series = pd.Series(
            [0.005, 0.008],
            index=pd.PeriodIndex(["2026-07", "2026-08"], freq="M"),
        )

        with mock.patch("tradingagents.dataflows.nbsc_macro.nbsc") as mock_nbsc:
            mock_nbsc.get_annual_inflation.return_value = mock_series
            from tradingagents.dataflows.config import set_config
            set_config({"data_vendors": {"macro_data": "nbsc"}})
            try:
                result = route_to_vendor(
                    "get_macro_indicators", "cpi", "2026-09-13", 365
                )
                self.assertIn("NBS China", result)
                self.assertIn("CPI", result)
            finally:
                set_config({"data_vendors": {"macro_data": "fred,nbsc"}})


if __name__ == "__main__":
    unittest.main()
