#!/usr/bin/env python3
"""
mcp_server/server.py
====================
MCP (Model Context Protocol) server for Polymarket Engine.
Provides tools for AI agents to interact with GitHub issues and manage tasks.

Uses stdio transport - reads JSON requests from stdin, writes JSON responses to stdout.
"""
import json
import os
import sys
from pathlib import Path

from mcp_server.github_ops import GitHubOps
from mcp_server.task_manager import TaskManager
from mcp_server.audit import AuditLog
from mcp_server.memory import Memory


def main():
    """Main entry point for MCP server."""
    token = os.environ.get("GITHUB_TOKEN")
    owner = os.environ.get("GITHUB_OWNER")
    repo = os.environ.get("GITHUB_REPO")

    if not token or not owner or not repo:
        print(json.dumps({"error": "Missing GITHUB_TOKEN, GITHUB_OWNER, or GITHUB_REPO"}), flush=True)
        sys.exit(1)

    gh = GitHubOps(token, owner, repo)
    tm = TaskManager(gh)
    audit = AuditLog("data/audit.jsonl")
    memory = Memory("data/lessons.json", gh)

    tools = {
        "get_next_task": tm.get_next_task,
        "claim_task": tm.claim_task,
        "complete_task": tm.complete_task,
        "read_lessons": memory.read,
        "write_lessons": memory.write,
        "read_progress": gh.read_file,
        "update_progress": gh.update_progress,
        "get_open_positions": lambda: gh.read_db_query("SELECT * FROM trades WHERE status='open'"),
        "get_balance": lambda: gh.read_db_query("SELECT current_usdc FROM balance WHERE id=1"),
        "log_audit": audit.log,
    }

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"error": "Invalid JSON"}), flush=True)
            continue

        tool = request.get("tool")
        args = request.get("args", {})

        audit.log(tool, args)

        try:
            if tool in tools:
                if tool in ("read_progress",):
                    result = tools[tool](args.get("file_path", "PROGRESS.md"))
                else:
                    result = tools[tool](**args)
            else:
                result = {"error": f"Unknown tool: {tool}"}
        except Exception as e:
            result = {"error": str(e)}

        print(json.dumps({"result": result}), flush=True)


if __name__ == "__main__":
    main()