# CLAUDE CODE HANDOFF — Sector Command Live

Paste this entire file as your first message to Claude Code, and attach `sector-command-live.zip`. It contains everything you need: full context, current state, exact tasks in order, and the traps to avoid.

---

## WHO I AM / CONTEXT

I'm Cameron, a high school junior building a quantitative finance pipeline for college applications (targeting Wharton, Ross, Goizueta, Kelley). I have 7 GitHub repos under `cameroncc333` forming one research program. This task is about deploying the capstone that ties them together.

**My environment:**
- MacBook Pro, files on `~/Desktop`
- Python projects use a virtual env called `rl_env` (on Desktop)
- `python3` / `pip3` commands (Python 3.12 based on the `rl.cpython-312` file I saw)
- GitHub user: `cameroncc333`
- IMPORTANT: I once paused a training run by closing my laptop lid. Always use `caffeinate -i` for long jobs.

**My 7 repos (all live on github.com/cameroncc333):**
1. `AAS-Website` — business site source
2. `AAS-Pricing-Model` — 8-variable calculus cost function + Monte Carlo
3. `fed-rate-sector-analysis` — FOMC rate-decision event study
4. `equity-sector-analyzer` — 2,033-line live dashboard, 30+ metrics, Black-Scholes Greeks, Fama-French
5. `fomc-sentiment-analyzer` — FinBERT NLP across 91 FOMC meetings
6. `algo-trading-system` — sector rotation backtester (238 trades, +64.5%, 0.30 Sharpe, -23.5% maxDD)
7. `rl-portfolio-optimizer` — the RL capstone ("Sector Command"): PPO/A2C/SAC agents, trained 500K steps on 11 sector ETFs, regime-adaptive reward. Located at `~/Desktop/rl-portfolio-optimizer`. Has a `models/` folder and uses the `rl_env` virtualenv.

---

## WHAT THIS NEW REPO IS (in the attached zip)

`sector-command-live` is a NEW, ADDITIVE orchestrator repo. It does NOT replace the 7 repos. It sits on top, consumes their signals, adds a live news layer, and runs the loop that texts me a daily trade briefing I reply to. Architecture:

```
RL ensemble (PPO/A2C/SAC) = THE BRAIN, picks the sector
News sentiment            = REAL signal, conviction modifier (can't change the ticker)
Multi-repo signals        = equity-analyzer + algo-system + fed + fomc all VOTE on the RL pick
VIX / regime              = can force SPY (neutral) or BIL (cash) abstain actions
Politics (Congressional)  = RESEARCH-ONLY, logged, NEVER feeds a trade
Governance layer          = hard rules nothing overrides (max 30%/sector, VIX>35 forces cash, need 2/3 agents)
→ Telegram briefing → I reply BUY/SELL/SKIP + reason → logged to SQLite + Google Sheets
```

**Files in the zip (all written and tested in a sandbox — they run):**
- `main_engine.py` — orchestrator (collect → decide → notify → log). Has `--dry-run` flag.
- `engine/decision.py` — the brain: governance, abstain logic, news + cross-repo conviction modifiers. Pure logic, has its own test cases in `__main__`.
- `engine/repo_signals.py` — computes LIVE signals using each repo's methodology (RSI, composite score, fed context, fomc mood) and produces a cross-repo agreement count. TESTED LIVE — pulls real Yahoo Finance data.
- `engine/journal.py` — SQLite (always) + Google Sheets (optional) logging, plus `attach_human_reply()` for my Telegram responses.
- `feeders/news_feeder.py` — financial news → FinBERT or lexicon fallback → per-sector sentiment. `NEWS_MODE` env var: `transformer` | `lexicon` | `auto`.
- `feeders/political_feeder.py` — Quiver Quant disclosures, research-only, degrades to empty without a key.
- `interface/telegram_bot.py` — briefing formatter + command parser. Header has the @BotFather setup steps.
- `webhook.py` — Flask app to receive my Telegram replies (deploy on Vercel like my old one).
- `core_quant_lib/quant.py` — shared Black-Scholes + regime detection to consolidate across repos.
- `docs/SPY_BIL_RETRAINING_SPEC.md` — exact steps to retrain on 13 assets. READ THIS, it's the core ML task.
- `.github/workflows/daily-signals.yml` — runs the engine 4×/day on cron.
- `README.md`, `SUMMARY.md` (7-repo map + Mermaid diagram), `RESEARCH_JOURNAL.md`.

---

## CURRENT STATE — WHAT'S REAL VS STUB (CRITICAL)

The pipeline runs end-to-end RIGHT NOW, but:
- **LIVE & working:** market data (RSI/momentum/Sharpe/rel-strength), algo-system composite score, news sentiment, VIX regime, cross-repo agreement count, data-freshness stamp.
- **STUB (fake):** `load_rl_signal()` in `main_engine.py` returns hardcoded test data (`BUY XLK, 72%`). The freshness stamp prints "⚠️ RL PICK IS A STUB" until you fix this. **No real money moves until this says LIVE.**
- **Default until set:** fed context reads `FED_RATE`/`FED_LAST_ACTION` env vars (defaults 3.625% / hold).

---

## YOUR TASKS, IN EXACT ORDER

### TASK 0 — Verify models exist
```bash
cd ~/Desktop/rl-portfolio-optimizer && ls -la models/
```
Report what's there. Expect folders/files like `PPO_w0_s42`, `A2C_w0_s42`, `SAC_w0_s42` (or `.zip`). 
- If present → models survived, proceed to Task 2 (skip retrain-from-scratch unless we expand to 13 assets, which we DO want — see Task 3).
- If missing/empty → we retrain (Task 3 covers it).

### TASK 1 — Set up the new repo locally
Unzip `sector-command-live.zip` to `~/Desktop/sector-command-live`. Create the virtualenv, install deps, confirm the dry run works:
```bash
cd ~/Desktop/sector-command-live
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-full.txt
python main_engine.py --dry-run
```
Confirm you see a briefing print with "RL model: STUB" in the freshness section. That's correct at this stage.

### TASK 2 — Inspect my real RL inference code
Open `~/Desktop/rl-portfolio-optimizer`. Find how it loads models and predicts (likely `evaluate.py` or `train.py`, plus `portfolio_env.py`, `config.py`, `data_loader.py`). I need you to understand exactly how to: load the 3 production agents, build today's feature vector, run each agent, and get the ensemble target/action/confidence. Then write a small function/script in rl-portfolio-optimizer that outputs today's decision as JSON with these keys:
```json
{"votes": {"PPO":"BUY XLK","A2C":"BUY XLK","SAC":"HOLD"}, "target":"XLK",
 "action":"BUY", "confidence":72, "current_weight":12.0, "rsi":48.0,
 "rel_strength":0.8, "vix":19.5, "regime":"NORMAL", "ghost_alpha":0.0}
```

### TASK 3 — Add SPY + BIL abstain actions and retrain (the core ML work)
Follow `docs/SPY_BIL_RETRAINING_SPEC.md` in the zip EXACTLY. Summary:
- Expand universe 11 → 13 assets (add SPY, BIL) in `config.py`, `data_loader.py`, `portfolio_env.py`. Replace any hardcoded `11` with `N_ASSETS`.
- Retrain the 3 production agents (PPO/A2C/SAC, window 0, seed 42, 500K steps each) using `caffeinate -i`.
- **RUN THE BENCHMARK** (11-asset forced vs 13-asset abstain): record Sharpe, total return, max drawdown, % days in abstain. DO NOT SKIP — these numbers are for my whitepaper and essays.
- Package new models as `models-live-v1.1.tar.gz`, upload as a GitHub Release tagged `v1.1-models`, patch the workflow to download from the new release URL.

### TASK 4 — Wire real models into the orchestrator
In `sector-command-live/main_engine.py`, replace the `load_rl_signal()` STUB. Easiest bridge: have the rl-portfolio-optimizer script from Task 2 write today's decision to a JSON file, then set env var `RL_SIGNAL_JSON=/path/to/that.json`. The code already reads that path and stamps the source as LIVE automatically. Confirm `python main_engine.py --dry-run` now shows "RL model: LIVE".

### TASK 5 — Telegram (do this early, it's independent)
Follow the setup steps in the header of `interface/telegram_bot.py`:
1. @BotFather → /newbot → get TELEGRAM_TOKEN
2. message the bot, hit getUpdates URL → get TELEGRAM_CHAT_ID
3. Test: `TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python main_engine.py` — a real briefing should hit my phone.

### TASK 6 — Deploy
- Push `sector-command-live` to a new GitHub repo `cameroncc333/sector-command-live`.
- Add GitHub Secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, optional `NEWSAPI_KEY`, `QUIVER_API_KEY`, `GOOGLE_CREDS_B64`, `SHEET_ID`.
- Deploy `webhook.py` on Vercel (same pattern as my old Twilio webhook), set the Telegram webhook URL.
- Confirm the GitHub Action runs (use the manual `workflow_dispatch` trigger first).

### TASK 7 — Add event-triggered alerts (the one remaining feature)
Build a small module that fires an EXTRA Telegram alert outside the 4 scheduled runs when: VIX spikes >20% intraday, regime flips calm→stressed, or any held position drops >5%. Wire it as a separate lightweight GitHub Action on a more frequent cron, OR a check at the top of each run. Keep it simple.

---

## TRAPS / THINGS NOT TO DO (learned the hard way)
- **Don't move real money while freshness says STUB.** The whole point of the stamp is to prevent this.
- **Use `caffeinate -i` for training.** Closing the lid pauses the process otherwise.
- **`pip install` may need `--break-system-packages`** on this Python, or just always use the venv.
- **Politics stays research-only.** Do not wire `political_feeder` into `decision.py`'s conviction logic. It's logged for a research hypothesis (does disclosed activity correlate with returns after the 45-day STOCK Act delay — likely null, which is itself a finding).
- **Don't merge the 7 repos into one.** This orchestrator sits ON TOP. The separate repos are individually impressive for admissions; keep them.
- **FinBERT-FOMC model** (`ZiweiChen/FinBERT-FOMC`) is real and tuned for Fed MINUTES, not news headlines — use it in fomc-sentiment-analyzer, keep base `ProsusAI/finbert` for the news feeder. It's already a config env var (`FINBERT_MODEL`).
- **Don't chase published Sharpe ratios >2-3** from papers — they don't survive live trading. We're optimizing for a documented decision process, not P&L.

## DEFINITION OF DONE
- [ ] Models confirmed/retrained on 13 assets, benchmark numbers recorded
- [ ] `load_rl_signal()` reads real output; dry-run shows "RL model: LIVE"
- [ ] Telegram briefing arrives on my phone; I can reply BUY/SELL/SKIP and it logs
- [ ] Repo pushed, GitHub Action runs 4×/day, webhook live
- [ ] Event-triggered alerts working
- [ ] Still in paper_mode=True (I flip to real money manually after 30 days)
