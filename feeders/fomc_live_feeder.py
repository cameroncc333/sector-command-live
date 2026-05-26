"""
fomc_live_feeder.py — Real-time FOMC sentiment injection

When the Fed meets, this module downloads the actual statement from
federalreserve.gov and scores it with FinBERT (the same FOMC-tuned model
from your fomc-sentiment-analyzer repo). The sentiment feeds into the
decision engine as a conviction modifier.

How it fits:
  - main_engine.py calls this once per run
  - If today is within WINDOW days of a meeting: tries to fetch + score
  - Returns a PMSIResult that modifies the news_by_sector dict
  - If no meeting nearby or Fed statement not yet posted: returns None (no-op)

The FinBERT-FOMC model (ZiweiChen/FinBERT-FOMC) is tuned on Fed MINUTES
and outperforms base FinBERT on monetary-policy-specific sentiment. It's
only loaded if a meeting is imminent (avoids 440MB model load on idle runs).
"""

import os
import re
import datetime

# FOMC 2024–2027 scheduled meeting dates (announce dates, not minute publish)
FOMC_DATES = [
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-11-05", "2025-12-17",
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16",
    # 2027
    "2027-01-27", "2027-03-17", "2027-05-05", "2027-06-16",
    "2027-07-28", "2027-09-15", "2027-11-03", "2027-12-15",
]
WINDOW = 3       # days before/after meeting date to look for the statement
MODEL = os.environ.get("FINBERT_FOMC_MODEL", "ZiweiChen/FinBERT-FOMC")


def nearest_meeting(today=None):
    """Return (date, days_delta) of the nearest FOMC meeting, or (None, None)."""
    today = today or datetime.date.today()
    best, best_delta = None, None
    for d in FOMC_DATES:
        dt = datetime.date.fromisoformat(d)
        delta = (today - dt).days
        if best_delta is None or abs(delta) < abs(best_delta):
            best, best_delta = dt, delta
    return best, best_delta


def is_meeting_window(today=None):
    """Return True if today is within WINDOW days of an FOMC meeting."""
    _, delta = nearest_meeting(today)
    return delta is not None and abs(delta) <= WINDOW


def fetch_fomc_statement(meeting_date: datetime.date) -> str:
    """
    Attempt to fetch the Fed statement for a given meeting date from
    federalreserve.gov. The URL pattern is:
      https://www.federalreserve.gov/newsevents/pressreleases/monetary{YYYYMMDD}a.htm
    Returns plain text of the statement, or empty string on failure.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
        datestr = meeting_date.strftime("%Y%m%d")
        url = f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{datestr}a.htm"
        r = requests.get(url, timeout=20, headers={"User-Agent": "SectorCommand/1.1"})
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        # Main content is in the article body
        article = soup.find("div", {"class": "col-xs-12 col-sm-8 col-md-8"})
        if not article:
            article = soup.find("div", id="leftColumn") or soup.body
        text = article.get_text(separator=" ") if article else ""
        # Strip excessive whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text[:4000]  # FinBERT max 512 tokens; we'll chunk
    except Exception as e:
        print(f"[fomc_live] fetch failed: {e}")
        return ""


def score_statement(text: str) -> float:
    """
    Score the FOMC statement with FinBERT-FOMC.
    Returns a sentiment score in [-1, +1]:
      positive (dovish / easing) → positive score
      negative (hawkish / tightening) → negative score
    """
    if not text:
        return 0.0
    try:
        from transformers import pipeline
        clf = pipeline("text-classification", model=MODEL,
                       top_k=None, truncation=True, max_length=512)
        # Chunk into 400-word pieces if long
        words = text.split()
        chunks = [" ".join(words[i:i+400]) for i in range(0, len(words), 400)]
        scores = []
        for chunk in chunks[:5]:  # max 5 chunks
            out = clf(chunk)[0]
            d = {o["label"].lower(): o["score"] for o in out}
            scores.append(d.get("positive", 0) - d.get("negative", 0))
        return round(sum(scores) / len(scores), 3) if scores else 0.0
    except ImportError:
        print("[fomc_live] transformers not installed — lexicon fallback")
        return _lexicon_score(text)
    except Exception as e:
        print(f"[fomc_live] FinBERT score failed: {e}")
        return _lexicon_score(text)


def _lexicon_score(text: str) -> float:
    """Lightweight lexicon fallback when transformers not available."""
    text = text.lower()
    dovish = sum(text.count(w) for w in [
        "rate cut", "lower rates", "accommodative", "easing", "support",
        "patient", "pause", "reduced", "labor market", "maximum employment"
    ])
    hawkish = sum(text.count(w) for w in [
        "rate hike", "increase rates", "restrictive", "tightening", "inflation",
        "above target", "expeditious", "ongoing increases", "higher for longer"
    ])
    total = dovish + hawkish
    return round((dovish - hawkish) / total, 3) if total else 0.0


def get_fomc_conviction(today=None) -> dict:
    """
    Main entry for main_engine.py. Returns a dict:
      {
        "sentiment": float (-1 to +1),  # dovish+ / hawkish-
        "meeting_date": str,
        "label": "DOVISH" | "NEUTRAL" | "HAWKISH",
        "note": str,                     # human-readable line for briefing
        "active": bool,                  # True if meeting window is active
      }
    Returns {"active": False} if no meeting nearby.
    """
    today = today or datetime.date.today()
    if not is_meeting_window(today):
        return {"active": False}

    meeting_dt, delta = nearest_meeting(today)
    print(f"[fomc_live] FOMC meeting window active (meeting: {meeting_dt}, Δ{delta:+d}d)")

    statement_text = fetch_fomc_statement(meeting_dt)
    if not statement_text:
        print(f"[fomc_live] Statement not yet posted for {meeting_dt}")
        return {
            "active": True, "meeting_date": str(meeting_dt),
            "sentiment": 0.0, "label": "PENDING",
            "note": f"FOMC meeting {meeting_dt} (statement not yet published)",
        }

    score = score_statement(statement_text)
    label = "DOVISH" if score > 0.15 else "HAWKISH" if score < -0.15 else "NEUTRAL"
    note = (f"Fed statement {meeting_dt}: {label} ({score:+.2f}) — "
            f"{'rate-sensitive growth sectors favored' if label == 'DOVISH' else 'defensive/financials favored' if label == 'HAWKISH' else 'neutral impact'}")

    return {
        "active": True,
        "meeting_date": str(meeting_dt),
        "sentiment": score,
        "label": label,
        "note": note,
    }


if __name__ == "__main__":
    import json
    today = datetime.date.today()
    meeting, delta = nearest_meeting(today)
    print(f"Today: {today}")
    print(f"Nearest FOMC: {meeting} ({delta:+d} days)")
    print(f"In window ({WINDOW}d): {is_meeting_window(today)}")
    result = get_fomc_conviction(today)
    print(json.dumps(result, indent=2, default=str))
