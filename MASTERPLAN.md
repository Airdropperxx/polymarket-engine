# POLYMARKET ENGINE v3.2 — MASTER BUILD PLAN

**Version:** 3.2 | **Capital:** $100 USDC | **Phase:** Dry-run data collection | **Date:** March 2026

---

## WHAT THIS IS

A zero-infrastructure Polymarket prediction market trading engine.
Runs on GitHub Actions free tier (public repo) — no VPS, no WebSocket, no paid subscriptions.
Currently in dry-run mode collecting pattern data before deploying real capital.

**Dashboard:** https://airdropperxx.github.io/polymarket-engine/

---

## PART 0 — HARD CONSTRAINTS

```
CAPITAL:         $100 USDC starting. Every dollar is real.
LANGUAGE:        Python 3.11+. No Rust. No compiled languages.
INFRA COST:      $0/month. GitHub Actions free tier only.
STRATEGIES:      S1 + S10 active. S8 disabled (needs torch). S4/S6 deferred.
RISK:            Max 40% capital per trade (dry-run). Max 5% daily loss (live).
FEES:            ALWAYS use calc_fee(p) = 2.25 * (p*(1-p))^2. Never hardcode.
                 Per-share fee = calc_fee(buy_price) * buy_price
DRY_RUN:         True until 10+ cycles with real opportunities confirmed.
POLL INTERVAL:   30 min (GitHub Actions cron).
DEPENDENCIES:    requirements-scan.txt — 7 packages only. No torch, no web3 in GHA.
```

---

## PART 1 — ARCHITECTURE

### Seven Engines

```
┌─────────────────────────────────────────────────────────────┐
│                  POLYMARKET ENGINE v3.2                     │
│                                                             │
│  DataEngine          → Gamma API → market_snapshot.json.gz │
│       ↓                                                     │
│  MarketObserver      → price_history.json (time-series)     │
│       ↓                                                     │
│  SignalEngine        → S1 scan + S10 scan → ranked opps     │
│       ↓                                                     │
│  ExecutionEngine     → scan_log.json + trades.db            │
│       ↓                                                     │
│  StateEngine         → trades.db + lessons.json             │
│       ↓                                                     │
│  ReviewEngine        → Claude Haiku → lessons.json updates  │
│       ↓                                                     │
│  MonitorEngine       → Telegram (raw HTTP POST)             │
└─────────────────────────────────────────────────────────────┘
```

### Engine Contracts

**DataEngine**
- Fetches all active Polymarket markets from Gamma API
- Parses outcomePrices (JSON string), clobTokenIds (JSON string)
- Filters: skips resolved markets (price >= 0.999), volume < $10
- Detects category from Gamma's own field first, then keyword matching
- Saves gzip snapshot → data/market_snapshot.json.gz

**MarketObserver** (NEW in v3.2)
- Records price snapshots for top 200 markets every 30-min cycle
- Stores time-series in data/price_history.json (committed to git)
- Detects signals: momentum_up, momentum_down, sharp_move, resolution_drift, volume_spike
- NO separate workflow — embedded in scan cycle
- After 20+ cycles: enables momentum trading, smart-money following, NegRisk early detection
- Usecase: buy a market moving from 0.80→0.85→0.90 BEFORE it reaches 0.95

**SignalEngine**
- Orchestrates all strategy scanners
- Exception-shields each strategy (one crash never stops others)
- Scores and ranks opportunities, applies threshold filter

**ExecutionEngine**
- ONLY engine that places real orders
- In dry-run: logs ALL opportunities to scan_log.json for pattern mining
- Logs metadata: edge, probability, category, volume, spread, hour_utc, weekday

**StateEngine** — SQLite (trades.db) + lessons.json. Never calls external APIs.

**ReviewEngine** — Claude Haiku. Runs after resolutions. Max ±5% allocation change per cycle.

**MonitorEngine** — Telegram via raw requests.post(). No python-telegram-bot library.

---

## PART 2 — STRATEGIES

### Active

| ID | Name | Type | Capital | Window | Edge Source |
|----|------|------|---------|--------|-------------|
| S1 | NegRisk Arb | ArbEngine | $30 | Any | sum(YES ask) < 1.0 in NegRisk group |
| S10 | Near-Resolution | SignalEngine | $60 | 7 days | High-prob (0.85–0.989) approaching resolution |

### S1 — NegRisk Arbitrage
- In a NegRisk group, exactly one outcome resolves YES
- If sum(YES ask prices) < 1.0 - fees → buy all YES → guaranteed profit
- Example: 4-team bracket, prices 0.22+0.18+0.25+0.31 = 0.96 → edge = 0.04
- **Win rate: ~100%** (mathematical guarantee, not probabilistic)
- **Category**: determined from dominant category of legs
- Key filter: min_leg_bid >= 0.01 (rejects stale markets with no real bids)

### S10 — Near-Resolution Harvest
- Markets where YES or NO is 0.85–0.989 and resolves within 7 days
- Edge: probability - buy_price - fee (per-share fee = calc_fee(ask) * ask)
- **Win rate: 88–95%** target
- max_probability: 0.989 — critical filter that excludes already-resolved markets
- Category bonus in scoring: finance (+0.08) > crypto (+0.06) > sports (+0.04) > politics (-0.02)

### Deferred

| ID | Name | Reason | Condition to Enable |
|----|------|--------|---------------------|
| S8 | Logical Arb | Needs torch (1.5GB, breaks GHA) | Capital > $500, local/VPS |
| S4 | Chainlink Sniper | Needs WebSocket (<2min reaction) | VPS with persistent process |
| S6 | Synth AI | Needs $1k capital + $200/mo infra | Phase 3 |

---

## PART 3 — DATA COLLECTION & PATTERNS

### What data is being collected

```
data/scan_log.json        — Every opportunity seen (executed or not)
data/price_history.json   — Price time-series, 200 markets, every 30 min
data/market_snapshot.json.gz — Full market state per cycle
data/trades.db            — SQLite trade records
data/lessons.json         — ReviewEngine memory
```

All files committed to git after every scan cycle — git IS the database.

### Patterns being detected

1. **Time-of-day alpha** — Hour distribution in scan_log reveals when most opportunities appear
2. **Category edge curves** — Finance/crypto at p=0.90–0.95 typically more reliable than politics
3. **Optimal probability range** — Edge peaks at 0.88–0.95 (wide enough spread, low enough fees)
4. **Momentum signals** — 3+ consecutive price increases = entry signal (MarketObserver)
5. **Volume spikes** — >3x average volume = smart money entering, follow direction
6. **NegRisk divergence** — Group sum drifting from 1.00 = early S1 arb signal
7. **Day-of-week patterns** — Sports markets cluster Mon/Wed/Fri (game days)

### How patterns improve strategies

- If finance category shows highest avg edge → raise allocations/weight for finance
- If 90–95% probability range shows best win rate → narrow S10 min_probability
- If momentum signal precedes 3%+ price move → build S11 momentum strategy
- If volume spikes reliably predict direction → add volume filter to S10

---

## PART 4 — FEE FORMULA

```python
# Canonical formula — never change, never hardcode
def calc_fee(p: float) -> float:
    return 2.25 * (p * (1.0 - p)) ** 2

# Verification
assert abs(calc_fee(0.5)  - 0.140625) < 0.0001   # peak fee
assert calc_fee(0.95) < 0.006                      # tiny at high prob
assert calc_fee(0.0) == 0.0
assert calc_fee(1.0) == 0.0

# Per-share fee (used in edge calculation):
fee_per_share = calc_fee(buy_price) * buy_price

# Edge calculation:
edge = probability - buy_price - fee_per_share
```

---

## PART 5 — DEPENDENCIES

### GitHub Actions (requirements-scan.txt — 7 packages only)
```
requests==2.31.0
pyyaml==6.0.1
sqlalchemy==2.0.28
structlog==24.1.0
tenacity==8.2.3
python-dotenv==1.0.1
anthropic==0.25.0
```

### Live trading only (installed conditionally when DRY_RUN=false)
```
py-clob-client==0.16.0
web3==6.14.0        # MUST be exactly 6.14.0 — newer versions break py-clob-client
```

### Why this split?
py-clob-client + web3 + sentence-transformers in one requirements.txt causes
pip dependency resolution to exceed maximum depth and fail. The 7-package scan set
has zero conflicts and installs in ~15 seconds.

---

## PART 6 — GITHUB ACTIONS WORKFLOWS

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| scan.yml | Every 30 min | Full scan cycle: fetch → observe → scan → execute → commit |
| resolve_check.yml | Every hour | Check open positions for resolution |
| daily_review.yml | 00:00 UTC | ReviewEngine + lessons.json update |
| test.yml | On push/PR | Run test suite |
| bootstrap.yml | Manual | Create GitHub Issues from TASKS.yaml |

**scan.yml key settings:**
- `PYTHONPATH: ${{ github.workspace }}` — required for engines/strategies imports
- `permissions: contents: write` — required for git push data/
- No requirements file — individual pip install commands to avoid resolver

---

## PART 7 — CAPITAL PROGRESSION

```
Phase   Capital     Strategies           Condition
────────────────────────────────────────────────────────────────
MVP     $100        S1 + S10 dry-run     Collecting pattern data now
P1      $100        S1 + S10 live        10+ dry cycles, opps confirmed, DRY_RUN=false
P2      $150–500    S1 + S10             10+ real trades, win rate ≥ 85%
P3      $1,000+     Add S8 (locally)     Capital justifies torch install
P4      $2,500+     Add S5 market-making $52/mo VPS justified
P5      $5,000+     Add S6 Synth AI      $200/mo infra justified
```

---

## PART 8 — KNOWN ISSUES & DECISIONS

| Issue | Decision |
|-------|----------|
| Gamma API returns outcomePrices as JSON string | Parse with json.loads() |
| Markets show price=1.0 after resolution | Filter: skip if price >= 0.999 |
| strategies.yaml multi-doc YAML crash | Deleted — use individual s*.yaml files |
| sentence-transformers pulls torch (1.5GB) | S8 disabled at MVP, lazy import |
| python-telegram-bot conflicts with anthropic | Replaced with raw requests.post() |
| chromadb removed | State lost between ephemeral runners — use json file instead |
| web3 > 6.14.0 breaks py-clob-client | Pinned exactly to 6.14.0 |
