"""
performance_tracker.py — paper portfolio P&L and ghost alpha tracking

Reads the decision journal to compute paper-trade performance over time.
Every approved BUY/SELL/SKIP from the Telegram interface is logged; this module
turns that log into a running P&L vs the SPY buy-and-hold ghost portfolio.

Used in two places:
  1. main_engine.py reads ghost_alpha to stamp each briefing with live alpha
  2. telegram_bot.py formats a mini performance block in every briefing

Design: stateless read from SQLite + live price fetch. No extra state files.
"""

import os
import json
import sqlite3
import datetime

DB_PATH = os.environ.get(
    "JOURNAL_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "sector_command.db"),
)


def _get_decisions(db_path=DB_PATH, n=None):
    """Return all logged decisions with human replies, oldest-first."""
    if not os.path.exists(db_path):
        return []
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        q = "SELECT * FROM decisions WHERE human_command IS NOT NULL ORDER BY id ASC"
        if n:
            q += f" LIMIT {n}"
        rows = con.execute(q).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[perf] db read failed: {e}")
        return []


def _fetch_price(ticker, date_str):
    """Fetch closing price for ticker on or after date_str. Returns float or None."""
    try:
        import yfinance as yf
        # pull 5 days to handle weekends/holidays
        dt = datetime.date.fromisoformat(date_str[:10])
        end = dt + datetime.timedelta(days=7)
        raw = yf.download(ticker, start=dt.isoformat(), end=end.isoformat(),
                          progress=False, auto_adjust=True)
        if raw is None or len(raw) == 0:
            return None
        close = raw["Close"]
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        return float(close.dropna().iloc[0])
    except Exception:
        return None


def _fetch_current_price(ticker):
    """Fetch today's most recent price for ticker."""
    try:
        import yfinance as yf
        raw = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
        close = raw["Close"]
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        return float(close.dropna().iloc[-1])
    except Exception:
        return None


class PaperPortfolio:
    """
    Simulates a paper-trading portfolio from the decision log.

    Rules (simple, defensible):
    - Start with $10,000 notional at the first logged decision.
    - Each BUY reply allocates the full notional to the recommended ticker.
    - Each SELL/SKIP keeps the previous position.
    - Returns are marked-to-market at the most recent close.
    - Ghost (benchmark): same notional in SPY buy-and-hold from the same start date.
    """

    INITIAL = 10_000.0

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._decisions = None

    @property
    def decisions(self):
        if self._decisions is None:
            self._decisions = _get_decisions(self.db_path)
        return self._decisions

    def compute(self):
        """
        Returns a dict with:
          portfolio_value, ghost_value, alpha_pct, n_trades,
          win_rate, start_date, days_running, current_ticker, current_weight_pct
        """
        dec = self.decisions
        if not dec:
            return self._empty()

        start_date = dec[0]["date"]
        ghost_start = _fetch_price("SPY", start_date)
        ghost_current = _fetch_current_price("SPY")

        if not ghost_start or not ghost_current:
            return self._empty()

        ghost_value = self.INITIAL * (ghost_current / ghost_start)

        # Simulate trades
        port_value = self.INITIAL
        entry_price = None
        entry_ticker = None
        n_trades = 0
        wins = 0

        for d in dec:
            cmd = (d.get("human_command") or "").upper()
            ticker = d.get("recommended_ticker")
            date = d.get("date") or ""

            if cmd == "BUY" and ticker:
                if entry_ticker and entry_price and ticker != entry_ticker:
                    # Close old position
                    exit_px = _fetch_price(entry_ticker, date)
                    if exit_px and entry_price:
                        ret = exit_px / entry_price - 1
                        port_value *= (1 + ret)
                        n_trades += 1
                        if ret > 0:
                            wins += 1
                # Open new position
                entry_ticker = ticker
                entry_price = _fetch_price(ticker, date)

        # Mark current position to market
        if entry_ticker and entry_price:
            current_px = _fetch_current_price(entry_ticker)
            if current_px:
                ret = current_px / entry_price - 1
                port_value *= (1 + ret)

        days_running = 0
        try:
            start_dt = datetime.date.fromisoformat(start_date[:10])
            days_running = (datetime.date.today() - start_dt).days
        except Exception:
            pass

        alpha_pct = ((port_value / ghost_value) - 1) * 100

        return {
            "portfolio_value": round(port_value, 2),
            "ghost_value": round(ghost_value, 2),
            "alpha_pct": round(alpha_pct, 2),
            "n_trades": n_trades,
            "win_rate": round(wins / n_trades * 100, 1) if n_trades > 0 else None,
            "start_date": start_date,
            "days_running": days_running,
            "current_ticker": entry_ticker,
            "portfolio_return_pct": round((port_value / self.INITIAL - 1) * 100, 2),
            "spy_return_pct": round((ghost_value / self.INITIAL - 1) * 100, 2),
        }

    def ghost_alpha(self):
        """Quick helper: return current alpha % vs SPY (0.0 if unavailable)."""
        result = self.compute()
        return result.get("alpha_pct", 0.0)

    @staticmethod
    def _empty():
        return {
            "portfolio_value": None, "ghost_value": None,
            "alpha_pct": 0.0, "n_trades": 0,
            "win_rate": None, "start_date": None, "days_running": 0,
            "current_ticker": None, "portfolio_return_pct": 0.0, "spy_return_pct": 0.0,
        }


def format_performance_block(perf: dict) -> str:
    """Format a short performance summary string for the Telegram briefing."""
    if not perf or perf.get("portfolio_value") is None:
        return "  Paper P&L: no closed trades yet (paper mode)"

    lines = []
    port_r = perf.get("portfolio_return_pct", 0)
    spy_r = perf.get("spy_return_pct", 0)
    alpha = perf.get("alpha_pct", 0)
    n = perf.get("n_trades", 0)
    days = perf.get("days_running", 0)
    win = perf.get("win_rate")

    lines.append(f"  Paper portfolio: {port_r:+.1f}%  |  SPY: {spy_r:+.1f}%")
    alpha_emoji = "🟢" if alpha >= 0 else "🔴"
    lines.append(f"  Alpha vs SPY: {alpha_emoji} {alpha:+.2f}%  ({n} trades, {days}d)")
    if win is not None:
        lines.append(f"  Win rate: {win:.0f}%")

    return "\n".join(lines)


if __name__ == "__main__":
    pp = PaperPortfolio()
    result = pp.compute()
    import json as _json
    print(_json.dumps(result, indent=2, default=str))
