#!/usr/bin/env python3
"""Batch runner for TradingAgents — analyze multiple tickers non-interactively.

Usage
-----
1. Create a batch config file (YAML or JSON):

```yaml
# batch_config.yaml
tickers:
  - 300308.SZ    # 中际旭创
  - 300750.SZ    # 宁德时代
  - 688256.SH    # 寒武纪

# Analysis date (YYYY-MM-DD). Omit for today.
analysis_date: "2026-09-12"

# Analysts to run (any subset). Order is fixed internally.
analysts: [market, social, news, fundamentals]

# Research depth: number of debate + risk rounds (1 = fast, 2 = thorough)
research_depth: 1

# Output language for reports
output_language: English

# LLM settings
llm_provider: openai
quick_think_llm: gpt-5.6-luna
deep_think_llm: gpt-5.6
# backend_url: null        # use provider default
# temperature: null

# Results directory (per-ticker subdirs created automatically)
results_dir: ./batch_results

# Stop the entire batch if one ticker fails
stop_on_error: false

# Delay (seconds) between tickers to avoid rate-limiting
delay_between_tickers: 5
```

2. Run the batch:

    python batch_run.py batch_config.yaml
    python batch_run.py batch_config.yaml --only 300308.SZ,688256.SH
    python batch_run.py batch_config.yaml --skip 300750.SZ

    # Or pass tickers inline without a config file:
    python batch_run.py --tickers 300308.SZ,300750.SZ,688256.SH --date 2026-09-12

3. Results are written to {results_dir}/{ticker}/{date}/reports/
   A summary CSV with all decisions is written to {results_dir}/batch_summary.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# --- YAML support (optional, falls back to JSON) ---
try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

logger = logging.getLogger("batch_run")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_BATCH_CONFIG: dict[str, Any] = {
    "tickers": [],
    "analysis_date": None,          # None = today
    "analysts": ["market", "social", "news", "fundamentals"],
    "research_depth": 1,
    "output_language": "English",
    "llm_provider": None,           # None = read from .env / DEFAULT_CONFIG
    "quick_think_llm": None,
    "deep_think_llm": None,
    "backend_url": None,
    "temperature": None,
    "results_dir": "./batch_results",
    "stop_on_error": False,
    "delay_between_tickers": 5,
}


def load_batch_config(config_path: str | None) -> dict[str, Any]:
    """Load batch config from YAML or JSON file, merged with defaults."""
    cfg = dict(DEFAULT_BATCH_CONFIG)

    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            if not _HAS_YAML:
                raise ImportError("PyYAML not installed. pip install pyyaml or use .json")
            file_cfg = yaml.safe_load(text)
        elif path.suffix == ".json":
            file_cfg = json.loads(text)
        else:
            # Try YAML first (it's a superset of JSON), then JSON
            if _HAS_YAML:
                file_cfg = yaml.safe_load(text)
            else:
                file_cfg = json.loads(text)
        cfg.update(file_cfg)

    return cfg


# ---------------------------------------------------------------------------
# Single-ticker analysis
# ---------------------------------------------------------------------------

def run_single_ticker(
    ticker: str,
    analysis_date: str,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Run analysis for one ticker. Returns result dict with reports + decision.

    Returns:
        {
            "ticker": str,
            "date": str,
            "status": "success" | "error",
            "decision": str | None,    # final trade decision excerpt
            "reports_dir": str | None,
            "error": str | None,
            "elapsed_seconds": float,
        }
    """
    start = time.time()

    # Import here so --help is fast and so env vars set by the caller
    # (e.g. from .env) are already in place.
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.reporting import write_report_tree
    from tradingagents.dataflows.config import set_config

    # Build config from DEFAULT_CONFIG + batch overrides
    config = DEFAULT_CONFIG.copy()
    if cfg.get("research_depth"):
        config["max_debate_rounds"] = cfg["research_depth"]
        config["max_risk_discuss_rounds"] = cfg["research_depth"]
    if cfg.get("quick_think_llm"):
        config["quick_think_llm"] = cfg["quick_think_llm"]
    if cfg.get("deep_think_llm"):
        config["deep_think_llm"] = cfg["deep_think_llm"]
    if cfg.get("llm_provider"):
        provider = cfg["llm_provider"]
        config["llm_provider"] = provider
        # Auto-resolve backend URL for known providers if not explicitly set
        if not cfg.get("backend_url"):
            provider_lower = provider.lower()
            if provider_lower == "minimax":
                config["backend_url"] = "https://api.minimax.io/v1"
            elif provider_lower == "minimax-cn":
                config["backend_url"] = "https://api.minimaxi.com/v1"
    if cfg.get("backend_url"):
        config["backend_url"] = cfg["backend_url"]
    if cfg.get("temperature") is not None:
        config["temperature"] = cfg["temperature"]
    if cfg.get("output_language"):
        config["output_language"] = cfg["output_language"]

    # Analysts
    analysts = cfg.get("analysts", ["market", "social", "news", "fundamentals"])
    # Normalize to ordered list
    analyst_order = ["market", "social", "news", "fundamentals"]
    selected_analysts = [a for a in analyst_order if a in analysts]

    # Resolve asset type
    ticker_upper = ticker.upper()
    if ticker_upper.endswith((".SS", ".SZ", ".BJ")):
        asset_type = "stock"
    elif ticker_upper.endswith((".HK", ".T", ".L", ".TO", ".AX", ".NS", ".BO")):
        asset_type = "stock"
    else:
        asset_type = "stock"

    logger.info("Starting analysis: %s on %s (analysts: %s)", ticker, analysis_date, selected_analysts)

    # Initialize graph
    graph = TradingAgentsGraph(
        selected_analysts,
        config=config,
        debug=False,
    )

    # Resolve instrument context
    instrument_context = graph.resolve_instrument_context(ticker, asset_type)

    # Create initial state
    init_state = graph.propagator.create_initial_state(
        ticker,
        analysis_date,
        asset_type=asset_type,
        instrument_context=instrument_context,
    )

    # Get graph args
    args = graph.propagator.get_graph_args()

    # Stream the analysis
    trace = []
    try:
        for chunk in graph.graph.stream(init_state, **args):
            trace.append(chunk)
            # Log progress
            for key in ["market_report", "sentiment_report", "news_report",
                        "fundamentals_report", "investment_debate_state",
                        "trader_investment_plan", "risk_debate_state"]:
                if key in chunk and chunk[key]:
                    logger.info("  [%s] %s completed", ticker, key)
    except Exception as e:
        elapsed = time.time() - start
        logger.error("FAILED: %s — %s", ticker, e)
        return {
            "ticker": ticker,
            "date": analysis_date,
            "status": "error",
            "decision": None,
            "reports_dir": None,
            "error": str(e),
            "elapsed_seconds": round(elapsed, 1),
        }

    # Merge chunks into final state
    final_state = {}
    for chunk in trace:
        final_state.update(chunk)

    elapsed = time.time() - start

    # Extract final decision
    decision = None
    if final_state.get("risk_debate_state", {}).get("judge_decision"):
        decision = final_state["risk_debate_state"]["judge_decision"]
    elif final_state.get("final_trade_decision"):
        decision = final_state["final_trade_decision"]

    # Save reports
    results_root = Path(cfg.get("results_dir", "./batch_results"))
    ticker_dir = results_root / ticker / analysis_date
    report_dir = ticker_dir / "reports"
    try:
        report_file = write_report_tree(final_state, ticker, ticker_dir)
        logger.info("Reports saved to: %s", ticker_dir)
    except Exception as e:
        logger.warning("Failed to save reports for %s: %s", ticker, e)
        report_file = None

    # Save raw state as JSON
    try:
        state_file = ticker_dir / "final_state.json"
        # Strip non-serializable values
        clean_state = {}
        for k, v in final_state.items():
            try:
                json.dumps(v, ensure_ascii=False)
                clean_state[k] = v
            except (TypeError, ValueError):
                clean_state[k] = str(v)[:500]
        state_file.write_text(
            json.dumps(clean_state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass

    logger.info("DONE: %s in %.1fs — decision: %s",
                ticker, elapsed, (decision or "N/A")[:120])

    return {
        "ticker": ticker,
        "date": analysis_date,
        "status": "success",
        "decision": decision,
        "reports_dir": str(ticker_dir) if ticker_dir.exists() else None,
        "error": None,
        "elapsed_seconds": round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# Batch orchestration
# ---------------------------------------------------------------------------

def run_batch(cfg: dict[str, Any], only: list[str] | None = None, skip: list[str] | None = None):
    """Run analysis for all tickers in the config."""
    tickers = cfg.get("tickers", [])
    if not tickers:
        logger.error("No tickers in config. Use --tickers or add them to the config file.")
        sys.exit(1)

    # Apply --only / --skip filters
    if only:
        only_set = {t.strip().upper() for t in only}
        tickers = [t for t in tickers if t.upper() in only_set]
    if skip:
        skip_set = {t.strip().upper() for t in skip}
        tickers = [t for t in tickers if t.upper() not in skip_set]

    if not tickers:
        logger.error("No tickers left after filtering.")
        sys.exit(1)

    # Resolve analysis date
    analysis_date = cfg.get("analysis_date") or datetime.datetime.now().strftime("%Y-%m-%d")

    logger.info("=" * 60)
    logger.info("Batch Run: %d ticker(s) on %s", len(tickers), analysis_date)
    logger.info("Analysts: %s", cfg.get("analysts", "all"))
    logger.info("Results:  %s", cfg.get("results_dir", "./batch_results"))
    logger.info("=" * 60)

    results = []
    delay = cfg.get("delay_between_tickers", 0)

    for i, ticker in enumerate(tickers):
        ticker = ticker.strip()
        if not ticker:
            continue

        logger.info("[%d/%d] %s", i + 1, len(tickers), ticker)

        try:
            result = run_single_ticker(ticker, analysis_date, cfg)
        except Exception as e:
            result = {
                "ticker": ticker,
                "date": analysis_date,
                "status": "error",
                "decision": None,
                "reports_dir": None,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_seconds": 0,
            }
            logger.error("UNEXPECTED ERROR: %s\n%s", ticker, traceback.format_exc())

        results.append(result)

        # Write summary CSV after each ticker (so partial results survive a crash)
        write_summary_csv(results, cfg)

        # Stop on error
        if result["status"] == "error" and cfg.get("stop_on_error", False):
            logger.error("stop_on_error=True — aborting batch.")
            break

        # Delay between tickers
        if i < len(tickers) - 1 and delay > 0:
            logger.info("Waiting %ds before next ticker...", delay)
            time.sleep(delay)

    # Final summary
    logger.info("=" * 60)
    logger.info("Batch Complete: %d/%d succeeded", sum(1 for r in results if r["status"] == "success"), len(results))
    for r in results:
        status_icon = "✅" if r["status"] == "success" else "❌"
        decision_preview = (r["decision"] or "")[:80].replace("\n", " ")
        logger.info("  %s %s — %s — %.1fs", status_icon, r["ticker"], decision_preview, r["elapsed_seconds"])
    logger.info("Summary CSV: %s", Path(cfg.get("results_dir", "./batch_results")) / "batch_summary.csv")
    logger.info("=" * 60)

    return results


def write_summary_csv(results: list[dict], cfg: dict[str, Any]):
    """Write (or overwrite) the batch summary CSV."""
    results_dir = Path(cfg.get("results_dir", "./batch_results"))
    results_dir.mkdir(parents=True, exist_ok=True)
    csv_path = results_dir / "batch_summary.csv"

    fieldnames = [
        "ticker", "date", "status", "elapsed_seconds",
        "decision", "reports_dir", "error",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            row = {k: r.get(k, "") for k in fieldnames}
            # Truncate decision for CSV readability
            if row["decision"] and len(row["decision"]) > 500:
                row["decision"] = row["decision"][:497] + "..."
            writer.writerow(row)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Batch-run TradingAgents on multiple tickers non-interactively.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # From a YAML config file
  python batch_run.py batch_config.yaml

  # Inline tickers, skip the config file
  python batch_run.py --tickers 300308.SZ,300750.SZ,688256.SH --date 2026-09-12

  # Run only specific tickers from the config
  python batch_run.py batch_config.yaml --only 300308.SZ,688256.SH

  # Skip a ticker
  python batch_run.py batch_config.yaml --skip 300750.SZ
        """,
    )
    parser.add_argument(
        "config",
        nargs="?",
        help="Path to batch config file (YAML or JSON). Optional if --tickers is used.",
    )
    parser.add_argument(
        "--tickers",
        help="Comma-separated ticker list (overrides config file).",
    )
    parser.add_argument(
        "--date",
        help="Analysis date YYYY-MM-DD (overrides config file).",
    )
    parser.add_argument(
        "--only",
        help="Comma-separated tickers to run (filter from config).",
    )
    parser.add_argument(
        "--skip",
        help="Comma-separated tickers to skip.",
    )
    parser.add_argument(
        "--analysts",
        help="Comma-separated analyst types: market,social,news,fundamentals",
    )
    parser.add_argument(
        "--depth",
        type=int,
        help="Research depth (debate + risk rounds). Default: 1",
    )
    parser.add_argument(
        "--language",
        help="Output language for reports. Default: English",
    )
    parser.add_argument(
        "--provider",
        help="LLM provider (openai, google, anthropic, etc.).",
    )
    parser.add_argument(
        "--results-dir",
        help="Directory for results. Default: ./batch_results",
    )
    args = parser.parse_args()

    # Load config
    cfg = load_batch_config(args.config)

    # CLI overrides
    if args.tickers:
        cfg["tickers"] = [t.strip() for t in args.tickers.split(",")]
    if args.date:
        cfg["analysis_date"] = args.date
    if args.analysts:
        cfg["analysts"] = [a.strip() for a in args.analysts.split(",")]
    if args.depth:
        cfg["research_depth"] = args.depth
    if args.language:
        cfg["output_language"] = args.language
    if args.provider:
        cfg["llm_provider"] = args.provider
    if args.results_dir:
        cfg["results_dir"] = args.results_dir

    # Validate
    if not cfg["tickers"]:
        parser.error("No tickers specified. Use a config file or --tickers.")

    # Run
    only_list = args.only.split(",") if args.only else None
    skip_list = args.skip.split(",") if args.skip else None

    run_batch(cfg, only=only_list, skip=skip_list)


if __name__ == "__main__":
    main()
