"""
Vercel serverless Telegram webhook (Flask) — BUY/SELL/SKIP/BOUGHT/BALANCE/STATUS/WHY/ALPHA.
All state lives in Redis (Upstash). No SQLite, no heavy ML libraries.
"""

from flask import Flask, request, jsonify
import json
import os
import datetime
import requests as req

app = Flask(__name__)


# ── Redis helpers ──────────────────────────────────────────────────────────────

def _redis_get(key: str):
    url   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        return None
    try:
        r = req.post(url, json=["GET", key],
                     headers={"Authorization": f"Bearer {token}"}, timeout=3)
        raw = r.json().get("result")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _redis_set(key: str, value, ttl: int = 7776000):  # 90 days default
    url   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        return
    try:
        req.post(url, json=["SETEX", key, ttl, json.dumps(value, default=str)],
                 headers={"Authorization": f"Bearer {token}"}, timeout=3)
    except Exception:
        pass


# ── Telegram send ──────────────────────────────────────────────────────────────

def _tg_send(text: str):
    token   = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        req.post(f"https://api.telegram.org/bot{token}/sendMessage",
                 json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                 timeout=8)
    except Exception:
        pass


# ── GitHub Actions dispatch ────────────────────────────────────────────────────

def _trigger_actions_run():
    pat  = os.environ.get("GITHUB_PAT", "")
    repo = os.environ.get("GITHUB_REPO", "")
    if not pat or not repo:
        return
    try:
        req.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/daily-signals.yml/dispatches",
            json={"ref": "main"},
            headers={"Authorization": f"token {pat}",
                     "Accept": "application/vnd.github+json"},
            timeout=5,
        )
    except Exception:
        pass


# ── Command parser ─────────────────────────────────────────────────────────────

def _parse(text: str) -> dict:
    tokens = text.strip().split()
    if not tokens:
        return {"command": "UNKNOWN", "tokens": []}
    first = tokens[0].upper()
    KNOWN = {"BUY", "SELL", "SKIP", "HOLD", "STATUS", "WHY", "ALPHA",
             "PORTFOLIO", "PERF", "BALANCE", "BOUGHT", "SOLD", "EXPLAIN"}
    command = first if first in KNOWN else "QUESTION"
    ticker = amount = None
    if command == "BUY" and len(tokens) > 1:
        second = tokens[1].upper()
        if second in ("A", "B", "C", "D", "E"):
            amount = ord(second) - ord("A") + 1
        elif second.replace("-", "").isalpha():
            ticker = second
        else:
            try:
                amount = int(second)
            except Exception:
                pass
    if command == "BALANCE" and len(tokens) > 1:
        try:
            amount = float(tokens[1].replace("$", "").replace(",", ""))
        except Exception:
            pass
    reason = " ".join(tokens[2:]) if len(tokens) > 2 else ""
    return {"command": command, "ticker": ticker, "amount": amount,
            "reason": reason, "tokens": tokens}


# ── Alpaca paper order ─────────────────────────────────────────────────────────

def _alpaca_buy(ticker: str, confidence: int = 50) -> dict:
    key    = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return {"executed": False, "reason": "no_alpaca_keys"}
    try:
        balance = float(os.environ.get("DEFAULT_BALANCE", "0") or "0")
    except Exception:
        balance = 0
    pct      = 0.30 if confidence >= 75 else 0.20 if confidence >= 50 else 0.10
    notional = round(balance * pct, 2) if balance > 0 else 500.0
    base     = "https://paper-api.alpaca.markets"
    hdrs     = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                "Content-Type": "application/json"}
    try:
        pos_r = req.get(f"{base}/v2/positions", headers=hdrs, timeout=5)
        if pos_r.status_code == 200:
            for pos in pos_r.json():
                req.delete(f"{base}/v2/positions/{pos['symbol']}", headers=hdrs, timeout=5)
        order_r = req.post(f"{base}/v2/orders", headers=hdrs, timeout=8,
                           json={"symbol": ticker, "notional": notional,
                                 "side": "buy", "type": "market", "time_in_force": "day"})
        if order_r.status_code in (200, 201):
            return {"executed": True, "notional": notional, "ticker": ticker}
        return {"executed": False, "reason": order_r.text[:200]}
    except Exception as e:
        return {"executed": False, "reason": str(e)[:200]}


# ── Command handlers ───────────────────────────────────────────────────────────

def _handle(text: str) -> str:
    cmd      = _parse(text)
    command  = cmd["command"]
    briefing = _redis_get("sc:last_briefing") or {}
    ranked   = briefing.get("ranked_opportunities") or []

    if command in ("BUY", "SELL", "SKIP", "HOLD"):
        ticker = cmd["ticker"]
        amount = cmd["amount"]

        if amount and not ticker and ranked:
            idx = int(amount) - 1
            if 0 <= idx < len(ranked):
                opp    = ranked[idx]
                ticker = opp.get("ticker") if isinstance(opp, dict) else None

        ticker     = ticker or briefing.get("ticker") or "SPY"
        confidence = int(briefing.get("confidence") or 50)

        today   = datetime.date.today().isoformat()
        replies = _redis_get("sc:journal_replies") or []
        if not isinstance(replies, list):
            replies = []
        replies = [r for r in replies if r.get("date") != today]
        replies.append({"date": today, "command": command,
                        "reason": cmd["reason"] or "(no reason given)",
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        replies = replies[-90:]
        _redis_set("sc:journal_replies", replies)

        alpaca_line = ""
        if command == "BUY" and ticker:
            result = _alpaca_buy(ticker, confidence)
            if result.get("executed"):
                alpaca_line = f"\n🏦 Alpaca paper: BUY {ticker} (${result.get('notional', 500):,.0f})"
            elif not os.environ.get("ALPACA_API_KEY"):
                alpaca_line = "\n(Add ALPACA_API_KEY + ALPACA_SECRET_KEY to Vercel env vars to auto-execute)"

        bought_reminder = ""
        if command == "BUY" and ticker:
            bought_reminder = f"\n\n💡 Log real money: <code>BOUGHT {ticker} [dollar amount]</code>"

        return (f"✅ Logged: <b>{command} {ticker}</b>"
                + (f"\nReason: {cmd['reason']}" if cmd["reason"] else "")
                + alpaca_line
                + "\n📊 Paper mode active"
                + bought_reminder)

    elif command == "STATUS":
        if not briefing:
            return "No briefing yet. GitHub Actions runs at 9AM, 10:30AM, 12PM, 2PM, 3:30PM, 4:30PM EDT."
        ticker = briefing.get("ticker", "—")
        action = briefing.get("action", "—")
        conf   = briefing.get("confidence", "—")
        date   = briefing.get("date", "—")
        gen    = (briefing.get("freshness") or {}).get("generated_utc", "—")
        return (f"📋 Last signal ({date}): <b>{action} {ticker}</b> @ {conf}%\n"
                f"Generated: {gen}\n"
                f"Reply <code>BUY</code>, <code>SKIP</code>, or ask a question.")

    elif command == "WHY":
        trace = briefing.get("why_trace") or []
        if trace:
            return "🧠 <b>Reasoning:</b>\n" + "\n".join(f"• {t}" for t in trace if t)
        return "No reasoning trace available for this briefing."

    elif command == "ALPHA":
        picks = briefing.get("equity_alpha_picks") or []
        if not picks:
            return ("📊 <b>Equity Alpha</b>\n\nNo picks yet — generated with each daily briefing.")
        EMOJI = {"HIGH": "🔥", "MEDIUM": "✅", "LOW": "🟡"}
        lines = ["📈 <b>Stock Alpha Picks</b>"]
        for i, p in enumerate(picks[:5], 1):
            emoji   = EMOJI.get(p.get("conviction"), "⚪")
            dollar  = f" → <b>${p['suggested_dollar']:.0f}</b>" if p.get("suggested_dollar") else ""
            lines.append(f"  {i}. {emoji} <b>{p['ticker']}</b> ({p.get('sector_name', '')}) "
                         f"score {p.get('composite_score', 0):.0f}/100{dollar}")
            if p.get("conviction_tagline"):
                lines.append(f"     💡 {p['conviction_tagline']}")
        return "\n".join(lines)

    elif command == "BALANCE":
        amount = cmd["amount"]
        if amount and amount > 0:
            _redis_set("sc:balance", amount)
            return (f"✅ Balance set to <b>${amount:,.0f}</b>\n"
                    f"Reply <code>PORTFOLIO</code> to see holdings.")
        else:
            val = _redis_get("sc:balance")
            bal = float(val) if val is not None else None
            if bal:
                return f"Current balance: <b>${bal:,.0f}</b>\nUpdate: <code>BALANCE 15000</code>"
            return "Set your balance: <code>BALANCE 12500</code>"

    elif command == "BOUGHT":
        tokens = cmd["tokens"]
        if len(tokens) < 3:
            return ("Format: <code>BOUGHT XLE 500</code>\n"
                    "With note: <code>BOUGHT XLE 500 earnings breakout</code>")
        ticker = tokens[1].upper()
        nums, note_parts = [], []
        for t in tokens[2:]:
            try:
                nums.append(float(t.replace("$", "").replace(",", "")))
            except ValueError:
                note_parts.append(t)
        notes = " ".join(note_parts).lstrip("-–: ").strip() or None
        if len(nums) == 2:
            dollar = round(nums[0] * nums[1], 2)
            entry  = {"ticker": ticker, "shares": nums[0], "avg_cost": nums[1], "dollar_value": dollar}
        elif len(nums) == 1:
            entry = {"ticker": ticker, "dollar_value": nums[0]}
        else:
            return "Format: <code>BOUGHT XLE 500</code>"
        holdings = _redis_get("sc:holdings") or []
        if not isinstance(holdings, list):
            holdings = []
        holdings = [h for h in holdings if h.get("ticker") != ticker]
        entry["date_bought"] = datetime.date.today().isoformat()
        if notes:
            entry["notes"] = notes
        holdings.append(entry)
        _redis_set("sc:holdings", holdings)
        dollar = entry.get("dollar_value", 0)
        return (f"✅ Logged: <b>{ticker}</b> ${dollar:,.0f}"
                + (f"\nNote: {notes}" if notes else "")
                + "\nReply <code>PORTFOLIO</code> to see all holdings.")

    elif command == "SOLD":
        tokens = cmd["tokens"]
        ticker = tokens[1].upper() if len(tokens) > 1 else None
        if not ticker:
            return "Format: <code>SOLD XLE</code>"
        holdings = _redis_get("sc:holdings") or []
        if not isinstance(holdings, list):
            holdings = []
        holdings = [h for h in holdings if h.get("ticker") != ticker]
        _redis_set("sc:holdings", holdings)
        return f"✅ <b>{ticker}</b> removed.\nReply <code>PORTFOLIO</code> to confirm."

    elif command == "PORTFOLIO":
        balance_raw = _redis_get("sc:balance")
        balance     = float(balance_raw) if balance_raw is not None else None
        if not balance:
            return "Set your balance first: <code>BALANCE 12500</code>"
        holdings = _redis_get("sc:holdings") or []
        if not holdings:
            return (f"Balance: <b>${balance:,.0f}</b>\nNo open positions.\n"
                    f"Log one: <code>BOUGHT XLF 500</code>")
        total_invested = sum(h.get("dollar_value", 0) for h in holdings if isinstance(h, dict))
        lines = [f"💼 <b>Portfolio</b>  (Balance: ${balance:,.0f})"]
        for h in holdings:
            if not isinstance(h, dict):
                continue
            t   = h.get("ticker", "?")
            d   = h.get("dollar_value") or 0
            pct = round(d / balance * 100, 1) if balance else 0
            note = f"  — {h['notes']}" if h.get("notes") else ""
            lines.append(f"  • <b>{t}</b>  ${d:,.0f}  ({pct}%){note}")
        cash = balance - total_invested
        lines.append(f"\nInvested: ${total_invested:,.0f}  ·  Cash: ${cash:,.0f}")
        return "\n".join(lines)

    elif command == "PERF":
        perf = briefing.get("performance") or {}
        if perf.get("portfolio_return_pct") is not None:
            p = perf["portfolio_return_pct"]
            s = perf.get("spy_return_pct", 0)
            a = perf.get("alpha_pct", 0)
            n = perf.get("n_trades", 0)
            return (f"📈 <b>Performance</b>\n"
                    f"Portfolio: <b>{p:+.1f}%</b>  SPY: {s:+.1f}%  Alpha: {a:+.2f}%\n"
                    f"{n} trades tracked")
        return "No completed trades yet. Reply BUY to a briefing to start tracking."

    else:
        return ("Commands:\n"
                "<code>BUY A</code>  <code>BUY XLF</code>  <code>SELL</code>  <code>SKIP</code>\n"
                "<code>STATUS</code>  <code>WHY</code>  <code>ALPHA</code>  <code>PORTFOLIO</code>  <code>PERF</code>\n"
                "<code>BALANCE 12500</code>  <code>BOUGHT XLE 500</code>  <code>SOLD XLE</code>")


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/api/webhook", methods=["POST"])
def telegram_webhook():
    try:
        body = request.get_json(force=True, silent=True) or {}
        msg  = body.get("message") or body.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        if text:
            reply = _handle(text)
            _tg_send(reply)
            cmd = text.strip().split()[0].upper() if text.strip() else ""
            if cmd in ("BALANCE", "BOUGHT", "SOLD"):
                _trigger_actions_run()
    except Exception as e:
        _tg_send(f"⚠️ Error: {e}")
    return jsonify({"ok": True})


@app.route("/api/webhook", methods=["GET"])
def health():
    briefing = _redis_get("sc:last_briefing") or {}
    balance  = _redis_get("sc:balance")
    holdings = _redis_get("sc:holdings") or []
    return jsonify({
        "ok":            True,
        "service":       "sector-command-webhook",
        "briefing_date": briefing.get("date", "none"),
        "balance":       balance,
        "n_holdings":    len(holdings) if isinstance(holdings, list) else 0,
        "env_telegram":  bool(os.environ.get("TELEGRAM_TOKEN")),
        "env_redis":     bool(os.environ.get("UPSTASH_REDIS_REST_URL")),
        "env_alpaca":    bool(os.environ.get("ALPACA_API_KEY")),
        "env_github_pat": bool(os.environ.get("GITHUB_PAT")),
    })


# also handle root path (Vercel may strip /api/webhook prefix)
@app.route("/", methods=["POST"])
def telegram_webhook_root():
    return telegram_webhook()


@app.route("/", methods=["GET"])
def health_root():
    return health()
