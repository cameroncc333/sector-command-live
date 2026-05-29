"""
position_tracker.py — tracks Cameron's real money holdings + portfolio balance

SQLite-backed. Two tables:
  user_config   — single-row: current balance, last updated
  holdings      — one row per open position (ticker, shares, avg_cost, date)

This is separate from journal.py (paper trades) because this tracks
REAL MONEY across ALL assets (sectors, crypto, gold, etc.).

Commands that write here:
  BALANCE 12500           → set portfolio cash balance
  BOUGHT XLE 5 47.50      → log 5 shares of XLE at $47.50
  BOUGHT XLE 500          → log ~$500 position (shares computed from current price)
  SOLD XLE                → remove XLE from holdings
"""

import os
import json
import sqlite3
import datetime

DB_PATH = os.environ.get(
    "HOLDINGS_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "holdings.db"),
)


class PositionTracker:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                shares REAL,
                avg_cost REAL,
                dollar_value REAL,
                date_bought TEXT,
                notes TEXT
            )
        """)
        con.commit()
        con.close()

    # ── Redis helpers (Upstash — persists across Railway restarts) ──────

    def _redis(self, *cmd):
        """Run a Redis command via Upstash REST API. Silent no-op if not configured."""
        url   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
        if not url or not token:
            return None
        try:
            import requests as _req
            r = _req.post(url, json=list(cmd),
                          headers={"Authorization": f"Bearer {token}"}, timeout=4)
            return r.json().get("result")
        except Exception:
            return None

    # ── balance ──────────────────────────────────────────────────────────

    def set_balance(self, amount: float):
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        con = sqlite3.connect(self.db_path)
        con.execute("""
            INSERT INTO user_config (key, value, updated_at)
            VALUES ('balance', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (str(amount), ts))
        con.commit()
        con.close()
        self._redis("SET", "sc:balance", str(amount))

    def get_balance(self):
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT value FROM user_config WHERE key='balance'").fetchone()
        con.close()
        if row:
            return float(row[0])
        # Try Redis (survives Railway restarts)
        val = self._redis("GET", "sc:balance")
        if val:
            return float(val)
        # Final fallback: DEFAULT_BALANCE env var
        env_bal = os.environ.get("DEFAULT_BALANCE")
        return float(env_bal) if env_bal else None

    # ── positions ────────────────────────────────────────────────────────

    def add_position(self, ticker: str, shares: float = None,
                     avg_cost: float = None, dollar_value: float = None,
                     notes: str = None):
        self.remove_position(ticker)
        ts = datetime.date.today().isoformat()
        con = sqlite3.connect(self.db_path)
        con.execute("""
            INSERT INTO holdings (ticker, shares, avg_cost, dollar_value, date_bought, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (ticker.upper(), shares, avg_cost, dollar_value, ts, notes))
        con.commit()
        con.close()
        self._sync_holdings_to_redis()

    def remove_position(self, ticker: str):
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM holdings WHERE ticker=?", (ticker.upper(),))
        con.commit()
        con.close()
        self._sync_holdings_to_redis()

    def _sync_holdings_to_redis(self):
        """Push current SQLite holdings to Redis so they survive cold starts."""
        holdings = self._get_holdings_from_sqlite()
        self._redis("SET", "sc:holdings", json.dumps(holdings))

    def _get_holdings_from_sqlite(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM holdings ORDER BY date_bought DESC").fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_holdings(self):
        rows = self._get_holdings_from_sqlite()
        if rows:
            return rows
        # SQLite empty (cold start) — restore from Redis
        val = self._redis("GET", "sc:holdings")
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
        return []

    # ── portfolio summary ─────────────────────────────────────────────────

    def portfolio_summary(self, current_prices: dict = None) -> dict:
        """
        Returns a summary dict:
          balance, holdings (with current value), total_invested,
          total_current_value, unrealized_pnl_pct, cash_remaining
        `current_prices` is {ticker: price}. If None, fetches via yfinance.
        """
        balance = self.get_balance()
        holdings = self.get_holdings()

        if current_prices is None:
            current_prices = self._fetch_prices([h["ticker"] for h in holdings])

        rows = []
        total_invested = 0.0
        total_current  = 0.0

        for h in holdings:
            ticker = h["ticker"]
            price  = current_prices.get(ticker)

            # Compute current value
            if h["shares"] and h["avg_cost"] and price:
                cost_basis   = h["shares"] * h["avg_cost"]
                current_val  = h["shares"] * price
            elif h["dollar_value"] and price and h["avg_cost"]:
                shares_est   = h["dollar_value"] / h["avg_cost"]
                cost_basis   = h["dollar_value"]
                current_val  = shares_est * price
            elif h["dollar_value"]:
                cost_basis   = h["dollar_value"]
                current_val  = h["dollar_value"]   # no price → mark at cost
                price        = None
            else:
                cost_basis   = 0.0
                current_val  = 0.0

            pnl = current_val - cost_basis
            pnl_pct = (pnl / cost_basis * 100) if cost_basis else 0.0

            total_invested += cost_basis
            total_current  += current_val

            rows.append({
                "ticker":        ticker,
                "shares":        h["shares"],
                "avg_cost":      h["avg_cost"],
                "current_price": round(price, 2) if price else None,
                "cost_basis":    round(cost_basis, 2),
                "current_value": round(current_val, 2),
                "pnl_dollar":    round(pnl, 2),
                "pnl_pct":       round(pnl_pct, 2),
                "date_bought":   h["date_bought"],
                "alloc_pct":     round(current_val / balance * 100, 1) if balance else None,
                "notes":         h.get("notes"),
            })

        unrealized_pnl = total_current - total_invested
        unrealized_pct = (unrealized_pnl / total_invested * 100) if total_invested else 0.0
        cash_remaining = (balance - total_invested) if balance else None

        return {
            "balance":          balance,
            "holdings":         rows,
            "total_invested":   round(total_invested, 2),
            "total_current":    round(total_current, 2),
            "unrealized_pnl":   round(unrealized_pnl, 2),
            "unrealized_pct":   round(unrealized_pct, 2),
            "cash_remaining":   round(cash_remaining, 2) if cash_remaining is not None else None,
        }

    def _fetch_prices(self, tickers: list) -> dict:
        if not tickers:
            return {}
        import math
        try:
            import yfinance as yf
            data = yf.download(tickers, period="2d", progress=False, auto_adjust=True, timeout=8)
            has_levels = hasattr(data.columns, "get_level_values")
            close = data["Close"] if (has_levels and "Close" in data.columns.get_level_values(0)) else data
            out = {}
            if len(tickers) == 1:
                p = float(close.dropna().iloc[-1]) if len(close.dropna()) else None
                if p is not None and not math.isnan(p):
                    out[tickers[0]] = p
            else:
                for t in tickers:
                    if t not in close.columns:
                        continue
                    try:
                        p = float(close[t].dropna().iloc[-1])
                        if not math.isnan(p):
                            out[t] = p
                    except Exception:
                        pass
            return out
        except Exception as e:
            print(f"[position_tracker] price fetch failed ({e})")
        return {}

    # ── helpers for webhook parse ─────────────────────────────────────────

    @staticmethod
    def parse_bought_command(tokens: list):
        """
        Parse: BOUGHT XLE 5 47.50   → {ticker, shares=5, avg_cost=47.50}
               BOUGHT XLE 500       → {ticker, dollar_value=500}
               BOUGHT BTC-USD 1000  → {ticker, dollar_value=1000}
        Returns None on bad input.
        """
        # tokens[0] is "BOUGHT", tokens[1] is ticker
        if len(tokens) < 3:
            return None
        ticker = tokens[1].upper()
        nums = []
        for t in tokens[2:]:
            try:
                nums.append(float(t.replace("$", "").replace(",", "")))
            except ValueError:
                pass
        if len(nums) == 2:
            # shares + price
            return {"ticker": ticker, "shares": nums[0], "avg_cost": nums[1],
                    "dollar_value": round(nums[0] * nums[1], 2)}
        elif len(nums) == 1:
            # dollar amount
            return {"ticker": ticker, "dollar_value": nums[0]}
        return None


if __name__ == "__main__":
    import json
    pt = PositionTracker(db_path="/tmp/test_holdings.db")
    pt.set_balance(12500)
    pt.add_position("XLE", shares=10, avg_cost=95.50, dollar_value=955)
    pt.add_position("BTC-USD", dollar_value=500)
    summary = pt.portfolio_summary()
    print(json.dumps(summary, indent=2))
