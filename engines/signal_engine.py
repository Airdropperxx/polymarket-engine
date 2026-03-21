"""
engines/signal_engine.py
========================
Orchestrates one complete scan cycle.

Does NOT loop — GitHub Actions cron (or local cron) handles scheduling.
Called once per invocation by scripts/run_scan_cycle.py, then exits.

Contract: run_one_cycle() ALWAYS returns a dict, even on total failure.
No exception propagates to the caller.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import yaml
import structlog

from engines.data_engine import DataEngine
from engines.execution_engine import ExecutionEngine
from engines.state_engine import StateEngine
from engines.review_engine import ReviewEngine
from engines.monitor_engine import MonitorEngine
from strategies.base import BaseStrategy, Opportunity

log = structlog.get_logger()


class SignalEngine:
    """
    Orchestrates strategy scanning, scoring, filtering, and execution.
    One instance per scan cycle invocation.
    """

    def __init__(
        self,
        data_engine: DataEngine,
        execution_engine: ExecutionEngine,
        state_engine: StateEngine,
        review_engine: ReviewEngine,
        monitor_engine: MonitorEngine,
        config: dict,
    ) -> None:
        self._data       = data_engine
        self._execution  = execution_engine
        self._state      = state_engine
        self._review     = review_engine
        self._monitor    = monitor_engine
        self._config     = config
        self._strategies: list[tuple[BaseStrategy, dict]] = []  # (strategy, config)

    # -----------------------------------------------------------------------
    # Strategy registration
    # -----------------------------------------------------------------------

    def register(self, strategy: BaseStrategy, config_path: str) -> None:
        """
        Load strategy YAML config and register the strategy.
        Validates required keys exist before registering.
        """
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))

        # Validate required config keys
        required = {"max_position_pct", "kelly_fraction", "threshold"}
        missing = required - set(cfg.keys())
        if missing:
            log.error("signal_engine.config_invalid",
                      strategy=strategy.name, missing=list(missing))
            raise ValueError(f"Strategy config {config_path} missing keys: {missing}")

        self._strategies.append((strategy, cfg))
        log.info("signal_engine.registered", strategy=strategy.name, config=config_path)

    # -----------------------------------------------------------------------
    # Main cycle
    # -----------------------------------------------------------------------

    def run_one_cycle(self) -> dict:
        """
        Execute one complete scan cycle.

        Returns summary dict — ALWAYS, even on failure:
          markets_scanned, negrisk_groups, opportunities_found,
          opportunities_filtered, trades_executed, trades_rejected,
          strategies_errored, resolved_this_cycle, duration_seconds
        """
        t_start = time.time()
        summary = {
            "markets_scanned":        0,
            "negrisk_groups":         0,
            "opportunities_found":    0,
            "opportunities_filtered": 0,
            "trades_executed":        0,
            "trades_rejected":        0,
            "strategies_errored":     0,
            "resolved_this_cycle":    0,
            "duration_seconds":       0.0,
            "strategy_opps": {
                "s10_opportunities": 0,
                "s1_opportunities": 0,
                "s8_opportunities": 0,
            },
        }

        try:
            # 1. Fetch market data
            markets = self._data.fetch_all_markets()
            groups  = self._data.fetch_negrisk_groups()

            summary["markets_scanned"] = len(markets)
            summary["negrisk_groups"]  = len(groups)

            if not markets:
                log.warning("signal_engine.no_markets")
                return summary

            # 2. Build market lookup for ExecutionEngine
            market_map = {m.market_id: m for m in markets}

            # 3. Run all strategy scanners
            all_opps: list[tuple[Opportunity, BaseStrategy, dict]] = []

            for strategy, s_cfg in self._strategies:
                try:
                    opps = strategy.scan(markets, groups, s_cfg)
                    for opp in opps:
                        opp.score = strategy.score(opp, s_cfg)
                    all_opps.extend((opp, strategy, s_cfg) for opp in opps)
                    summary["opportunities_found"] += len(opps)
                    
                    # Track per-strategy opportunities
                    strat_name = strategy.name
                    if "s10" in strat_name:
                        summary["strategy_opps"]["s10_opportunities"] += len(opps)
                    elif "s1" in strat_name:
                        summary["strategy_opps"]["s1_opportunities"] += len(opps)
                    elif "s8" in strat_name:
                        summary["strategy_opps"]["s8_opportunities"] += len(opps)
                except Exception as exc:
                    log.error("signal_engine.scan_error",
                              strategy=strategy.name, error=str(exc))
                    summary["strategies_errored"] += 1
                    self._monitor.send("error", component=strategy.name, error=str(exc))
                    # Continue — one broken strategy never kills the cycle

            # 4. Filter and rank
            threshold_opps = [
                (opp, strat, cfg)
                for (opp, strat, cfg) in all_opps
                if opp.score >= cfg.get("threshold", 0.5)
            ]
            threshold_opps.sort(key=lambda x: x[0].score, reverse=True)
            summary["opportunities_filtered"] = len(threshold_opps)

            # 5. Execute top N
            max_per_cycle = self._config.get("engine", {}).get("max_per_cycle", 3)

            for opp, strategy, s_cfg in threshold_opps[:max_per_cycle]:
                market_state = market_map.get(opp.market_id)
                if market_state is None:
                    log.warning("signal_engine.market_not_found", market_id=opp.market_id)
                    summary["trades_rejected"] += 1
                    continue

                try:
                    trade_id = self._execution.execute_opportunity(
                        opp, strategy, market_state, s_cfg
                    )
                    if trade_id:
                        summary["trades_executed"] += 1
                        self._monitor.send(
                            "trade_executed",
                            strategy=opp.strategy,
                            action=opp.action,
                            question=opp.market_question,
                            size=s_cfg["max_position_pct"] * self._state.get_current_balance(),
                            price=opp.win_probability,
                        )
                    else:
                        summary["trades_rejected"] += 1
                except Exception as exc:
                    log.error("signal_engine.execute_error",
                              market_id=opp.market_id, error=str(exc))
                    summary["trades_rejected"] += 1

            # 6. Check for resolved positions
            resolved = self._check_resolutions(market_map)
            summary["resolved_this_cycle"] = len(resolved)
            for market_id in resolved:
                self._review.run_after_resolution(market_id)

            # 7. Save scan results for dashboard
            self._save_scan_results(markets, summary)

        except Exception as exc:
            # Outer safety net — should never reach here
            log.error("signal_engine.cycle_error", error=str(exc))
            self._monitor.send("error", component="signal_engine", error=str(exc))

        summary["duration_seconds"] = round(time.time() - t_start, 2)
        log.info("signal_engine.cycle_complete", **summary)
        return summary

    # -----------------------------------------------------------------------
    # Resolution detection
    # -----------------------------------------------------------------------

    def _check_resolutions(self, market_map: dict) -> list[str]:
        """
        Detect open positions whose markets no longer appear in the active market list.
        A market disappearing from the active feed is a strong signal it has resolved.
        Returns list of market_ids that appear to have resolved.
        """
        open_positions = self._state.get_open_positions()
        resolved_ids: list[str] = []

        for pos in open_positions:
            mid = pos["market_id"]
            if mid not in market_map:
                # Market gone from active feed — likely resolved
                # Mark as resolved with unknown outcome; check_resolutions.py
                # does the authoritative resolution lookup via CLOB API.
                log.info("signal_engine.possible_resolution", market_id=mid)
                resolved_ids.append(mid)

        return resolved_ids

    def _save_scan_results(self, markets: list, summary: dict) -> None:
        """Save scan results for dashboard."""
        try:
            from pathlib import Path

            categories = {"sports": 0, "politics": 0, "crypto": 0, "other": 0}
            for m in markets:
                cat = getattr(m, "category", "other") if hasattr(m, "category") else "other"
                categories[cat] = categories.get(cat, 0) + 1

            markets_valid = sum(1 for m in markets if 0.01 <= getattr(m, "yes_price", 0.5) <= 0.99)

            data = {
                "ingestion": {
                    "markets_scanned": summary.get("markets_scanned", 0),
                    "markets_valid": markets_valid,
                    "negrisk_groups": summary.get("negrisk_groups", 0),
                    "categories": categories,
                },
                "strategies": summary.get("strategy_opps", {
                    "s10_opportunities": 0,
                    "s1_opportunities": 0,
                    "s8_opportunities": 0,
                }),
                "trades_executed": summary.get("trades_executed", 0),
                "trades_rejected": summary.get("trades_rejected", 0),
            }

            Path("data").mkdir(exist_ok=True)
            with open("data/last_scan.json", "w") as f:
                json.dump(data, f)

            log.info("signal_engine.scan_results_saved")
        except Exception as exc:
            log.error("signal_engine.save_scan_error", error=str(exc))
