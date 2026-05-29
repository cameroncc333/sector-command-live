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

# Sector ETF universe — expanded with company names, tickers, and sub-industry terms
SECTOR_KEYWORDS = {
    "XLK": ["tech", "technology", "software", "semiconductor", "chip", "ai ", " ai,",
            "artificial intelligence", "cloud", "apple", "microsoft", "nvidia", "broadcom",
            "amd", "qualcomm", "oracle", "salesforce", "adobe", "data center", "cybersecurity",
            "gpu", "machine learning", "quantum", "aapl", "msft", "nvda", "avgo"],
    "XLF": ["bank", "banking", "financial", "lending", "interest rate", "jpmorgan", "goldman",
            "morgan stanley", "citigroup", "insurance", "credit", "federal reserve", "fed rate",
            "fintech", "blackrock", "visa", "mastercard", "payment", "jpm", "bac", "wfc", "gs"],
    "XLE": ["oil", "energy", "crude", "gas", "opec", "drilling", "exxon", "chevron",
            "conocophillips", "barrel", "refinery", "lng", "natural gas", "pipeline",
            "petroleum", "xom", "cvx", "cop", "slb", "oxy"],
    "XLV": ["health", "pharma", "drug", "biotech", "fda", "medical", "vaccine", "hospital",
            "eli lilly", "unitedhealth", "abbvie", "merck", "pfizer", "clinical trial",
            "biosimilar", "medicare", "insurance claim", "lly", "unh", "jnj", "abbv", "mrk"],
    "XLY": ["consumer discretionary", "retail", "amazon", "tesla", "home depot",
            "auto", "restaurant", "travel", "leisure", "luxury", "e-commerce",
            "consumer spending", "discretionary", "amzn", "tsla", "hd", "mcd", "nke"],
    "XLP": ["consumer staples", "grocery", "household", "food", "beverage", "walmart",
            "procter", "coca-cola", "pepsi", "costco", "staples", "cpg", "packaged food",
            "wmt", "pg", "ko", "pep", "cost"],
    "XLI": ["industrial", "manufacturing", "aerospace", "defense", "boeing", "caterpillar",
            "honeywell", "machinery", "freight", "logistics", "ge aerospace", "ups",
            "infrastructure", "lockheed", "raytheon", "cat", "hon", "rtx", "lmt"],
    "XLB": ["materials", "mining", "chemical", "steel", "copper", "commodity",
            "lithium", "aluminum", "gold mining", "fertilizer", "linde", "freeport",
            "air products", "sherwin", "lin", "apd", "fcx", "nem"],
    "XLRE": ["real estate", "reit", "property", "mortgage", "housing", "commercial real",
             "data center reit", "cell tower", "prologis", "american tower", "equinix",
             "office", "multifamily", "pld", "amt", "eqix", "spg"],
    "XLU": ["utility", "utilities", "electric", "power grid", "renewable", "nuclear",
             "solar", "wind energy", "nextera", "duke energy", "grid", "transmission",
             "rate case", "nee", "duk", "so", "aep"],
    "XLC": ["communication", "media", "telecom", "streaming", "social media", "meta",
             "alphabet", "google", "netflix", "disney", "comcast", "advertising",
             "content", "5g", "wireless", "nflx", "dis", "cmcsa", "chtr"],
}

# Expanded positive/negative lexicon with financial domain terms
POS_WORDS_EXTRA = {"accelerates", "accelerating", "breakout", "outperform", "outperforms",
                   "buyback", "dividend", "raised", "raises", "initiates", "overweight",
                   "acquisition", "partnership", "contract", "beat", "exceeds", "raised guidance",
                   "upgrade", "upgraded", "positive", "expands", "expansion"}
NEG_WORDS_EXTRA = {"downgrade", "downgraded", "underweight", "misses", "lowered", "cuts guidance",
                   "investigation", "lawsuit", "recall", "bankruptcy", "layoffs", "restructuring",
                   "shortfall", "disappoints", "disappointing", "suspended", "halted", "probe"}

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
        feeds = [
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",           # CNBC Markets
            "https://www.cnbc.com/id/10000664/device/rss/rss.html",             # CNBC Business
            "https://finance.yahoo.com/rss/topstories",                         # Yahoo Finance
            "https://feeds.reuters.com/reuters/businessNews",                   # Reuters Business
            "https://www.marketwatch.com/rss/topstories",                       # MarketWatch
            "https://feeds.bloomberg.com/markets/news.rss",                     # Bloomberg Markets
            "https://feeds.bloomberg.com/economics/news.rss",                   # Bloomberg Economics
            "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",        # NYT Business
            "https://nypost.com/business/feed/",                                # NY Post Business
            "https://www.barrons.com/rss/the-trader",                           # Barron's The Trader
            "https://feeds.feedburner.com/TheStreet-Stocks",                    # TheStreet
            "https://www.investing.com/rss/news.rss",                           # Investing.com
            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",                    # WSJ Markets
            "https://feeds.a.dj.com/rss/RSSWSJD.xml",                          # WSJ US Business
            "https://fortune.com/feed/fortune-feeds/?id=3252520",               # Fortune Finance
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
            # FINBERT_MODEL options (in order of accuracy vs speed):
            #   "ProsusAI/finbert"           — gold standard, 440MB (default)
            #   "yiyanghkust/finbert-tone"   — finer-grained tone (same size)
            #   "distilbert-base-uncased-finetuned-sst-2-english" — 265MB, faster, less domain-specific
            # DistilBERT (93.23% accuracy per 2025 Atlantis Press study) is a strong
            # lightweight alternative when GitHub Actions memory is tight.
            model_id = os.environ.get("FINBERT_MODEL", "ProsusAI/finbert")
            self._finbert = pipeline("text-classification", model=model_id, top_k=None)
        scores = []
        for h in headlines:
            try:
                out = self._finbert(h[:512])[0]
                d = {o["label"].lower(): o["score"] for o in out}
                # Handle both FinBERT labels (positive/negative/neutral)
                # and DistilBERT labels (POSITIVE/NEGATIVE) and finbert-tone labels
                pos = d.get("positive", d.get("pos", d.get("POSITIVE", 0)))
                neg = d.get("negative", d.get("neg", d.get("NEGATIVE", 0)))
                scores.append(float(pos - neg))
            except Exception:
                scores.append(0.0)
        return scores

    def _score_lexicon(self, headlines):
        all_pos = POS_WORDS | POS_WORDS_EXTRA
        all_neg = NEG_WORDS | NEG_WORDS_EXTRA
        scores = []
        for h in headlines:
            words = set(re.findall(r"[a-z]+", h.lower()))
            # Phrase-level matching for multi-word terms
            h_lower = h.lower()
            pos = len(words & all_pos) + sum(1 for p in ["beat estimates", "raised guidance",
                "record revenue", "strong earnings"] if p in h_lower)
            neg = len(words & all_neg) + sum(1 for p in ["misses estimates", "lowered guidance",
                "disappointed investors", "revenue shortfall"] if p in h_lower)
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


def get_stocktwits_sentiment(ticker: str) -> dict | None:
    """
    Fetch real-time social sentiment for a ticker from StockTwits.
    No API key required. Returns bullish/bearish ratio as a -1..+1 score.

    StockTwits is the primary social platform for retail traders — high signal
    for meme-adjacent tickers (TSLA, NVDA, AMD) and sector ETFs.

    Research: Jay Lin (Medium 2024) — StockTwits retail sentiment has 3-5 day
    leading correlation with price momentum for large-cap US equities.
    """
    import requests as _req
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        r = _req.get(url, timeout=8, headers={"User-Agent": "SectorCommand/1.0"})
        if r.status_code != 200:
            return None
        data = r.json()
        messages = data.get("messages", [])
        if not messages:
            return None

        bull, bear, total = 0, 0, 0
        for m in messages[:50]:  # last 50 messages
            sentiment = (m.get("entities", {}).get("sentiment") or {}).get("basic")
            if sentiment == "Bullish":
                bull += 1
                total += 1
            elif sentiment == "Bearish":
                bear += 1
                total += 1

        if total == 0:
            return None

        score = round((bull - bear) / total, 3)   # -1..+1
        return {
            "ticker":       ticker,
            "bullish":      bull,
            "bearish":      bear,
            "total_tagged": total,
            "score":        score,
            "signal":       "BULLISH" if score > 0.2 else "BEARISH" if score < -0.2 else "NEUTRAL",
        }
    except Exception as e:
        print(f"[news_feeder] StockTwits fetch failed for {ticker}: {e}")
        return None


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
