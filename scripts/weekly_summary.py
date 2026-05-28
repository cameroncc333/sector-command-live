#!/usr/bin/env python3
"""
weekly_summary.py — Rich Sunday Telegram summary for Sector Command.

Sent every Sunday ~4:00 PM ET (20:00 UTC) after market close.
Covers the past 5 trading days: decisions, P&L vs SPY, equity alpha top picks,
overall portfolio health, and what to watch next week.

Usage:
    TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... JOURNAL_DB=data/sector_command.db \
    python scripts/weekly_summary.py
"""

import os
import sys
import json
import sqlite3
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Telegram ─────────────────────────────────────────────────────────────────

def _tg_send(token, chat_id, text):
    import requests
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )
    if not r.ok:
        print(f"[weekly] Telegram error {r.status_code}: {r.text[:120]}")
    return r.ok


# ── Data loaders ──────────────────────────────────────────────────────────────

def _week_decisions(db_path, days=7):
    if not os.path.exists(db_path):
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM decisions WHERE date >= ? ORDER BY id ASC", (cutoff,)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[weekly] DB read failed: {e}")
        return []


def _extract_close(raw, ticker=None):
    """Robustly extract a Close price Series from a yfinance download result."""
    if raw is None or len(raw) == 0:
        return None
    # MultiIndex columns: raw["Close"] returns a DataFrame keyed by ticker
    if hasattr(raw.columns, "get_level_values") and "Close" in raw.columns.get_level_values(0):
        c = raw["Close"]
    elif "Close" in raw.columns:
        c = raw["Close"]
    else:
        c = raw
    # If still a DataFrame (multi-ticker), get the specific ticker or first column
    if hasattr(c, "columns"):
        if ticker and ticker in c.columns:
            c = c[ticker]
        else:
            c = c.iloc[:, 0]
    return c.dropna() if hasattr(c, "dropna") else c


def _fetch_price(ticker, date_str):
    try:
        import yfinance as yf
        dt  = datetime.date.fromisoformat(date_str[:10])
        end = dt + datetime.timedelta(days=7)
        raw = yf.download(ticker, start=dt.isoformat(), end=end.isoformat(),
                          progress=False, auto_adjust=True)
        close = _extract_close(raw, ticker)
        if close is None or len(close) == 0:
            return None
        return float(close.iloc[0])
    except Exception:
        return None


def _fetch_current_price(ticker):
    try:
        import yfinance as yf
        raw   = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
        close = _extract_close(raw, ticker)
        if close is None or len(close) == 0:
            return None
        return float(close.iloc[-1])
    except Exception:
        return None


def _load_last_briefing():
    # Check Redis first (Railway disk is stale between GH Actions commits)
    try:
        import requests as _req
        _ru = os.environ.get("UPSTASH_REDIS_REST_URL", "")
        _rt = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
        if _ru and _rt:
            r = _req.post(_ru, json=["GET", "sc:last_briefing"],
                          headers={"Authorization": f"Bearer {_rt}"}, timeout=3)
            raw = r.json().get("result")
            if raw:
                return json.loads(raw)
    except Exception:
        pass
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "last_briefing.json"
    )
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ── Weekly P&L calculation ────────────────────────────────────────────────────

def _weekly_trade_results(decisions):
    """
    For each BUY this week, compute return from entry to today.
    Returns list of (ticker, date, return_pct) and a SPY comparison.
    """
    buys = [
        d for d in decisions
        if (d.get("human_command") or "").upper() == "BUY"
        and d.get("recommended_ticker")
    ]
    if not buys:
        return [], None, None

    results = []
    for buy in buys:
        ticker = buy["recommended_ticker"]
        entry_date = buy.get("date", "")
        entry_px = _fetch_price(ticker, entry_date)
        current_px = _fetch_current_price(ticker)
        if entry_px and current_px:
            ret = (current_px / entry_px - 1) * 100
            results.append((ticker, entry_date, ret, entry_px, current_px))

    if not results:
        return [], None, None

    first_date = results[0][1]
    spy_entry = _fetch_price("SPY", first_date)
    spy_now = _fetch_current_price("SPY")
    spy_ret = (spy_now / spy_entry - 1) * 100 if spy_entry and spy_now else None
    avg_port = sum(r[2] for r in results) / len(results)

    return results, avg_port, spy_ret


# ── Formatting helpers ────────────────────────────────────────────────────────

def _regime_emoji(regime):
    return {
        "BULL": "🟢", "BEAR": "🔴", "STRESSED": "🔴",
        "CALM": "🔵", "HIGH_RATES": "🟠", "NORMAL": "⚪",
    }.get(regime or "", "⚪")


def _cmd_label(cmd):
    return {
        "BUY": "🟢 BUY", "SELL": "🔴 SELL",
        "SKIP": "⏭ SKIP", "HOLD": "🟡 HOLD",
    }.get((cmd or "").upper(), cmd or "pending")


def _sign(v):
    return "+" if v >= 0 else ""


# ── Build the message ─────────────────────────────────────────────────────────

def build_summary(db_path):
    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=6)  # Mon–Sun

    decisions = _week_decisions(db_path, days=7)
    briefing = _load_last_briefing()

    regime = briefing.get("regime", "NORMAL")
    vix = briefing.get("vix")
    rl_action = briefing.get("action", "—")
    rl_target = briefing.get("ticker", "—")

    lines = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines += [
        "⚡ <b>SECTOR COMMAND — Weekly Summary</b>",
        f"<i>Week of {week_start.strftime('%b %d')} – {today.strftime('%b %d, %Y')}</i>",
        "",
    ]

    # ── Market regime + VIX term structure ────────────────────────────────
    vix_s = f"VIX {vix:.1f}" if vix else "VIX —"
    macro_data = briefing.get("macro") or {}
    vts = macro_data.get("vix_term_structure", {})
    vts_str = ""
    if vts and vts.get("ratio"):
        ts_sym = "🔴" if vts.get("ts_regime") == "BACKWARDATION" else "🟡"
        vts_str = f"  |  {ts_sym} VIX/VIX3M {vts.get('ratio','?')} [{vts.get('ts_regime','')}]"
        if vts.get("event_risk"):
            vts_str += " ⚡"
    lines += [
        f"{_regime_emoji(regime)} <b>Regime:</b> {regime}  |  {vix_s}{vts_str}",
        f"🤖 <b>RL Signal:</b> {rl_action} {rl_target}",
        "",
    ]

    # ── Decisions this week ───────────────────────────────────────────────────
    lines.append("<b>── Decisions This Week ──</b>")
    if not decisions:
        lines.append("  No decisions logged this week")
    else:
        for d in decisions:
            cmd = d.get("human_command") or ""
            ticker = d.get("recommended_ticker") or d.get("rl_target") or "?"
            date_s = (d.get("date") or "")[:10]
            conf = d.get("confidence")
            conf_s = f" ({conf}%)" if conf else ""
            reason = (d.get("human_reason") or "")[:55]
            reason_s = f" — {reason}" if reason else ""
            lines.append(f"  {date_s}: {_cmd_label(cmd)} <b>{ticker}</b>{conf_s}{reason_s}")
    lines.append("")

    # ── Weekly P&L ────────────────────────────────────────────────────────────
    lines.append("<b>── Weekly P&amp;L ──</b>")
    trade_results, avg_port, spy_ret = _weekly_trade_results(decisions)
    if avg_port is not None:
        port_e = "🟢" if avg_port >= 0 else "🔴"
        lines.append(f"  Paper portfolio: {port_e} {_sign(avg_port)}{avg_port:.2f}%")
        if spy_ret is not None:
            spy_e = "🟢" if spy_ret >= 0 else "🔴"
            alpha = avg_port - spy_ret
            alpha_e = "🟢" if alpha >= 0 else "🔴"
            lines.append(f"  SPY benchmark:   {spy_e} {_sign(spy_ret)}{spy_ret:.2f}%")
            lines.append(f"  Alpha vs SPY:    {alpha_e} {_sign(alpha)}{alpha:.2f}%")
        if trade_results:
            lines.append("  <i>Trade details:</i>")
            for ticker, date_s, ret, entry_px, cur_px in trade_results:
                e = "🟢" if ret >= 0 else "🔴"
                lines.append(f"    {e} {ticker}: {_sign(ret)}{ret:.2f}%  (${entry_px:.2f}→${cur_px:.2f})")
    else:
        lines.append("  No traded positions this week — no BUY decisions with price data")
    lines.append("")

    # ── Overall portfolio since inception ─────────────────────────────────────
    lines.append("<b>── Portfolio Since Inception ──</b>")
    try:
        from engine.performance_tracker import PaperPortfolio, format_performance_block
        perf = PaperPortfolio(db_path).compute()
        block = format_performance_block(perf).strip()
        for ln in block.splitlines():
            lines.append(ln)
        days_r = perf.get("days_running", 0)
        if days_r:
            lines.append(f"  Running: {days_r} days since {perf.get('start_date','?')[:10]}")
    except Exception as e:
        lines.append(f"  (Stats unavailable: {e})")
    lines.append("")

    # ── Equity alpha picks for next week ─────────────────────────────────────
    equity_picks = briefing.get("equity_alpha_picks") or []
    if equity_picks:
        lines.append("<b>── Top Equity Alpha Picks for Next Week ──</b>")
        factor_regime = (equity_picks[0].get("factor_regime") or "").replace("_", " ") if equity_picks else ""
        if factor_regime:
            lines.append(f"  <i>Factor regime: {factor_regime}</i>")
        for i, p in enumerate(equity_picks[:5], 1):
            ticker = p.get("ticker", "?")
            score = p.get("composite_score", 0)
            conviction = p.get("conviction", "")
            tagline = p.get("conviction_tagline", "")
            sector_etf = p.get("sector_etf", "")
            dollar = p.get("suggested_dollar")
            dollar_s = f" → <b>${dollar:.0f}</b>" if dollar else ""
            conv_e = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔵"}.get(conviction, "⚪")
            rl_match = "⭐" if sector_etf == rl_target else ""
            lines.append(
                f"  {i}. {conv_e}{rl_match} <b>{ticker}</b> ({sector_etf}) "
                f"score {score:.0f}{dollar_s}"
            )
            if tagline:
                lines.append(f"     💡 {tagline}")
        lines.append("")

    # ── Sector opportunities ──────────────────────────────────────────────────
    ranked = briefing.get("ranked_opportunities") or []
    if ranked:
        lines.append("<b>── Sectors to Watch Next Week ──</b>")
        for r in (ranked[:4] if isinstance(ranked[0], dict) else []):
            t = r.get("ticker") or "?"
            score = r.get("score", 0)
            if isinstance(score, float) and score <= 1.0:
                score = int(score * 100)
            headline = (r.get("news_headline") or "")[:55]
            headline_s = f": {headline}" if headline else ""
            lines.append(f"  • <b>{t}</b> (score {score:.0f}){headline_s}")
        lines.append("")

    # ── Footer ────────────────────────────────────────────────────────────────
    lines.append(
        "Reply <b>PERF</b> for live P&amp;L  |  "
        "<b>ALPHA</b> for equity picks  |  "
        "<b>STATUS</b> for RL readout"
    )

    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    db_path = os.environ.get("JOURNAL_DB",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "sector_command.db"))

    summary = build_summary(db_path)
    print("──── Weekly Summary ────")
    print(summary)
    print("────────────────────────")

    if not token or not chat_id:
        print("[weekly] No Telegram credentials — printed only, not sent")
        return

    ok = _tg_send(token, chat_id, summary)
    if ok:
        print("[weekly] Summary sent to Telegram.")
    else:
        print("[weekly] Telegram send failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
