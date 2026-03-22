"""
strategies/base.py — BaseStrategy, Opportunity, Resolution dataclasses.

All strategies inherit BaseStrategy and implement:
  scan()       → list[Opportunity]     — pure, no side effects
  score()      → float [0.0, 1.0]
  size()       → float USDC           — must cap at max_position_pct * bankroll
  on_resolve() → Resolution

Fee formula (canonical — never change):
  fee = 2.25 × 0.25 × (p × (1 − p))²
  calc_fee(0.5)  == 0.140625   ← peak, 1.41% of position
  calc_fee(0.95) ≈ 0.000127    ← tiny, 0.013%
  calc_fee(0.0)  == 0.0
  calc_fee(1.0)  == 0.0
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class Opportunity:
    strategy:              str
    market_id:             str
    market_question:       str
    action:                str           # BUY_YES | BUY_NO | BUY_ALL_YES
    edge:                  float         # net edge after fees
    win_probability:       float         # [0.0, 1.0]
    max_payout:            float         # $1.00 for binary
    time_to_resolution_sec: int
    metadata:              dict = field(default_factory=dict)
    score:                 float = 0.0   # set by SignalEngine after scan


@dataclass
class Resolution:
    trade_id:    str
    market_id:   str
    won:         bool
    cost_usdc:   float
    payout_usdc: float
    pnl_usdc:    float
    roi:         float
    strategy:    str
    notes:       str        = ""
    lessons:     list[str]  = field(default_factory=list)


# ── BaseStrategy ───────────────────────────────────────────────────────────────

class BaseStrategy(ABC):
    name: str = "base"

    # ── Fee formula (canonical — never override) ───────────────────────────────

    @staticmethod
    def calc_fee(p: float) -> float:
        """
        Polymarket taker fee formula.
        fee = 2.25 × 0.25 × (p × (1 − p))²

        Verification:
          calc_fee(0.5)  ≈ 0.140625   (1.41% — peak at p=0.5)
          calc_fee(0.95) ≈ 0.000127   (0.013%)
          calc_fee(0.0)  == 0.0
          calc_fee(1.0)  == 0.0
        """
        p = max(0.0, min(1.0, p))
        return 2.25 * (p * (1.0 - p)) ** 2

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def scan(self, markets: list, groups: dict, config: dict) -> list[Opportunity]:
        """
        Scan markets for opportunities. MUST be pure — no side effects, no API calls.
        Exception: S8 may call Claude Haiku for classification (cached).
        """
        ...

    @abstractmethod
    def score(self, opp: Opportunity, config: dict) -> float:
        """Return float in [0.0, 1.0]. Higher = better opportunity."""
        ...

    @abstractmethod
    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        """
        Return USDC position size.
        MUST: floor at 1.0, cap at config['max_position_pct'] * bankroll.
        """
        ...

    @abstractmethod
    def on_resolve(self, trade: dict, outcome: str, config: dict) -> Resolution:
        """Called by StateEngine when a position resolves. Return Resolution."""
        ...
