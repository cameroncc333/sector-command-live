"""
political_feeder.py — Congressional trade disclosures (RESEARCH-ONLY)

READ THIS FIRST — the design rationale matters for your essays:

You asked whether there's *any* good use for the political-trading idea. There is,
but NOT the one the Gemini thread pushed. Here's the honest framing:

  - The STOCK Act gives politicians up to ~45 days to disclose trades. By the time
    a disclosure is public, the information edge is gone — it's priced in. So using
    it as a LIVE TRADE SIGNAL is not defensible and we do not do that.

  - But it IS a legitimate RESEARCH artifact. The interesting question isn't
    "copy the trade" — it's "does disclosed Congressional activity correlate with
    subsequent sector returns AT ALL, after the disclosure delay?" That's a real,
    publishable hypothesis (and the likely answer — "no meaningful edge survives
    the delay" — is itself a finding worth reporting, exactly like your FOMC null
    result).

So this module:
  - fetches disclosures (Quiver Quant API if you have a key, else returns empty)
  - aggregates a per-sector "disclosure count" as a CONTEXT LABEL only
  - is consumed by the engine purely for the briefing's research line and the
    research log — it is NEVER passed into the conviction/abstain logic.

This separation (real signal vs. research label) is the kind of disciplined
boundary that reads as rigorous rather than gimmicky.
"""

import os
from collections import defaultdict

# Reuse the same sector keyword map concept, but for company/sector tagging.
# Quiver returns tickers; we'd map individual tickers -> sector ETF. For the
# free/no-key path we just bucket by any sector ETF mentioned directly.
TICKER_TO_SECTOR = {
    # a small illustrative map; extend as needed
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "GOOGL": "XLC", "META": "XLC",
    "JPM": "XLF", "GS": "XLF", "BAC": "XLF", "XOM": "XLE", "CVX": "XLE",
    "UNH": "XLV", "PFE": "XLV", "AMZN": "XLY", "TSLA": "XLY", "WMT": "XLP",
    "BA": "XLI", "CAT": "XLI", "NEE": "XLU", "AMT": "XLRE",
}


class PoliticalFeeder:
    def __init__(self, quiver_key=None):
        self.quiver_key = quiver_key or os.environ.get("QUIVER_API_KEY", "")

    def fetch_recent(self, days=30):
        """
        Return a list of disclosure dicts: [{ticker, transaction, politician, date}].
        Priority:
          1. Quiver Quant API (if QUIVER_API_KEY is set)
          2. House STOCK Act community data (free public S3, no key required)
          3. Senate STOCK Act community data (free public S3, no key required)
        Data is the same STOCK Act disclosures — just different aggregation sources.
        """
        if self.quiver_key:
            result = self._fetch_quiver()
            if result:
                return result

        # Free fallback: community-maintained STOCK Act disclosure aggregators
        # These scrape the official House/Senate disclosure PDFs into JSON
        result = self._fetch_house_public(days) + self._fetch_senate_public(days)
        return result

    def _fetch_quiver(self):
        try:
            import requests
            url = "https://api.quiverquant.com/beta/live/congresstrading"
            headers = {"Authorization": f"Bearer {self.quiver_key}"}
            r = requests.get(url, headers=headers, timeout=20)
            return r.json() if r.status_code == 200 else []
        except Exception as e:
            print(f"[political_feeder] Quiver fetch failed ({e})")
            return []

    def _fetch_house_public(self, days=30):
        """
        House STOCK Act disclosures via community-maintained public dataset.
        Source: housestockwatcher.com (aggregates official House disclosure PDFs)
        """
        try:
            import requests
            import datetime as _dt
            url = "https://house-stock-watcher-data.s3-us-east-2.amazonaws.com/data/all_transactions.json"
            r = requests.get(url, timeout=30, headers={"User-Agent": "SectorCommand/1.1"})
            if r.status_code != 200:
                return []
            cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
            raw = r.json()
            recent = [d for d in raw if (d.get("transaction_date") or "") >= cutoff]
            # Normalize to our schema
            return [{"Ticker": d.get("ticker", "").upper(),
                     "Transaction": d.get("type", ""),
                     "politician": d.get("representative", ""),
                     "date": d.get("transaction_date", "")}
                    for d in recent if d.get("ticker")]
        except Exception as e:
            print(f"[political_feeder] House public data failed ({e})")
            return []

    def _fetch_senate_public(self, days=30):
        """
        Senate STOCK Act disclosures via community-maintained public dataset.
        """
        try:
            import requests
            import datetime as _dt
            url = "https://senate-stock-watcher-data.s3-us-east-2.amazonaws.com/aggregate/all_transactions.json"
            r = requests.get(url, timeout=30, headers={"User-Agent": "SectorCommand/1.1"})
            if r.status_code != 200:
                return []
            cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
            raw = r.json()
            recent = [d for d in raw if (d.get("transaction_date") or "") >= cutoff]
            return [{"Ticker": d.get("ticker", "").upper(),
                     "Transaction": d.get("type", ""),
                     "politician": d.get("senator", ""),
                     "date": d.get("transaction_date", "")}
                    for d in recent if d.get("ticker")]
        except Exception as e:
            print(f"[political_feeder] Senate public data failed ({e})")
            return []

    def sector_disclosure_summary(self, days=30):
        """
        Aggregate disclosures into a per-sector summary for the RESEARCH log/label.
        Returns {ticker_etf: {"buys": n, "sells": n}}.  NEVER used as a trade input.
        """
        disclosures = self.fetch_recent(days)
        summary = defaultdict(lambda: {"buys": 0, "sells": 0})
        for d in disclosures:
            tkr = (d.get("Ticker") or d.get("ticker") or "").upper()
            sector = TICKER_TO_SECTOR.get(tkr)
            if not sector:
                continue
            txn = (d.get("Transaction") or d.get("transaction") or "").lower()
            if "purchase" in txn or "buy" in txn:
                summary[sector]["buys"] += 1
            elif "sale" in txn or "sell" in txn:
                summary[sector]["sells"] += 1
        return dict(summary)

    def briefing_note(self, ticker, days=30):
        """One-line research note for the briefing, or None. Clearly delay-stamped."""
        summary = self.sector_disclosure_summary(days)
        s = summary.get(ticker)
        if not s or (s["buys"] == 0 and s["sells"] == 0):
            return None
        return (f"{s['buys']} disclosed buys / {s['sells']} sells in {ticker} "
                f"over {days}d (≤45d reporting delay — research only)")


if __name__ == "__main__":
    pf = PoliticalFeeder()  # no key -> empty, proving graceful degradation
    print("Disclosure summary (no key):", pf.sector_disclosure_summary())
    print("Briefing note for XLF:", pf.briefing_note("XLF"))
    print("Module runs fine without a Quiver key — political layer is optional.")
