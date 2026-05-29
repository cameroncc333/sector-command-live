# Sector Command Live

**A production-deployed quantitative trading system** that integrates deep reinforcement learning, real-time NLP sentiment analysis, multi-asset portfolio optimization, and conversational AI into a single live decision engine — running autonomously in production, texting daily trade briefings, and managing real paper positions through a live web dashboard.

Built by [Cameron Camarotti](https://github.com/cameroncc333)

---

## Overview

Most quantitative finance projects are backtests — historical simulations that look good in a notebook but have never touched live data. This is different.

Sector Command runs **four times every trading day** via automated CI/CD, pulls live market data, runs a trained reinforcement learning ensemble, applies institutional-grade risk management, and delivers a ranked briefing to both a **live web dashboard** and a **Telegram interface** where positions can be executed, tracked, and explained in plain English via an integrated large language model. Every decision is logged to a persistent audit trail with full reasoning attached.

The system was built to answer a specific question: *can a systematic, explainable quantitative process — not a black box — outperform a passive SPY allocation while managing real downside risk?* After 30 days of paper trading, paper mode is disabled and the system trades real capital.

---

## Live Web Dashboard

The system includes a full web dashboard deployed on Railway at the same URL as the Telegram webhook. No separate setup required.

**Five tabs:**

| Tab | What it shows |
|---|---|
| 📡 Signal | Today's RL recommendation, confidence meter, agent votes, VIX term structure, PCR options sentiment, StockTwits social, FOMC status, WHY reasoning trace |
| 💼 Portfolio | Real holdings with live P&L, equity curve, VaR/CVaR risk metrics, Alpaca paper account, cash position |
| 📊 Alpha | Individual stock picks (80-stock factor model), sector signal table, 4-week rotation heatmap, ranked opportunities |
| 📓 Journal | Full decision log with expandable WHY traces, stats (total/buys/skips/abstains), load-more |
| 🌐 Macro | Yield curve, DXY dollar index, VIX term structure, PCR, earnings calendar |

**Quick links in dashboard header:** Alpaca paper account · Telegram bot · 2-minute auto-refresh with timestamp

---

## Architecture

The design separates the brain (what to buy) from the governance layer (whether to act) from the sizing engine (how much to risk). These are three distinct concerns that most retail systems collapse into one — and that collapse is where edge cases become losses.

```
RL Ensemble ─────────────────────────────────────────────┐
  PPO / A2C / SAC agents                                  │
  Walk-forward validated on 11 SPDR sector ETFs           │
  Abstain actions: SPY (no conviction) / BIL (crisis)     │
                                                          ▼
News Sentiment ────────────────────────────────► Decision Engine (GRPO-normalized)
  FinBERT NLP across 7 live RSS feeds                     │
  Conviction modifier — cannot change the RL ticker       │  Governance layer:
  Lexicon fallback when transformer unavailable            │  • ≥ 2/3 agent agreement required
                                                          │  • VIX > 35 → force BIL (cash)
Options PCR ───────────────────────────────────────────► │  • Max 30% per position
  Put/Call ratio via yfinance (SPY + QQQ)                 │  • Paper mode until validated
  Contrarian: FEARFUL=+4, COMPLACENT=−3                   │
                                                          │
VIX Term Structure ────────────────────────────────────► │
  VIX/VIX3M ratio → BACKWARDATION/CONTANGO               │
  Near-term event risk via VIX9D/VIX ratio                │
                                                          │
Cross-Repo Corroboration ──────────────────────────────► │
  4 upstream repos re-run live signals                    │
  Equity technicals, algo composite score,                │
  Fed policy context, FOMC sentiment                      ▼
                                                  Multi-Asset Ranker
FOMC Live Feeder ──────────────────────────────►  Sectors + BTC/ETH + GLD/TLT/QQQ
  Live Fed statements from federalreserve.gov             │
  Scored with FinBERT-FOMC                                ▼
                                                  Risk Stack
Macro Overlay ─────────────────────────────────►  VaR(95%) / CVaR(95%) per position
  Yield curve (10yr − 13w T-bill spread)          Kelly criterion sizing
  DXY dollar index                                Trailing stop / RSI / momentum / stale-loss
  Sector rotation heatmap (4-week rel. strength)  Earnings proximity warnings
                                                          │
Equity Alpha (80-stock factor model) ──────────►         │
  Value / Quality / Momentum / Technical / Low-Vol        │
  Sector-neutral cross-sectional scoring                  │
  Regime-adaptive weights (stressed → low-vol)            │
                                                          ▼
                                                  Telegram + Web Dashboard
                                                  Option A / B / C ranked picks
                                                  Dollar amounts + Kelly fractions
                                                  Sell alerts + earnings warnings
                                                  Gemini answers any question
                                                          │
                                                          ▼
                                                  SQLite + Redis Audit Log
                                                  Every decision, every signal, full reasoning
                                                  Redis = live API cache (24h TTL briefings)
```

---

## GRPO-Inspired Confidence Normalization

Derived from DeepSeek-R1 (arXiv 2501.12948). All conviction modifiers — news sentiment, PCR, RSI, VIX term structure, cross-repo corroboration — are collected as a group and normalized so no single signal can dominate the final confidence score by more than ±20 points.

Before: signals could stack and swing confidence ±40+ points (reward hacking vulnerability).  
After: group-relative normalization caps total positive and total negative contributions independently. One extreme signal cannot override all other information.

---

## What makes it non-trivial

**Reinforcement learning with proper validation.** The PPO, A2C, and SAC agents were trained using walk-forward cross-validation — the same methodology used in institutional backtesting to prevent lookahead bias. Each agent produces a sector allocation; the ensemble requires ≥ 2/3 agreement before acting. Disagreement produces an explicit abstain to SPY rather than a forced trade.

**News sentiment that can't override the model.** FinBERT NLP runs across seven live financial RSS feeds and scores sentiment per sector. It can raise or lower confidence, or trigger an abstain — but it cannot change which sector the RL model selected. This constraint is architectural, not advisory.

**Options PCR feeds the decision engine.** Put/call ratio from yfinance options chains (free, no key) is computed for SPY and QQQ on each run. High PCR = fear = contrarian buy signal (+4 confidence). Low PCR = complacency = caution (−3). This signal flows into the GRPO modifier group alongside news, RSI, and VIX term structure.

**VIX term structure as regime signal.** The VIX/VIX3M ratio distinguishes front-loaded fear spikes (backwardation, −8 confidence) from calm markets (steep contango, +4). The VIX9D/VIX near-term ratio detects event risk (FOMC, CPI). Both feed the decision engine as modifiers, not just context.

**80-stock cross-sectional factor model.** Individual stock picks are generated using the same framework as institutional alpha funds (BlackRock BDVEX, Schroders): Value / Quality / Momentum / Technical / Low-Vol factors, scored cross-sectionally within each sector to read pure idiosyncratic alpha. Factor weights shift dynamically with macro regime: stressed markets tilt toward Low-Vol + Quality; calm/bull markets tilt toward Momentum.

**Kelly criterion from live signals.** With no historical win/loss database yet, Kelly fractions are derived analytically: p is bounded RL confidence, b is estimated payoff from momentum magnitude and Sharpe ratio. Half-Kelly is applied. Position fractions of 5–15% are mathematically consistent with assigned conviction tiers.

**Congressional trading is walled off by design.** STOCK Act disclosures are logged as research context and are explicitly excluded from the decision path in code — not by convention. The ~45-day legal reporting delay makes them economically useless as a signal.

**Seven upstream repositories feed one decision.** `repo_signals.py` re-runs the live signal computation from each repo — equity technicals, factor composite scores, Fed policy sensitivity, FOMC mood proxy — and produces a cross-repo agreement count that modifies conviction.

---

## Live data sources (all live on each run — nothing static)

| Source | What it produces | When fetched |
|---|---|---|
| yfinance | All sector ETF prices, VIX, VIX3M, VIX9D, yield curve (10yr/13w T-bill), DXY, BTC/ETH/GLD/TLT/QQQ | Every run + live in dashboard API |
| yfinance options chains | Put/Call ratio for SPY and QQQ | Every main engine run |
| 7 financial RSS feeds | Per-sector news sentiment via FinBERT or lexicon fallback | Every run |
| StockTwits API | Social sentiment score for top holdings of RL sector (free, no key) | Every run |
| federalreserve.gov | Live FOMC statements scored with FinBERT-FOMC | ±3 days of meeting dates |
| House/Senate STOCK Act S3 | Congressional trade disclosures (research-only context label) | Every run |
| Alpaca paper trading API | Real paper order execution, positions, portfolio history | On BUY/SELL command + dashboard |
| Upstash Redis | API cache (24h briefing TTL, 4h equity alpha TTL) | Dashboard polls every 2 min |
| SQLite | Persistent journal: decisions, signals, P&L, audit trail | Every decision + Telegram reply |

**RL signal note:** The PPO/A2C/SAC ensemble signal (`rl_signal.json`) is regenerated by GitHub Actions 4×/day. Between runs it is intentionally static — the `freshness.rl_source` field in every briefing shows `LIVE` (real model output) or `STUB` (fallback). All other data (VIX, news, PCR, macro, sectors) is fetched live on every request.

---

## System behavior

The system runs on a schedule — no human intervention required once deployed.

- **09:00 EDT** — pre-open briefing: full signal refresh, ranked picks with dollar amounts and Kelly fractions
- **12:00 EDT** — midday update: intraday regime check, position monitoring
- **15:30 EDT** — pre-close: sell signal sweep, earnings proximity check
- **16:30 EDT** — EOD summary: P&L update, ghost alpha vs SPY
- **Every 30 minutes** — event watcher: VIX spike (>20% intraday), regime flip, any position down >5%, earnings within 2 days
- **Every Sunday** — weekly Telegram summary with P&L vs SPY, top alpha picks, sectors to watch

All briefings are delivered to Telegram with full reasoning. Positions can be executed, logged, and queried via text commands. Any question about the market, a ticker, or the system's reasoning is answered by Gemini with the full current market state injected as context.

---

## Telegram commands

```
BUY A / BUY XLF          Log decision + optional Alpaca paper order
BUY 1 / BUY 2            Pick by rank from today's briefing
SELL / SKIP / HOLD        Log decision
STATUS                    Last signal summary
WHY                       Full GRPO reasoning trace (every modifier shown)
PERF                      Paper P&L vs SPY + alpha + win rate + info ratio
RISK                      VaR/CVaR per position, yield curve, DXY
ALPHA                     Top individual stock picks with factor scores
CRYPTO / GOLD             Live crypto + macro hedge signals
PORTFOLIO                 Real holdings with live P&L
BALANCE 12500             Set investable balance
BOUGHT XLE 5 47.50        Log 5 shares at $47.50
BOUGHT XLE 500            Log $500 position (shares auto-calculated)
SOLD XLE                  Remove position
EXPLAIN XLF               What is this sector?
HOW MUCH XLF              Sizing guidance
REPORT                    Generate HTML research report
```
Or ask any question in plain English — Gemini answers with today's live market state.

---

## Deployment

| Component | Platform |
|---|---|
| Webhook + dashboard (Flask) | Railway (always-on, gunicorn) |
| RL inference + daily signals | GitHub Actions (4×/day cron) |
| Event alerts | GitHub Actions (every 30 min) |
| Weekly summary | GitHub Actions (Sunday 4 PM ET) |
| API cache | Upstash Redis (serverless) |
| Data persistence | SQLite (Railway volume) |
| Paper execution | Alpaca Markets paper API |

**Required environment variables:**
```
TELEGRAM_TOKEN          Telegram bot token from @BotFather
TELEGRAM_CHAT_ID        Your Telegram chat ID
UPSTASH_REDIS_REST_URL  Upstash Redis REST endpoint
UPSTASH_REDIS_REST_TOKEN Upstash Redis auth token
GITHUB_PAT              GitHub personal access token (triggers Actions)
ALPACA_API_KEY          Alpaca paper trading key (optional)
ALPACA_SECRET_KEY       Alpaca paper trading secret (optional)
DEFAULT_BALANCE         Starting portfolio balance (e.g. 12500)
PAPER_MODE              1 = paper mode (default), 0 = live after 30d validation
GEMINI_API_KEY          Google Gemini API key (for natural language Q&A)
NEWSAPI_KEY             NewsAPI key (optional — RSS fallback works without it)
QUIVER_API_KEY          Quiver Quant key (optional — STOCK Act research context only)
```

---

## The seven-repository research program

This system is the final layer of a research program built bottom-up:

| Repository | What it proved |
|---|---|
| `aas-pricing-model` | A multi-variable calculus cost function (8 inputs, partial-derivative optimization) that prices real service contracts for an active business |
| `fed-rate-sector-analysis` | A statistical event study measuring sector return sensitivity to FOMC rate decisions across 30/60/90-day windows |
| `equity-sector-analyzer` | A 2,000-line live dashboard computing 30+ risk metrics including Black-Scholes Greeks and Fama-French 5-factor decomposition |
| `fomc-sentiment-analyzer` | FinBERT NLP applied to 91 FOMC meeting statements; established the NLP methodology reused in this system's news feeder |
| `algo-trading-system` | A rules-based sector rotation backtester: 238 trades, +64.5% total return, 0.30 Sharpe, −23.5% max drawdown — the baseline the RL agents must beat |
| `rl-portfolio-optimizer` | The RL capstone: PPO/A2C/SAC trained 500,000 steps on 11 sector ETFs with regime-adaptive reward shaping and walk-forward validation |
| **`sector-command-live`** (this repo) | **The production layer: live data, live decisions, live execution, live risk management** |

Each repo is independent and does something real on its own. Together they form a complete quantitative research pipeline — from business math through statistical research through NLP through reinforcement learning through live deployment.

---

## Technical stack

Python · stable-baselines3 (PPO/A2C/SAC) · transformers + FinBERT · Flask · Railway · GitHub Actions · SQLite · Upstash Redis · Alpaca Markets API · Google Gemini · yfinance · scipy (Black-Scholes) · pandas · numpy

---

*Runs in paper mode during the initial 30-day validation period. Not financial advice.*
