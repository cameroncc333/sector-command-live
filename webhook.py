"""
webhook.py — Sector Command Live: Telegram webhook + live dashboard + API

Two things in one Flask app (deploy together on Railway):
  1. POST /api/webhook     — Telegram reply handler (all commands)
  2. GET  /                — live trading dashboard
  3. GET  /api/*           — JSON endpoints the dashboard polls every 3 minutes

Dashboard URL: https://your-railway-app.up.railway.app/
Set webhook:   https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-url/api/webhook

Full command list: see interface/telegram_bot.py docstring
"""

import os
import json
import datetime
from flask import Flask, request, jsonify, render_template, send_from_directory


# ── Redis cache helpers ───────────────────────────────────────────────────
# Used to cache slow API responses (yfinance, NewsFeeder) across cold starts.
# All endpoints that make external calls cache their result for CACHE_TTL seconds.
# This keeps Railway response times <500ms even after idle restarts.

CACHE_TTL = 300  # 5 minutes


def _rcache_get(key: str):
    url   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        return None
    try:
        import requests as _req
        r = _req.post(url, json=["GET", key],
                      headers={"Authorization": f"Bearer {token}"}, timeout=3)
        raw = r.json().get("result")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _rcache_set(key: str, value, ttl: int = CACHE_TTL):
    url   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        return
    try:
        import requests as _req
        _req.post(url, json=["SETEX", key, ttl, json.dumps(value, default=str)],
                  headers={"Authorization": f"Bearer {token}"}, timeout=3)
    except Exception:
        pass

from interface.telegram_bot import (
    TelegramBot, parse_command,
    format_crypto_briefing, format_portfolio, format_sizing_guide,
    format_explain, answer_question,
)
from engine.journal import Journal
from engine.position_tracker import PositionTracker

try:
    from engine.llm_router import ask as llm_ask, load_market_context_from_disk
except Exception as _e:
    print(f"[webhook] llm_router unavailable: {_e}")
    llm_ask = None
    load_market_context_from_disk = None

try:
    from engine.risk_metrics import macro_snapshot, portfolio_var, format_risk_block, sector_rotation_matrix
except Exception as _e:
    print(f"[webhook] risk_metrics unavailable: {_e}")
    macro_snapshot = None
    portfolio_var = None
    format_risk_block = None
    sector_rotation_matrix = None

try:
    from engine.sell_signals import check_exit_signals, format_sell_alerts
except Exception as _e:
    print(f"[webhook] sell_signals unavailable: {_e}")
    check_exit_signals = None
    format_sell_alerts = None

try:
    from engine.performance_tracker import PaperPortfolio
except Exception as _e:
    print(f"[webhook] performance_tracker unavailable: {_e}")
    PaperPortfolio = None

try:
    from engine.alpaca_executor import execute_command as alpaca_execute, portfolio_summary
except Exception as _e:
    print(f"[webhook] alpaca_executor unavailable: {_e}")
    alpaca_execute = None
    portfolio_summary = None

try:
    from engine.repo_signals import collect_all, sector_technicals, algo_composite_signal
except Exception as _e:
    print(f"[webhook] repo_signals unavailable: {_e}")
    collect_all = None
    sector_technicals = None
    algo_composite_signal = None

try:
    from engine.earnings_calendar import upcoming_earnings_for_holdings
except Exception as _e:
    print(f"[webhook] earnings_calendar unavailable: {_e}")
    upcoming_earnings_for_holdings = None

app     = Flask(__name__, template_folder="templates")
bot     = TelegramBot()
journal = Journal()
pt      = PositionTracker()


# ── TELEGRAM WEBHOOK ──────────────────────────────────────────────────────

@app.route("/api/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    msg  = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    if not text:
        return jsonify({"ok": True})

    cmd     = parse_command(text)
    command = cmd["command"]

    # ── Trade commands (BUY / SELL / SKIP / HOLD) ────────────────────────
    if command in ("BUY", "SELL", "SKIP", "HOLD"):
        recent    = journal.recent(1)
        ranked    = _load_ranked_opportunities()
        # BUY 1 / BUY 2 — pick by rank from latest briefing
        amount = cmd.get("amount")
        ticker = cmd.get("ticker")
        if amount and not ticker and ranked:
            idx    = int(amount) - 1
            if 0 <= idx < len(ranked):
                opp    = ranked[idx]
                ticker = opp.get("ticker") if isinstance(opp, dict) else opp.ticker
        ticker     = ticker or (recent[0]["recommended_ticker"] if recent else None)
        confidence = recent[0]["confidence"] if recent else 50

        decision_id = journal.attach_human_reply(command, cmd.get("reason") or "(no reason given)")
        # Bust status cache so dashboard reflects the new reply immediately
        _rcache_set("sc:api_status", None, ttl=1)

        alpaca_result = {"executed": False}
        if command in ("BUY", "SELL") and ticker:
            alpaca_result = alpaca_execute(command, ticker, confidence or 50)

        alpaca_line = ""
        if alpaca_result.get("executed"):
            alpaca_line = f"\n🏦 Alpaca paper order: {command} {ticker}"
        elif not os.environ.get("ALPACA_API_KEY"):
            alpaca_line = "\n(Add ALPACA_API_KEY to auto-execute paper orders)"

        bought_reminder = ""
        if command == "BUY" and ticker:
            bought_reminder = (f"\n\n💡 If you spent real money, log it:\n"
                               f"<code>BOUGHT {ticker} [dollar amount]</code>\n"
                               f"Example: <code>BOUGHT {ticker} 500</code>")
        reply = (f"✅ Logged: {command} {ticker or ''}".rstrip() +
                 (f"\nReason: {cmd['reason']}" if cmd.get("reason") else "") +
                 alpaca_line +
                 "\n📊 Paper mode active" +
                 bought_reminder)
        bot.send_message(reply)

    # ── STATUS ────────────────────────────────────────────────────────────
    elif command == "STATUS":
        recent = journal.recent(1)
        if recent:
            r = recent[0]
            bot.send_message(
                f"📋 Last signal ({r['date']}): "
                f"<b>{r['recommended_action']} {r['recommended_ticker']}</b> "
                f"@ {r['confidence']}% confidence\n"
                f"Your call: {r['human_command'] or '— (no reply yet)'}"
            )
        else:
            bot.send_message("No decisions logged yet. Run main_engine.py to generate today's briefing.")

    # ── WHY ───────────────────────────────────────────────────────────────
    elif command == "WHY":
        recent = journal.recent(1)
        if recent and recent[0].get("why_trace"):
            raw_trace = recent[0]["why_trace"]
            try:
                trace = json.loads(raw_trace) if isinstance(raw_trace, str) else raw_trace
                if not isinstance(trace, list):
                    trace = str(raw_trace).split(" | ")
            except Exception:
                trace = str(raw_trace).split(" | ")
            bot.send_message("🧠 <b>Reasoning:</b>\n" + "\n".join(f"• {t}" for t in trace if t))
        else:
            bot.send_message("No reasoning trace available.")

    # ── PERF ──────────────────────────────────────────────────────────────
    elif command == "PERF":
        if PaperPortfolio is None:
            bot.send_message("⚠️ Performance tracker unavailable.")
            return jsonify({"ok": True})
        pp    = PaperPortfolio()
        perf  = pp.compute()
        alpaca = portfolio_summary() if portfolio_summary else {}
        lines = ["📈 <b>Performance Summary</b>"]
        if perf.get("portfolio_value") is not None:
            lines += [
                f"Paper P&L: <b>{perf['portfolio_return_pct']:+.1f}%</b>",
                f"SPY ghost: {perf['spy_return_pct']:+.1f}%",
                f"Alpha: {perf['alpha_pct']:+.2f}%  ({perf['n_trades']} trades, {perf['days_running']}d)",
            ]
        else:
            lines.append("No completed trades yet — reply BUY to a briefing first.")
        if alpaca.get("equity"):
            lines += ["",
                      f"🏦 Alpaca account: ${alpaca['equity']:,.0f}",
                      f"Daily P&L: {alpaca['daily_pnl_pct']:+.2f}%"]
        # Append VaR / macro risk block
        try:
            holdings = pt.get_holdings()
            var_data = portfolio_var(holdings) if holdings else {}
            macro    = macro_snapshot()
            risk_block = format_risk_block(var_data, macro)
            if risk_block:
                lines += ["", risk_block]
        except Exception:
            pass
        bot.send_message("\n".join(lines))

    # ── RISK ──────────────────────────────────────────────────────────────
    elif command == "RISK":
        try:
            holdings = pt.get_holdings()
            var_data = portfolio_var(holdings) if holdings else {}
            macro    = macro_snapshot()
            msg = format_risk_block(var_data, macro)
            bot.send_message(msg if msg else "No holdings to analyze. Log positions first with BOUGHT.")
        except Exception as e:
            bot.send_message(f"Risk calculation failed: {e}")

    # ── REPORT ────────────────────────────────────────────────────────────
    elif command == "REPORT":
        try:
            from engine.report_generator import generate_html_report
            path  = generate_html_report()
            fname = os.path.basename(path)
            bot.send_message(f"📄 Report generated.\nView: /reports/{fname}")
        except Exception as e:
            bot.send_message(f"Report generation failed: {e}")

    # ── CRYPTO ────────────────────────────────────────────────────────────
    elif command == "CRYPTO":
        try:
            from feeders.crypto_feeder import get_crypto_signals
            sigs = get_crypto_signals()
            bot.send_message(format_crypto_briefing(sigs))
        except Exception as e:
            bot.send_message(f"⚠️ Crypto data unavailable: {e}")

    # ── GOLD ──────────────────────────────────────────────────────────────
    elif command == "GOLD":
        try:
            from feeders.crypto_feeder import get_crypto_signals
            sigs  = get_crypto_signals()
            macro = {t: v for t, v in sigs.items() if v.get("type") == "macro"}
            bot.send_message(format_crypto_briefing(macro) if macro
                             else "⚠️ Macro data unavailable.")
        except Exception as e:
            bot.send_message(f"⚠️ Macro data unavailable: {e}")

    # ── PORTFOLIO ─────────────────────────────────────────────────────────
    elif command == "PORTFOLIO":
        summary = pt.portfolio_summary()
        bot.send_message(format_portfolio(summary))

    # ── BALANCE 12500 ────────────────────────────────────────────────────
    elif command == "BALANCE":
        amount = cmd.get("amount")
        if amount and amount > 0:
            pt.set_balance(amount)
            bot.send_message(
                f"✅ Balance set to <b>${amount:,.0f}</b>\n"
                f"To make this permanent across restarts, set DEFAULT_BALANCE={int(amount)} "
                f"in your Railway environment variables.\n"
                f"Reply <code>PORTFOLIO</code> to see your holdings."
            )
        else:
            current = pt.get_balance()
            if current:
                bot.send_message(f"Current balance: ${current:,.0f}\nUpdate: <code>BALANCE 15000</code>")
            else:
                bot.send_message("Set your balance: <code>BALANCE 12500</code>")

    # ── BOUGHT XLE 5 47.50 ───────────────────────────────────────────────
    elif command == "BOUGHT":
        parsed = PositionTracker.parse_bought_command(cmd.get("tokens", []))
        if parsed:
            # Dollar-only buy: auto-fetch current price so P&L can be tracked
            if parsed.get("dollar_value") and not parsed.get("avg_cost"):
                try:
                    prices = pt._fetch_prices([parsed["ticker"]])
                    if prices.get(parsed["ticker"]):
                        p = round(prices[parsed["ticker"]], 4)
                        parsed["avg_cost"] = p
                        parsed["shares"]   = round(parsed["dollar_value"] / p, 4)
                except Exception:
                    pass
            pt.add_position(
                ticker=parsed["ticker"],
                shares=parsed.get("shares"),
                avg_cost=parsed.get("avg_cost"),
                dollar_value=parsed.get("dollar_value"),
            )
            dollar = parsed.get("dollar_value") or 0
            price_note = (f"  ({parsed['shares']:.4f} shares @ ${parsed['avg_cost']:.2f})"
                          if parsed.get("shares") and parsed.get("avg_cost") else "")
            bot.send_message(
                f"✅ Position logged: <b>{parsed['ticker']}</b>\n"
                f"Amount: ${dollar:,.0f}{price_note}\n"
                f"Reply <code>PORTFOLIO</code> to see your full holdings."
            )
        else:
            bot.send_message(
                "Format: <code>BOUGHT XLE 5 47.50</code> (5 shares at $47.50)\n"
                "Or: <code>BOUGHT XLE 500</code> ($500 position)"
            )

    # ── SOLD XLE ─────────────────────────────────────────────────────────
    elif command == "SOLD":
        ticker = cmd.get("ticker")
        if not ticker and cmd.get("tokens") and len(cmd["tokens"]) > 1:
            ticker = cmd["tokens"][1].upper()
        if ticker:
            pt.remove_position(ticker)
            bot.send_message(f"✅ <b>{ticker}</b> removed from holdings.\nReply <code>PORTFOLIO</code> to see updated holdings.")
        else:
            bot.send_message("Format: <code>SOLD XLE</code>")

    # ── EXPLAIN XLF ──────────────────────────────────────────────────────
    elif command == "EXPLAIN":
        ticker = cmd.get("ticker")
        if not ticker and cmd.get("tokens") and len(cmd.get("tokens", [])) > 1:
            ticker = cmd["tokens"][1].upper()
        bot.send_message(format_explain(ticker or "?"))

    # ── HOW MUCH XLE ─────────────────────────────────────────────────────
    elif command == "HOW_MUCH":
        ticker  = cmd.get("ticker")
        balance = pt.get_balance()
        if not balance:
            bot.send_message("Set your balance first: <code>BALANCE 12500</code>")
        elif ticker:
            # Try to get conviction from latest ranked opportunities
            conviction = "MEDIUM"
            for opp in (_load_ranked_opportunities() or []):
                t = opp.get("ticker") if isinstance(opp, dict) else opp.ticker
                if t == ticker.upper():
                    conviction = opp.get("conviction") if isinstance(opp, dict) else opp.conviction
                    break
            bot.send_message(format_sizing_guide(ticker, balance, conviction))
        else:
            bot.send_message("Format: <code>HOW MUCH XLE</code>")

    # ── ALPHA — individual stock picks ────────────────────────────────
    elif command == "ALPHA":
        b     = _load_latest_briefing()
        picks = b.get("equity_alpha_picks", [])
        if picks:
            from engine.equity_alpha import format_equity_alpha_telegram
            bot.send_message(format_equity_alpha_telegram(picks, n=5))
        else:
            bot.send_message(
                "📊 <b>Equity Alpha</b>\n\nNo stock picks yet — they're generated with each daily briefing.\n"
                "Run the daily briefing or wait for the next scheduled run."
            )

    # ── Natural language question (routed through Gemini) ────────────────
    elif command == "QUESTION":
        if llm_ask is None or load_market_context_from_disk is None:
            bot.send_message(answer_question(text, _load_latest_briefing()))
        else:
            ctx = load_market_context_from_disk()
            summary = pt.portfolio_summary()
            if summary.get("balance"):
                ctx["portfolio_summary"] = summary
                ctx["balance"] = summary["balance"]
            reply = llm_ask(text, ctx)
            bot.send_message(reply, parse_mode="")

    else:
        bot.send_message(
            "Commands:\n"
            "<code>BUY A</code> <code>BUY XLF</code> <code>SELL</code> <code>SKIP</code>\n"
            "<code>STATUS</code> <code>WHY</code> <code>PERF</code> <code>RISK</code>\n"
            "<code>CRYPTO</code> <code>GOLD</code> <code>ALPHA</code> <code>PORTFOLIO</code> <code>REPORT</code>\n"
            "<code>BALANCE 12500</code> <code>BOUGHT XLE 5 47.50</code> <code>SOLD XLE</code>\n"
            "<code>EXPLAIN XLF</code> <code>HOW MUCH XLF</code>\n"
            "Or ask any question in plain English."
        )

    return jsonify({"ok": True})


# ── DASHBOARD ─────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def dashboard():
    try:
        portfolio = pt.portfolio_summary(current_prices={})
    except Exception:
        portfolio = {}
    resp = app.make_response(render_template("dashboard.html", portfolio=portfolio))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/reports/<path:filename>")
def serve_report(filename):
    report_dir = os.path.join(os.path.dirname(__file__), "data", "reports")
    return send_from_directory(report_dir, filename)


# ── DASHBOARD API ─────────────────────────────────────────────────────────

@app.route("/api/status", methods=["GET"])
def api_status():
    # Return Redis-cached response if fresh (prevents cold-start timeout from
    # yfinance/NewsFeeder calls). Pass ?bust=1 to force a refresh.
    bust = request.args.get("bust")
    if not bust:
        cached = _rcache_get("sc:api_status")
        if cached:
            return jsonify(cached)

    # Prefer Redis briefing (always fresh on Railway) over local JSON file
    briefing = _load_latest_briefing()

    rl_path = os.environ.get("RL_SIGNAL_JSON",
                             os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rl_signal.json"))
    rl = {}
    if os.path.exists(rl_path):
        try:
            with open(rl_path) as f:
                rl = json.load(f)
        except Exception:
            pass

    # Top ranked opportunity from last briefing (what was actually recommended)
    ranked = briefing.get("ranked_opportunities") or []
    top = ranked[0] if ranked else {}
    target = (top.get("ticker") if isinstance(top, dict) else None) or briefing.get("ticker") or rl.get("target", "XLK")

    # Live VIX — fast single-ticker fetch (~1s), overrides stale file value
    live_vix = briefing.get("vix") or rl.get("vix")
    try:
        import yfinance as _yf
        _vd = _yf.download("^VIX", period="2d", progress=False, auto_adjust=True)
        _vc = _vd["Close"] if hasattr(_vd.columns, "get_level_values") else _vd
        if hasattr(_vc, "columns"):
            _vc = _vc.iloc[:, 0]
        live_vix = round(float(_vc.dropna().iloc[-1]), 1)
    except Exception:
        pass

    # Use news + FOMC from the Redis briefing (fresh from last GitHub Actions run).
    # Do NOT re-run NewsFeeder / FOMC on each status poll — those are slow network calls
    # (FinBERT inference, HuggingFace) that cause Railway timeouts. The briefing data
    # is already the most recent scheduled run; the dashboard polls every 3 min.
    news_sentiment  = briefing.get("news_sentiment")
    top_headline    = briefing.get("news_headline")
    fomc_live       = briefing.get("fomc_live") or {"active": False}

    freshness = briefing.get("freshness") or {
        "rl_source":     rl.get("_source", "STUB"),
        "generated_utc": rl.get("_generated_utc", "—"),
    }

    result = {
        "action":         briefing.get("action") or rl.get("action"),
        "ticker":         target,
        "confidence":     briefing.get("confidence") or rl.get("confidence"),
        "regime":         briefing.get("regime") or rl.get("regime"),
        "vix":            live_vix,
        "rsi":            briefing.get("rsi") or rl.get("rsi"),
        "rel_strength":   briefing.get("rel_strength") or rl.get("rel_strength"),
        "votes":          rl.get("votes"),
        "abstain_reason": briefing.get("abstain_reason"),
        "news_sentiment": news_sentiment,
        "top_headline":   top_headline,
        "repo_detail":    briefing.get("repo_detail") or {},
        "fomc_live":      fomc_live,
        "freshness":      freshness,
        "herd_market_warning":          rl.get("herd_market_warning", False),
        "dispersion_percentile":        rl.get("dispersion_percentile"),
        "cross_sectional_dispersion":   rl.get("cross_sectional_dispersion"),
        "social_sentiment":             briefing.get("social_sentiment") or {},
        "macro":                        briefing.get("macro") or {},
        "options_signals":              briefing.get("options_signals") or {},
        "ranked_opportunities":         briefing.get("ranked_opportunities") or [],
        "sell_alerts":                  briefing.get("sell_alerts") or "",
        "earnings_warning":             briefing.get("earnings_warning") or "",
        "why_trace":                    briefing.get("why_trace") or [],
    }
    _rcache_set("sc:api_status", result, ttl=CACHE_TTL)
    return jsonify(result)


@app.route("/api/sectors", methods=["GET"])
def api_sectors():
    bust = request.args.get("bust")
    if not bust:
        cached = _rcache_get("sc:api_sectors")
        if cached:
            return jsonify(cached)
    try:
        if sector_technicals is None or algo_composite_signal is None:
            return jsonify({"error": "repo_signals unavailable", "sectors": []})
        tech = sector_technicals()
        algo = algo_composite_signal()
        news_by_sector = {}
        try:
            from feeders.news_feeder import NewsFeeder
            news = NewsFeeder().daily_sector_sentiment(limit=40)
            news_by_sector = news["by_sector"]
        except Exception:
            pass

        SECTOR_NAMES = {
            "XLK": "Technology",       "XLF": "Financials",      "XLV": "Health Care",
            "XLY": "Consumer Discret.", "XLP": "Consumer Staples", "XLE": "Energy",
            "XLI": "Industrials",       "XLB": "Materials",        "XLRE": "Real Estate",
            "XLU": "Utilities",         "XLC": "Communication",
        }
        sectors = []
        for ticker, name in SECTOR_NAMES.items():
            t = tech.get(ticker, {})
            a = algo["by_sector"].get(ticker, {})
            sectors.append({
                "ticker": ticker, "name": name,
                "rsi": t.get("rsi"), "mom": t.get("mom"), "sharpe": t.get("sharpe"),
                "rel": t.get("rel"), "algo_score": a.get("score"),
                "algo_eligible": a.get("eligible"),
                "news_sentiment": news_by_sector.get(ticker),
            })
        result = {"sectors": sectors}
        _rcache_set("sc:api_sectors", result, ttl=CACHE_TTL)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "sectors": []})


@app.route("/api/crypto", methods=["GET"])
def api_crypto():
    try:
        from feeders.crypto_feeder import get_crypto_signals
        return jsonify({"crypto": get_crypto_signals()})
    except Exception as e:
        return jsonify({"error": str(e), "crypto": {}})


@app.route("/api/decisions", methods=["GET"])
def api_decisions():
    try:
        n = int(request.args.get("n", 20))
        return jsonify({"decisions": journal.recent(n)})
    except Exception as e:
        return jsonify({"error": str(e), "decisions": []})


@app.route("/api/performance", methods=["GET"])
def api_performance():
    try:
        pp   = PaperPortfolio()
        perf = pp.compute()
        try:
            from engine.alpaca_executor import get_portfolio_history
            hist = get_portfolio_history(period="1M", timeframe="1D")
            ts   = hist.get("timestamp", [])
            eq   = hist.get("equity", [])
            if ts and eq:
                start    = eq[0] or 1
                dates    = [datetime.datetime.fromtimestamp(t).strftime("%m/%d") for t in ts]
                port_pct = [round((v / start - 1) * 100, 2) for v in eq]
                # Fetch real SPY returns over same window for a meaningful benchmark line
                spy_pct = [0.0] * len(dates)
                try:
                    import yfinance as _yf
                    _start = datetime.datetime.fromtimestamp(ts[0]).strftime("%Y-%m-%d")
                    _end   = (datetime.datetime.fromtimestamp(ts[-1]) +
                               datetime.timedelta(days=3)).strftime("%Y-%m-%d")
                    _raw   = _yf.download("SPY", start=_start, end=_end,
                                          progress=False, auto_adjust=True)
                    _c = _raw["Close"] if hasattr(_raw.columns, "get_level_values") else _raw
                    if hasattr(_c, "columns"):
                        _c = _c.iloc[:, 0]
                    _vals = _c.dropna()
                    if len(_vals) > 0:
                        _base    = float(_vals.iloc[0])
                        _spy_raw = [round((float(v) / _base - 1) * 100, 2) for v in _vals]
                        spy_pct  = _spy_raw[:len(dates)]
                        while len(spy_pct) < len(dates):
                            spy_pct.append(spy_pct[-1])
                except Exception:
                    pass
                perf["history"] = {"dates": dates, "portfolio": port_pct, "spy": spy_pct}
        except Exception:
            perf["history"] = None
        return jsonify(perf)
    except Exception as e:
        return jsonify({"error": str(e), "alpha_pct": 0.0})


@app.route("/api/portfolio", methods=["GET"])
def api_portfolio():
    """Real holdings + P&L for the dashboard."""
    try:
        summary = pt.portfolio_summary()
        return jsonify(summary)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/alpaca", methods=["GET"])
def api_alpaca():
    try:
        return jsonify(portfolio_summary())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/risk", methods=["GET"])
def api_risk():
    """VaR, CVaR, macro snapshot for the dashboard risk panel."""
    try:
        if portfolio_var is None or macro_snapshot is None:
            return jsonify({"error": "risk_metrics unavailable", "var": {}, "macro": {}})
        holdings = pt.get_holdings()
        var_data = portfolio_var(holdings) if holdings else {}
        macro    = macro_snapshot()
        return jsonify({"var": var_data, "macro": macro})
    except Exception as e:
        return jsonify({"error": str(e), "var": {}, "macro": {}})


@app.route("/api/rotation", methods=["GET"])
def api_rotation():
    """4-week sector rotation heatmap data."""
    try:
        matrix = sector_rotation_matrix(weeks=4)
        sectors = ["XLK","XLF","XLE","XLV","XLY","XLP","XLI","XLB","XLRE","XLU","XLC"]
        names   = {"XLK":"Tech","XLF":"Financials","XLE":"Energy","XLV":"Health",
                   "XLY":"Cons.Disc","XLP":"Staples","XLI":"Industrials","XLB":"Materials",
                   "XLRE":"Real Est.","XLU":"Utilities","XLC":"Comm."}
        rows = [{"ticker": t, "name": names.get(t, t), "weeks": matrix.get(t, [])}
                for t in sectors if t in matrix]
        return jsonify({"rotation": rows})
    except Exception as e:
        return jsonify({"error": str(e), "rotation": []})


@app.route("/api/equity-alpha", methods=["GET"])
def api_equity_alpha():
    """Top individual stock picks from the cross-sectional factor model.

    Never triggers live compute — that runs in main_engine.py (GitHub Actions).
    Reads from Redis → last_briefing.json → empty. This keeps the endpoint
    fast (<200ms) and prevents Railway timeout from an 80-ticker yfinance fetch.
    """
    bust = request.args.get("bust")
    if not bust:
        cached = _rcache_get("sc:equity_alpha")
        if cached:
            picks = cached.get("picks", cached) if isinstance(cached, dict) else cached
            return jsonify({"picks": picks})
    b     = _load_latest_briefing()
    picks = b.get("equity_alpha_picks", [])
    return jsonify({"picks": picks})


@app.route("/api/macro", methods=["GET"])
def api_macro():
    """Yield curve, dollar index, and VIX term structure."""
    try:
        if macro_snapshot is None:
            return jsonify({"error": "risk_metrics unavailable"})
        return jsonify(macro_snapshot())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/options", methods=["GET"])
def api_options():
    """Put/call ratio for SPY and QQQ."""
    try:
        from feeders.options_feeder import get_options_sentiment
        return jsonify(get_options_sentiment(["SPY", "QQQ"]))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/cron/briefing", methods=["GET"])
def cron_briefing():
    """Called by cron-job.org to trigger the daily briefing workflow on GitHub Actions."""
    import requests as _req
    secret = os.environ.get("CRON_SECRET", "")
    if secret and request.args.get("key") != secret:
        return jsonify({"error": "unauthorized"}), 401
    token = os.environ.get("GITHUB_PAT", "")
    if not token:
        return jsonify({"error": "GITHUB_PAT not set"}), 500
    url = "https://api.github.com/repos/cameroncc333/sector-command-live/actions/workflows/daily-signals.yml/dispatches"
    r = _req.post(url, json={"ref": "main"},
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/vnd.github+json"}, timeout=10)
    return jsonify({"status": r.status_code})


@app.route("/cron/alerts", methods=["GET"])
def cron_alerts():
    """Called by cron-job.org to trigger the 30-min event alert workflow."""
    import requests as _req
    secret = os.environ.get("CRON_SECRET", "")
    if secret and request.args.get("key") != secret:
        return jsonify({"error": "unauthorized"}), 401
    token = os.environ.get("GITHUB_PAT", "")
    if not token:
        return jsonify({"error": "GITHUB_PAT not set"}), 500
    url = "https://api.github.com/repos/cameroncc333/sector-command-live/actions/workflows/event-alerts.yml/dispatches"
    r = _req.post(url, json={"ref": "main"},
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/vnd.github+json"}, timeout=10)
    return jsonify({"status": r.status_code})


@app.route("/debug-balance", methods=["GET"])
def debug_balance():
    env_bal = os.environ.get("DEFAULT_BALANCE", "NOT SET")
    db_bal = pt.get_balance()
    try:
        summary = pt.portfolio_summary(current_prices={})
        summary_err = None
    except Exception as e:
        summary = None
        summary_err = str(e)
    return jsonify({
        "DEFAULT_BALANCE_env": env_bal,
        "get_balance_result": db_bal,
        "db_path": pt.db_path,
        "portfolio_summary": summary,
        "portfolio_summary_error": summary_err,
    })


@app.route("/health", methods=["GET"])
def health():
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    return jsonify({
        "status": "ok",
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "gemini": "key_set" if gemini_key else "no_key",
        "telegram": "key_set" if os.environ.get("TELEGRAM_TOKEN") else "no_key",
    })


@app.route("/test-gemini", methods=["GET"])
def test_gemini():
    """List available Gemini models for this API key, then test the configured one."""
    import requests as _req
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return jsonify({"error": "no key"})
    try:
        # List all available models
        list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        lr = _req.get(list_url, timeout=15)
        models_data = lr.json()
        available = [m["name"] for m in models_data.get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
        # Test each model to find one that works
        results = {}
        test_payload = {"contents": [{"parts": [{"text": "Say hi."}]}]}
        for m in available[:8]:  # test first 8 to avoid timeout
            name = m.replace("models/", "")
            test_url = f"https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent?key={key}"
            try:
                tr = _req.post(test_url, json=test_payload, timeout=8)
                data = tr.json()
                if tr.status_code == 200:
                    reply = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    results[name] = f"✅ WORKS: {reply[:40]}"
                else:
                    results[name] = f"❌ {tr.status_code}: {data.get('error',{}).get('message','?')[:60]}"
            except Exception as ex:
                results[name] = f"⚠️ timeout/error"
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)})


# ── internal helpers ──────────────────────────────────────────────────────

_BRIEFING_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "last_briefing.json")


def _load_ranked_opportunities() -> list:
    """Load ranked opportunities — checks Redis first, then JSON file."""
    return _load_latest_briefing().get("ranked_opportunities", [])


def _load_latest_briefing() -> dict:
    # Redis first (freshest, always consistent)
    cached = _rcache_get("sc:last_briefing")
    if cached:
        return cached
    # JSON fallback (written by main_engine.py on each run)
    try:
        if os.path.exists(_BRIEFING_PATH):
            with open(_BRIEFING_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
