#!/bin/bash
# setup.sh — first-time local setup for Sector Command Live
#
# Run this once from ~/Desktop/sector-command-live (or wherever you cloned it):
#   chmod +x setup.sh && ./setup.sh
#
# What it does:
#   1. Creates and activates a local Python venv
#   2. Installs dependencies
#   3. Verifies the dry-run works
#   4. Prints the Telegram setup checklist

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "════════════════════════════════════════"
echo "  Sector Command Live — First-Time Setup"
echo "════════════════════════════════════════"
echo ""

# ── Python venv ────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "[1/4] Creating Python virtual environment..."
    python3 -m venv venv
else
    echo "[1/4] Virtual environment already exists, skipping."
fi

echo ""
echo "[2/4] Activating venv and installing dependencies..."
source venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements-full.txt --quiet
echo "  ✓ Dependencies installed"

# ── Dry run ────────────────────────────────────────────────────────
echo ""
echo "[3/4] Running dry-run to verify the pipeline works..."
python main_engine.py --dry-run
echo ""
echo "  ✓ Dry-run complete (RL model will show STUB until you wire generate_rl_signal.py)"

# ── Telegram setup reminder ────────────────────────────────────────
echo ""
echo "[4/4] Telegram setup checklist (do this once on your phone):"
echo "  □ Open Telegram → search @BotFather → /newbot → name it 'Sector Command'"
echo "  □ Copy the TOKEN (looks like 123456789:AAH...)"
echo "  □ Message your new bot 'hi'"
echo "  □ Visit https://api.telegram.org/bot<TOKEN>/getUpdates in a browser"
echo "  □ Find 'chat':{'id': XXXXXXX} — that's your CHAT_ID"
echo "  □ Test locally:"
echo "      TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python main_engine.py --dry-run"
echo "  □ Add to GitHub Secrets: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID"
echo ""
echo "════════════════════════════════════════"
echo "  Optional secrets for full features:"
echo "    NEWSAPI_KEY       → real-time news (free tier: newsapi.org)"
echo "    QUIVER_API_KEY    → paid congressional trades (free public fallback works)"
echo "    GOOGLE_CREDS_B64  → Google Sheets mirror (base64-encoded service account JSON)"
echo "    SHEET_ID          → Google Sheets spreadsheet ID"
echo "    RL_SIGNAL_JSON    → path to rl_signal.json from generate_rl_signal.py"
echo "════════════════════════════════════════"
echo ""
echo "  ── Wire the real RL models (one-time) ──────────────────────────"
echo "  cd ~/rl-portfolio-optimizer && source rl_env/bin/activate"
echo "  python generate_rl_signal.py --out $SCRIPT_DIR/data/rl_signal.json"
echo "  RL_SIGNAL_JSON=data/rl_signal.json python main_engine.py --dry-run"
echo "  Should show 'RL model: LIVE' in the freshness stamp"
echo ""
echo "  ── Local cron to auto-refresh RL signal each morning ───────────"
echo "  Add this to your crontab (crontab -e):"
echo "  0 8 * * 1-5 cd ~/rl-portfolio-optimizer && source rl_env/bin/activate && caffeinate -i python generate_rl_signal.py --out $SCRIPT_DIR/data/rl_signal.json && cd $SCRIPT_DIR && git add data/rl_signal.json && git diff --cached --quiet || git commit -m 'rl: daily signal' && git push"
echo ""
echo "  ── GitHub repo setup ───────────────────────────────────────────"
echo "  cd $SCRIPT_DIR"
echo "  git init && git remote add origin git@github.com:cameroncc333/sector-command-live.git"
echo "  git add -A && git commit -m 'feat: Sector Command Live — initial deploy'"
echo "  git push -u origin main"
echo ""

