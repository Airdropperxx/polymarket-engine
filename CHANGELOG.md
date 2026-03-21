# Changelog

## v0.2.0 (March 21, 2026)

### Added
- **backtest.py** - Historical simulation from gamma-api.polymarket.com
  - Supports s10_near_resolution and s1_negrisk_arb strategies
  - Outputs win_rate, total_roi, max_drawdown
  - Results saved to data/backtest_results.json
- **health_check.py** - Diagnostic script to verify all connections
  - Checks: CLOB API, Polygon RPC, Anthropic, Telegram, wallet balance
  - Prints current engine state and configuration
  - Exit 0 if healthy, 1 if any check fails

### Phase 2 (Hardening) Complete
- Error handling audit: all engines have try/except, structlog, and tenacity @retry
- Pre-trade validation complete
- Ready for first real trades

---

## v0.1.0-mvp (March 21, 2026)

### Added
- **MCP Server** - GitHub Memory MCP Server for AI agent task management (10 tools)
- **StateEngine** - SQLite persistence for trades, balance, lessons
- **DataEngine** - REST polling for Polymarket markets with fee_rate_bps
- **ExecutionEngine** - Order submission with 5 risk gates
- **SignalEngine** - Orchestrates scan/score/filter pipeline
- **MonitorEngine** - Telegram alerts for key events
- **ReviewEngine** - AI-powered learning loop with Claude Haiku

### Strategies
- **S10 Near Resolution** - Trade markets within 60 min of resolution
- **S1 NegRisk Arb** - Guaranteed arbitrage on NegRisk group markets
- **S8 Logical Arb** - Cross-market logical violation detection with LLM

### Configuration
- `engine.yaml` - Global engine config with allocations (sum to 1.0)
- Strategy configs: `s10_near_resolution.yaml`, `s1_negrisk.yaml`, `s8_logical.yaml`
- Initial capital: $100 USDC
- Risk limits: 5% daily loss, 5 max positions, 15% max per trade

### GitHub Actions
- `scan.yml` - Market scanning (30-min poll)
- `resolve_check.yml` - Check for market resolutions
- `daily_review.yml` - Daily AI review of trades
- `test.yml` - Run test suite
- `bootstrap.yml` - Bootstrap GitHub issues from TASKS.yaml

### Initial Lessons
1. Fee formula: 2.25 * 0.25 * (p*(1-p))^2
2. NEVER hardcode fee_rate_bps - always read from MarketState
3. S10 sports: min_probability=0.93
4. S1 NegRisk: buy_all_yes when sum(YES prices) < 1.00
5. S8 classifier cache must be committed to repo

---

For older releases, see [release tags](https://github.com/Airdropperxx/polymarket-engine/tags).