#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate simplified email-friendly HTML report from trading_results.db.
Clean table format, grouped by decision, no JS dependency.

Usage:
    python generate_simple_report.py --date 2026-09-24 --output /tmp/tradingagents_report_20260924.html
"""

import argparse
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path("/Users/michaelwang/trading/TradingAgents_Mine/TradingAgents/batch_pipeline/trading_results.db")

DECISION_COLORS = {
    "SELL": "#e53935",
    "REDUCE": "#8E24AA",
    "UNDERWEIGHT": "#FB8C00",
    "HOLD": "#43A047",
    "MAINTAIN": "#78909C",
    "BUY": "#1B5E20",
    "OVERWEIGHT": "#1565C0",
    "N/A": "#9E9E9E",
}

DECISION_ORDER = ["SELL", "REDUCE", "UNDERWEIGHT", "HOLD", "MAINTAIN", "BUY", "OVERWEIGHT", "N/A", "600026"]


def fmt_price(v):
    if v is None or v == "" or v == 0:
        return "—"
    try:
        return f"¥{float(v):.2f}"
    except:
        return str(v)


def fmt_conf(v):
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.1f}"
    except:
        return str(v)


def generate_simple_report(analysis_date: str, db_path: Path = DB_PATH) -> str:
    conn = sqlite3.connect(str(db_path))

    rows = conn.execute(
        """SELECT stock_id, stock_name, sector_name, price_now,
                  decision, decision_detail, target_price, stop_price,
                  confidence, elapsed_seconds, status, error_message
           FROM run_results WHERE analysis_date=?
           ORDER BY status DESC, decision, sector_name, stock_id""",
        (analysis_date,),
    ).fetchall()
    conn.close()

    if not rows:
        return f"<html><body><h1>No results for {analysis_date}</h1></body></html>"

    # Separate success and failed
    success_rows = [r for r in rows if r[10] == "success"]
    failed_rows = [r for r in rows if r[10] != "success"]

    # Stats
    total = len(rows)
    success = len(success_rows)
    failed = len(failed_rows)

    decision_counts = {}
    for r in success_rows:
        d = r[4] or "N/A"
        decision_counts[d] = decision_counts.get(d, 0) + 1

    # Group by decision
    grouped = {}
    for r in success_rows:
        d = r[4] or "N/A"
        if d not in grouped:
            grouped[d] = []
        grouped[d].append(r)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Build HTML
    html_parts = []
    html_parts.append(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股分析报告 {analysis_date}</title>
<style>
  body {{ font-family: -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; background: #f8f9fa; color: #333; margin: 0; padding: 20px; }}
  .wrap {{ max-width: 800px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px 0; color: #1a1a2e; }}
  .subtle {{ color: #888; font-size: 13px; margin-bottom: 20px; }}
  .stats {{ display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }}
  .stat {{ background: #fff; border-radius: 8px; padding: 12px 20px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.08); min-width: 80px; }}
  .stat .num {{ font-size: 24px; font-weight: 700; }}
  .stat .lbl {{ font-size: 11px; color: #888; margin-top: 2px; text-transform: uppercase; }}
  .section {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 20px; overflow: hidden; }}
  .section-header {{ padding: 12px 16px; font-size: 15px; font-weight: 700; color: #fff; display: flex; justify-content: space-between; align-items: center; }}
  .section-header .count {{ background: rgba(255,255,255,0.25); padding: 2px 10px; border-radius: 10px; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f5f5f5; padding: 8px 12px; text-align: left; font-weight: 600; color: #555; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #eee; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover {{ background: #f9f9f9; }}
  .ticker {{ font-family: 'SF Mono', 'Consolas', monospace; font-size: 12px; color: #666; }}
  .name {{ font-weight: 600; }}
  .price {{ text-align: right; font-family: 'SF Mono', 'Consolas', monospace; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; color: #fff; }}
  .failed-list {{ padding: 12px 16px; }}
  .failed-item {{ font-size: 12px; color: #888; padding: 3px 0; }}
  .footer {{ text-align: center; color: #aaa; font-size: 11px; margin-top: 24px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📊 A股交易分析报告</h1>
  <div class="subtle">分析日期 {analysis_date} · 生成于 {now_str} · TradingAgents + MiniMax-M3</div>

  <div class="stats">
    <div class="stat"><div class="num">{total}</div><div class="lbl">Total</div></div>
    <div class="stat"><div class="num" style="color:#43A047">{success}</div><div class="lbl">Success</div></div>
    <div class="stat"><div class="num" style="color:#e53935">{failed}</div><div class="lbl">Failed</div></div>
    <div class="stat"><div class="num" style="color:#1565C0">{success/max(total,1)*100:.0f}%</div><div class="lbl">Rate</div></div>
  </div>
""")

    # Decision sections
    for dec in DECISION_ORDER:
        if dec not in grouped:
            continue
        items = grouped[dec]
        color = DECISION_COLORS.get(dec, "#78909C")
        count = len(items)

        html_parts.append(f"""
  <div class="section">
    <div class="section-header" style="background:{color}">
      <span>{dec}</span>
      <span class="count">{count} 只</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>代码</th>
          <th>名称</th>
          <th>板块</th>
          <th style="text-align:right">现价</th>
          <th style="text-align:right">目标价</th>
          <th style="text-align:right">止损价</th>
        </tr>
      </thead>
      <tbody>
""")

        for r in items:
            sid, sname, sector, price, decision, detail, tgt, stop, conf, elapsed, status, errmsg = r
            html_parts.append(f"""        <tr>
          <td><span class="ticker">{sid or ''}</span></td>
          <td><span class="name">{sname or ''}</span></td>
          <td>{sector or '—'}</td>
          <td class="price">{fmt_price(price)}</td>
          <td class="price">{fmt_price(tgt)}</td>
          <td class="price">{fmt_price(stop)}</td>
        </tr>
""")

        html_parts.append("""      </tbody>
    </table>
  </div>
""")

    # Failed section
    if failed_rows:
        html_parts.append("""
  <div class="section">
    <div class="section-header" style="background:#9E9E9E">
      <span>FAILED</span>
      <span class="count">""" + str(len(failed_rows)) + """ 只</span>
    </div>
    <div class="failed-list">
""")
        for r in failed_rows:
            sid, sname, sector, price, decision, detail, tgt, stop, conf, elapsed, status, errmsg = r
            err_short = (errmsg or "")[:80]
            html_parts.append(f'      <div class="failed-item">{sid or ""} {sname or ""} — {err_short}</div>\n')

        html_parts.append("""    </div>
  </div>
""")

    html_parts.append(f"""
  <div class="footer">
    TradingAgents A-Stock Analysis · {analysis_date} · Batch run with 5 workers<br>
    Data: Tushare · LLM: MiniMax-M3 · DB: trading_results.db
  </div>
</div>
</body>
</html>""")

    return "".join(html_parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Analysis date YYYY-MM-DD")
    parser.add_argument("--output", required=True, help="Output HTML file path")
    parser.add_argument("--db-path", default=str(DB_PATH), help="SQLite DB path")
    args = parser.parse_args()

    html_content = generate_simple_report(args.date, Path(args.db_path))

    Path(args.output).write_text(html_content, encoding="utf-8")
    print(f"Report generated: {args.output} ({len(html_content)} bytes)")


if __name__ == "__main__":
    main()
