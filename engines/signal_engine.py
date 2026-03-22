"""
engines/signal_engine.py — Strategy orchestrator. NEVER places orders.

run_one_cycle() ALWAYS returns a dict, even on total failure.
Wraps every strategy.scan() in try/except — one broken strategy
cannot crash the cycle.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from strategies.base import BaseStrategy, Opportunity
from engines.data_engine import MarketState

log = structlog.get_logger(component="signal_engine")


class SignalEngine:
    def __init__(self, config: dict):
        self.config:    dict                 = config
        self.strategies: list[BaseStrategy]  = []

    def register(self, strategy: BaseStrategy) -> None:
        """Register a strategy instance."""
        self.strategies.append(strategy)
        log.info("strategy_registered", name=strategy.name)

    def run_one_cycle(self,
                      markets: list[MarketState],
                      groups:  dict[str, list[MarketState]]) -> dict:
        """
        Scan all strategies, score, filter, rank.
        Returns summary dict. NEVER raises.
        """
        start       = time.time()
        all_opps:   list[Opportunity] = []
        scan_errors = 0

        for strategy in self.strategies:
            # Get strategy-specific config
            strat_cfg = self.config.get(strategy.name, self.config)

            if not strat_cfg.get("enabled", True):
                log.info("strategy_disabled", name=strategy.name)
                continue

            try:
                opps = strategy.scan(markets, groups, self.config)
            except Exception as e:
                log.error("strategy_scan_failed",
                          strategy=strategy.name, error=str(e))
                scan_errors += 1
                continue

            # Score each opportunity
            for opp in opps:
                try:
                    opp.score = strategy.score(opp, self.config)
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
