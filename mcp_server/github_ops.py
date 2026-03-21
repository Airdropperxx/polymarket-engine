"""GitHub operations using PyGithub."""

import json
import os
from pathlib import Path
from typing import Optional

import structlog
import yaml
from github import Github

log = structlog.get_logger()


class GitHubOps:
    """GitHub API operations for task management."""

    def __init__(self, token: str, owner: str, repo: str):
        self._client = Github(token)
        self._repo = self._client.get_repo(f"{owner}/{repo}")
        self._owner = owner
        self._repo_name = repo

    def get_issue(self, issue_number: int):
        """Get an issue by number."""
        try:
            return self._repo.get_issue(issue_number)
        except Exception as exc:
            log.error("github_ops.get_issue_error", issue_number=issue_number, error=str(exc))
            return None

    def get_all_issues(self, state: str = "open"):
        """Get all issues in the repository."""
        try:
            return list(self._repo.get_issues(state=state))
        except Exception as exc:
            log.error("github_ops.get_all_issues_error", error=str(exc))
            return []

    def get_issues_by_label(self, label: str, state: str = "open"):
        """Get issues filtered by label."""
        try:
            return list(self._repo.get_issues(labels=[label], state=state))
        except Exception as exc:
            log.error("github_ops.get_issues_by_label_error", label=label, error=str(exc))
            return []

    def add_label_to_issue(self, issue_number: int, label: str):
        """Add a label to an issue."""
        try:
            issue = self._repo.get_issue(issue_number)
            issue.add_to_labels(label)
            log.info("github_ops.label_added", issue=issue_number, label=label)
            return True
        except Exception as exc:
            log.error("github_ops.add_label_error", issue=issue_number, label=label, error=str(exc))
            return False

    def remove_label_from_issue(self, issue_number: int, label: str):
        """Remove a label from an issue."""
        try:
            issue = self._repo.get_issue(issue_number)
            issue.remove_from_labels(label)
            log.info("github_ops.label_removed", issue=issue_number, label=label)
            return True
        except Exception as exc:
            log.error("github_ops.remove_label_error", issue=issue_number, label=label, error=str(exc))
            return False

    def close_issue(self, issue_number: int):
        """Close an issue."""
        try:
            issue = self._repo.get_issue(issue_number)
            issue.edit(state="closed")
            log.info("github_ops.issue_closed", issue=issue_number)
            return True
        except Exception as exc:
            log.error("github_ops.close_issue_error", issue=issue_number, error=str(exc))
            return False

    def add_comment_to_issue(self, issue_number: int, comment: str):
        """Add a comment to an issue."""
        try:
            issue = self._repo.get_issue(issue_number)
            issue.create_comment(comment)
            log.info("github_ops.comment_added", issue=issue_number)
            return True
        except Exception as exc:
            log.error("github_ops.add_comment_error", issue=issue_number, error=str(exc))
            return False

    def read_file(self, file_path: str) -> str:
        """Read a file from the repository."""
        try:
            contents = self._repo.get_contents(file_path)
            return contents.decoded_content.decode("utf-8")
        except Exception as exc:
            log.error("github_ops.read_file_error", path=file_path, error=str(exc))
            return ""

    def update_file(self, file_path: str, content: str, message: str):
        """Update a file in the repository."""
        try:
            contents = self._repo.get_contents(file_path)
            self._repo.update_file(file_path, message, content, contents.sha)
            log.info("github_ops.file_updated", path=file_path)
            return True
        except Exception as exc:
            log.error("github_ops.update_file_error", path=file_path, error=str(exc))
            return False

    def create_file(self, file_path: str, content: str, message: str):
        """Create a new file in the repository."""
        try:
            self._repo.create_file(file_path, message, content)
            log.info("github_ops.file_created", path=file_path)
            return True
        except Exception as exc:
            log.error("github_ops.create_file_error", path=file_path, error=str(exc))
            return False

    def read_db_query(self, query: str, db_path: str = "data/trades.db") -> list:
        """Execute a query on the local database file."""
        try:
            import sqlite3
            db_full_path = Path(db_path)
            if not db_full_path.exists():
                return []
            conn = sqlite3.connect(db_full_path)
            cursor = conn.cursor()
            cursor.execute(query)
            results = cursor.fetchall()
            conn.close()
            return results
        except Exception as exc:
            log.error("github_ops.db_query_error", query=query, error=str(exc))
            return []

    def update_progress(self, task_id: str, status: str, notes: str = "") -> bool:
        """Update progress in PROGRESS.md."""
        try:
            content = self.read_file("PROGRESS.md")
            if not content:
                return False
            lines = content.split("\n")
            updated = False
            for i, line in enumerate(lines):
                if task_id in line:
                    if status == "done":
                        lines[i] = line.replace("⏳ TODO", "✅ DONE")
                    elif status == "in_progress":
                        lines[i] = line.replace("⏳ TODO", "🔄 IN PROGRESS")
                    updated = True
                    break
            if updated:
                new_content = "\n".join(lines)
                return self.update_file("PROGRESS.md", new_content, f"Update progress: {task_id}")
            return False
        except Exception as exc:
            log.error("github_ops.update_progress_error", task_id=task_id, error=str(exc))
            return False