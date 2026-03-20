"""
engines/review_engine.py
========================
AI-powered post-resolution learning loop.

Called after every resolution and daily at 00:00 UTC.
Updates data/lessons.json with new strategy insights and allocation adjustments.

Uses Claude Haiku for cost efficiency (~$0.001 per review).
NEVER places orders. NEVER raises — all failures are logged and swallowed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
import structlog

from engines.state_engine import StateEngine

log = structlog.get_logger()

_SYSTEM_PROMPT = """You are a quantitative trading strategy reviewer for a Polymarket prediction market engine.

INPUT: A JSON object with recent resolved trades and existing lessons.
OUTPUT: Valid JSON ONLY. No preamble. No markdown fences. No explanation outside the JSON.

Output schema:
{
  "strategy_score_updates": {
    "strategy_name": {"win_rate": float, "avg_roi": float, "allocation_delta": float}
  },
  "new_lessons": ["lesson string", ...],
  "deprecated_lesson_indices": [0, 1, ...],
  "reasoning": "one sentence explaining the main change"
}

Rules:
- Lessons must be SPECIFIC and FALSIFIABLE.
  BAD:  "S10 sometimes loses in sports"
  GOOD: "S10 sports win rate 71% when margin < 2 goals AND time < 10 min. Raise threshold to 0.95."
- allocation_delta: max ±0.05 per strategy per cycle. Never exceed this.
- Only adjust allocation if strategy has >= 5 trades in the input.
- Deprecate a lesson only if there is clear contradicting evidence (not one outlier).
- If fewer than 3 trades total: set strategy_score_updates to {} and new_lessons to [].
"""


class ReviewEngine:
    """AI-powered post-resolution learning loop."""

    def __init__(self, state_engine: StateEngine, config: dict) -> None:
        self._state  = state_engine
        self._config = config
        self._client = anthropic.Anthropic()

    def run_after_resolution(self, market_id: str) -> dict:
        """
        Run review triggered by a specific market resolution.
        Uses last 48 hours of resolved trades.
        Returns status dict. NEVER raises.
        """
        trades = self._state.get_recent_resolved_trades(hours=48)
        if not trades:
            return {"status": "skipped", "reason": "no_trades"}
        return self._run(trades, context=f"triggered by resolution of {market_id}")

    def run_daily_review(self) -> dict:
        """
        Run full daily review at 00:00 UTC.
        Uses last 7 days of resolved trades. NEVER raises.
        """
        trades = self._state.get_recent_resolved_trades(hours=168)
        return self._run(trades, context="daily review")

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _run(self, trades: list[dict], context: str) -> dict:
        lessons = self._state.get_lessons()

        user_content = json.dumps({
            "context": context,
            "trade_count": len(trades),
            "trades": trades[:50],  # cap at 50 to stay within token limits
            "existing_lessons": lessons.get("lessons", []),
            "current_scores": lessons.get("strategy_scores", {}),
        }, default=str)

        try:
            response = self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = response.content[0].text
        except Exception as exc:
            log.error("review_engine.api_error", error=str(exc))
            return {"status": "error", "reason": str(exc)}

        updates = self._parse_response(raw)
        if not updates:
            return {"status": "error", "reason": "could_not_parse_response"}

        self._apply_updates(updates, lessons)
        log.info("review_engine.updated", reasoning=updates.get("reasoning", ""))
        return {
            "status": "updated",
            "new_lessons": updates.get("new_lessons", []),
            "reasoning": updates.get("reasoning", ""),
        }

    def _parse_response(self, text: str) -> Optional[dict]:
        """Parse JSON from LLM response with fallback regex extraction."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        log.error("review_engine.parse_failed", raw=text[:200])
        return None

    def _apply_updates(self, updates: dict, lessons: dict) -> None:
        """Apply reviewer output to lessons dict and save."""
        MAX_ALLOCATION_DELTA = 0.05
        MAX_LESSONS = 20

        # Update strategy scores and allocation weights
        for strategy, score_update in updates.get("strategy_score_updates", {}).items():
            if strategy not in lessons.setdefault("strategy_scores", {}):
                lessons["strategy_scores"][strategy] = {}
            entry = lessons["strategy_scores"][strategy]
            for k in ("win_rate", "avg_roi"):
                if k in score_update:
                    entry[k] = score_update[k]
            delta = float(score_update.get("allocation_delta", 0.0))
            delta = max(-MAX_ALLOCATION_DELTA, min(MAX_ALLOCATION_DELTA, delta))  # enforce cap
            entry["allocation"] = round(entry.get("allocation", 0.0) + delta, 4)

        # Add new lessons (prune oldest if over limit)
        existing = lessons.setdefault("lessons", [])
        for lesson in updates.get("new_lessons", []):
            if lesson and lesson not in existing:
                existing.append(lesson)
        while len(existing) > MAX_LESSONS:
            deprecated = existing.pop(0)
            lessons.setdefault("deprecated_lessons", []).append(deprecated)

        # Deprecate old lessons
        indices = sorted(updates.get("deprecated_lesson_indices", []), reverse=True)
        for i in indices:
            if 0 <= i < len(existing):
                deprecated = existing.pop(i)
                lessons.setdefault("deprecated_lessons", []).append(deprecated)

        # Append capital history entry
        lessons.setdefault("capital_history", []).append({
            "date": datetime.now(timezone.utc).date().isoformat(),
            "balance": self._state.get_current_balance(),
            "pnl_today": self._state.get_daily_pnl(),
        })

        lessons["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._state.save_lessons(lessons)
