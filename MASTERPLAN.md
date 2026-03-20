# POLYMARKET ENGINE v3 — MASTER BUILD PLAN
**Version:** 3.1 (Post-Audit) | **Capital:** $100 USDC | **Arch:** GitHub Actions + Local Cron | **Date:** March 2026

---

## WHAT THIS IS

A Polymarket prediction market trading engine. Three zero-infra strategies from day one.
Runs on GitHub Actions free tier (public repo) OR local cron (maximum security).
No VPS. No WebSocket. No paid subscriptions in MVP.

---

## PART 0 — HARD CONSTRAINTS

```
CAPITAL:        $100 USDC starting. Every dollar is real.
LANGUAGE:       Python 3.11+. No Rust. No compiled languages in MVP.
INFRA COST:     $0/month for MVP. Free tiers only.
STRATEGIES:     Only zero-infra strategies until capital > $1,000.
RISK:           Max 15% capital per trade. Max 5% daily loss → engine halts.
FEES:           ALWAYS read fee_rate_bps from MarketState. NEVER hardcode.
                Re-fetch from clob_client.get_market() if MarketState is stale (>5 min).
DRY_RUN:        3 full cycles DRY_RUN=true before ANY real trade.
POLL INTERVAL:  30 min (GitHub Actions public repo) or 5 min (local cron).
```

---

## PART 1 — SECURITY & DEPLOYMENT

### GitHub Secrets on Public Repos Are Safe

GitHub encrypts secrets with libsodium (NaCl box) using a repo-specific public key.
The decrypted value exists ONLY inside the runner process during that job.
It is auto-masked in logs. Forks do NOT inherit secrets. This is standard practice.

What the public sees in your workflow file:
```yaml
POLYMARKET_PRIVATE_KEY: ${{ secrets.POLYMARKET_PRIVATE_KEY }}   # just the name
```
What runs inside the job (never logged, never visible):
```
POLYMARKET_PRIVATE_KEY=your_actual_key
```

### Deployment Options

| Option | Security | Cost | Uptime | Best For |
|--------|----------|------|--------|----------|
| **A: Public repo + GHA Secrets** | ✅ Keys safe | $0 | ✅ 99.9% | Simplest setup |
| **B: Private repo + GHA** | ✅ Code+keys | $0 at 90-min poll | ✅ 99.9% | Code privacy |
| **C: Local cron + private repo** | ✅ Max security | $0 | ⚠️ Machine uptime | Full control |
| **D: Fully local (no GitHub)** | ✅ Max security | $0 | ⚠️ Machine uptime | Air-gapped |

**GitHub Actions Minutes by Poll Interval (private repo free tier = 2,000 min/month):**
```
30-min poll:  ~3,330 min/month → exceeds free private tier
60-min poll:  ~2,250 min/month → exceeds free private tier (barely)
90-min poll:  ~1,770 min/month → fits free private tier
Public repo:  unlimited minutes regardless of poll interval
```

**Local cron setup (Mac/Linux):** `crontab -e`
```cron
*/30 * * * *  cd /path/to/polymarket-engine && python scripts/run_scan_cycle.py >> logs/scan.log 2>&1
0 * * * *     cd /path/to/polymarket-engine && python scripts/check_resolutions.py >> logs/resolve.log 2>&1
0 0 * * *     cd /path/to/polymarket-engine && python scripts/run_daily_review.py >> logs/review.log 2>&1
```

---

## PART 2 — STRATEGIES: FINAL TABLE

### ✅ Active Strategies

| ID | Name | Engine Type | Capital | Infra | Win Rate | Phase |
|----|------|-------------|---------|-------|----------|-------|
| S1 | NegRisk Arbitrage | ArbEngine | $10+ | $0 | 100% | **MVP** |
| S8 | Logical Impossibility Arb | ArbEngine | $50+ | $0 | 80–90% | **MVP** |
| S10 | Near-Resolution Harvest | SignalEngine | $100+ | $0 | 88–95% | **MVP** |
| S6 | Synth AI (Bittensor SN50) | SignalEngine | $1,000+ | $200/mo | 62–72% | Phase 3 |
| S5 | Market Making + Rebates | MakerEngine | $2,500+ | $52/mo VPS | 55–70% | Phase 4 |
| S7 | Options Implied Prob Arb | ArbEngine | $5,000+ | $150/mo | 60–68% | Phase 4 |

### ❌ Permanently Removed

| ID | Name | Reason |
|----|------|--------|
| S2 | YES+NO Spread | Dynamic fee (Jan 2026) exceeds spread at p=0.50. Dead math. |
| S3 | Latency Arb | 73% profit to sub-100ms co-located bots. Requires Rust + bare metal. |
| S9 | News Speed Arb | $400+/mo infra before first dollar earned. Not viable under $5K. |
| **S4** | **Chainlink Sniper** | **Requires <2 min reaction. Incompatible with 30–90 min cron polling.** Reintroduce only with persistent WebSocket process on VPS. |

---

## PART 3 — ARCHITECTURE

### The Six Engines

```
┌─────────────────────────────────────────────────────────────────────┐
│                    POLYMARKET ENGINE v3.1                           │
│                                                                     │
│  ┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │  DATA ENGINE    │  │  SIGNAL ENGINE   │  │ EXECUTION ENGINE │  │
│  │                 │  │                  │  │                  │  │
│  │ REST poller     │→ │ Strategy registry│→ │ 5 risk gates     │  │
│  │ Market cache    │  │ scan→score→rank  │  │ Order signer     │  │
│  │ NegRisk groups  │  │ Exception shield │  │ CLOB submitter   │  │
│  │ fee_rate_bps ✓  │  │ max_per_cycle    │  │ DRY_RUN guard    │  │
│  └─────────────────┘  └──────────────────┘  └──────────────────┘  │
│           │                    │                      │            │
│  ┌─────────────────────────────▼──────────────────────▼──────────┐ │
│  │                      STATE ENGINE                             │ │
│  │   trades.db (SQLite) | lessons.json | positions (in-memory)  │ │
│  └───────────────────────────────────────┬────────────────────── ┘ │
│                                          │                         │
│  ┌───────────────────────────────────────▼───────────────────────┐ │
│  │                   REVIEW ENGINE (AI Reviewer)                 │ │
│  │   Trigger: every resolution + daily 00:00 UTC                │ │
│  │   Model: claude-haiku-4-5-20251001                           │ │
│  │   Output: lessons.json + allocation adjustments              │ │
│  └───────────────────────────────────────┬────────────────────── ┘ │
│                                          │                         │
│  ┌───────────────────────────────────────▼───────────────────────┐ │
│  │                   MONITOR ENGINE                              │ │
│  │   Telegram alerts | structured logs | GitHub commits         │ │
│  └─────────────────────────────────────────────────────────────  ┘ │
└─────────────────────────────────────────────────────────────────────┘
```

### Engine Contracts (Locked — Do Not Change)

```
DataEngine
  Input:   config (poll_interval, endpoints)
  Output:  list[MarketState]  ← includes fee_rate_bps per market
           dict[group_id, list[MarketState]]  ← NegRisk groups (2+ markets only)
  Rules:   NEVER raises. Returns stale cache on error. NEVER trades.

SignalEngine
  Input:   list[MarketState], negrisk_groups, registered strategies + configs
  Output:  dict (cycle summary — markets_scanned, opps_found, trades_executed, etc.)
  Rules:   Wraps every strategy.scan() in try/except. NEVER trades. NEVER raises.
           run_one_cycle() ALWAYS returns a dict, even on total failure.

ExecutionEngine
  Input:   Opportunity, BaseStrategy ref, MarketState (for fee_rate_bps)
  Output:  trade_id (str) if executed/dry-run | None if rejected
  Rules:   ONLY engine that submits orders. 5 risk gates in fixed order.
           DRY_RUN=true → NEVER submits. fee_rate_bps NEVER hardcoded.

StateEngine
  Input:   TradeRecord writes, resolution events, balance updates
  Output:  daily_pnl, open_positions, balance, lessons
  Rules:   NEVER calls external APIs. NEVER raises on read operations.
           get_daily_pnl() returns 0.0 on empty DB.

ReviewEngine
  Input:   recent resolved trades, existing lessons.json
  Output:  updated lessons.json + allocation deltas (±5% max per cycle)
  Rules:   NEVER trades. NEVER raises. Returns {'status':'skipped'} if 0 trades.
           JSON parse has try/except + regex fallback.

MonitorEngine
  Input:   event_type + kwargs
  Output:  Telegram message (fire-and-forget)
  Rules:   NEVER raises. Silent if TELEGRAM_BOT_TOKEN missing. Messages < 200 chars.
```

### Repository Structure

```
polymarket-engine/
├── MASTERPLAN.md                    ← This document
├── TASKS.yaml                       ← 28 tasks, source of truth for GitHub Issues
├── CHANGELOG.md
├── README.md
│
├── engines/
│   ├── __init__.py
│   ├── data_engine.py               ← REST polling + MarketState + fee_rate_bps
│   ├── signal_engine.py             ← Orchestrator, run_one_cycle(), exception shield
│   ├── execution_engine.py          ← ONLY engine that touches real money
│   ├── state_engine.py              ← SQLite + lessons.json persistence
│   ├── review_engine.py             ← Claude AI learning loop
│   └── monitor_engine.py            ← Telegram alerts
│
├── strategies/
│   ├── base.py                      ← BaseStrategy, Opportunity, Resolution dataclasses
│   ├── s1_negrisk_arb.py            ← NegRisk multi-outcome arb [MVP]
│   ├── s8_logical_arb.py            ← Embedding + LLM logical impossibility [MVP]
│   ├── s10_near_resolution.py       ← Near-certain outcome harvest [MVP]
│   ├── s6_synth_ai.py               ← Bittensor SN50 [Phase 3, $1K+]
│   ├── s5_market_maker.py           ← Market making + rebates [Phase 4, $2.5K+]
│   └── s7_options_arb.py            ← Deribit options arb [Phase 4, $5K+]
│
├── configs/
│   ├── engine.yaml                  ← Global config: capital, risk, allocations
│   ├── s1_negrisk.yaml
│   ├── s8_logical.yaml
│   ├── s10_near_resolution.yaml     ← max_minutes_remaining = 60 (FIXED)
│   └── .env.example
│
├── data/
│   ├── lessons.json                 ← Git-tracked. ReviewEngine memory.
│   ├── s8_direction_cache.json      ← Git-tracked. S8 LLM classifier cache.
│   ├── trades.db                    ← Git-tracked (binary). SQLite trade log.
│   └── market_cache/                ← Git-ignored.
│
├── tests/
│   ├── test_engines.py
│   ├── test_strategies.py
│   ├── test_integration.py
│   └── fixtures/
│       └── sample_markets.json      ← 5 sample markets covering all test cases
│
├── scripts/
│   ├── run_scan_cycle.py            ← Entry point for scan.yml / local cron
│   ├── check_resolutions.py         ← Entry point for resolve_check.yml
│   ├── run_daily_review.py          ← Entry point for daily_review.yml
│   ├── scan_once.py                 ← Manual: scan + print, no trades
│   ├── backtest.py                  ← Historical simulation
│   ├── health_check.py              ← Verify all connections
│   └── bootstrap_github.py          ← Create GitHub Issues from TASKS.yaml
│
├── mcp_server/                      ← Runs LOCALLY on dev machine. NOT on GHA.
│   ├── server.py                    ← stdio MCP server, 10 tools
│   ├── github_ops.py
│   ├── task_manager.py
│   ├── audit.py
│   └── memory.py
│
├── .github/
│   └── workflows/
│       ├── scan.yml                 ← Every 30 min. HF model cached. timeout=15min.
│       ├── resolve_check.yml        ← Every 60 min. Separate concurrency group.
│       ├── daily_review.yml         ← 00:00 UTC. Separate concurrency group.
│       ├── test.yml                 ← Every push/PR.
│       └── bootstrap.yml            ← workflow_dispatch only.
│
├── requirements.txt                 ← chromadb REMOVED
├── requirements-dev.txt
├── .gitignore                       ← includes *.db-wal, *.db-shm
└── .env                             ← Git-ignored always
```

---

## PART 4 — DATA FLOW (One Complete Cycle)

```
SCAN CYCLE (every 30 min via GHA cron or local cron):

1. scripts/run_scan_cycle.py starts
2. DataEngine.fetch_all_markets()
   → GET https://gamma-api.polymarket.com/markets?active=true&closed=false
   → Parse into list[MarketState] (includes fee_rate_bps per market)
   → Update NegRisk groups cache
3. SignalEngine.run_one_cycle()
   → For each registered strategy:
       try: opps = strategy.scan(markets, groups, config)
       except: log error, continue (never crash)
       for opp in opps: opp.score = strategy.score(opp, config)
   → Merge all opps → filter score >= threshold → sort descending
   → Take top max_per_cycle
4. For each qualifying opp:
   ExecutionEngine.execute_opportunity(opp, strategy, market_state)
   → Gate 1: daily_pnl check
   → Gate 2: open position count check
   → Gate 3: min size check ($1 floor)
   → Gate 4: DRY_RUN guard
   → Gate 5: read fee_rate_bps from market_state (already fetched in step 2)
              re-fetch via clob_client.get_market() only if MarketState > 5 min old
   → Sign order → submit → log to StateEngine → alert via MonitorEngine
5. StateEngine: check for resolved markets → trigger ReviewEngine
6. scripts/run_scan_cycle.py exits
7. GitHub Actions: git add data/ → git commit → git push

RESOLUTION CYCLE (every 60 min):
1. scripts/check_resolutions.py
2. StateEngine.get_open_positions()
3. For each: clob_client.get_market(token_id) → check if resolved
4. If resolved: StateEngine.mark_resolved() → ReviewEngine.run_after_resolution()
5. Commit state

DAILY REVIEW (00:00 UTC):
1. scripts/run_daily_review.py
2. ReviewEngine.run_daily_review() (7-day trade window)
3. Claude Haiku API → parse JSON → apply lesson updates
4. MonitorEngine: send daily_summary + lesson_update alerts
5. Commit lessons.json + engine.yaml
```

---

## PART 5 — FEE FORMULA (Canonical — Never Change)

```python
fee = 2.25 × 0.25 × (p × (1 − p))²

# Verification (these must always hold):
assert abs(calc_fee(0.5)  - 0.140625) < 0.0001   # 1.40625% — peak
assert abs(calc_fee(0.95) - 0.000127) < 0.00005  # ~0.013%
assert calc_fee(0.0) == 0.0
assert calc_fee(1.0) == 0.0

# At p=0.50: 1.4063% — avoid as taker
# At p=0.90: 0.0456%
# At p=0.95: 0.0127%

# fee_rate_bps in MarketState = this formula × 10000, fetched from Polymarket API
# ALWAYS use MarketState.fee_rate_bps for order construction
# NEVER hardcode feeRateBps = 0 or any fixed value
```

---

## PART 6 — CAPITAL PROGRESSION

```
Phase    Capital     Strategy Mix                  Infra/mo
──────────────────────────────────────────────────────────
MVP      $100        S1(40%) S8(20%) S10(40%)      $0
P2       $150–500    Same + backtested              $0
P3       $1,000+     Add S6 (Synth AI)              $200
P4       $2,500+     Add S5 (Market Making)         $52 VPS
P5       $5,000+     Add S7 (Options Arb)           $202

Checkpoint 1: $100 → $150
  □ 10+ real trades per strategy
  □ Reviewer has run 3+ cycles
  □ lessons.json has 8+ lessons
  □ No strategy has lost >30% of its allocated capital

Checkpoint 2: $150 → $300
  □ Win rates within ±10% of backtested predictions
  □ Backtest completed for S1 and S10

Checkpoint 3: $300 → $1,000
  □ Consider S6 (Synth AI, $200/mo — justified at this capital)
  □ Consider moving to local cron if on GitHub Actions (more control)

Checkpoint 4: $1,000+
  □ S6 active. Monitor for 50+ trades before adding S5.
```

---

## PART 7 — TECH STACK

```
Language:        Python 3.11+
Blockchain:      Polygon PoS (Chain ID: 137)
SDK:             py-clob-client==0.16.0
                 web3==6.14.0  ← PIN EXACT. Newer versions break py-clob-client.
HTTP:            requests==2.31.0, tenacity==8.2.3 (retry)
AI (reviewer):   anthropic==0.25.0 → claude-haiku-4-5-20251001
AI (S8):         anthropic==0.25.0 → claude-haiku-4-5-20251001 (classification)
NLP (S8):        sentence-transformers==2.7.0 (all-MiniLM-L6-v2, 80MB)
                 numpy (cosine similarity — already installed by sentence-transformers)
                 chromadb REMOVED — state lost between ephemeral GHA runners
Database:        sqlalchemy==2.0.28 (SQLite)
Logging:         structlog==24.1.0
Config:          python-dotenv==1.0.1, pyyaml==6.0.1
Telegram:        python-telegram-bot==21.0
MCP:             PyGithub==2.1.1 (for mcp_server GitHub API calls)
Testing:         pytest==8.1.0, pytest-asyncio, pytest-cov, responses, freezegun
```

---

## PART 8 — VERIFY BEFORE ANY REAL TRADE

```bash
# 1. Fee formula
python -c "
from strategies.base import BaseStrategy
assert abs(BaseStrategy.calc_fee(0.5) - 0.140625) < 0.0001, 'FAIL'
print('fee formula OK')
"

# 2. Allocations sum to 1.0
python -c "
import yaml
cfg = yaml.safe_load(open('configs/engine.yaml'))
t = sum(cfg['allocations'].values())
assert abs(t - 1.0) < 0.001, f'FAIL: {t}'
print('allocations OK:', cfg['allocations'])
"

# 3. No hardcoded fee rates in source
grep -rn "feeRateBps\s*=\s*[0-9]" engines/ strategies/ && echo "FAIL: hardcoded fee" || echo "fee rate OK"

# 4. No secrets in tracked files
git diff --cached | grep -i "private_key\|api_secret\|passphrase" && echo "FAIL: key in commit" || echo "secrets OK"

# 5. SQLite WAL files not tracked
cat .gitignore | grep "db-wal" && echo "WAL gitignored OK" || echo "FAIL: add *.db-wal to .gitignore"

# 6. DRY_RUN 3-cycle test
DRY_RUN=true python scripts/run_scan_cycle.py
DRY_RUN=true python scripts/run_scan_cycle.py
DRY_RUN=true python scripts/run_scan_cycle.py
# All 3 must complete. Check logs show DRY_RUN_ prefix on any trade_ids.
```
