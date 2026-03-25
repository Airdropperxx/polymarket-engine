# TASK-F: Future improvements (post-stabilisation)
## Status: BACKLOG | Do after TASK-A through TASK-E complete

## F1: S11 Momentum activation
After 20+ scan cycles, enable S11. Needs price_history.json with 2+ observations per market.
Files: strategies/s11_inplay_momentum.py, configs/s11_inplay_momentum.yaml

## F2: Pattern analysis
After 50+ resolved trades, create scripts/analyze_patterns.py:
- Time-of-day win distribution
- Category edge curves (crypto vs sports vs politics)
- Optimal probability range analysis
Output: data/pattern_analysis.json

## F3: ReviewEngine verification
Verify engines/review_engine.py runs after each resolution and writes lessons.json.
Lessons should appear on dashboard Lessons tab.

## F4: Alert system
Wire up MonitorEngine: Telegram/email alert on resolution, daily summary, balance drop alert.

## F5: Live trading readiness checklist
- Kelly-fraction position sizing
- Max 5% per position
- CLOB API key rotation
- Slippage tolerance config
