"""
strategies/base.py
==================
Abstract base class and shared dataclasses for all trading strategies.

ENGINE CALL ORDER PER CYCLE:
    scan() → score() → [filter above threshold] → size() → execute → on_resolve()

RULES FOR ALL STRATEGIES:
    scan()       → pure function, no side effects, no network calls
                   (S8 LLM classifier is the only permitted exception)
    score()      → returns float in [0.0, 1.0] ONLY
    size()       → MUST cap at config['max_position_pct'] * bankroll
                   MUST floor at 1.0 (minimum $1 trade)
    on_resolve() → returns dict with keys: won, roi, notes, lessons
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Shared dataclasses — imported by engines and strategies alike
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    """A single trading opportunity returned by strategy.scan()."""

    strategy: str
    market_id: str
    market_question: str
    action: str             # 'buy_yes' | 'buy_no' | 'buy_all_yes' | 'sell_all_yes'
    edge: float             # Net expected profit as fraction of cost (0.05 = 5%), after fees
    win_probability: float  # 0.0–1.0. Use 1.0 for guaranteed NegRisk arb.
    max_payout: float       # Payout per $1 invested on win (e.g. 1/0.93 ≈ 1.075)
    time_to_resolution_sec: int
    metadata: dict = field(default_factory=dict)
    score: float = 0.0      # Filled by strategy.score() after scan()

    def __post_init__(self) -> None:
        if self.edge <= 0:
            raise ValueError(f"edge must be > 0, got {self.edge:.6f}")
        if not (0.0 <= self.win_probability <= 1.0):
            raise ValueError(f"win_probability must be in [0, 1], got {self.win_probability}")
        if self.max_payout < 1.0:
            raise ValueError(f"max_payout must be >= 1.0, got {self.max_payout}")
        if self.action not in ("buy_yes", "buy_no", "buy_all_yes", "sell_all_yes"):
            raise ValueError(f"Unknown action: {self.action!r}")


@dataclass
class Resolution:
    """Passed to strategy.on_resolve() when a market settles."""

    trade_id: str
    market_id: str
    won: bool
    cost_usdc: float
    payout_usdc: float
    pnl_usdc: float
    roi: float          # pnl_usdc / cost_usdc
    strategy: str
    notes: str = ""


# ---------------------------------------------------------------------------
# BaseStrategy
# ---------------------------------------------------------------------------

class BaseStrategy(ABC):
    """
    Abstract base class every strategy must implement.
    The engine calls these 4 methods — strategies NEVER call the engine.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier. Must match key in engine.yaml allocations."""
        ...

    @abstractmethod
    def scan(
        self,
        markets: list,          # list[MarketState] from DataEngine
        negrisk_groups: dict,   # dict[group_id, list[MarketState]]
        config: dict,           # this strategy's YAML config dict
    ) -> list[Opportunity]:
        """
        Scan current market state for opportunities.
        CONTRACT:
          - Pure function: no side effects, no external state mutation.
          - No network calls (S8 LLM classifier is the only permitted exception).
          - Returns [] (empty list), never None, if no opportunities found.
          - Never raises: log and return [] on any internal error.
        """
        ...

    @abstractmethod
    def score(self, opp: Opportunity, config: dict) -> float:
        """
        Score an opportunity from 0.0 to 1.0.
        CONTRACT:
          - Returns float in [0.0, 1.0] ONLY. Engine filters on this value.
          - Higher = better quality opportunity.
          - Default: opp.edge * opp.win_probability (edge-adjusted quality).
        """
        ...

    @abstractmethod
    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        """
        Calculate position size in USDC.
        CONTRACT:
          - MUST cap at config['max_position_pct'] * bankroll (hard ceiling).
          - MUST floor at 1.0 (never return less than $1).
          - Returns USDC amount to deploy.
        """
        ...

    @abstractmethod
    def on_resolve(self, resolution: Resolution) -> dict:
        """
        Called when a market resolves. Return lessons for the ReviewEngine.
        CONTRACT:
          - Must return dict with EXACTLY these keys:
            {'won': bool, 'roi': float, 'notes': str, 'lessons': list[str]}
          - 'lessons' may be an empty list if nothing to learn.
        """
        ...

    # ------------------------------------------------------------------
    # Shared helpers — available to all strategies, no override needed
    # ------------------------------------------------------------------

    @staticmethod
    def calc_fee(p: float) -> float:
        """
        Polymarket dynamic taker fee formula (canonical, post-Jan 2026).

        fee = 2.25 × 0.25 × (p × (1 − p))²

        Verification (these must always hold):
            calc_fee(0.5)  ≈ 0.140625  (1.40625% — peak fee, worst for takers)
            calc_fee(0.95) ≈ 0.000127  (~0.013%)
            calc_fee(0.0)  == 0.0
            calc_fee(1.0)  == 0.0

        Args:
            p: probability in [0.0, 1.0]

        Returns:
            Fee rate as a fraction (0.02 = 2%). NOT in basis points.
        """
        return 2.25 * 0.25 * (p * (1.0 - p)) ** 2

    @staticmethod
    def calc_kelly_size(
        win_probability: float,
        payout_ratio: float,
        bankroll: float,
        kelly_fraction: float,
        max_position_pct: float,
    ) -> float:
        """
        Fractional Kelly criterion with hard cap and $1 floor.

        kelly_fraction=0.25 → quarter-Kelly (conservative default).
        kelly_fraction=0.50 → half-Kelly (for guaranteed arbs like S1).

        Args:
            win_probability: estimated probability of winning [0.0, 1.0]
            payout_ratio:    payout per $1 on win (>= 1.0)
            bankroll:        available capital in USDC
            kelly_fraction:  multiplier on full Kelly (0.25 = quarter-Kelly)
            max_position_pct: hard ceiling as fraction of bankroll

        Returns:
            USDC position size. Always >= 1.0, always <= bankroll * max_position_pct.
        """
        if payout_ratio <= 1.0:
            return 1.0
        # Full Kelly: f* = p - (1-p) / (b-1)   where b = payout_ratio
        kelly = win_probability - (1.0 - win_probability) / (payout_ratio - 1.0)
        kelly = max(0.0, kelly)
        size = bankroll * kelly * kelly_fraction
        return max(1.0, min(size, bankroll * max_position_pct))
