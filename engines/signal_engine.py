"""
engines/signal_engine.py — Strategy orchestrator. NEVER places orders.

run_one_cycle() ALWAYS returns a dict, even on total failure.
Wraps every strategy.scan() in try/except — one broken strategy
cannot crash the cycle.

v2: accepts observer_hints dict so strategies can use pre-filtered signal
    lists instead of brute-force scanning all markets.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from strategies.base import BaseStrategy, Opportunity
from engines.data_engine import MarketState

log = structlog.get_logger(component="signal_engine")


class SignalEngine:
    def __init__(self, config: dict, data_engine=None, **kwargs):
        self.config:    dict                 = config
        self.strategies: list[BaseStrategy]  = []

    def register(self, strategy: BaseStrategy) -> None:
        """Register a strategy instance."""
        self.strategies.append(strategy)
        log.info("strategy_registered", name=strategy.name)

    def run_one_cycle(self,
                      markets:        list[MarketState],
                      groups:         dict[str, list[MarketState]],
                      observer_hints: dict | None = None) -> dict:
        """
        Scan all strategies, score, filter, rank.
        observer_hints — optional dict built from MarketObserver signals:
          {
            "resolution_drift":    [market_id, ...],   # S10 priority list
            "momentum_up":         [market_id, ...],   # S11 priority list
            "momentum_down":       [market_id, ...],   # S11 priority list
            "sharp_move":          [market_id, ...],   # S11 reversion candidates
            "negrisk_imbalance":   [market_id, ...],   # S1 fast-path groups
            "volume_spike":        [market_id, ...],   # any strategy can use
          }
        Strategies read hints via config["observer_hints"] injected per-cycle.
        Returns summary dict. NEVER raises.
        """
        start       = time.time()
        all_opps:   list[Opportunity] = []
        scan_errors = 0

        # Inject observer hints into config snapshot for this cycle only
        cycle_config = dict(self.config)
        if observer_hints:
            cycle_config["observer_hints"] = observer_hints
            log.info("observer_hints_injected",
                     types=list(observer_hints.keys()),
                     total_markets=sum(len(v) for v in observer_hints.values()))

        for strategy in self.strategies:
            # Get strategy-specific config
            strat_cfg = cycle_config.get(strategy.name, cycle_config)

            if not strat_cfg.get("enabled", True):
                log.info("strategy_disabled", name=strategy.name)
                continue

            try:
                opps = strategy.scan(markets, groups, cycle_config)
            except Exception as e:
                log.error("strategy_scan_failed",
                          strategy=strategy.name, error=str(e))
                scan_errors += 1
                continue

            # Score each opportunity
            for opp in opps:
                try:
                    opp.score = strategy.score(opp, cycle_config)
                except Exception as e:
                    log.warning("strategy_score_failed",
                                strategy=strategy.name, error=str(e))
                    opp.score = 0.0

            # Apply threshold filter
            threshold = float(strat_cfg.get("threshold", 0.50))
            passing   = [o for o in opps if o.score >= threshold]

            log.info("strategy_scan_done",
                     strategy=strategy.name,
                     found=len(opps),
                     passing_threshold=len(passing),
                     threshold=threshold)

            all_opps.extend(passing)

        # Sort all opportunities by score descending
        all_opps.sort(key=lambda o: o.score, reverse=True)

        elapsed = round(time.time() - start, 2)
        return {
            "markets_scanned":   len(markets),
            "negrisk_groups":    len(groups),
            "opportunities":     all_opps,
            "opps_found":        len(all_opps),
            "scan_errors":       scan_errors,
            "strategies_run":    len(self.strategies),
            "elapsed_sec":       elapsed,
        }
