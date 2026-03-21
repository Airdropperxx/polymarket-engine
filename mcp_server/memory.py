"""Memory management for lessons.json."""

import json
from pathlib import Path
from typing import Optional

import structlog
from mcp_server.github_ops import GitHubOps

log = structlog.get_logger()


class Memory:
    """Manages lessons.json reading and writing."""

    def __init__(self, lessons_path: str = "data/lessons.json", github_ops: Optional[GitHubOps] = None):
        self._path = Path(lessons_path)
        self._gh = github_ops

    def read(self) -> dict:
        """Read lessons.json content."""
        try:
            if self._path.exists():
                with open(self._path, "r") as f:
                    return json.load(f)
            if self._gh:
                content = self._gh.read_file(str(self._path))
                if content:
                    return json.loads(content)
            return {"error": "File not found"}
        except json.JSONDecodeError as exc:
            log.error("memory.read_json_error", path=str(self._path), error=str(exc))
            return {"error": f"Invalid JSON: {exc}"}
        except Exception as exc:
            log.error("memory.read_error", path=str(self._path), error=str(exc))
            return {"error": str(exc)}

    def write(self, content: dict) -> dict:
        """Overwrite lessons.json and commit to GitHub."""
        try:
            json_str = json.dumps(content, indent=2)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                f.write(json_str)
            if self._gh:
                self._gh.update_file(
                    str(self._path),
                    json_str,
                    f"Update lessons.json"
                )
            log.info("memory.lessons_updated")
            return {"status": "saved", "path": str(self._path)}
        except Exception as exc:
            log.error("memory.write_error", path=str(self._path), error=str(exc))
            return {"error": str(exc)}