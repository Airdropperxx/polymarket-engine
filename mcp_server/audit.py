"""Audit logging for MCP server tool calls."""

import json
import os
from datetime import datetime
from pathlib import Path


class AuditLog:
    """Logs all tool calls to a JSONL file."""

    def __init__(self, log_path: str = "data/audit.jsonl"):
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, details: dict = None) -> dict:
        """Log an event with timestamp and details."""
        try:
            entry = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "event": event,
                "details": details or {},
            }
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            return {"status": "logged", "event": event}
        except Exception as exc:
            return {"error": f"Failed to log: {exc}"}

    def get_recent(self, limit: int = 10) -> list:
        """Get recent log entries."""
        try:
            if not self._log_path.exists():
                return []
            entries = []
            with open(self._log_path, "r") as f:
                for line in f:
                    if line.strip():
                        entries.append(json.loads(line))
            return entries[-limit:]
        except Exception:
            return []