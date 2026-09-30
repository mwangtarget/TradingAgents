#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Parallel TradingAgents Batch Runner — 5 concurrent workers.

Reads the stock list, skips already-completed stocks in the DB,
runs remaining stocks with 5 parallel processes, inserts results into SQLite.

Usage:
    nohup python parallel_batch_run.py --stock-list results/sector_top5_20260915.csv \
        --date 2026-09-20 --workers 5 --db-path trading_results.db \
        > parallel_batch_20260920.log 2>&1 &
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import multiprocessing as mp
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# --- Project paths ---
TA_PROJECT = Path("/Users/michaelwang/trading/TradingAgents_Mine/TradingAgents")
BATCH_DIR = TA_PROJECT / "batch_pipeline"
DB_PATH = BATCH_DIR / "trading_results.db"
STOCK_LIST_DEFAULT = BATCH_DIR / "stock_lists" / "sector_top3_batch_a.csv"

logger = logging.getLogger("parallel_batch")

# ---------------------------------------------------------------------------
# DB helpers (inline to avoid import issues in child processes)
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS run_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    analysis_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    sector_name TEXT,
    price_now REAL,
    decision TEXT,
    decision_detail TEXT,
    target_price REAL,
    stop_price REAL,
    confidence TEXT,
    market_report TEXT,
    sentiment_report TEXT,
    news_report TEXT,
    fundamentals_report TEXT,
    trader_plan TEXT,
    elapsed_seconds REAL,
    status TEXT,
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_run_results_stock_date
ON run_results(stock_id, analysis_date);
CREATE INDEX IF NOT EXISTS idx_run_results_run_date
ON run_results(run_date);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute(CREATE_TABLE_SQL)
    conn.executescript(CREATE_INDEX_SQL)
    conn.commit()
    return conn


def insert_result(conn: sqlite3.Connection, result: dict) -> int:
    cols = [
        "run_date", "analysis_date", "stock_id", "stock_name", "sector_name",
        "price_now", "decision", "decision_detail", "target_price", "stop_price",
        "confidence", "market_report", "sentiment_report", "news_report",
        "fundamentals_report", "trader_plan", "elapsed_seconds", "status",
        "error_message",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    values = [result.get(c) for c in cols]
    cursor = conn.execute(
        f"INSERT INTO run_results ({', '.join(cols)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return cursor.lastrowid


def get_completed_stocks(conn: sqlite3.Connection, analysis_date: str) -> set:
    """Return set of stock_ids already completed (success) for this date."""
    rows = conn.execute(
        "SELECT stock_id FROM run_results WHERE analysis_date=? AND status='success'",
        (analysis_date,),
    ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Decision parsing
# ---------------------------------------------------------------------------

def parse_decision(final_trade_decision: str | None,
                   trader_plan: str | None) -> dict:
    text = (final_trade_decision or "") + "\n" + (trader_plan or "")
    result = {"decision": None, "target_price": None,
              "stop_price": None, "confidence": None}
    if not text.strip():
        return result

    for pat in [r'\*\*(?:Rating|Action|Recommendation|Decision)\*\*:\s*(\w+)',
                r'(?:Rating|Action|Recommendation|Decision):\s*(\w+)']:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result["decision"] = m.group(1).strip().upper()
            break

    for pat in [r'target\s*(?:price)?\s*(?:of\s*)?[¥￥]?\s*([\d,]+\.?\d*)',
                r'entry\s*(?:at|price)?\s*(?:the\s*)?[¥￥]?\s*([\d,]+\.?\d*)',
                r'[¥￥]\s*([\d,]+\.?\d*)\s*(?:retest|target|entry)',
                r'target\s*[:：]\s*[¥￥]?\s*([\d,]+\.?\d*)']:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                result["target_price"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
            break

    for pat in [r'stop\s*(?:price|loss)?\s*(?:at\s*)?[¥￥]?\s*([\d,]+\.?\d*)',
                r'hard\s*(?:daily-close\s*)?stop\s*at\s*[¥￥]?\s*([\d,]+\.?\d*)',
                r'stop\s*[:：]\s*[¥￥]?\s*([\d,]+\.?\d*)']:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                result["stop_price"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
            break

    for pat in [r'\*\*Confidence\*\*:\s*(\w+)', r'Confidence:\s*(\w+)']:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result["confidence"] = m.group(1).strip().capitalize()
            break

    return result


# ---------------------------------------------------------------------------
# Worker process — runs one ticker analysis
# ---------------------------------------------------------------------------

def worker_run_ticker(args_tuple) -> dict:
    """Run TradingAgents for one ticker. Designed for multiprocessing."""
    ticker, analysis_date, cfg, stock_meta, db_path_str = args_tuple

    # Ensure absolute path before any chdir
    db_path_str = str(Path(db_path_str).resolve())

    start = time.time()
    original_cwd = os.getcwd()
    os.chdir(str(TA_PROJECT))

    # Each worker gets its own DB connection — must init tables
    conn = sqlite3.connect(db_path_str, timeout=60)
    conn.execute(CREATE_TABLE_SQL)
    conn.executescript(CREATE_INDEX_SQL)
    conn.commit()

    try:
        # Load env
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.reporting import write_report_tree

        # Load TUSHARE_TOKEN from config or env
        token = os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            for env_path in [os.path.expanduser("~/.qclaw/workspace/.env"),
                             os.path.expanduser("~/.qclaw/workspace/a-stock-quant/.env")]:
                if os.path.exists(env_path):
                    with open(env_path) as f:
                        for line in f:
                            if line.strip().startswith("TUSHARE_TOKEN="):
                                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                                break
                    if token:
                        break
        if token:
            os.environ["TUSHARE_TOKEN"] = token

        # Load MINIMAX_CN_API_KEY from .env in project
        env_file = TA_PROJECT / ".env"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("MINIMAX_CN_API_KEY=") and not os.environ.get("MINIMAX_CN_API_KEY"):
                        os.environ["MINIMAX_CN_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if line.startswith("MINIMAX_API_KEY=") and not os.environ.get("MINIMAX_API_KEY"):
                        os.environ["MINIMAX_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")

        # Build config
        config = DEFAULT_CONFIG.copy()
        if cfg.get("research_depth"):
            config["max_debate_rounds"] = cfg["research_depth"]
            config["max_risk_discuss_rounds"] = cfg["research_depth"]
        if cfg.get("quick_think_llm"):
            config["quick_think_llm"] = cfg["quick_think_llm"]
        if cfg.get("deep_think_llm"):
            config["deep_think_llm"] = cfg["deep_think_llm"]
        provider = cfg.get("llm_provider", "minimax-cn")
        config["llm_provider"] = provider
        if provider == "minimax-cn":
            config["backend_url"] = "https://api.minimaxi.com/v1"
        elif provider == "minimax":
            config["backend_url"] = "https://api.minimax.io/v1"
        if cfg.get("output_language"):
            config["output_language"] = cfg["output_language"]

        analysts = cfg.get("analysts", ["market", "social", "news", "fundamentals"])
        analyst_order = ["market", "social", "news", "fundamentals"]
        selected = [a for a in analyst_order if a in analysts]

        print(f"  [{os.getpid()}] Starting: {ticker} on {analysis_date}", flush=True)

        graph = TradingAgentsGraph(selected, config=config, debug=False)
        instrument_context = graph.resolve_instrument_context(ticker, "stock")
        init_state = graph.propagator.create_initial_state(
            ticker, analysis_date, asset_type="stock",
            instrument_context=instrument_context,
        )
        args = graph.propagator.get_graph_args()

        trace = []
        for chunk in graph.graph.stream(init_state, **args):
            trace.append(chunk)

        final_state = {}
        for chunk in trace:
            final_state.update(chunk)

        elapsed = time.time() - start
        decision_info = parse_decision(
            final_state.get("final_trade_decision"),
            final_state.get("trader_investment_plan"),
        )

        # Save reports
        results_root = Path(cfg.get("results_dir", str(TA_PROJECT / "batch_results")))
        ticker_dir = results_root / ticker / analysis_date
        try:
            write_report_tree(final_state, ticker, ticker_dir)
        except Exception:
            pass
        try:
            clean = {}
            for k, v in final_state.items():
                try:
                    json.dumps(v, ensure_ascii=False)
                    clean[k] = v
                except (TypeError, ValueError):
                    clean[k] = str(v)[:500]
            (ticker_dir / "final_state.json").write_text(
                json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

        result = {
            "run_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "analysis_date": analysis_date,
            "stock_id": ticker,
            "stock_name": stock_meta.get("name", "") if stock_meta else "",
            "sector_name": stock_meta.get("sector_name", "") if stock_meta else "",
            "price_now": stock_meta.get("latest_close") if stock_meta else None,
            "decision": decision_info["decision"],
            "decision_detail": final_state.get("final_trade_decision", ""),
            "target_price": decision_info["target_price"],
            "stop_price": decision_info["stop_price"],
            "confidence": decision_info["confidence"],
            "market_report": final_state.get("market_report", ""),
            "sentiment_report": final_state.get("sentiment_report", ""),
            "news_report": final_state.get("news_report", ""),
            "fundamentals_report": final_state.get("fundamentals_report", ""),
            "trader_plan": final_state.get("trader_investment_plan", ""),
            "elapsed_seconds": round(elapsed, 1),
            "status": "success",
            "error_message": None,
        }
        print(f"  [{os.getpid()}] DONE: {ticker} in {elapsed:.0f}s — {result['decision']}", flush=True)

    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{os.getpid()}] FAILED: {ticker} — {e}", flush=True)
        result = {
            "run_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "analysis_date": analysis_date,
            "stock_id": ticker,
            "stock_name": stock_meta.get("name", "") if stock_meta else "",
            "sector_name": stock_meta.get("sector_name", "") if stock_meta else "",
            "price_now": stock_meta.get("latest_close") if stock_meta else None,
            "decision": None, "decision_detail": None,
            "target_price": None, "stop_price": None, "confidence": None,
            "market_report": None, "sentiment_report": None,
            "news_report": None, "fundamentals_report": None,
            "trader_plan": None,
            "elapsed_seconds": round(elapsed, 1),
            "status": "error",
            "error_message": f"{type(e).__name__}: {e}",
        }
    finally:
        os.chdir(original_cwd)
        # Insert into DB from worker
        try:
            insert_result(conn, result)
        except Exception as db_err:
            print(f"  [{os.getpid()}] DB insert failed for {ticker}: {db_err}", flush=True)
        conn.close()

    return result


# ---------------------------------------------------------------------------
# Stock list loading
# ---------------------------------------------------------------------------

def load_stock_list(csv_path: Path) -> list:
    stocks = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stocks.append({
                "ts_code": row["ts_code"],
                "name": row.get("name", ""),
                "sector_name": row.get("sector_name", ""),
                "latest_close": float(row["latest_close"]) if row.get("latest_close") else None,
            })
    return stocks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="Parallel TradingAgents batch runner")
    parser.add_argument("--stock-list", default=str(STOCK_LIST_DEFAULT))
    parser.add_argument("--tickers", help="Comma-separated tickers")
    parser.add_argument("--date", default=datetime.datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--provider", default="minimax-cn")
    parser.add_argument("--quick-llm", default="MiniMax-M3")
    parser.add_argument("--deep-llm", default="MiniMax-M3")
    parser.add_argument("--analysts", default="market,social,news,fundamentals")
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--language", default="English")
    parser.add_argument("--results-dir", default=str(TA_PROJECT / "batch_results"))
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    db_path = Path(args.db_path).resolve()  # Always use absolute path
    conn = init_db(db_path)
    logger.info("Database: %s", db_path)

    # Load stock list
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        stock_meta = {}
    else:
        stocks = load_stock_list(Path(args.stock_list))
        tickers = [s["ts_code"] for s in stocks]
        stock_meta = {s["ts_code"]: s for s in stocks}
        logger.info("Loaded %d stocks", len(tickers))

    # Skip already completed
    completed = get_completed_stocks(conn, args.date)
    remaining = [t for t in tickers if t not in completed]
    logger.info("Already completed: %d | Remaining: %d", len(completed), len(remaining))

    if not remaining:
        logger.info("All stocks already completed!")
        conn.close()
        return

    if args.limit:
        remaining = remaining[:args.limit]

    # Build config
    cfg = {
        "analysts": [a.strip() for a in args.analysts.split(",")],
        "research_depth": args.depth,
        "output_language": args.language,
        "llm_provider": args.provider,
        "quick_think_llm": args.quick_llm,
        "deep_think_llm": args.deep_llm,
        "results_dir": args.results_dir,
    }

    logger.info("=" * 60)
    logger.info("Parallel Batch: %d tickers, %d workers", len(remaining), args.workers)
    logger.info("Provider: %s | Quick: %s | Deep: %s", args.provider, args.quick_llm, args.deep_llm)
    logger.info("=" * 60)

    # Build task args
    tasks = []
    for ticker in remaining:
        meta = stock_meta.get(ticker, {})
        tasks.append((ticker, args.date, cfg, meta, str(db_path)))

    # Run with multiprocessing pool
    start_time = time.time()
    with mp.Pool(processes=args.workers) as pool:
        results = pool.map(worker_run_ticker, tasks)

    elapsed_total = time.time() - start_time
    success_count = sum(1 for r in results if r["status"] == "success")

    logger.info("=" * 60)
    logger.info("Parallel Batch Complete: %d/%d succeeded in %.0f seconds",
                success_count, len(results), elapsed_total)
    logger.info("=" * 60)

    # Print summary
    all_rows = conn.execute(
        """SELECT run_date, stock_id, stock_name, sector_name, price_now,
                  decision, target_price, stop_price, elapsed_seconds, status
           FROM run_results WHERE analysis_date=?
           ORDER BY sector_name, stock_id""",
        (args.date,),
    ).fetchall()

    total = len(all_rows)
    total_success = sum(1 for r in all_rows if r[9] == "success")
    decisions = {}
    for r in all_rows:
        d = r[5] or "N/A"
        decisions[d] = decisions.get(d, 0) + 1

    print(f"\nTotal: {total} | Success: {total_success} | Failed: {total - total_success}")
    dec_summary = ", ".join(f"{k}: {v}" for k, v in sorted(decisions.items()))
    print(f"Decisions: {dec_summary}")

    conn.close()


if __name__ == "__main__":
    main()
