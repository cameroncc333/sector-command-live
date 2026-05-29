# Sector Command Live

**A production-deployed quantitative trading system** integrating deep reinforcement learning, real-time NLP sentiment analysis, multi-asset portfolio optimization, and conversational AI — running autonomously every trading day, delivering ranked briefings via Telegram, and tracking paper positions on a live web dashboard.

Built by [Cameron Camarotti](https://github.com/cameroncc333)

**Live dashboard:** https://sector-command-live.vercel.app

---

## What It Does

Every trading day at **9AM, 12PM, and 3:30PM EDT**, a GitHub Actions job:

1. Pulls live market data (yfinance — VIX, prices, RSI, momentum)
2. Runs a trained RL ensemble (PPO + A2C + SAC) to pick the highest-conviction sector ETF
3. Applies a governance layer (2/3 agent agreement required to act; otherwise routes to SPY)
4. Scores live news headlines with FinBERT NLP sentiment (15 RSS feeds)
5. Computes macro indicators (yield curve, DXY, VaR, Kelly criterion)
6. Sends a ranked briefing to Telegram with Option A/B/C and sizing guidance
7. Publishes fresh data to the live dashboard
8. Logs the full decision with reasoning trace to a persistent audit trail

You reply `BUY A`, `SKIP`, or ask a question in plain English — Gemini AI answers with full market context.

---

## Live Dashboard

**URL:** https://sector-command-live.vercel.app

Five tabs, all live data:

| Tab | What It Shows |
|---|---|
| 📡 Today's Signal | RL recommendation, confidence, agent votes (PPO/A2C/SAC), VIX regime, options PCR, news sentiment, macro |
| 📈 Opportunities | Ranked A/B/C picks with Kelly sizing, paper P&L vs SPY |
| 💼 My Positions | Real-money holdings logged via Telegram + Alpaca paper account with live broker P&L |
| 📓 Journal | Every briefing logged — VIX, regime, decision, your reply (BUY/SKIP/no reply), full reasoning trace |
| 🌐 (Today) | WHY trace, decision reasoning, freshness indicators |

**Dashboard updates:** Automatically refreshes every 10 minutes. Hit **⟳ Refresh** for instant update. Data freshness shown in footer.

---

## Telegram Commands

Send these to your Telegram bot at any time:

| Command | What It Does |
|---|---|
| `BUY A` / `BUY B` / `BUY C` | Executes the ranked pick on Alpaca paper + logs your decision |
| `BUY XLF` | Buys a specific ticker on Alpaca paper |
| `SKIP` | Logs that you passed on today's signal |
| `HOLD` | Logs hold decision |
| `STATUS` | Shows last briefing date, ticker, confidence |
| `WHY` | Prints the full reasoning trace |
| `ALPHA` | Top stock picks from the factor model |
| `PORTFOLIO` | Shows your real-money holdings and cash position |
| `BALANCE 15000` | Sets your investable balance (drives % sizing in briefings) |
| `BOUGHT XLE 500` | Logs a $500 real-money position in XLE |
| `BOUGHT XLE 10 48.50 my reason` | Logs 10 shares at $48.50 with a note |
| `SOLD XLE` | Removes XLE from your holdings |
| `PERF` | Paper portfolio return vs SPY |
| Any question | "Should I add to crypto?", "What is XLF?" — Gemini AI answers |

**All commands are logged.** BUY/SKIP decisions appear in the Journal tab after the next briefing run.

---

## Architecture

```
RL Ensemble (brain)
  ├─ PPO agent  ─┐
  ├─ A2C agent  ─┼─ majority vote required (≥2/3) ──► Governance Layer
  └─ SAC agent  ─┘                                         │
                                                           ▼
News Sentiment (FinBERT)                          Decision Engine
  15 RSS feeds ──────────────── modifier ──────► ticker + confidence + sizing
  (WSJ, Bloomberg, Reuters, etc)
                                                           │
Macro Layer                                                ▼
  Yield curve (10yr-2yr)                          Telegram Briefing
  DXY dollar index       ──────── context ──────► (Option A/B/C + Kelly %)
  VaR / CVaR / Kelly
                                                           │
Cross-repo Corroboration                                   ▼
  algo-trading-system    ──────── agree/disagree ─► Journal + Redis + Dashboard
  fed-rate-sector-analysis
```

**Key design decisions:**
- **Governance layer**: ≥ 2/3 RL agent agreement required to act. When agents disagree, routes to SPY (broad market). This is intentional discipline, not a bug.
- **News cannot change the ticker** — it only modifies confidence ±10 pts. RL picks the asset; news tells you how much to trust it.
- **Political disclosures (STOCK Act)** are always research-only and never passed into conviction logic.
- **Paper mode** is active for the first 30 days. To go live: set `PAPER_MODE=0` in Vercel env vars.

---

## Data Flow

```
GitHub Actions (3x/day)         Vercel (webhook, always-on)
─────────────────────────       ────────────────────────────
main_engine.py                  api/webhook.js (Node.js)
  → reads Redis balance/holdings   → handles Telegram commands
  → fetches live prices (yfinance) → reads/writes Redis
  → generates briefing.json        → sc:balance (your balance)
  → pushes to Redis (sc:last_briefing)  sc:holdings (positions)
  → sends Telegram briefing        sc:journal_replies (BUY/SKIP)
  → commits briefing.json to repo  → returns live data on GET

GitHub Pages (static)           Redis (Upstash, persistent)
  docs/briefing.json  ──────► dashboard reads both sources
  docs/journal.json   ─────┘  /api/webhook overlaid for live balance
```

---

## Repository Structure

```
main_engine.py              Entry point — runs all 12 signal stages
engine/
  decision.py               GRPO-normalized scoring, governance layer
  performance_tracker.py    Paper P&L vs SPY benchmark
  position_tracker.py       Real-money holdings (SQLite + Redis sync)
  alpaca_executor.py        Alpaca paper trading API
  equity_alpha.py           80-stock cross-sectional factor model
  risk_metrics.py           VaR, CVaR, Kelly, yield curve, DXY
  sell_signals.py           Trailing stop, RSI overbought, momentum flip
  earnings_calendar.py      Sector ETF earnings event alerts
  llm_router.py             Gemini conversational AI context builder
feeders/
  sector_feeder.py          SPDR ETF price/RSI/momentum
  news_feeder.py            15 RSS feeds → FinBERT NLP
  crypto_feeder.py          BTC/ETH + GLD/TLT/QQQ via yfinance
  fomc_live_feeder.py       Fed statement scraping + FinBERT-FOMC
interface/
  telegram_bot.py           Briefing formatter, all Telegram commands
api/
  webhook.js                Vercel Node.js webhook (Telegram + REST API)
docs/
  index.html                Live dashboard (GitHub Pages)
  briefing.json             Latest signal (auto-generated, committed 3x/day)
  journal.json              Decision history (auto-generated)
.github/workflows/
  daily-signals.yml         3x/day signal runs (9AM, 12PM, 3:30PM EDT)
  event-alerts.yml          Earnings/sell-signal watcher (manual trigger)
  weekly-report.yml         Sunday summary report
generate_rl_signal.py       Run locally → commits RL signal to repo
data/
  rl_signal.json            Latest RL ensemble output (committed)
  sector_command.db         Journal + decisions SQLite DB (committed)
```

---

## Daily Routine

**You don't need to do anything to keep the system running.** GitHub Actions fires automatically.

Your daily flow:
1. **9AM** — check Telegram for the morning briefing. Reply `BUY A`, `SKIP`, or ask a question.
2. **12PM** — midday check arrives. Reply or ignore.
3. **3:30PM** — pre-close briefing. Good time to decide if you want to enter before close.
4. **Anytime** — open the dashboard to see live signals, your positions, and paper P&L.
5. **When you make a real trade** — send `BOUGHT XLE 500 my reason` to log it. The dashboard updates on next refresh.

**To update the RL signal** (do this from your MacBook every 1-2 weeks):
```bash
cd ~/Documents/rl-portfolio-optimizer
source rl_env/bin/activate
cd ~/Downloads/sector-command-live-2
python generate_rl_signal.py
git add data/rl_signal.json && git commit -m "update RL signal" && git push
```

---

## Setup / Environment Variables

### GitHub Secrets (for Actions)
| Secret | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Your Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `ALPACA_API_KEY` | Alpaca paper trading key |
| `ALPACA_SECRET_KEY` | Alpaca paper trading secret |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis auth token |
| `GEMINI_API_KEY` | Google Gemini API (conversational AI) |
| `DEFAULT_BALANCE` | Default position sizing balance |

### Vercel Env Vars (for webhook)
Same as above, plus:
| Var | Purpose |
|---|---|
| `GITHUB_PAT` | Fine-grained PAT with Actions:write — triggers dashboard refresh on BALANCE/BOUGHT/SOLD |
| `GITHUB_REPO` | `cameroncc333/sector-command-live` |

### After first deploy
Register your Telegram webhook:
```
curl "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://sector-command-live.vercel.app/api/webhook"
```

---

## Performance Tracking

- **Alpaca paper account** starts at $100,000. Every `BUY` reply auto-executes a market order on Alpaca's paper broker. The dashboard shows live equity, daily P&L, and total return vs the $100k baseline.
- **SPY comparison** (shown at 9AM, 12PM, 3:30PM): SPY's daily return is fetched each run so you can see if today's Alpaca moves beat or trailed the market.
- **Journal replies**: Every BUY/SKIP you send appears next to the matching briefing in the Journal tab after the next briefing run (up to 3-hour delay).

---

## Related Repositories

This is the capstone system. It draws signals from:

| Repo | What It Contributes |
|---|---|
| [rl-portfolio-optimizer](https://github.com/cameroncc333/rl-portfolio-optimizer) | PPO/A2C/SAC trained agents, walk-forward validation |
| [algo-trading-system](https://github.com/cameroncc333/algo-trading-system) | Momentum/mean-reversion cross-repo corroboration signal |
| [fed-rate-sector-analysis](https://github.com/cameroncc333/fed-rate-sector-analysis) | Cross-repo sector rotation corroboration |
| [equity-sector-analyzer](https://github.com/cameroncc333/equity-sector-analyzer) | Factor model for individual stock picks |
| [fomc-sentiment-analyzer](https://github.com/cameroncc333/fomc-sentiment-analyzer) | Fed statement NLP via FinBERT-FOMC |
| [AAS-Pricing-Model](https://github.com/cameroncc333/AAS-Pricing-Model) | Underlying quant research models |

---

*Paper mode active · Auto-transitions to live after 30 days of validated paper performance*
