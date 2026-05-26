"""
report_generator.py — weekly PDF/HTML research report

Generates a professional research report from the decision journal.
Used in two ways:
  1. GitHub Actions runs it weekly → saves report.html → commits to repo
     → viewable at github.io or as a direct download
  2. `/REPORT` Telegram command → sends download link

The HTML report looks like a quant research note: clean, printable,
charts included as inline SVG/base64. No server needed.

Output: data/reports/sector_command_YYYY-MM-DD.html
"""

import os
import json
import sqlite3
import datetime
import base64

DB_PATH = os.environ.get(
    "JOURNAL_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "sector_command.db"),
)
REPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "reports")


def _get_all_decisions(db_path=DB_PATH):
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM decisions ORDER BY id ASC").fetchall()
    con.close()
    return [dict(r) for r in rows]


def _weekly_stats(decisions):
    """Compute high-level stats for the report."""
    total = len(decisions)
    buys = sum(1 for d in decisions if d.get("human_command") == "BUY")
    sells = sum(1 for d in decisions if d.get("human_command") == "SELL")
    skips = sum(1 for d in decisions if d.get("human_command") == "SKIP")
    abstains = sum(1 for d in decisions if d.get("abstain_reason"))
    avg_confidence = (
        round(sum(d.get("confidence", 0) or 0 for d in decisions) / total)
        if total else 0
    )
    regime_counts = {"CALM": 0, "NORMAL": 0, "STRESSED": 0}
    for d in decisions:
        r = (d.get("regime") or "NORMAL").upper()
        regime_counts[r] = regime_counts.get(r, 0) + 1
    return {
        "total_runs": total, "buys": buys, "sells": sells,
        "skips": skips, "abstains": abstains,
        "avg_confidence": avg_confidence, "regime_counts": regime_counts,
    }


def _news_chart_data(decisions):
    """Build data for a news sentiment trend line."""
    points = []
    for d in decisions:
        if d.get("news_sentiment") is not None and d.get("date"):
            points.append({"date": d["date"], "sentiment": d["news_sentiment"]})
    return points


def _confidence_chart_data(decisions):
    """Confidence trend over time."""
    return [
        {"date": d["date"], "confidence": d.get("confidence", 0)}
        for d in decisions if d.get("date") and d.get("confidence") is not None
    ]


def generate_html_report(output_path: str = None) -> str:
    """
    Generate the weekly HTML research report.
    Returns the output file path.
    """
    decisions = _get_all_decisions()
    stats = _weekly_stats(decisions)
    news_data = _news_chart_data(decisions[-30:])   # last 30 runs
    conf_data = _confidence_chart_data(decisions[-30:])

    recent_10 = decisions[-10:][::-1]   # last 10, newest first

    today = datetime.date.today().isoformat()
    report_date_label = datetime.date.today().strftime("%B %d, %Y")

    # ── Decisions table HTML ──────────────────────────────────────
    rows_html = ""
    for d in recent_10:
        action = d.get("human_command") or "—"
        color = {"BUY": "#00ff88", "SELL": "#ff4444", "SKIP": "#ffaa00"}.get(action, "#888")
        regime_icon = {"CALM": "🟢", "NORMAL": "🟡", "STRESSED": "🔴"}.get(
            (d.get("regime") or "").upper(), "⚪")
        abstain = "✓" if d.get("abstain_reason") else ""
        rows_html += f"""
        <tr>
          <td>{d.get('date','—')}</td>
          <td>{regime_icon} {d.get('regime','—')}</td>
          <td>{d.get('recommended_ticker','—')}</td>
          <td>{d.get('confidence','—')}%</td>
          <td style="color:{color};font-weight:600">{action}</td>
          <td style="color:#888;font-size:11px">{abstain}</td>
        </tr>"""

    # ── Inline Chart.js data ──────────────────────────────────────
    news_labels = json.dumps([p["date"] for p in news_data])
    news_values = json.dumps([p["sentiment"] for p in news_data])
    conf_labels = json.dumps([p["date"] for p in conf_data])
    conf_values = json.dumps([p["confidence"] for p in conf_data])

    regime_labels = json.dumps(list(stats["regime_counts"].keys()))
    regime_values = json.dumps(list(stats["regime_counts"].values()))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sector Command — Research Report {today}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f5f6f8; color: #1a1d2e; }}
    .page {{ max-width: 960px; margin: 0 auto; padding: 32px 24px; }}
    .header {{ background: #1a1d2e; color: white; padding: 28px 32px; border-radius: 12px; margin-bottom: 24px; }}
    .header h1 {{ font-size: 26px; font-weight: 700; letter-spacing: -0.5px; }}
    .header .sub {{ color: #8892a4; margin-top: 4px; font-size: 13px; }}
    .badge {{ display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 11px;
               font-weight: 600; margin-left: 10px; }}
    .badge-paper {{ background: #ff9f43; color: #1a1d2e; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
                   gap: 16px; margin-bottom: 24px; }}
    .stat-card {{ background: white; border-radius: 10px; padding: 18px 20px;
                  box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
    .stat-card .label {{ font-size: 11px; color: #8892a4; text-transform: uppercase;
                          letter-spacing: 0.5px; margin-bottom: 6px; }}
    .stat-card .value {{ font-size: 28px; font-weight: 700; color: #1a1d2e; }}
    .stat-card .sub {{ font-size: 12px; color: #8892a4; margin-top: 2px; }}
    .chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }}
    .chart-card {{ background: white; border-radius: 10px; padding: 20px;
                   box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
    .chart-card h3 {{ font-size: 13px; color: #8892a4; text-transform: uppercase;
                       letter-spacing: 0.5px; margin-bottom: 14px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ background: #f0f2f5; padding: 10px 12px; text-align: left; font-size: 11px;
          color: #8892a4; text-transform: uppercase; letter-spacing: 0.5px; }}
    td {{ padding: 10px 12px; border-bottom: 1px solid #f0f2f5; }}
    tr:last-child td {{ border-bottom: none; }}
    .table-card {{ background: white; border-radius: 10px; overflow: hidden;
                   box-shadow: 0 1px 4px rgba(0,0,0,0.08); margin-bottom: 24px; }}
    .table-card .card-header {{ padding: 16px 20px; border-bottom: 1px solid #f0f2f5; }}
    .table-card .card-header h2 {{ font-size: 15px; font-weight: 600; }}
    .table-card .card-body {{ padding: 0; }}
    .footer {{ text-align: center; color: #8892a4; font-size: 12px; padding-top: 12px; }}
    @media print {{
      body {{ background: white; }}
      .page {{ padding: 0; }}
    }}
  </style>
</head>
<body>
<div class="page">

  <div class="header">
    <h1>Sector Command Live
      <span class="badge badge-paper">PAPER MODE</span>
    </h1>
    <div class="sub">Weekly Research Report — {report_date_label} &nbsp;|&nbsp;
      RL Ensemble: PPO / A2C / SAC &nbsp;|&nbsp; 11-sector ETF universe
    </div>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="label">Total Runs</div>
      <div class="value">{stats['total_runs']}</div>
      <div class="sub">engine executions</div>
    </div>
    <div class="stat-card">
      <div class="label">BUY Orders</div>
      <div class="value" style="color:#00aa55">{stats['buys']}</div>
      <div class="sub">paper executed</div>
    </div>
    <div class="stat-card">
      <div class="label">Skipped</div>
      <div class="value" style="color:#ffaa00">{stats['skips']}</div>
      <div class="sub">human override</div>
    </div>
    <div class="stat-card">
      <div class="label">Abstains</div>
      <div class="value" style="color:#4dabf7">{stats['abstains']}</div>
      <div class="sub">governance triggered</div>
    </div>
    <div class="stat-card">
      <div class="label">Avg Confidence</div>
      <div class="value">{stats['avg_confidence']}%</div>
      <div class="sub">ensemble score</div>
    </div>
  </div>

  <div class="chart-grid">
    <div class="chart-card">
      <h3>News Sentiment Trend (FinBERT)</h3>
      <canvas id="newsChart" height="180"></canvas>
    </div>
    <div class="chart-card">
      <h3>Ensemble Confidence Trend</h3>
      <canvas id="confChart" height="180"></canvas>
    </div>
    <div class="chart-card">
      <h3>Market Regime Distribution</h3>
      <canvas id="regimeChart" height="180"></canvas>
    </div>
  </div>

  <div class="table-card">
    <div class="card-header"><h2>Recent Decisions (Last 10 Runs)</h2></div>
    <div class="card-body">
      <table>
        <thead><tr>
          <th>Date</th><th>Regime</th><th>Recommendation</th>
          <th>Confidence</th><th>Your Action</th><th>Abstain</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </div>

  <div class="footer">
    Sector Command Live &nbsp;·&nbsp; github.com/cameroncc333/sector-command-live
    &nbsp;·&nbsp; Generated {today} &nbsp;·&nbsp; Paper mode (no real capital at risk)
  </div>

</div>

<script>
const newsCtx = document.getElementById('newsChart').getContext('2d');
new Chart(newsCtx, {{
  type: 'line',
  data: {{
    labels: {news_labels},
    datasets: [{{
      data: {news_values},
      borderColor: '#4dabf7', backgroundColor: 'rgba(77,171,247,0.1)',
      borderWidth: 2, pointRadius: 3, tension: 0.3, fill: true
    }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ min: -1, max: 1, grid: {{ color: '#f0f2f5' }} }},
      x: {{ ticks: {{ maxTicksLimit: 6 }}, grid: {{ display: false }} }}
    }}
  }}
}});

const confCtx = document.getElementById('confChart').getContext('2d');
new Chart(confCtx, {{
  type: 'bar',
  data: {{
    labels: {conf_labels},
    datasets: [{{
      data: {conf_values},
      backgroundColor: {conf_values}.map(v =>
        v >= 75 ? '#00aa55' : v >= 50 ? '#ffaa00' : '#ff4444'),
      borderRadius: 3,
    }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ min: 0, max: 100, grid: {{ color: '#f0f2f5' }} }},
      x: {{ ticks: {{ maxTicksLimit: 6 }}, grid: {{ display: false }} }}
    }}
  }}
}});

const regCtx = document.getElementById('regimeChart').getContext('2d');
new Chart(regCtx, {{
  type: 'doughnut',
  data: {{
    labels: {regime_labels},
    datasets: [{{
      data: {regime_values},
      backgroundColor: ['#00aa55', '#ffaa00', '#ff4444'],
    }}]
  }},
  options: {{ plugins: {{ legend: {{ position: 'right' }} }} }}
}});
</script>
</body>
</html>"""

    # Write to file
    os.makedirs(REPORT_DIR, exist_ok=True)
    out = output_path or os.path.join(REPORT_DIR, f"sector_command_{today}.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"[report] Written → {out}")
    return out


if __name__ == "__main__":
    path = generate_html_report()
    print(f"Open: file://{path}")
