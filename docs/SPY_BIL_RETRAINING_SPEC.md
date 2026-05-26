# SPY + BIL Abstain Action — Retraining Spec

This is the one piece that must run on **your MacBook** (it needs the RL models and
training time, not this sandbox). Follow it in your `rl-portfolio-optimizer` repo.
Everything is exact and in order.

## Goal
Expand the RL universe from 11 sectors to 13 assets by adding **SPY** (broad-market
neutral / low-conviction abstain) and **BIL** (cash / defensive abstain). Then
benchmark 11-asset forced allocation vs. 13-asset with abstain. The benchmark delta
is the whitepaper's backbone — **do not skip it.**

## Step 1 — `config.py`: expand the universe
```python
# before
SECTORS = ["XLK","XLF","XLE","XLV","XLY","XLP","XLI","XLB","XLRE","XLU","XLC"]
# after
SECTORS  = ["XLK","XLF","XLE","XLV","XLY","XLP","XLI","XLB","XLRE","XLU","XLC"]
ABSTAIN  = ["SPY","BIL"]            # NEW
UNIVERSE = SECTORS + ABSTAIN       # 13 assets
N_ASSETS = len(UNIVERSE)           # was 11, now 13
```
Update any hardcoded `11` (action space size, observation reshape) to `N_ASSETS`.

## Step 2 — `data_loader.py`: pull the two new tickers
Add `SPY` and `BIL` to the download list. BIL started trading in 2007 (fine for your
2008 start). SPY you already pull as the benchmark — now also include it as a
*tradeable* asset in the feature matrix. Keep the existing XLRE inception handling.

## Step 3 — `portfolio_env.py`: let the agent hold the abstain assets
The action space is already continuous logits over assets; widening from 11 → 13 is
mostly automatic once `N_ASSETS` propagates. Two deliberate touches:
- BIL's volatility is ~0, so its risk-adjusted reward contribution is near-zero —
  the agent will only choose it when *everything else looks worse*, which is exactly
  the defensive behavior you want. No special-casing needed.
- Optionally add a tiny holding bonus for SPY/BIL during high-VIX steps so the agent
  learns abstain is "allowed," not penalized. Keep it small (≤ 0.05× reward) so it
  doesn't dominate.

## Step 4 — retrain the 3 production agents
```bash
# from rl-portfolio-optimizer/, with your venv active and lid-sleep disabled
caffeinate -i python train.py --agent PPO --window 0 --seed 42 --timesteps 500000
caffeinate -i python train.py --agent A2C --window 0 --seed 42 --timesteps 500000
caffeinate -i python train.py --agent SAC --window 0 --seed 42 --timesteps 500000
```
`caffeinate -i` keeps the Mac awake without you holding the lid open (this was the
thing that kept pausing your runs). SAC is the slow one — expect it to run longest.

## Step 5 — the benchmark (the important part)
Run `evaluate.py` twice and record both:
```bash
python evaluate.py --universe 11   # forced allocation baseline
python evaluate.py --universe 13   # with SPY/BIL abstain
```
Capture for each: **annualized Sharpe, total return, max drawdown, % of days in
abstain (SPY or BIL)**. The expected, defensible result: similar or slightly lower
return, but **meaningfully lower drawdown and volatility** — i.e., better
risk-adjusted behavior. Save the numbers; they go straight into the whitepaper and
your essays as "I identified a structural constraint, removed it, and measured the
improvement."

## Step 6 — repackage and ship the models
```bash
tar -czf models-live-v1.1.tar.gz models/PPO_w0_s42 models/A2C_w0_s42 models/SAC_w0_s42
# upload as a GitHub Release asset tagged v1.1-models, get the permanent URL
```
Then patch the `rl-portfolio-optimizer` workflow YAML to download from the new
release URL (same pattern you used for v1.0).

## Step 7 — wire into the orchestrator
In `sector-command-live/main_engine.py`, replace the `load_rl_signal()` stub: have
your RL repo write today's ensemble decision to a small JSON
(`votes/target/action/confidence/vix/regime/...`) and point `RL_SIGNAL_JSON` at it,
**or** import your inference function directly. The orchestrator already accepts a
JSON path via the `RL_SIGNAL_JSON` env var — that's the cleanest bridge.

## Done-check
- [ ] `N_ASSETS == 13` everywhere, no stray hardcoded 11
- [ ] SPY + BIL appear in saved weight heatmaps
- [ ] Benchmark table recorded (11 vs 13): Sharpe / return / drawdown / abstain %
- [ ] New models tarball released, workflow patched
- [ ] `load_rl_signal()` reads real output, dry-run still passes
