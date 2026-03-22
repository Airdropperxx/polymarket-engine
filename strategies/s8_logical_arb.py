"""
strategies/s8_logical_arb.py -- Logical impossibility arbitrage.

STATUS: DISABLED at MVP (enabled: false in configs/strategies.yaml).
Reason: sentence-transformers pulls torch (~1.5GB) which breaks GitHub Actions.
Enable when: capital > $500 AND running locally or on a VPS.

When enabled:
  Uses cosine similarity on question embeddings to find candidate pairs,
  then Claude Haiku to confirm logical exclusivity, then checks price sum > 1.0.
  Cache in data/s8_direction_cache.json -- committed to repo between runs.
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
        self._model = None
        self._embeddings: dict = {}
        self._direction_cache = self._load_cache()

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

    def _get_model(self):
        """Lazy-load sentence-transformers. Only runs when explicitly enabled."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer("all-MiniLM-L6-v2")
                log.info("s8_model_loaded")
            except ImportError:
                log.warning("s8_sentence_transformers_not_installed")
                return None
            except Exception as e:
                log.warning("s8_model_load_failed", error=str(e))
                return None
        return self._model

    def _embed(self, text: str):
        model = self._get_model()
        if model is None:
            return None
        try:
            return model.encode(text, normalize_embeddings=True)
        except Exception:
            return None

    @staticmethod
    def _cosine_sim(a, b) -> float:
        if a is None or b is None:
            return 0.0
        try:
            import numpy as np
            return float(max(-1.0, min(1.0, float(np.dot(a, b)))))
        except ImportError:
            return 0.0

    def _classify_pair(self, q_a: str, q_b: str) -> Optional[str]:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=50,
                system=(
                    "Classify whether two prediction market questions are mutually "
                    "exclusive (only one can resolve YES). "
                    "Reply with exactly one word: exclusive, compatible, or unclear."
                ),
                messages=[{"role": "user",
                           "content": f"Question A: {q_a}\nQuestion B: {q_b}"}]
            )
            verdict = response.content[0].text.strip().lower()
            return verdict if verdict in ("exclusive", "compatible", "unclear") else "unclear"
        except Exception as e:
            log.warning("s8_llm_classify_failed", error=str(e))
            return None

    def scan(self, markets: list, groups: dict, config: dict) -> list[Opportunity]:
        cfg = config.get("s8_logical_arb", config)

        # DEFAULT FALSE at MVP -- avoids torch import entirely
        if not cfg.get("enabled", False):
            log.debug("s8_disabled_skipping")
            return []

        min_edge = float(cfg.get("min_edge_after_fees", 0.030))
        sim_threshold = float(cfg.get("similarity_threshold", 0.75))
        min_volume = float(cfg.get("min_volume_24h", 300.0))
        max_llm_calls = int(cfg.get("max_llm_calls_per_scan", 5))

        candidates = [m for m in markets
                      if m.volume_24h >= min_volume
                      and m.seconds_to_resolution > 0]

        if len(candidates) < 2:
            return []

        for m in candidates:
            if m.market_id not in self._embeddings:
                emb = self._embed(m.question)
                if emb is not None:
                    self._embeddings[m.market_id] = emb

        opps = []
        llm_calls = 0
        cache_dirty = False

        for m_a, m_b in combinations(candidates, 2):
            emb_a = self._embeddings.get(m_a.market_id)
            emb_b = self._embeddings.get(m_b.market_id)
            if emb_a is None or emb_b is None:
                continue

            sim = self._cosine_sim(emb_a, emb_b)
            if sim < sim_threshold:
                continue

            key = self._cache_key(m_a.market_id, m_b.market_id)
            verdict = self._direction_cache.get(key)

            if verdict is None:
                if llm_calls >= max_llm_calls:
                    continue
                verdict = self._classify_pair(m_a.question, m_b.question)
                llm_calls += 1
                if verdict:
                    self._direction_cache[key] = verdict
                    cache_dirty = True

            if verdict != "exclusive":
                continue

            opp = self._build_arb_opp(m_a, m_b, min_edge, sim)
            if opp:
                opps.append(opp)

        if cache_dirty:
            self._save_cache()

        log.info("s8_scan_complete", candidates=len(candidates),
                 llm_calls=llm_calls, opportunities_found=len(opps))
        return opps

    def _build_arb_opp(self, m_a, m_b, min_edge, sim) -> Optional[Opportunity]:
        yes_sum = m_a.yes_price + m_b.yes_price
        if yes_sum <= 1.0:
            return None

        target = m_a if m_a.yes_price >= m_b.yes_price else m_b
        other = m_b if target is m_a else m_a

        buy_price = target.no_ask
        fee = self.calc_fee(buy_price)
        edge = (yes_sum - 1.0) - fee - 0.01

        if edge < min_edge:
            return None

        return Opportunity(
            strategy=self.name,
            market_id=target.market_id,
            market_question=target.question,
            action="BUY_NO",
            edge=round(edge, 5),
            win_probability=round(1.0 - target.yes_price, 4),
            max_payout=1.0,
            time_to_resolution_sec=min(m_a.seconds_to_resolution,
                                       m_b.seconds_to_resolution),
            metadata={
                "token_id": target.no_token_id,
                "buy_price": round(buy_price, 4),
                "fee": round(fee, 6),
                "yes_sum": round(yes_sum, 4),
                "similarity": round(sim, 4),
                "contradicting_market_id": other.market_id,
                "contradicting_question": other.question[:100],
                "contradicting_yes_price": round(other.yes_price, 4),
                "target_yes_price": round(target.yes_price, 4),
                "category": target.category,
                "volume_24h": target.volume_24h,
                "fee_rate_bps": target.fee_rate_bps,
            },
        )

    def score(self, opp: Opportunity, config: dict) -> float:
        import math
        meta = opp.metadata
        edge = opp.edge
        sim = meta.get("similarity", 0.75)
        yes_sum = meta.get("yes_sum", 1.0)
        vol = meta.get("volume_24h", 300.0)

        edge_score = min(1.0, max(0.0, (edge - 0.03) / 0.07))
        sim_score = min(1.0, max(0.0, (sim - 0.75) / 0.25))
        misprice = min(1.0, (yes_sum - 1.0) / 0.20)
        vol_score = min(1.0, math.log10(max(vol, 300)) / math.log10(50000))

        raw = (edge_score * 0.40 + sim_score * 0.30
               + misprice * 0.20 + vol_score * 0.10)
        return round(min(1.0, max(0.0, raw)), 4)

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        cfg = config.get("s8_logical_arb", config)
        max_pct = float(cfg.get("max_position_pct", 0.15))
        kelly_frac = float(cfg.get("kelly_fraction", 0.25))
        buy_price = opp.metadata.get("buy_price", 0.5)
        b = (1.0 - buy_price) / buy_price if buy_price > 0 else 0
        p = opp.win_probability
        q = 1.0 - p
        kelly_full = (b * p - q) / b if b > 0 else 0.0
        position = kelly_full * kelly_frac * bankroll
        return round(max(1.0, min(max_pct * bankroll, position)), 2)

    def on_resolve(self, trade: dict, outcome: str, config: dict) -> Resolution:
        cost_usdc = trade.get("cost_usdc", 0.0)
        fee_usdc = trade.get("fee_usdc", 0.0)
        shares = trade.get("shares", 0.0)
        meta = trade.get("metadata", {})
        sim = meta.get("similarity", 0.0)
        yes_sum = meta.get("yes_sum", 1.0)
        won = (outcome == "win")
        payout = shares * 1.0 if won else 0.0
        pnl_usdc = payout - cost_usdc - fee_usdc
        roi = pnl_usdc / cost_usdc if cost_usdc > 0 else 0.0

        lessons = []
        if not won:
            lessons.append(
                f"S8 LOSS: yes_sum={yes_sum:.2f} sim={sim:.2f}. "
                "Raise similarity_threshold or min_edge."
            )
        else:
            lessons.append(
                f"S8 WIN: yes_sum={yes_sum:.2f} sim={sim:.2f} ROI={roi:.1%}."
            )

        return Resolution(
            trade_id=trade.get("trade_id", ""),
            market_id=trade.get("market_id", ""),
            won=won,
            cost_usdc=round(cost_usdc, 4),
            payout_usdc=round(payout, 4),
            pnl_usdc=round(pnl_usdc, 4),
            roi=round(roi, 4),
            strategy=self.name,
            notes=f"{'WIN' if won else 'LOSS'}: yes_sum={yes_sum:.2f} ROI={roi:.1%}",
            lessons=lessons,
        )
