#!/usr/bin/env python3
"""
scripts/bootstrap_github.py
============================
Run ONCE to create all GitHub Issues, labels, and milestones from TASKS.yaml.

Triggered by: .github/workflows/bootstrap.yml (workflow_dispatch only)
Also runnable locally:
    GITHUB_TOKEN=ghp_... GITHUB_OWNER=you GITHUB_REPO=polymarket-engine \\
    python scripts/bootstrap_github.py

SKIP RULE: If an issue with the same title already exists, it is skipped.
Re-running this script is always safe — no duplicates created.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from github import Github, GithubException


def _rate_limit_wait(g: Github) -> None:
    """Check rate limit and wait if needed."""
    rate_limit = g.get_rate_limit()
    core = rate_limit.core
    if core.remaining < 10:
        wait_time = (core.reset - time.time()) + 1
        if wait_time > 0:
            print(f"  [RATE LIMIT] Waiting {wait_time:.0f}s for API limit reset...")
            time.sleep(wait_time)


def _safe_api_call(func, *args, max_retries: int = 3, **kwargs):
    """Execute an API call with retry logic for rate limits."""
    for attempt in range(max_retries):
        try:
            _rate_limit_wait(func.__self__ if hasattr(func, '__self__') else None)
            return func(*args, **kwargs)
        except GithubException as exc:
            if exc.status == 403 and "rate limit" in str(exc.data).lower():
                wait_time = int(exc.headers.get("X-RateLimit-Reset", 0)) - time.time() + 1
                if wait_time > 0 and attempt < max_retries - 1:
                    print(f"  [RATE LIMIT] Got 403, waiting {wait_time:.0f}s...")
                    time.sleep(wait_time)
                    continue
            raise
    return None


def main() -> None:
    token = os.environ["GITHUB_TOKEN"]
    owner = os.environ["GITHUB_OWNER"]
    repo_name = os.environ["GITHUB_REPO"]

    g    = Github(token)
    repo = g.get_repo(f"{owner}/{repo_name}")

    tasks_data = yaml.safe_load(open("TASKS.yaml"))

    # ── 1. Create labels ─────────────────────────────────────────────────────
    print("Creating labels...")
    existing_labels = {lbl.name for lbl in repo.get_labels()}

    for label_def in tasks_data.get("labels", []):
        name = label_def["name"]
        if name in existing_labels:
            print(f"  [SKIP] label: {name}")
            continue
        try:
            repo.create_label(
                name=name,
                color=label_def.get("color", "ededed"),
                description=label_def.get("description", ""),
            )
            print(f"  [OK]   label: {name}")
            time.sleep(0.5)  # rate limit courtesy
        except GithubException as exc:
            if exc.status == 403:
                print(f"  [RATE LIMIT] Waiting 60s before retry...")
                time.sleep(60)
                try:
                    repo.create_label(
                        name=name,
                        color=label_def.get("color", "ededed"),
                        description=label_def.get("description", ""),
                    )
                    print(f"  [OK]   label: {name}")
                except GithubException as exc2:
                    print(f"  [ERR]  label: {name} — {exc2.data.get('message', str(exc2))}")
            else:
                print(f"  [ERR]  label: {name} — {exc.data.get('message', str(exc))}")

    # ── 2. Create milestones ──────────────────────────────────────────────────
    print("\nCreating milestones...")
    existing_milestones = {m.title: m for m in repo.get_milestones(state="open")}
    milestone_map: dict[str, object] = {}

    for ms_def in tasks_data.get("milestones", []):
        title = ms_def["title"]
        if title in existing_milestones:
            print(f"  [SKIP] milestone: {title}")
            milestone_map[title] = existing_milestones[title]
            continue
        try:
            ms = repo.create_milestone(
                title=title,
                description=ms_def.get("description", ""),
            )
            milestone_map[title] = ms
            print(f"  [OK]   milestone: {title}")
            time.sleep(0.2)
        except GithubException as exc:
            print(f"  [ERR]  milestone: {title} — {exc.data.get('message', str(exc))}")

    # ── 3. Create issues ──────────────────────────────────────────────────────
    print("\nCreating issues...")

    # Build set of existing issue titles to avoid duplicates
    existing_titles = set()
    for issue in repo.get_issues(state="all"):
        existing_titles.add(issue.title)

    phase_to_milestone = {
        "MVP": "v0.1.0-mvp",
        "P2":  "v0.2.0",
        "P3":  "v0.3.0",
        "P4":  "v1.0.0-production",
    }

    created = 0
    skipped = 0

    for task in tasks_data.get("tasks", []):
        task_id = task["id"]
        title   = f"[{task_id}] {task['title']}"

        if title in existing_titles:
            print(f"  [SKIP] {title}")
            skipped += 1
            continue

        # Build issue body
        body = _build_issue_body(task)

        # Resolve labels
        label_names = task.get("labels", [])
        issue_labels = []
        for lname in label_names:
            try:
                issue_labels.append(repo.get_label(lname))
            except GithubException:
                pass  # Label may not exist yet — skip

        # Resolve milestone
        ms_title = phase_to_milestone.get(task.get("phase", ""))
        milestone = milestone_map.get(ms_title) if ms_title else None

        try:
            kwargs = {
                "title": title,
                "body":  body,
                "labels": issue_labels,
            }
            if milestone:
                kwargs["milestone"] = milestone

            repo.create_issue(**kwargs)
            print(f"  [OK]   {title}")
            created += 1
            time.sleep(0.5)  # GitHub API rate limiting
        except GithubException as exc:
            print(f"  [ERR]  {title} — {exc.data.get('message', str(exc))}")

    print(f"\nDone. Created: {created} | Skipped (already exist): {skipped}")


def _build_issue_body(task: dict) -> str:
    """Build the GitHub Issue body from a task dict."""
    lines = [
        f"## {task['id']} — {task['title']}",
        "",
        f"**Phase:** {task.get('phase', '?')} | "
        f"**Priority:** {task.get('priority', '?')} | "
        f"**Context Pack:** {task.get('context_pack', '?')}",
        f"**Estimated tokens:** {task.get('estimated_tokens', '?')} | "
        f"**Depends on:** {', '.join(task.get('depends_on', [])) or 'none'}",
        "",
        "## Context",
        "",
        task.get("description", "").strip(),
        "",
    ]

    files = task.get("files", [])
    if files:
        lines += ["## Files to Create/Modify", ""]
        for f in files:
            lines.append(f"- `{f}`")
        lines.append("")

    criteria = task.get("acceptance_criteria", [])
    if criteria:
        lines += ["## Acceptance Criteria", ""]
        for c in criteria:
            lines.append(f"- [ ] {c}")
        lines.append("")

    lines += [
        "## Agent Instructions",
        "",
        "1. Read the full task context above",
        "2. Implement exactly as specified — no extra features",
        "3. Run the verification commands listed in acceptance criteria",
        "4. Check off all acceptance criteria above",
        f"5. Commit with: `[feat/fix/config/test]({task.get('id', 'scope').lower()}): description`",
        "6. Close this issue via `complete_task()` MCP tool or manually",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    main()
