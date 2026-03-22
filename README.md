# Polymarket Engine v3.2

A zero-infrastructure Polymarket prediction market trading engine.
Runs entirely on **GitHub Actions free tier** — no VPS, no WebSocket, no paid subscriptions.

**Current capital:** $100 USDC (dry-run data collection phase)
**Dashboard:** https://airdropperxx.github.io/polymarket-engine/
**Status:** Scanning every 30 min, collecting pattern data

---

## Active Strategies

| ID | Name | Status | Win Rate | Capital |
|----|------|--------|----------|---------|
| S1 | NegRisk Arbitrage | ✅ Active | ~100% (mathematical arb) | $30 |
| S10 | Near-Resolution Harvest | ✅ Active | 88–95% target | $60 |
| S8 | Logical Impossibility Arb | ⏸ Disabled (MVP) | 80–90% target | $10 |

**Removed/deferred:** S4 (Chainlink — needs WebSocket), S6 (Synth AI — needs $1k capital)

---

## Quick Start

1. Fork repo → set to **Public** (unlimited Actions minutes)
2. Settings → Secrets → add all 9 secrets (see table below)
3. Settings → Variables → add `DRY_RUN = true`
4. Actions → **Market Scanner** → Run workflow × 3 (verify dry-run works)
5. Check dashboard at `https://<your-username>.github.io/polymarket-engine/`
6. After 10+ dry-run cycles with opportunities found → set `DRY_RUN = false`

### Required Secrets

| Secret | Where to get it |
|--------|----------------|
| `POLYMARKET_API_KEY` | polymarket.com → connect wallet → API settings |
| `POLYMARKET_API_SECRET` | same |
| `POLYMARKET_PASSPHRASE` | same |
| `POLYMARKET_WALLET_ADDRESS` | your Polygon wallet (0x...) |
| `POLYMARKET_PRIVATE_KEY` | wallet private key (no 0x prefix) |
| `POLYGON_RPC_URL` | alchemy.com → create free Polygon Mainnet app |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | @userinfobot on Telegram |

---

## Architecture

```
GitHub Actions (every 30 min)
        │
        ▼
run_scan_cycle.py
        │
        ├── DataEngine          → Fetches all active Polymarket markets via Gamma API
        │   └── market_snapshot.json.gz  (compressed, committed to repo)
        │
        ├── MarketObserver      → Records price history for top 200 markets
        │   └── price_history.json  (time-series, committed to repo)
        │
        ├── SignalEngine        → Runs S1 + S10 scanners
        │   ├── S1: NegRisk groups where sum(YES ask) < 1.0
        │   └── S10: High-prob markets (0.85–0.989) resolving within 7 days
        │
        ├── ExecutionEngine     → Logs all opportunities to scan_log.json
        │   └── scan_log.json   (pattern mining dataset, committed to repo)
        │
        ├── StateEngine         → SQLite trade log
        │   └── trades.db + lessons.json
        │
        └── git push data/      → All state persists across ephemeral GHA runners
```

---

## Data Collection (Dry-Run Phase)

Every scan cycle collects:

| File | Contents | Size |
|------|----------|------|
| `data/scan_log.json` | All opportunities seen (edge, prob, category, score, timing) | ~100KB+ |
| `data/price_history.json` | Price time-series for 200 top markets | ~73KB+ |
| `data/market_snapshot.json.gz` | Full market state snapshot | ~390KB compressed |
| `data/trades.db` | All dry-run trade records (SQLite) | ~45KB |
| `data/lessons.json` | ReviewEngine learnings | ~1KB |

The **MarketObserver** detects: momentum (consistent price movement), sharp reversals,
volume spikes (smart-money entry), and resolution drift (high-prob near-expiry).
After 20–30 cycles the dataset is large enough to identify reliable patterns.

---

## Fee Formula

```python
fee = 2.25 * (p * (1 - p)) ** 2
# calc_fee(0.5)  = 0.140625  ← peak
# calc_fee(0.95) = 0.005077
# calc_fee(0.0)  = 0.0
```

Always multiply by position size for actual dollar fee. Never hardcode fee values.

---

## Config Files

| File | Purpose |
|------|---------|
| `configs/engine.yaml` | Capital, risk limits, allocations |
| `configs/s10_near_resolution.yaml` | S10 thresholds |
| `configs/s1_negrisk.yaml` | S1 thresholds |
| `configs/s8_logical.yaml` | S8 thresholds (disabled) |

---

## GitHub Pages Dashboard

Enable at: Settings → Pages → Deploy from branch → main → / (root)

Dashboard URL: `https://<username>.github.io/polymarket-engine/`

Tabs: Overview · Trades · Data Collected · Pattern Analysis · Observer Signals · Strategy Refinement

Reads live from `data/scan_log.json`, `data/price_history.json`, `data/lessons.json` via GitHub raw.
Auto-refreshes every 5 minutes.

---

## Capital Progression

```
Phase   Capital    Strategies          Condition to advance
──────────────────────────────────────────────────────────────
MVP     $100       S1 + S10           10+ dry-run cycles, opps found, patterns identified
P2      $150–500   S1 + S10           10+ real trades, win rate ≥ 85%, reviewer running
P3      $1,000+    Add S8             Backtest complete, lessons.json has 10+ entries
P4      $2,500+    Add S6 (Synth AI)  $200/mo infra justified
```
