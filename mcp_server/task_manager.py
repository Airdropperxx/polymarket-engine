"""Task management for MCP server."""

import structlog
from mcp_server.github_ops import GitHubOps

log = structlog.get_logger()


class TaskManager:
    """Manages GitHub issues as tasks."""

    def __init__(self, github_ops: GitHubOps):
        self._gh = github_ops

    def get_next_task(self) -> dict:
        """Get the highest-priority unclaimed task (status:todo)."""
        try:
            issues = self._gh.get_issues_by_label("status:todo")
            if not issues:
                return {"error": "No tasks found", "tasks": []}
            sorted_issues = sorted(
                issues,
                key=lambda i: (
                    int(i.number),
                )
            )
            issue = sorted_issues[0]
            return {
                "task_id": f"TASK-{issue.number}",
                "title": issue.title,
                "number": issue.number,
                "url": issue.html_url,
                "labels": [l.name for l in issue.labels],
            }
        except Exception as exc:
            log.error("task_manager.get_next_task_error", error=str(exc))
            return {"error": str(exc)}

    def claim_task(self, task_id: str) -> dict:
        """Claim a task by changing label to status:in-progress."""
        try:
            task_num = int(task_id.replace("TASK-", ""))
            self._gh.add_label_to_issue(task_num, "status:in-progress")
            self._gh.remove_label_from_issue(task_num, "status:todo")
            log.info("task_manager.task_claimed", task_id=task_id)
            return {"status": "claimed", "task_id": task_id}
        except Exception as exc:
            log.error("task_manager.claim_task_error", task_id=task_id, error=str(exc))
            return {"error": str(exc)}

    def complete_task(self, task_id: str, notes: str = "") -> dict:
        """Complete a task by closing the issue and adding completion comment."""
        try:
            task_num = int(task_id.replace("TASK-", ""))
            comment = f"Task completed. Notes: {notes}" if notes else "Task completed."
            self._gh.add_comment_to_issue(task_num, comment)
            self._gh.remove_label_from_issue(task_num, "status:in-progress")
            self._gh.add_label_to_issue(task_num, "status:done")
            self._gh.close_issue(task_num)
            log.info("task_manager.task_completed", task_id=task_id)
            return {"status": "completed", "task_id": task_id}
        except Exception as exc:
            log.error("task_manager.complete_task_error", task_id=task_id, error=str(exc))
            return {"error": str(exc)}

    def get_task_status(self, task_id: str) -> dict:
        """Get the current status of a task."""
        try:
            task_num = int(task_id.replace("TASK-", ""))
            issue = self._gh.get_issue(task_num)
            if not issue:
                return {"error": "Task not found"}
            return {
                "task_id": task_id,
                "title": issue.title,
                "state": issue.state,
                "labels": [l.name for l in issue.labels],
            }
        except Exception as exc:
            log.error("task_manager.get_task_status_error", task_id=task_id, error=str(exc))
            return {"error": str(exc)}