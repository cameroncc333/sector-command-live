"""
news_feeder.py — News sentiment as a REAL trade signal

This is the addition you asked about: looking at the news and basing trades on it.
Of everything in the Gemini thread, this is the strongest defensible add — news
sentiment genuinely moves sector ETFs, and you already own the FinBERT machinery
from your fomc-sentiment-analyzer repo.

What it does:
  1. Pulls recent financial headlines (NewsAPI free tier, or RSS fallback)
  2. Maps each headline to the sector(s) it's about, via keyword routing
  3. Scores sentiment with FinBERT  -> P(pos) - P(neg)  in [-1, +1]
  4. Aggregates to a per-sector sentiment score for the day
  5. Returns a dict {sector_ticker: sentiment} the engine uses as a CONVICTION
     MODIFIER (it can raise/lower confidence or trigger abstain — it does NOT
     override the RL agent's choice). This is the defensible architecture:
     RL is the brain, sentiment is a gating modifier.

HONEST NOTE: FinBERT is ~440MB. On GitHub Actions free tier this is borderline.
Two modes:
  - "transformer": load real FinBERT (use locally / on a beefier runner)
  - "lexicon":     lightweight fallback keyword sentiment (always works, no model)
Set NEWS_MODE env var. Default falls back to lexicon if transformers isn't installed.
"""

import os
import re
from collections import defaultdict

# Sector ETF universe (matches your existing 11)
SECTOR_KEYWORDS = {
    "XLK": ["tech", "technology", "software", "semiconductor", "chip", "ai ", "cloud", "apple", "microsoft", "nvidia"],
    "XLF": ["bank", "financial", "lending", "interest rate", "jpmorgan", "goldman", "insurance", "credit"],
    "XLE": ["oil", "energy", "crude", "gas", "opec", "drilling", "exxon", "chevron", "barrel"],
    "XLV": ["health", "pharma", "drug", "biotech", "fda", "medical", "vaccine", "hospital"],
    "XLY": ["consumer discretionary", "retail", "amazon", "tesla", "auto", "restaurant", "travel", "leisure"],
    "XLP": ["consumer staples", "grocery", "household", "food", "beverage", "walmart", "procter"],
    "XLI": ["industrial", "manufacturing", "aerospace", "defense", "boeing", "machinery", "freight"],
    "XLB": ["materials", "mining", "chemical", "steel", "copper", "commodity"],
    "XLRE": ["real estate", "reit", "property", "mortgage", "housing", "commercial real"],
    "XLU": ["utility", "utilities", "electric", "power grid", "renewable", "nuclear"],
    "XLC": ["communication", "media", "telecom", "streaming", "social media", "meta", "google", "netflix"],
}

# Tiny finance lexicon for the no-model fallback
POS_WORDS = {"beat", "beats", "surge", "surges", "rally", "rallies", "gain", "gains", "jump", "jumps",
             "soar", "soars", "record", "strong", "growth", "upgrade", "upgrades", "boost", "boosts",
             "profit", "profits", "outperform", "bullish", "rebound", "recovery", "optimism"}
NEG_WORDS = {"miss", "misses", "plunge", "plunges", "slump", "slumps", "fall", "falls", "drop", "drops",
             "crash", "crashes", "weak", "loss", "losses", "downgrade", "downgrades", "cut", "cuts",
             "fear", "fears", "recession", "bearish", "selloff", "warning", "warns", "decline", "slowdown"}


class NewsFeeder:
    def __init__(self, mode=None, newsapi_key=None):
        self.mode = mode or os.environ.get("NEWS_MODE", "auto")
        self.newsapi_key = newsapi_key or os.environ.get("NEWSAPI_KEY", "")
        self._finbert = None
        if self.mode == "auto":
            self.mode = "transformer" if self._transformers_available() else "lexicon"

    @staticmethod
    def _transformers_available():
        try:
            import transformers  # noqa
            return True
        except ImportError:
            return False

    # ---- headline ingestion -------------------------------------------
    def fetch_headlines(self, limit=60):
        """Return a list of headline strings. Tries NewsAPI, falls back to RSS."""
        if self.newsapi_key:
            try:
                return self._fetch_newsapi(limit)
            except Exception as e:
                print(f"[news_feeder] NewsAPI failed ({e}); falling back to RSS")
        try:
            return self._fetch_rss(limit)
        except Exception as e:
            print(f"[news_feeder] RSS failed ({e}); returning empty list")
            return []

    def _fetch_newsapi(self, limit):
        import requests
        url = "https://newsapi.org/v2/top-headlines"
        params = {"category": "business", "language": "en",
                  "pageSize": min(limit, 100), "apiKey": self.newsapi_key}
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
        return [a["title"] for a in data.get("articles", []) if a.get("title")]

    def _fetch_rss(self, limit):
        """Free, keyless fallback: pull multiple finance RSS feeds."""
        import requests
        # Ordered by reliability; we try all and deduplicate
        feeds = [
            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",           # WSJ Markets
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",    # CNBC Markets
            "https://www.cnbc.com/id/10000664/device/rss/rss.html",     # CNBC Business
            "https://finance.yahoo.com/rss/topstories",                 # Yahoo Finance
            "https://www.marketwatch.com/rss/topstories",               # MarketWatch
            "https://feeds.reuters.com/reuters/businessNews",           # Reuters Business
            "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", # NYT Business
        ]
        seen, titles = set(), []
        for feed in feeds:
            if len(titles) >= limit:
                break
            try:
                r = requests.get(feed, timeout=10,
                                 headers={"User-Agent": "SectorCommand/1.1"})
                found = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", r.text)
                for t in found:
                    t = t.strip()
                    if len(t) > 25 and t.lower() not in seen:
                        seen.add(t.lower())
                        titles.append(t)
            except Exception:
                continue
        return titles[:limit]

    # ---- sentiment scoring --------------------------------------------
    def _score_finbert(self, headlines):
        if self._finbert is None:
            from transformers import pipeline
            # NOTE: verify this exact model id on HuggingFace before trusting it.
            # Base ProsusAI/finbert is the safe known-good default.
            model_id = os.environ.get("FINBERT_MODEL", "ProsusAI/finbert")
            self._finbert = pipeline("text-classification", model=model_id, top_k=None)
        scores = []
        for h in headlines:
            out = self._finbert(h[:512])[0]
            d = {o["label"].lower(): o["score"] for o in out}
            scores.append(d.get("positive", 0) - d.get("negative", 0))
        return scores

    def _score_lexicon(self, headlines):
        scores = []
        for h in headlines:
            words = set(re.findall(r"[a-z]+", h.lower()))
            pos = len(words & POS_WORDS)
            neg = len(words & NEG_WORDS)
            total = pos + neg
            scores.append((pos - neg) / total if total else 0.0)
        return scores

    def score(self, headlines):
        if self.mode == "transformer":
            try:
                return self._score_finbert(headlines)
            except Exception as e:
                print(f"[news_feeder] FinBERT failed ({e}); using lexicon")
        return self._score_lexicon(headlines)

    # ---- sector routing + aggregation ---------------------------------
    @staticmethod
    def route_to_sectors(headline):
        h = headline.lower()
        hits = [tkr for tkr, kws in SECTOR_KEYWORDS.items() if any(k in h for k in kws)]
        return hits

    def daily_sector_sentiment(self, limit=60):
        """
        Main entry point. Returns:
          {
            "by_sector": {ticker: avg_sentiment, ...},
            "top_headline": str,
            "n_headlines": int,
            "mode": "transformer"|"lexicon",
          }
        """
        headlines = self.fetch_headlines(limit)
        if not headlines:
            return {"by_sector": {}, "top_headline": None, "n_headlines": 0, "mode": self.mode}

        scores = self.score(headlines)
        bucket = defaultdict(list)
        for h, s in zip(headlines, scores):
            for tkr in self.route_to_sectors(h):
                bucket[tkr].append(s)

        by_sector = {tkr: round(sum(v) / len(v), 3) for tkr, v in bucket.items() if v}
        # pick the headline with largest absolute sentiment as "top"
        top_idx = max(range(len(scores)), key=lambda i: abs(scores[i])) if scores else None
        top_headline = headlines[top_idx] if top_idx is not None else None

        return {
            "by_sector": by_sector,
            "top_headline": top_headline,
            "n_headlines": len(headlines),
            "mode": self.mode,
        }


if __name__ == "__main__":
    # Offline smoke test with fake headlines (no network)
    feeder = NewsFeeder(mode="lexicon")
    fake = [
        "Tech stocks surge as Nvidia beats earnings estimates",
        "Bank shares slump on recession fears and rate cut warning",
        "Oil prices jump after OPEC supply cut",
        "Healthcare rallies on strong drug trial results",
        "Retail sales drop, consumer discretionary weak",
    ]
    sc = feeder.score(fake)
    bucket = defaultdict(list)
    for h, s in zip(fake, sc):
        for tkr in feeder.route_to_sectors(h):
            bucket[tkr].append(s)
    print("Per-sector sentiment:")
    for tkr, v in bucket.items():
        print(f"  {tkr}: {round(sum(v)/len(v),3)}")
