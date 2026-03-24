"""
engines/review_engine.py — AI learning loop using Claude Haiku.
NEVER trades. NEVER raises. Returns {'status': 'skipped'} if 0 trades.
JSON parse has try/except + regex fallback per spec.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import structlog

from engines.state_engine import StateEngine

log = structlog.get_logger(component="review_engine")

SYSTEM_PROMPT = """You are a trading strategy reviewer for a Polymarket prediction market engine.

Review the recent resolved trades and current lessons, then output ONLY valid JSON:
{
  "strategy_score_updates": {
    "s10_near_resolution": {"allocation_delta": 0.0, "notes": "..."},
    "s1_negrisk_arb":      {"allocation_delta": 0.0, "notes": "..."},
    "s8_logical_arb":      {"allocation_delta": 0.0, "notes": "..."}
  },
  "new_lessons": ["specific lesson string"],
  "deprecated_lesson_indices": [],
  "reasoning": "brief explanation"
}

RULES:
- Output ONLY valid JSON — no prose, no markdown, no backticks
- allocation_delta: max ±0.05 per strategy per cycle
- Min 5 trades before adjusting any allocation
- Lessons must be specific: "S10 sports at p=0.91 loses 29%" not "S10 is risky"
- All allocations must still sum to 1.0 after deltas applied
"""


class ReviewEngine:
    def __init__(self, state_engine: StateEngine, config: dict):
        self.state  = state_engine
        self.config = config

    def run_after_resolution(self) -> dict:
        return self._run(window_hours=48)

    def run_daily_review(self) -> dict:
        return self._run(window_hours=168)  # 7 days

    def _run(self, window_hours: int = 48) -> dict:
        recent = self.state.get_recent_resolved_trades(hours=window_hours)
        if not recent:
            log.info("review_skipped_no_trades")
            return {"status": "skipped"}

        lessons_data = self.state.get_lessons()
        api_key      = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("review_skipped_no_api_key")
            return {"status": "skipped_no_key"}

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)

            prompt = (
                f"Recent trades ({len(recent)}):\n"
                f"{json.dumps(recent[:20], indent=2)}\n\n"
                f"Current lessons:\n"
                f"{json.dumps(lessons_data.get('lessons', []), indent=2)}"
            )

            response = client.messages.create(
                model      = "claude-haiku-4-5-20251001",
                max_tokens = 1000,
                system     = SYSTEM_PROMPT,
                messages   = [{"role": "user", "content": prompt}]
            )
            text = response.content[0].text.strip()

            # Parse JSON with fallback
            try:
                updates = json.loads(text)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    try:
                        updates = json.loads(match.group())
                    except Exception:
                        return {"status": "error", "raw": text[:200]}
                else:
                    return {"status": "error"}

            # Apply updates
            self._apply_updates(lessons_data, updates)
            self.state.save_lessons(lessons_data)

            log.info("review_complete",
                     new_lessons=len(updates.get("new_lessons", [])),
                     deprecated=len(updates.get("deprecated_lesson_indices", [])))
            return {"status": "ok", "updates": updates}

        except Exception as e:
            log.error("review_engine_failed", error=str(e))
            return {"status": "error"}

    def _apply_updates(self, lessons_data: dict, updates: dict) -> None:
        """Apply allocation deltas and new lessons. Never crashes."""
        try:
            # Add new lessons
            for lesson in updates.get("new_lessons", []):
                if lesson and len(lesson) > 10:
                    lessons_data.setdefault("lessons", []).append(lesson)

            # Deprecate old lessons
            deprecated_idx = sorted(
                updates.get("deprecated_lesson_indices", []), reverse=True
            )
            lessons = lessons_data.get("lessons", [])
            for idx in deprecated_idx:
                if 0 <= idx < len(lessons):
                    lessons_data.setdefault("deprecated_lessons", []).append(
                        lessons.pop(idx)
                    )

            # Apply allocation deltas (capped at ±0.05)
            scores = lessons_data.get("strategy_scores", {})
            for strat, update in updates.get("strategy_score_updates", {}).items():
                delta = float(update.get("allocation_delta", 0.0))
                delta = max(-0.05, min(0.05, delta))   # hard cap
                if strat in scores:
                    scores[strat]["allocation"] = round(
                        scores[strat].get("allocation", 0.0) + delta, 3
                    )

            lessons_data["last_updated"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            log.warning("apply_updates_failed", error=str(e))
