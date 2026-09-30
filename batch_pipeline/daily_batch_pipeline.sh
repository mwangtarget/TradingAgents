#!/bin/bash
# daily_batch_pipeline.sh
# End-to-end daily A-stock batch analysis pipeline.
# Runs parallel batch → generates HTML report → outputs summary JSON for email agent.
#
# Usage: bash daily_batch_pipeline.sh [YYYY-MM-DD]
# If date omitted, uses today.

set -euo pipefail

# --- Configuration ---
BATCH_DIR="/Users/michaelwang/trading/TradingAgents_Mine/TradingAgents/batch_pipeline"
TA_PROJECT="/Users/michaelwang/trading/TradingAgents_Mine/TradingAgents"
PYTHON="/Users/michaelwang/miniconda3/envs/tradingagents/bin/python"
DB_PATH="$BATCH_DIR/trading_results.db"
# Auto-select batch A or B based on day of month (odd=A, even=B)
DAY_OF_MONTH=$(date +%d)
if [ $((DAY_OF_MONTH % 2)) -eq 1 ]; then
  BATCH_FILE="sector_top3_batch_a.csv"
else
  BATCH_FILE="sector_top3_batch_b.csv"
fi
STOCK_LIST="$BATCH_DIR/stock_lists/$BATCH_FILE"
echo "Selected batch: $BATCH_FILE (day ${DAY_OF_MONTH})"
REPORT_DIR="/tmp"

# Date handling: accept YYYY-MM-DD or default to today
if [ -n "${1:-}" ]; then
  ANALYSIS_DATE="$1"
else
  ANALYSIS_DATE=$(date +%Y-%m-%d)
fi
DATE_COMPACT=$(echo "$ANALYSIS_DATE" | tr -d '-')
REPORT_PATH="$REPORT_DIR/tradingagents_report_${DATE_COMPACT}.html"
LOG_PATH="$BATCH_DIR/logs/parallel_batch_${DATE_COMPACT}.log"
# Summary JSON stays at the OpenClaw cron's CWD (~/.qclaw/workspace/) so the
# nightly email step can read it; pipeline code/DB now live in batch_pipeline/.
SUMMARY_PATH="/Users/michaelwang/.qclaw/workspace/daily_summary_${DATE_COMPACT}.json"

# --- Environment ---
export TUSHARE_TOKEN="73d044da8e0d5f08c5fe7050c8ea28a1cb9c72d4c88fb5e9c35fba60"
export MINIMAX_CN_API_KEY=$(grep '^MINIMAX_CN_API_KEY=' "$TA_PROJECT/.env" | cut -d= -f2-)
export PATH="/Users/michaelwang/miniconda3/envs/tradingagents/bin:$PATH"

if [ -z "$MINIMAX_CN_API_KEY" ]; then
  echo "ERROR: MINIMAX_CN_API_KEY not found in $TA_PROJECT/.env"
  exit 1
fi

echo "================================================"
echo "Daily Batch Pipeline"
echo "  Date: $ANALYSIS_DATE"
echo "  DB:   $DB_PATH"
echo "  Report: $REPORT_PATH"
echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================"

# --- Step 1: Parallel batch analysis ---
echo "[1/3] Running parallel batch analysis (5 workers)..."

cd "$BATCH_DIR"
"$PYTHON" parallel_batch_run.py \
  --stock-list "$STOCK_LIST" \
  --date "$ANALYSIS_DATE" \
  --provider minimax-cn \
  --quick-llm MiniMax-M3 \
  --deep-llm MiniMax-M3 \
  --workers 5 \
  --db-path "$DB_PATH" \
  2>&1 | tee "$LOG_PATH"

echo "[1/3] Batch analysis complete."

# --- Step 2: Generate simple email-friendly HTML report ---
echo "[2/3] Generating HTML report..."

"$PYTHON" "$BATCH_DIR/generate_simple_report.py" \
  --date "$ANALYSIS_DATE" \
  --db-path "$DB_PATH" \
  --output "$REPORT_PATH"

echo "[2/3] Report generated: $REPORT_PATH"

# --- Step 3: Output summary JSON for email agent ---
echo "[3/3] Generating summary..."

"$PYTHON" - "$ANALYSIS_DATE" "$DB_PATH" "$REPORT_PATH" "$SUMMARY_PATH" << 'PYEOF'
import json, sqlite3, sys

analysis_date = sys.argv[1]
db_path = sys.argv[2]
report_path = sys.argv[3]
summary_path = sys.argv[4]

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Status counts
cur.execute("SELECT status, COUNT(*) FROM run_results WHERE analysis_date=? GROUP BY status", (analysis_date,))
status_counts = dict(cur.fetchall())

# Decision distribution
cur.execute("""SELECT decision, COUNT(*) FROM run_results
    WHERE analysis_date=? AND status='success' GROUP BY decision ORDER BY COUNT(*) DESC""",
    (analysis_date,))
decisions = dict(cur.fetchall())

# SELL stocks
cur.execute("""SELECT stock_id, stock_name, sector_name, price_now, decision_detail
    FROM run_results WHERE analysis_date=? AND status='success' AND decision='SELL'
    ORDER BY stock_id""", (analysis_date,))
sell_stocks = [{"stock_id": r[0], "stock_name": r[1], "sector": r[2], "price": r[3], "detail": r[4][:200] if r[4] else ""} for r in cur.fetchall()]

# Failed stocks
cur.execute("""SELECT stock_id, stock_name, error_message FROM run_results
    WHERE analysis_date=? AND status='error' ORDER BY stock_id""", (analysis_date,))
failed = [{"stock_id": r[0], "stock_name": r[1], "error": r[2][:150] if r[2] else ""} for r in cur.fetchall()]

summary = {
    "analysis_date": analysis_date,
    "report_path": report_path,
    "total": sum(status_counts.values()),
    "success": status_counts.get("success", 0),
    "error": status_counts.get("error", 0),
    "decisions": decisions,
    "sell_stocks": sell_stocks,
    "failed_stocks": failed,
}

with open(summary_path, "w") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print(f"Summary: {summary['success']} success / {summary['error']} failed")
print(f"Report: {report_path}")
print(f"Summary: {summary_path}")

conn.close()
PYEOF

echo "================================================"
echo "Pipeline complete: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Report: $REPORT_PATH"
echo "Summary: $SUMMARY_PATH"
echo "================================================"