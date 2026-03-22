# Changelog

## v3.2.0 (March 2026) — Data Collection & Observability

### Added
- **MarketObserver engine** (`engines/market_observer.py`)
  - Polls top 200 markets every scan cycle (every 30 min)
  - Records price time-series to `data/price_history.json` (committed to repo)
  - Detects signals: momentum_up, momentum_down, sharp_move, resolution_drift, volume_spike
  - No separate workflow needed — runs embedded in scan cycle
  - After 20+ cycles builds dataset for: momentum trading, mean-reversion fading,
    smart-money following, NegRisk divergence early detection

- **scan_log.json pattern dataset**
  - Every opportunity (executed or skipped) logged with: edge, probability, category,
    volume_24h, spread, fee, days_to_resolution, hour_utc, weekday
  - Enables: time-of-day analysis, category edge curves, optimal probability range detection

- **6-tab dashboard** at `index.html` (GitHub Pages)
  - Overview, Trades, Data Collected, Pattern Analysis, Observer Signals, Strategy Refinement
  - Reads live from GitHub raw — no backend needed
  - Category colour-coded pills, full question text on hover

### Fixed
- **categories now populated** — S1 NegRisk calculates dominant category from legs
- **resolved markets filtered** — data_engine skips outcomePrices >= 0.999
- **multi-doc YAML removed** — strategies.yaml deleted, individual s*.yaml files used
- **Dashboard .nojekyll** — renamed from `nojekyll` to `.nojekyll` (dot prefix required)
- **S1 win_probability=1.0** — correct for NegRisk arb (mathematical guarantee), dashboard handles it
- **PYTHONPATH** — added to scan.yml so engines/ and strategies/ are importable

---

## v3.1.0 (March 2026) — Pipeline Fixes

### Fixed
- **Dependency resolution** — replaced full requirements.txt with requirements-scan.txt (7 packages)
- **ModuleNotFoundError** — added PYTHONPATH=${{ github.workspace }} to scan.yml
- **Gamma API parsing** — outcomePrices is JSON string, not array; clobTokenIds same
- **Fee formula** — corrected to `2.25 * (p*(1-p))^2`, was `2.25*0.25*...`
- **Fee application** — fee is position-level, must multiply by buy_price for per-share
- **S10 time filter** — max_minutes_remaining compared as seconds not minutes (×60 fix)

### Changed
- Data engine fetches all active markets (no end_date_max filter — was too restrictive)
- S10 window widened to 7 days, min_probability lowered to 0.85 to capture real opportunities
- max_per_cycle raised to 10, open position cap to 50 for dry-run data collection

---

## v0.3.0 (March 21, 2026) — Strategy Expansion

### Added
- S4 Chainlink Sniper (deferred — requires WebSocket, incompatible with GHA polling)
- S6 Synth AI / Bittensor SN50 (deferred — requires $1k capital + $200/mo infra)

---

## v0.2.0 (March 21, 2026) — Hardening

### Added
- Backtest scripts for S1 and S10
- Health check script
- Error handling audit
- Tag v0.2.0

---

## v0.1.0 (March 21, 2026) — MVP

### Added
- All six engines: data, signal, execution, state, review, monitor
- Three MVP strategies: S10, S1 (partial), S8 (disabled)
- GitHub Actions workflows: scan, resolve_check, daily_review, test, bootstrap
- MCP server for GitHub Issues task management
- Bootstrap script creating all task queue issues
