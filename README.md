# Polymarket Engine v3.1

A Polymarket prediction market trading engine. Runs on GitHub Actions free tier.
No VPS. Three zero-infra strategies: S1 (NegRisk arb), S8 (logical arb), S10 (near-resolution).

## Quick start

1. Fork repo, set to Public (unlimited Actions minutes)
2. Add 9 GitHub Secrets (see .env.example for names)
3. Add Variable: DRY_RUN = true
4. Actions → Test Suite → Run workflow (all must pass)
5. Actions → Bootstrap GitHub Issues → BOOTSTRAP (creates task queue)
6. Actions → Market Scanner → Run workflow × 3 (dry-run verification)
7. Set DRY_RUN = false when ready for real trades

## Required GitHub Secrets

| Secret | Source |
|---|---|
| POLYMARKET_API_KEY | polymarket.com → connect wallet → API settings |
| POLYMARKET_API_SECRET | same |
| POLYMARKET_PASSPHRASE | same |
| POLYMARKET_WALLET_ADDRESS | your Polygon wallet address (0x...) |
| POLYMARKET_PRIVATE_KEY | your wallet private key (no 0x prefix) |
| POLYGON_RPC_URL | alchemy.com → create free Polygon Mainnet app |
| ANTHROPIC_API_KEY | console.anthropic.com |
| TELEGRAM_BOT_TOKEN | message @BotFather on Telegram |
| TELEGRAM_CHAT_ID | message @userinfobot on Telegram |

## For AI agents working on tasks

Use `AGENT_SYSTEM_PROMPT.md` as your system prompt.
Then paste the GitHub Issue body for the task you are working on as your first message.
Match the context pack label on the issue to the pack below.

---

## Context Pack A — Strategy Implementation

```
You are implementing ONE trading strategy class that subclasses BaseStrategy.
The engine calls your 4 methods. You do NOT modify any engine file.

KEY TYPES (import from strategies.base and engines.data_engine):
  MarketState: market_id, question, yes_price, no_price, yes_bid, no_bid,
               volume_24h, seconds_to_resolution, category, negrisk_group_id, fee_rate_bps
  Opportunity: strategy, market_id, market_question, action, edge, win_probability,
               max_payout, time_to_resolution_sec, metadata, score
  Resolution:  trade_id, market_id, won, cost_usdc, payout_usdc, pnl_usdc, roi, strategy

RULES:
  scan()       → pure function, no side effects, no API calls (S8 LLM classifier: ok)
  score()      → returns float 0.0–1.0 ONLY
  size()       → MUST cap at config['max_position_pct'] * bankroll, floor at 1.0
  on_resolve() → returns {'won': bool, 'roi': float, 'notes': str, 'lessons': list[str]}
  calc_fee(p)  → ALWAYS use BaseStrategy.calc_fee(p), never hardcode fee values
  FEE FORMULA: 2.25 * 0.25 * (p * (1-p)) ** 2
  calc_fee(0.5) == 0.140625  ← verify this in your tests
```

---

## Context Pack B — Engine Core Component

```
You are implementing a core engine component.

THE SIX ENGINES AND THEIR ROLES:
  data_engine.py       → fetches market data, NEVER trades
  signal_engine.py     → orchestrates scan/score/filter, NEVER trades
  execution_engine.py  → THE ONLY engine that submits orders
  state_engine.py      → SQLite persistence, NEVER trades
  review_engine.py     → Claude API learning loop, NEVER trades
  monitor_engine.py    → Telegram alerts, NEVER trades

CRITICAL:
  Never call order APIs from any engine except execution_engine.py.
  execution_engine.py reads fee_rate_bps from MarketState.fee_rate_bps (set by DataEngine).
  If MarketState is stale (>5 min), re-fetch: clob_client.get_market(token_id)['feeRateBps'].
  There is NO get_fee_rate_bps() method in py-clob-client 0.16 — do not call it.
  All monetary values: USDC. All probabilities: float [0.0, 1.0].
  All external HTTP calls: @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
  All log calls: use structlog with keys: component, action, and relevant context fields.
```

---

## Context Pack C — Test Writing

```
You are writing tests for the Polymarket engine.

RULES:
  Never make real HTTP calls. Mock all HTTP with the 'responses' library.
  Use fixtures from tests/fixtures/sample_markets.json for market data.
  For every strategy, write these 3 scan() test cases:
    a) Market that SHOULD produce an Opportunity (correct thresholds)
    b) Market that should NOT (edge too small / probability below threshold)
    c) Market that should NOT (too far from resolution window)
  Use @freeze_time for any time-sensitive logic.
  Naming: test_[component]_[method]_[scenario]

REQUIRED test for every strategy:
  def test_fee_formula_correct():
      assert abs(BaseStrategy.calc_fee(0.5) - 0.140625) < 0.0001
```

---

## Context Pack D — YAML Config Writing

```
You are writing a strategy YAML config file.

REQUIRED structure:
  strategy: [must match strategy.name exactly]
  enabled: true
  [signal parameters — each with a comment]
  max_position_pct: [default 0.15]
  kelly_fraction: [default 0.25]
  threshold: [min score to execute, float 0.0–1.0]
  review:
    track_metrics: [list]
    rebalance_trigger: weekly

RULES:
  Probabilities: ALWAYS decimals 0.0–1.0, never write "90%", write 0.90.
  Capital: ALWAYS USDC dollars.
  Every value MUST have a comment explaining what it controls.
  S10 max_minutes_remaining: must be > poll_interval_minutes + 15 min buffer.
    With 30-min GHA poll → use 60. With 60-min poll → use 90.
```

---

## Context Pack E — ReviewEngine

```
You are working on engines/review_engine.py.

THE REVIEWER'S CONTRACT:
  Input:   list of recently resolved trades + current lessons.json
  Output:  JSON only: {strategy_score_updates, new_lessons, deprecated_lesson_indices, reasoning}
  Model:   claude-haiku-4-5-20251001 (cost-efficient)

SYSTEM PROMPT RULES (what Claude receives):
  Output ONLY valid JSON — no prose, no markdown
  allocation_delta: max ±0.05 per strategy per cycle
  Min 5 trades before adjusting any allocation
  Lessons must be specific: "S10 sports at p=0.91 loses 29%" not "S10 is risky"

JSON PARSE (mandatory fallback):
  try: updates = json.loads(response_text)
  except: match = re.search(r'\{.*\}', text, re.DOTALL); json.loads(match.group())
  except: return {'status': 'error'}   ← NEVER crash
```
