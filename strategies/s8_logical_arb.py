"""
strategies/s8_logical_arb.py — Logical impossibility arbitrage.

Thesis: Some sets of Polymarket questions are logically contradictory —
if A is true, B must be false. When both trade at high prices (e.g. both
"Biden wins" AND "Trump wins" both at 0.55), the sum exceeds 1.0, which
is a guaranteed arb: sell the overpriced side.

More specifically: if you find two mutually exclusive questions where:
  YES_A + YES_B > 1.0 + fees
→ sell the higher-priced YES (or buy the lower-priced NO)
→ guaranteed profit since only one can resolve YES

Detection pipeline:
  Step 1: Embedding similarity (cosine > 0.85) → candidate pairs
  Step 2: Claude Haiku classifier → confirm logical exclusivity
  Step 3: Price check → edge > min_edge_after_fees
  Step 4: Cache the LLM result (direction_cache.json) → never re-classify same pair

GitHub Actions compatibility:
  - sentence-transformers runs fine (CPU only, model cached in GHA pip cache)
  - LLM call is gated: only for new pairs not in cache
  - Cache committed to repo → survives across runs

KEY FIX vs original:
  - chromadb REMOVED (was deleted in requirements.txt — correct)
  - Pure numpy cosine similarity replaces vector DB
  - Cache keyed by sorted market_id pair to avoid duplicates
  - Embedding model loaded lazily (heavy import, skip if not needed)
"""

from __future__ import annotations

import json
import os
import time
from itertools import combinations
from pathlib import Path
from typing import Optional

import structlog

from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger(component="s8_logical_arb")

CACHE_PATH = Path("data/s8_direction_cache.json")


class S8LogicalArb(BaseStrategy):
    name = "s8_logical_arb"

    def __init__(self):
        self._model         = None   # lazy-loaded sentence-transformers model
        self._embeddings    = {}     # market_id -> np.ndarray (in-memory for this run)
        self._direction_cache = self._load_cache()

    # ── Cache ──────────────────────────────────────────────────────────────────

    def _load_cache(self) -> dict:
        if CACHE_PATH.exists():
            try:
                with open(CACHE_PATH) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_cache(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(self._direction_cache, f, indent=2)

    def _cache_key(self, id_a: str, id_b: str) -> str:
        return "|".join(sorted([id_a, id_b]))

    # ── Embedding model ────────────────────────────────────────────────────────

    def _get_model(self):
        """Lazy-load sentence-transformers. Only happens on first S8 scan."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer("all-MiniLM-L6-v2")
                log.info("s8_model_loaded")
            except ImportError:
                log.warning("s8_sentence_transformers_not_installed")
                return None
        return self._model

    def _embed(self, text: str):
        """Get or compute embedding for a market question."""
        import numpy as np
        model = self._get_model()
        if model is None:
            return None
        try:
            return model.encode(text, normalize_embeddings=True)
        except Exception as e:
            log.debug("s8_embed_failed", error=str(e))
            return None

    @staticmethod
    def _cosine_sim(a, b) -> float:
        import numpy as np
        if a is None or b is None:
            return 0.0
        dot = float(np.dot(a, b))
        return max(-1.0, min(1.0, dot))   # already normalized → dot = cosine

    # ── LLM classifier ─────────────────────────────────────────────────────────

    def _classify_pair(self, q_a: str, q_b: str) -> Optional[str]:
        """
        Call Claude Haiku to classify if two questions are mutually exclusive.
        Returns 'exclusive' | 'compatible' | 'unclear'
        Cached in direction_cache.json.
        """
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            log.warning("s8_no_anthropic_key")
            return None

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=50,
                system=(
                    "You classify whether two prediction market questions are "
                    "mutually exclusive (only one can resolve YES). "
                    "Reply with exactly one word: exclusive, compatible, or unclear."
                ),
                messages=[{
                    "role": "user",
                    "content": f"Question A: {q_a}\nQuestion B: {q_b}"
                }]
            )
            verdict = response.content[0].text.strip().lower()
            if verdict not in ("exclusive", "compatible", "unclear"):
                verdict = "unclear"
            return verdict
        except Exception as e:
            log.warning("s8_llm_classify_failed", error=str(e))
            return None

    # ── scan ──────────────────────────────────────────────────────────────────

    def scan(self,
             markets:  list[MarketState],
             groups:   dict,
             config:   dict) -> list[Opportunity]:
        """
        1. Embed all market questions
        2. Find pairs with cosine similarity > sim_threshold
        3. For uncached pairs: LLM classify (max llm_calls_per_scan)
        4. For exclusive pairs: check if price sum creates arb edge
        """
        cfg             = config.get("s8_logical_arb", config)
        min_edge        = float(cfg.get("min_edge_after_fees",  0.030))
        sim_threshold   = float(cfg.get("similarity_threshold", 0.75))
        min_volume      = float(cfg.get("min_volume_24h",       300.0))
        max_llm_calls   = int(cfg.get("max_llm_calls_per_scan",    5))
        # Focus on politics first (most logical violations), then other
        priority_cats   = cfg.get("priority_categories",
                                   ["politics", "other", "crypto", "sports"])

        # Filter to liquid markets only
        candidates = [m for m in markets
                      if m.volume_24h >= min_volume
                      and m.seconds_to_resolution > 0]

        if len(candidates) < 2:
            log.info("s8_too_few_candidates", count=len(candidates))
            return []

        # Sort by priority category
        def cat_rank(m):
            try:
                return priority_cats.index(m.category)
            except ValueError:
                return 99

        candidates.sort(key=cat_rank)

        # Embed all candidates (lazy, in-memory for this run)
        for m in candidates:
            if m.market_id not in self._embeddings:
                emb = self._embed(m.question)
                if emb is not None:
                    self._embeddings[m.market_id] = emb

        opps        = []
        llm_calls   = 0
        cache_dirty = False

        # Check all pairs
        for m_a, m_b in combinations(candidates, 2):
            # Must have embeddings
            emb_a = self._embeddings.get(m_a.market_id)
            emb_b = self._embeddings.get(m_b.market_id)
            if emb_a is None or emb_b is None:
                continue

            # Cosine similarity gate
            sim = self._cosine_sim(emb_a, emb_b)
            if sim < sim_threshold:
                continue

            # Cache lookup
            key = self._cache_key(m_a.market_id, m_b.market_id)
            verdict = self._direction_cache.get(key)

            if verdict is None:
                if llm_calls >= max_llm_calls:
                    continue  # budget exhausted for this scan
                verdict = self._classify_pair(m_a.question, m_b.question)
                llm_calls += 1
                if verdict:
                    self._direction_cache[key] = verdict
                    cache_dirty = True

            if verdict != "exclusive":
                continue

            # Price arb check: if YES_A + YES_B > 1 + fees → sell overpriced
            opp = self._build_arb_opp(m_a, m_b, min_edge, sim)
            if opp:
                opps.append(opp)

        if cache_dirty:
            self._save_cache()

        log.info("s8_scan_complete",
                 candidates=len(candidates),
                 llm_calls=llm_calls,
                 opportunities_found=len(opps))
        return opps

    def _build_arb_opp(self,
                       m_a:      MarketState,
                       m_b:      MarketState,
                       min_edge: float,
                       sim:      float) -> Optional[Opportunity]:
        """
        Check if YES_A + YES_B > 1.0 (impossible → one is mispriced).
        If so: buy the cheaper NO (equivalent to selling the overpriced YES).
        """
        yes_sum = m_a.yes_price + m_b.yes_price

        if yes_sum <= 1.0:
            return None  # no mispricing — might still be interesting as NO arb

        # Overpriced YES sum: we buy the NO of the cheaper one
        # (i.e. bet against the lower-probability YES which is still overpriced)
        if m_a.yes_price >= m_b.yes_price:
            target, other = m_a, m_b
        else:
            target, other = m_b, m_a

        # Buy NO of the target (the more expensive YES side)
        buy_price = target.no_ask
        fee       = self.calc_fee(buy_price)
        # If YES sum > 1, the correct NO probability is at least (yes_sum - 1 + target's NO)
        # Conservative edge estimate: just the overpricing minus fee
        edge = (yes_sum - 1.0) - fee - 0.01   # -0.01 execution buffer

        if edge < min_edge:
            return None

        return Opportunity(
            strategy             = self.name,
            market_id            = target.market_id,
            market_question      = target.question,
            action               = "BUY_NO",
            edge                 = round(edge, 5),
            win_probability      = round(1.0 - target.yes_price, 4),
            max_payout           = 1.0,
            time_to_resolution_sec = min(m_a.seconds_to_resolution,
                                         m_b.seconds_to_resolution),
            metadata={
                "token_id":         target.no_token_id,
                "buy_price":        round(buy_price, 4),
                "fee":              round(fee, 6),
                "yes_sum":          round(yes_sum, 4),
                "similarity":       round(sim, 4),
                "contradicting_market_id":   other.market_id,
                "contradicting_question":    other.question[:100],
                "contradicting_yes_price":   round(other.yes_price, 4),
                "target_yes_price":          round(target.yes_price, 4),
                "category":                  target.category,
                "volume_24h":                target.volume_24h,
                "fee_rate_bps":              target.fee_rate_bps,
            },
        )

    # ── score ─────────────────────────────────────────────────────────────────

    def score(self, opp: Opportunity, config: dict) -> float:
        import math
        meta        = opp.metadata
        edge        = opp.edge
        sim         = meta.get("similarity", 0.75)
        yes_sum     = meta.get("yes_sum", 1.0)
        vol         = meta.get("volume_24h", 300.0)

        # Edge score
        edge_score  = min(1.0, max(0.0, (edge - 0.03) / 0.07))
        # Similarity (higher = more confident the pair is exclusive)
        sim_score   = min(1.0, max(0.0, (sim - 0.75) / 0.25))
        # Mispricing severity
        misprice    = min(1.0, (yes_sum - 1.0) / 0.20)
        # Liquidity
        vol_score   = min(1.0, math.log10(max(vol, 300)) / math.log10(50000))

        raw = (edge_score * 0.40
             + sim_score  * 0.30
             + misprice   * 0.20
             + vol_score  * 0.10)

        return round(min(1.0, max(0.0, raw)), 4)

    # ── size ──────────────────────────────────────────────────────────────────

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        cfg         = config.get("s8_logical_arb", config)
        max_pct     = float(cfg.get("max_position_pct", 0.15))
        kelly_frac  = float(cfg.get("kelly_fraction", 0.25))

        buy_price   = opp.metadata.get("buy_price", 0.5)
        b           = (1.0 - buy_price) / buy_price
        p           = opp.win_probability
        q           = 1.0 - p
        kelly_full  = (b * p - q) / b if b > 0 else 0.0
        fraction    = kelly_full * kelly_frac
        position    = fraction * bankroll

        return round(max(1.0, min(max_pct * bankroll, position)), 2)

    # ── on_resolve ────────────────────────────────────────────────────────────

    def on_resolve(self, trade: dict, outcome: str, config: dict) -> Resolution:
        cost_usdc   = trade.get("cost_usdc", 0.0)
        fee_usdc    = trade.get("fee_usdc", 0.0)
        shares      = trade.get("shares", 0.0)
        meta        = trade.get("metadata", {})
        sim         = meta.get("similarity", 0.0)
        yes_sum     = meta.get("yes_sum", 1.0)

        won         = (outcome == "win")
        payout      = shares * 1.0 if won else 0.0
        pnl_usdc    = payout - cost_usdc - fee_usdc
        roi         = pnl_usdc / cost_usdc if cost_usdc > 0 else 0.0

        lessons = []
        if not won:
            lessons.append(
                f"S8 LOSS: yes_sum={yes_sum:.2f}, sim={sim:.2f}. "
                "Markets may not have been truly exclusive. "
                "Raise similarity_threshold or min_edge."
            )
        else:
            lessons.append(
                f"S8 WIN: yes_sum={yes_sum:.2f}, sim={sim:.2f}, ROI={roi:.1%}. "
                "Logical arb confirmed. Consider increasing max_llm_calls_per_scan."
            )

        return Resolution(
            trade_id    = trade.get("trade_id", ""),
            market_id   = trade.get("market_id", ""),
            won         = won,
            cost_usdc   = round(cost_usdc, 4),
            payout_usdc = round(payout, 4),
            pnl_usdc    = round(pnl_usdc, 4),
            roi         = round(roi, 4),
            strategy    = self.name,
            notes       = f"{'WIN' if won else 'LOSS'}: yes_sum={yes_sum:.2f}, ROI={roi:.1%}",
            lessons     = lessons,
        )
