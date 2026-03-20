"""
strategies/s8_logical_arb.py
=============================
Logical / combinatorial impossibility arbitrage.

Finds market pairs where probabilities violate logical constraints.
Example: P(Lakers win championship) > P(Western Conference team wins)
is IMPOSSIBLE — Lakers are a Western team, so P(Lakers) ≤ P(Western) always.

Two-phase approach:
  Phase 1 (local, free): sentence-transformers cosine similarity to find candidate pairs.
                         Uses numpy — no ChromaDB (state lost on ephemeral GHA runners).
  Phase 2 (LLM):         Claude Haiku classifies logical relationship.
                         Results cached in data/s8_direction_cache.json (git-tracked).
                         Cache survives between GitHub Actions runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import anthropic
import numpy as np
import structlog
from sentence_transformers import SentenceTransformer

from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger()

_CLASSIFIER_PROMPT = """\
Are these two prediction markets logically related?
Market A: "{question_a}"
Market B: "{question_b}"

If winning market A GUARANTEES winning market B: output a_subset_of_b
If winning market B GUARANTEES winning market A: output b_subset_of_a
If they CANNOT both be true simultaneously: output mutually_exclusive
Otherwise: output none

Respond with ONLY one of: a_subset_of_b, b_subset_of_a, mutually_exclusive, none\
"""

_VALID_DIRECTIONS = frozenset({"a_subset_of_b", "b_subset_of_a", "mutually_exclusive", "none"})


class LogicalArbStrategy(BaseStrategy):
    """S8: Logical impossibility arbitrage using embedding similarity + LLM classifier."""

    name = "s8_logical_arb"

    def __init__(self) -> None:
        # Model loaded lazily on first scan() call to avoid import-time download
        self._model: Optional[SentenceTransformer] = None
        self._direction_cache: dict[str, str] = {}
        self._cache_path: Optional[Path] = None
        self._anthropic = anthropic.Anthropic()

    # -----------------------------------------------------------------------
    # BaseStrategy interface
    # -----------------------------------------------------------------------

    def scan(
        self,
        markets: list,
        negrisk_groups: dict,
        config: dict,
    ) -> list[Opportunity]:
        self._ensure_model_loaded()
        self._load_cache(config.get("cache_path", "data/s8_direction_cache.json"))

        min_sim   = config.get("min_similarity_threshold", 0.65)
        min_gap   = config.get("min_probability_gap", 0.04)
        min_edge  = config.get("min_edge_after_fees", 0.025)
        max_new   = config.get("max_new_classifications_per_scan", 30)
        llm_model = config.get("llm_model", "claude-haiku-4-5-20251001")

        opps: list[Opportunity] = []
        new_classifications = 0

        # Phase 1: embed all market questions
        try:
            questions   = [m.question for m in markets]
            embeddings  = self._model.encode(questions, convert_to_numpy=True,
                                             show_progress_bar=False)
            # Normalize for cosine similarity
            norms       = np.linalg.norm(embeddings, axis=1, keepdims=True)
            norms       = np.where(norms == 0, 1e-8, norms)
            embeddings  = embeddings / norms
        except Exception as exc:
            log.error("s8.embed_error", error=str(exc))
            return []

        # Phase 2: find candidate pairs by cosine similarity
        n = len(markets)
        for i in range(n):
            for j in range(i + 1, n):
                sim = float(np.dot(embeddings[i], embeddings[j]))
                if sim < min_sim:
                    continue

                m_a, m_b = markets[i], markets[j]
                gap = abs(m_a.yes_price - m_b.yes_price)
                if gap < min_gap:
                    continue

                cache_key = f"{m_a.market_id}:{m_b.market_id}"
                direction = self._direction_cache.get(cache_key)

                if direction is None:
                    if new_classifications >= max_new:
                        continue  # budget exhausted for this cycle
                    direction = self._classify(m_a.question, m_b.question, llm_model)
                    if direction:
                        self._direction_cache[cache_key] = direction
                        new_classifications += 1

                if direction in (None, "none"):
                    continue

                opp = self._detect_violation(m_a, m_b, direction, min_edge)
                if opp:
                    opps.append(opp)

        self._save_cache()
        log.info("s8.scan_done",
                 candidates=n * (n - 1) // 2,
                 new_classifications=new_classifications,
                 opportunities=len(opps))
        return opps

    def score(self, opp: Opportunity, config: dict) -> float:
        """Score = edge (already accounts for win probability in logical arb)."""
        return min(opp.edge * 5.0, 1.0)  # 20% edge → score 1.0

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        return self.calc_kelly_size(
            opp.win_probability,
            opp.max_payout,
            bankroll,
            config.get("kelly_fraction", 0.25),
            config.get("max_position_pct", 0.10),
        )

    def on_resolve(self, resolution: Resolution) -> dict:
        lessons = []
        if not resolution.won:
            lessons.append(
                f"S8 loss on {resolution.market_id[:40]}: logical gap may not converge within horizon. "
                f"ROI={resolution.roi:.2%}. Consider tighter min_probability_gap."
            )
        return {"won": resolution.won, "roi": resolution.roi,
                "notes": resolution.notes, "lessons": lessons}

    # -----------------------------------------------------------------------
    # Violation detection
    # -----------------------------------------------------------------------

    def _detect_violation(
        self,
        m_a: MarketState,
        m_b: MarketState,
        direction: str,
        min_edge: float,
    ) -> Optional[Opportunity]:
        """
        Given a confirmed logical relationship, check if prices violate it.
        If they do, create an Opportunity for the underpriced market.
        """
        if direction == "a_subset_of_b" and m_a.yes_price > m_b.yes_price:
            # P(A) > P(B) but A ⊂ B → impossible. Buy B (underpriced, must converge to ≥ P(A))
            edge = (m_a.yes_price - m_b.yes_price) - 0.02  # subtract 2% worst-case fee
            if edge < min_edge:
                return None
            return Opportunity(
                strategy=self.name,
                market_id=m_b.market_id,
                market_question=m_b.question,
                action="buy_yes",
                edge=edge,
                win_probability=m_a.yes_price,   # should converge to at least this
                max_payout=1.0 / m_b.yes_price,
                time_to_resolution_sec=m_b.seconds_to_resolution,
                metadata={
                    "violation": "a_subset_of_b",
                    "market_a": m_a.market_id,
                    "price_a": m_a.yes_price,
                    "price_b": m_b.yes_price,
                },
            )

        if direction == "b_subset_of_a" and m_b.yes_price > m_a.yes_price:
            edge = (m_b.yes_price - m_a.yes_price) - 0.02
            if edge < min_edge:
                return None
            return Opportunity(
                strategy=self.name,
                market_id=m_a.market_id,
                market_question=m_a.question,
                action="buy_yes",
                edge=edge,
                win_probability=m_b.yes_price,
                max_payout=1.0 / m_a.yes_price,
                time_to_resolution_sec=m_a.seconds_to_resolution,
                metadata={
                    "violation": "b_subset_of_a",
                    "market_b": m_b.market_id,
                    "price_a": m_a.yes_price,
                    "price_b": m_b.yes_price,
                },
            )

        return None

    # -----------------------------------------------------------------------
    # LLM classifier
    # -----------------------------------------------------------------------

    def _classify(self, question_a: str, question_b: str, model: str) -> Optional[str]:
        """
        Ask Claude Haiku to classify the logical relationship between two markets.
        Returns one of: a_subset_of_b, b_subset_of_a, mutually_exclusive, none
        Returns None on any API error (never raises).
        """
        try:
            resp = self._anthropic.messages.create(
                model=model,
                max_tokens=20,
                messages=[{
                    "role": "user",
                    "content": _CLASSIFIER_PROMPT.format(
                        question_a=question_a[:120],
                        question_b=question_b[:120],
                    ),
                }],
            )
            result = resp.content[0].text.strip().lower()
            return result if result in _VALID_DIRECTIONS else "none"
        except Exception as exc:
            log.warning("s8.classifier_error", error=str(exc))
            return None  # Skip this pair — never crash scan()

    # -----------------------------------------------------------------------
    # Cache (JSON file, git-tracked, survives between GHA runs)
    # -----------------------------------------------------------------------

    def _ensure_model_loaded(self) -> None:
        if self._model is None:
            log.info("s8.loading_model", model="all-MiniLM-L6-v2")
            self._model = SentenceTransformer("all-MiniLM-L6-v2")

    def _load_cache(self, path: str) -> None:
        self._cache_path = Path(path)
        if self._cache_path.exists():
            try:
                self._direction_cache = json.loads(
                    self._cache_path.read_text(encoding="utf-8")
                )
                log.info("s8.cache_loaded", size=len(self._direction_cache))
            except json.JSONDecodeError:
                log.warning("s8.cache_corrupt", path=path)
                self._direction_cache = {}
        else:
            self._direction_cache = {}

    def _save_cache(self) -> None:
        if self._cache_path:
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                self._cache_path.write_text(
                    json.dumps(self._direction_cache, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                log.warning("s8.cache_save_error", error=str(exc))
