"""
journal.py — automated decision + research logging

Two destinations:
  1. Local SQLite (always on, zero setup, fast, the durable record).
     This is the lightweight "database instead of scattered CSVs" upgrade —
     SQLite ships with Python, no DuckDB dependency needed for this volume.
  2. Google Sheets (optional, if GOOGLE_CREDS_B64 + SHEET_ID are set) — mirrors
     your existing Sector Command sheet so the human-readable log stays in one place.

Every row captures the full state at decision time so you can do post-trade audits
later: what the agents said, what news/politics showed, what you decided and why.
"""

import os
import json
import base64
import sqlite3
import datetime

DB_PATH = os.environ.get(
    "JOURNAL_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "sector_command.db"),
)


class Journal:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _redis(self, *cmd):
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

    def _restore_replies_from_redis(self):
        """After cold-start seed, replay stored human replies so P&L survives restarts."""
        try:
            raw = self._redis("GET", "sc:journal_replies")
            if not raw:
                return
            replies = json.loads(raw) if isinstance(raw, str) else raw
            if not replies:
                return
            con = sqlite3.connect(self.db_path)
            for rep in replies:
                date = rep.get("date")
                command = rep.get("command")
                reason = rep.get("reason", "")
                if not date or not command:
                    continue
                # Apply to the most recent decision on that date
                row = con.execute(
                    "SELECT id FROM decisions WHERE date=? ORDER BY id DESC LIMIT 1", (date,)
                ).fetchone()
                if row:
                    con.execute("UPDATE decisions SET human_command=?, human_reason=? WHERE id=?",
                                (command, reason, row[0]))
            con.commit()
            con.close()
        except Exception:
            pass

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        con.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, date TEXT, regime TEXT, vix REAL,
                rl_action TEXT, rl_target TEXT, recommended_action TEXT,
                recommended_ticker TEXT, confidence INTEGER,
                abstain_reason TEXT, news_sentiment REAL, news_headline TEXT,
                political_note TEXT, why_trace TEXT, research_context TEXT,
                human_command TEXT, human_reason TEXT, paper_mode INTEGER
            )
        """)
        con.commit()
        con.close()

    def log_decision(self, briefing: dict, research_context: dict = None):
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Use top ranked opportunity as the displayed recommendation when one exists
        ranked = briefing.get("ranked_opportunities") or []
        top_ranked = ranked[0] if ranked else {}
        rec_ticker = (top_ranked.get("ticker") if isinstance(top_ranked, dict)
                      else getattr(top_ranked, "ticker", None)) or briefing.get("ticker")
        rec_conf   = (top_ranked.get("score") if isinstance(top_ranked, dict)
                      else getattr(top_ranked, "score", None))
        if rec_conf is not None:
            try:
                rec_conf = int(float(rec_conf) * 100) if float(rec_conf) <= 1 else int(float(rec_conf))
            except Exception:
                rec_conf = briefing.get("confidence")
        else:
            rec_conf = briefing.get("confidence")
        row = (
            ts, briefing.get("date"), briefing.get("regime"), briefing.get("vix"),
            briefing.get("action"), briefing.get("ticker"),
            briefing.get("action"), rec_ticker,
            rec_conf, briefing.get("abstain_reason"),
            briefing.get("news_sentiment"), briefing.get("news_headline"),
            briefing.get("political_note"),
            json.dumps(briefing.get("why_trace", [])),
            json.dumps(research_context or {}),
            None, None, 1 if briefing.get("paper_mode") else 0,
        )
        con = sqlite3.connect(self.db_path)
        cur = con.execute("""
            INSERT INTO decisions (
                ts, date, regime, vix, rl_action, rl_target,
                recommended_action, recommended_ticker, confidence,
                abstain_reason, news_sentiment, news_headline, political_note,
                why_trace, research_context, human_command, human_reason, paper_mode
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, row)
        decision_id = cur.lastrowid
        con.commit()
        con.close()

        # mirror to Google Sheets if configured (reuses your existing creds pattern)
        self._maybe_log_sheets(briefing, research_context)
        return decision_id

    def attach_human_reply(self, command: str, reason: str, decision_id: int = None):
        """Called by the webhook when you reply BUY/SELL/SKIP + reason."""
        con = sqlite3.connect(self.db_path)
        if decision_id is None:
            row = con.execute("SELECT id, date FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
            decision_id = row[0] if row else None
            decision_date = row[1] if row else None
        else:
            r = con.execute("SELECT date FROM decisions WHERE id=?", (decision_id,)).fetchone()
            decision_date = r[0] if r else None
        if decision_id is not None:
            con.execute("UPDATE decisions SET human_command=?, human_reason=? WHERE id=?",
                        (command, reason, decision_id))
            con.commit()
        con.close()

        # Persist to Redis so reply survives Railway restarts
        if decision_date:
            try:
                raw = self._redis("GET", "sc:journal_replies")
                replies = json.loads(raw) if raw and isinstance(raw, str) else (raw or [])
                # Replace any existing reply for this date, then append
                replies = [r for r in replies if r.get("date") != decision_date]
                replies.append({
                    "date": decision_date,
                    "command": command,
                    "reason": reason,
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
                # Keep last 90 days only
                replies = replies[-90:]
                self._redis("SET", "sc:journal_replies", json.dumps(replies))
            except Exception:
                pass

        return decision_id

    def _maybe_log_sheets(self, briefing, research_context):
        creds_b64 = os.environ.get("GOOGLE_CREDS_B64")
        sheet_id = os.environ.get("SHEET_ID")
        if not creds_b64 or not sheet_id:
            return
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            creds_json = json.loads(base64.b64decode(creds_b64))
            scopes = ["https://www.googleapis.com/auth/spreadsheets",
                      "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
            gc = gspread.authorize(creds)
            ws = gc.open_by_key(sheet_id).worksheet("Live Decisions")
            ws.append_row([
                briefing.get("date"), briefing.get("regime"), briefing.get("vix"),
                briefing.get("action"), briefing.get("ticker"),
                briefing.get("confidence"), briefing.get("abstain_reason"),
                briefing.get("news_sentiment"), briefing.get("news_headline"),
                briefing.get("political_note"),
                " | ".join(briefing.get("why_trace", [])),
            ])
        except Exception as e:
            print(f"[journal] Sheets mirror skipped ({e})")

    def recent(self, n=10):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        con.close()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    j = Journal(db_path="/tmp/test_journal.db")
    demo = {
        "date": "2026-05-25", "regime": "NORMAL", "vix": 18.0,
        "action": "BUY", "ticker": "XLF", "confidence": 88,
        "abstain_reason": None, "news_sentiment": 0.42,
        "news_headline": "Bank earnings beat", "political_note": "2 disclosed buys (research only)",
        "why_trace": ["News +0.42", "Confirming news → +8"], "paper_mode": True,
    }
    did = j.log_decision(demo, research_context={"news_mode": "lexicon", "news_headlines_scanned": 42})
    j.attach_human_reply("BUY", "agree, financials oversold and news strong", did)
    print("Logged decision id:", did)
    print("Recent:", json.dumps(j.recent(1), indent=2))
