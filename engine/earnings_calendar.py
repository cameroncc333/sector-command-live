"""
earnings_calendar.py — upcoming earnings warnings for held sector positions

For each sector ETF you hold or that the RL recommends, warns you if a major
holding reports earnings within the next 5 trading days. Earnings = event risk:
a stock can gap 10%+ in either direction, so knowing in advance lets you size
down or hedge rather than getting blindsided.

Data source: yfinance .calendar (free, no API key needed).
Top holdings are hardcoded per ETF — they change slowly (quarterly rebalancing)
and this avoids a slow API call for holdings lookup.
"""

import datetime

# Top 3-5 holdings by weight for each SPDR sector ETF (as of early 2026)
SECTOR_HOLDINGS = {
    "XLK":  ["AAPL", "MSFT", "NVDA", "AVGO", "ADBE"],
    "XLF":  ["BRK-B", "JPM", "V", "MA", "BAC"],
    "XLV":  ["LLY", "UNH", "JNJ", "ABBV", "MRK"],
    "XLY":  ["AMZN", "TSLA", "HD", "MCD", "NKE"],
    "XLP":  ["PG", "COST", "WMT", "KO", "PEP"],
    "XLE":  ["XOM", "CVX", "COP", "SLB", "EOG"],
    "XLI":  ["GE", "RTX", "CAT", "UNP", "HON"],
    "XLB":  ["LIN", "APD", "FCX", "NEM", "DOW"],
    "XLRE": ["PLD", "AMT", "EQIX", "PSA", "O"],
    "XLU":  ["NEE", "DUK", "SO", "AEP", "SRE"],
    "XLC":  ["META", "GOOGL", "NFLX", "DIS", "CMCSA"],
}

TRADING_DAYS_WINDOW = 5   # warn if earnings within this many trading days


def upcoming_earnings_for_sector(sector_etf: str, window_days: int = TRADING_DAYS_WINDOW) -> list:
    """
    Returns list of upcoming earnings dicts for the given sector ETF.
    Each dict: {ticker, earnings_date, days_away, sector}
    """
    holdings = SECTOR_HOLDINGS.get(sector_etf.upper(), [])
    return _check_tickers(holdings, sector_etf, window_days)


def upcoming_earnings_for_holdings(sector_etfs: list, window_days: int = TRADING_DAYS_WINDOW) -> list:
    """Check all provided sector ETFs at once. sector_etfs = list of ticker strings."""
    results = []
    for etf in sector_etfs:
        results.extend(upcoming_earnings_for_sector(etf, window_days))
    return results


def _check_tickers(tickers: list, sector: str, window_days: int) -> list:
    """Pull earnings dates via yfinance for a list of tickers."""
    results = []
    today   = datetime.date.today()
    try:
        import yfinance as yf
        for t in tickers:
            try:
                info = yf.Ticker(t).calendar
                if info is None or info.empty:
                    continue

                # yfinance .calendar returns a DataFrame with dates as index
                # Earnings Date is typically in the 'Earnings Date' column
                if "Earnings Date" in info.columns:
                    raw_dates = info["Earnings Date"].dropna().tolist()
                elif hasattr(info, "index") and "Earnings Date" in info.index:
                    raw_dates = [info.loc["Earnings Date"]]
                else:
                    continue

                for d in raw_dates[:1]:   # take the next upcoming date only
                    try:
                        if hasattr(d, "date"):
                            earn_date = d.date()
                        elif hasattr(d, "year"):
                            earn_date = d
                        else:
                            earn_date = datetime.datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                        days_away = (earn_date - today).days
                        if 0 <= days_away <= window_days * 1.5:   # slightly wider net
                            results.append({
                                "ticker":        t,
                                "sector_etf":    sector,
                                "earnings_date": earn_date.isoformat(),
                                "days_away":     days_away,
                                "urgency":       "HIGH" if days_away <= 2 else "MEDIUM",
                            })
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception as e:
        print(f"[earnings_calendar] check failed: {e}")
    return results


def format_earnings_warning(earnings: list) -> str:
    """
    Format earnings warnings for the Telegram briefing.
    Returns empty string if nothing is imminent.
    """
    if not earnings:
        return ""

    lines = ["📅 <b>Earnings Event Risk</b>"]
    for e in sorted(earnings, key=lambda x: x["days_away"]):
        urgency_emoji = "🔴" if e["urgency"] == "HIGH" else "🟡"
        days = e["days_away"]
        day_str = "TODAY" if days == 0 else "TOMORROW" if days == 1 else f"in {days} days"
        lines.append(f"  {urgency_emoji} <b>{e['ticker']}</b> ({e['sector_etf']}) reports {day_str} "
                     f"({e['earnings_date']}) — consider sizing down or hedging")
    return "\n".join(lines)


def earnings_warning_for_briefing(rl_target: str, held_sectors: list = None) -> str:
    """
    Convenience function for main_engine: checks the RL's target sector
    plus any sectors you currently hold.
    """
    sectors_to_check = list(set([rl_target] + (held_sectors or [])))
    earnings = upcoming_earnings_for_holdings(sectors_to_check, window_days=TRADING_DAYS_WINDOW)
    return format_earnings_warning(earnings), earnings


if __name__ == "__main__":
    print("Checking XLK earnings (top holdings)...")
    e = upcoming_earnings_for_sector("XLK")
    print(e if e else "No earnings within window")
    print(format_earnings_warning(e))
