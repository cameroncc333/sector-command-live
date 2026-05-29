// Vercel serverless Telegram webhook — Node.js
// All state in Redis (Upstash). Handles: BUY/SELL/SKIP/BOUGHT/BALANCE/STATUS/WHY/ALPHA/PORTFOLIO/PERF

// ── Redis ──────────────────────────────────────────────────────────────────────
async function redisGet(key) {
  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) return null;
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(['GET', key]),
    });
    const raw = (await r.json()).result;
    if (!raw) return null;
    try { return JSON.parse(raw); } catch { return raw; }
  } catch { return null; }
}

async function redisSet(key, value, ttl = 7776000) {
  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) return;
  try {
    await fetch(url, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(['SETEX', key, ttl, JSON.stringify(value)]),
    });
  } catch {}
}

// ── Telegram ───────────────────────────────────────────────────────────────────
async function tgSend(text) {
  const token = process.env.TELEGRAM_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token || !chatId) return;
  try {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: 'HTML' }),
    });
  } catch {}
}

// ── GitHub Actions dispatch (refreshes dashboard after BALANCE/BOUGHT) ─────────
async function triggerActions() {
  const pat = process.env.GITHUB_PAT;
  const repo = process.env.GITHUB_REPO;
  if (!pat || !repo) return;
  try {
    await fetch(`https://api.github.com/repos/${repo}/actions/workflows/daily-signals.yml/dispatches`, {
      method: 'POST',
      headers: { Authorization: `token ${pat}`, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: 'main' }),
    });
  } catch {}
}

// ── Alpaca paper buy ───────────────────────────────────────────────────────────
async function alpacaBuy(ticker, confidence = 50) {
  const key = process.env.ALPACA_API_KEY;
  const secret = process.env.ALPACA_SECRET_KEY;
  if (!key || !secret) return { executed: false, reason: 'no_alpaca_keys' };
  const balance = parseFloat(process.env.DEFAULT_BALANCE || '0') || 0;
  const pct = confidence >= 75 ? 0.30 : confidence >= 50 ? 0.20 : 0.10;
  const notional = balance > 0 ? Math.round(balance * pct * 100) / 100 : 500;
  const base = 'https://paper-api.alpaca.markets';
  const hdrs = { 'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret, 'Content-Type': 'application/json' };
  try {
    const posR = await fetch(`${base}/v2/positions`, { headers: hdrs });
    if (posR.ok) {
      const positions = await posR.json();
      await Promise.all(positions.map(p => fetch(`${base}/v2/positions/${p.symbol}`, { method: 'DELETE', headers: hdrs })));
    }
    const orderR = await fetch(`${base}/v2/orders`, {
      method: 'POST', headers: hdrs,
      body: JSON.stringify({ symbol: ticker, notional: String(notional), side: 'buy', type: 'market', time_in_force: 'day' }),
    });
    if (orderR.status === 200 || orderR.status === 201) return { executed: true, notional, ticker };
    return { executed: false, reason: (await orderR.text()).slice(0, 200) };
  } catch (e) { return { executed: false, reason: String(e).slice(0, 200) }; }
}

// ── Command parser ─────────────────────────────────────────────────────────────
function parseCmd(text) {
  const tokens = text.trim().split(/\s+/);
  if (!tokens.length) return { command: 'UNKNOWN', tokens: [] };
  const first = tokens[0].toUpperCase();
  const KNOWN = new Set(['BUY','SELL','SKIP','HOLD','STATUS','WHY','ALPHA','PORTFOLIO','PERF','BALANCE','BOUGHT','SOLD','EXPLAIN']);
  const command = KNOWN.has(first) ? first : 'QUESTION';
  let ticker = null, amount = null;
  if (command === 'BUY' && tokens.length > 1) {
    const s = tokens[1].toUpperCase();
    if ('ABCDE'.includes(s) && s.length === 1) amount = s.charCodeAt(0) - 65 + 1;
    else if (/^[A-Z][A-Z0-9-]{0,9}$/.test(s)) ticker = s;
    else { const n = parseInt(s); if (!isNaN(n)) amount = n; }
  }
  if (command === 'BALANCE' && tokens.length > 1) {
    const n = parseFloat(tokens[1].replace(/[$,]/g, ''));
    if (!isNaN(n)) amount = n;
  }
  return { command, ticker, amount, reason: tokens.slice(2).join(' '), tokens };
}

// ── Command handler ────────────────────────────────────────────────────────────
async function handle(text) {
  const cmd = parseCmd(text);
  const { command } = cmd;
  const briefing = await redisGet('sc:last_briefing') || {};
  const ranked = briefing.ranked_opportunities || [];

  // BUY / SELL / SKIP / HOLD
  if (['BUY','SELL','SKIP','HOLD'].includes(command)) {
    let { ticker, amount } = cmd;
    if (amount && !ticker && ranked.length) {
      const opp = ranked[Math.max(0, amount - 1)];
      if (opp) ticker = opp.ticker || null;
    }
    ticker = ticker || briefing.ticker || 'SPY';
    const confidence = parseInt(briefing.confidence || 50);

    const today = new Date().toISOString().slice(0, 10);
    let replies = await redisGet('sc:journal_replies') || [];
    if (!Array.isArray(replies)) replies = [];
    replies = replies.filter(r => r.date !== today);
    replies.push({ date: today, command, reason: cmd.reason || '(no reason given)', ts: new Date().toISOString() });
    await redisSet('sc:journal_replies', replies.slice(-90));

    let alpacaLine = '';
    if (command === 'BUY' && ticker) {
      const result = await alpacaBuy(ticker, confidence);
      if (result.executed) alpacaLine = `\n🏦 Alpaca paper: BUY ${ticker} ($${result.notional.toLocaleString()})`;
      else if (!process.env.ALPACA_API_KEY) alpacaLine = '\n(Add ALPACA_API_KEY to Vercel env vars to auto-execute)';
    }
    const boughtReminder = command === 'BUY' && ticker
      ? `\n\n💡 Log real money: <code>BOUGHT ${ticker} [dollar amount]</code>` : '';
    return `✅ Logged: <b>${command} ${ticker}</b>${cmd.reason ? `\nReason: ${cmd.reason}` : ''}${alpacaLine}\n📊 Paper mode active${boughtReminder}`;
  }

  if (command === 'STATUS') {
    if (!briefing.ticker) return 'No briefing yet. GitHub Actions runs at 9AM, 10:30AM, 12PM, 2PM, 3:30PM, 4:30PM EDT.';
    const gen = (briefing.freshness || {}).generated_utc || '—';
    return `📋 Last signal (${briefing.date || '—'}): <b>${briefing.action || '—'} ${briefing.ticker}</b> @ ${briefing.confidence || '—'}%\nGenerated: ${gen}\nReply <code>BUY</code>, <code>SKIP</code>, or ask a question.`;
  }

  if (command === 'WHY') {
    const trace = briefing.why_trace || [];
    if (trace.length) return '🧠 <b>Reasoning:</b>\n' + trace.filter(Boolean).map(t => `• ${t}`).join('\n');
    return 'No reasoning trace available.';
  }

  if (command === 'ALPHA') {
    const picks = briefing.equity_alpha_picks || [];
    if (!picks.length) return '📊 <b>Equity Alpha</b>\n\nNo picks yet — generated with each daily briefing.';
    const EMOJI = { HIGH: '🔥', MEDIUM: '✅', LOW: '🟡' };
    const lines = ['📈 <b>Stock Alpha Picks</b>'];
    picks.slice(0, 5).forEach((p, i) => {
      const dollar = p.suggested_dollar ? ` → <b>$${Math.round(p.suggested_dollar).toLocaleString()}</b>` : '';
      lines.push(`  ${i+1}. ${EMOJI[p.conviction]||'⚪'} <b>${p.ticker}</b> (${p.sector_name||''}) score ${Math.round(p.composite_score||0)}/100${dollar}`);
      if (p.conviction_tagline) lines.push(`     💡 ${p.conviction_tagline}`);
    });
    return lines.join('\n');
  }

  if (command === 'BALANCE') {
    const { amount } = cmd;
    if (amount && amount > 0) {
      await redisSet('sc:balance', amount);
      return `✅ Balance set to <b>$${amount.toLocaleString()}</b>\nReply <code>PORTFOLIO</code> to see holdings.`;
    }
    const val = await redisGet('sc:balance');
    const bal = val != null ? parseFloat(val) : null;
    return bal ? `Current balance: <b>$${bal.toLocaleString()}</b>\nUpdate: <code>BALANCE 15000</code>` : 'Set your balance: <code>BALANCE 12500</code>';
  }

  if (command === 'BOUGHT') {
    const { tokens } = cmd;
    if (tokens.length < 3) return 'Format: <code>BOUGHT XLE 500</code> or <code>BOUGHT XLE 500 my reason</code>';
    const ticker = tokens[1].toUpperCase();
    const nums = [], noteParts = [];
    tokens.slice(2).forEach(t => { const n = parseFloat(t.replace(/[$,]/g,'')); isNaN(n) ? noteParts.push(t) : nums.push(n); });
    const notes = noteParts.join(' ').replace(/^[-–: ]+/,'').trim() || null;
    let entry;
    if (nums.length === 2) entry = { ticker, shares: nums[0], avg_cost: nums[1], dollar_value: Math.round(nums[0]*nums[1]*100)/100 };
    else if (nums.length === 1) entry = { ticker, dollar_value: nums[0] };
    else return 'Format: <code>BOUGHT XLE 500</code>';
    let holdings = await redisGet('sc:holdings') || [];
    if (!Array.isArray(holdings)) holdings = [];
    holdings = holdings.filter(h => h.ticker !== ticker);
    entry.date_bought = new Date().toISOString().slice(0, 10);
    if (notes) entry.notes = notes;
    holdings.push(entry);
    await redisSet('sc:holdings', holdings);
    return `✅ Logged: <b>${ticker}</b> $${entry.dollar_value.toLocaleString()}${notes ? `\nNote: ${notes}` : ''}\nReply <code>PORTFOLIO</code> to see all holdings.`;
  }

  if (command === 'SOLD') {
    const ticker = cmd.tokens[1] ? cmd.tokens[1].toUpperCase() : null;
    if (!ticker) return 'Format: <code>SOLD XLE</code>';
    let holdings = await redisGet('sc:holdings') || [];
    holdings = Array.isArray(holdings) ? holdings.filter(h => h.ticker !== ticker) : [];
    await redisSet('sc:holdings', holdings);
    return `✅ <b>${ticker}</b> removed.\nReply <code>PORTFOLIO</code> to confirm.`;
  }

  if (command === 'PORTFOLIO') {
    const balRaw = await redisGet('sc:balance');
    const balance = balRaw != null ? parseFloat(balRaw) : null;
    if (!balance) return 'Set your balance first: <code>BALANCE 12500</code>';
    const holdings = await redisGet('sc:holdings') || [];
    if (!Array.isArray(holdings) || !holdings.length)
      return `Balance: <b>$${balance.toLocaleString()}</b>\nNo open positions.\nLog one: <code>BOUGHT XLF 500</code>`;
    let totalInvested = 0;
    const lines = [`💼 <b>Portfolio</b>  (Balance: $${balance.toLocaleString()})`];
    for (const h of holdings) {
      if (!h?.ticker) continue;
      const d = h.dollar_value || 0;
      totalInvested += d;
      const pct = Math.round(d / balance * 1000) / 10;
      lines.push(`  • <b>${h.ticker}</b>  $${d.toLocaleString()}  (${pct}%)${h.notes ? `  — ${h.notes}` : ''}`);
    }
    lines.push(`\nInvested: $${totalInvested.toLocaleString()}  ·  Cash: $${(balance - totalInvested).toLocaleString()}`);
    return lines.join('\n');
  }

  if (command === 'PERF') {
    const perf = briefing.performance || {};
    if (perf.portfolio_return_pct != null) {
      const { portfolio_return_pct: p, spy_return_pct: s = 0, alpha_pct: a = 0, n_trades: n = 0 } = perf;
      return `📈 <b>Performance</b>\nPortfolio: <b>${p >= 0 ? '+' : ''}${p.toFixed(1)}%</b>  SPY: ${s >= 0 ? '+' : ''}${s.toFixed(1)}%  Alpha: ${a >= 0 ? '+' : ''}${a.toFixed(2)}%\n${n} trades tracked`;
    }
    return 'No completed trades yet. Reply BUY to a briefing to start tracking.';
  }

  // Plain-English question — route to Gemini with full market context
  const geminiKey = process.env.GEMINI_API_KEY;
  if (!geminiKey) return '⚠️ GEMINI_API_KEY not set in Vercel env vars.';

  try {
    const b = briefing;
    const ranked = b.ranked_opportunities || [];
    const macro = b.macro || {};
    const alp = b.alpaca_paper || {};
    const ctx = [
      `Date: ${b.date || '—'}  Regime: ${b.regime || '—'}  VIX: ${b.vix || '—'}`,
      `RL decision: ${b.action || '—'} ${b.ticker || '—'} @ ${b.confidence || '—'}% confidence`,
      b.abstain_reason ? `Abstain reason: ${b.abstain_reason}` : '',
      `Ranked picks: ${ranked.slice(0,3).map((o,i)=>['A','B','C'][i]+') '+o.ticker+' '+o.conviction).join(', ')}`,
      `News sentiment: ${b.news_sentiment >= 0 ? '+' : ''}${(b.news_sentiment||0).toFixed(2)} | Top headline: ${b.news_headline || '—'}`,
      macro.yield_curve_spread != null ? `Yield curve: ${macro.yield_curve_spread.toFixed(2)}% | DXY: ${macro.dxy || '—'}` : '',
      alp.equity ? `Alpaca paper equity: $${alp.equity.toLocaleString()} (daily: ${(alp.daily_pnl_pct||0).toFixed(2)}%)` : '',
      `Crypto signals: ${JSON.stringify((b.crypto_signals||{}).signals||[])}`,
    ].filter(Boolean).join('\n');

    const prompt = `You are the AI assistant for Sector Command, a live quantitative trading system. Answer the user's question using the market data below. Be concise (3-5 sentences max), practical, and specific to the data shown.\n\nMarket context:\n${ctx}\n\nUser question: ${text}`;

    const r = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key=${geminiKey}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          contents: [{ parts: [{ text: prompt }] }],
          generationConfig: { maxOutputTokens: 350, temperature: 0.7 },
        }),
      }
    );
    if (!r.ok) {
      const errText = await r.text();
      return `⚠️ Gemini API error ${r.status}: ${errText.slice(0, 300)}`;
    }
    const data = await r.json();
    const answer = data.candidates?.[0]?.content?.parts?.[0]?.text;
    if (answer) return `🤖 <b>AI Answer</b>\n\n${answer.trim()}\n\n<i>Based on ${b.date || 'latest'} briefing data</i>`;
    return `⚠️ Gemini returned no answer. Response: ${JSON.stringify(data).slice(0, 200)}`;
  } catch (e) {
    return `⚠️ Gemini exception: ${String(e).slice(0, 200)}`;
  }
}

// ── Cron-job.org endpoints ─────────────────────────────────────────────────────
// cron-job.org hits /cron/briefing?key=SECRET  → triggers daily-signals.yml
// cron-job.org hits /cron/alerts?key=SECRET    → triggers event-alerts.yml
async function dispatchWorkflow(workflow) {
  const pat  = process.env.GITHUB_PAT;
  const repo = process.env.GITHUB_REPO;
  if (!pat || !repo) return { ok: false, reason: 'no GITHUB_PAT/GITHUB_REPO' };
  try {
    const r = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`,
      {
        method: 'POST',
        headers: { Authorization: `token ${pat}`, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ ref: 'main' }),
      }
    );
    return { ok: r.status === 204, status: r.status };
  } catch (e) {
    return { ok: false, reason: String(e) };
  }
}

// ── Vercel handler ─────────────────────────────────────────────────────────────
module.exports = async (req, res) => {
  const q    = req.query || {};
  const qkey = q.key || '';
  const cronSecret = process.env.CRON_SECRET || '';

  // /cron/briefing and /cron/alerts are rewritten here via vercel.json rewrites.
  // Vercel appends _cron=briefing|alerts so we know which workflow to fire.
  if (q._cron === 'briefing' || q._cron === 'alerts') {
    if (cronSecret && qkey !== cronSecret) return res.status(403).json({ ok: false, error: 'bad key' });
    const workflow = q._cron === 'briefing' ? 'daily-signals.yml' : 'event-alerts.yml';
    const result = await dispatchWorkflow(workflow);
    return res.status(result.ok ? 200 : 502).json({ ok: result.ok, workflow, ...result });
  }

  if (req.method === 'GET') {
    const briefing = await redisGet('sc:last_briefing') || {};
    const balance  = await redisGet('sc:balance');
    const holdings = await redisGet('sc:holdings') || [];
    const holdingsArr = Array.isArray(holdings) ? holdings : [];
    const balNum = balance != null ? parseFloat(balance) : null;
    // Enrich holdings with alloc_pct for the dashboard
    const holdingsEnriched = holdingsArr.map(h => ({
      ...h,
      alloc_pct: (balNum && h.dollar_value) ? Math.round(h.dollar_value / balNum * 1000) / 10 : null,
    }));
    return res.json({
      ok: true,
      service: 'sector-command-webhook',
      briefing_date: briefing.date || 'none',
      balance: balNum,
      holdings: holdingsEnriched,
      n_holdings: holdingsArr.length,
      env_telegram:  !!process.env.TELEGRAM_TOKEN,
      env_redis:     !!process.env.UPSTASH_REDIS_REST_URL,
      env_alpaca:    !!process.env.ALPACA_API_KEY,
      env_github_pat: !!process.env.GITHUB_PAT,
    });
  }

  try {
    const body = req.body || {};
    const msg  = body.message || body.edited_message || {};
    const text = (msg.text || '').trim();
    if (text) {
      const reply = await handle(text);
      await tgSend(reply);
      const first = text.trim().split(/\s+/)[0].toUpperCase();
      if (['BALANCE','BOUGHT','SOLD'].includes(first)) await triggerActions();
    }
  } catch (e) {
    await tgSend(`⚠️ Error: ${e.message}`);
  }
  res.json({ ok: true });
};
