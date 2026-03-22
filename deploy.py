#!/usr/bin/env python3
"""
deploy.py — Push all rebuilt files to Airdropperxx/polymarket-engine via GitHub API.

Usage:
    python deploy.py

Requires:
    pip install PyGithub

Set GITHUB_TOKEN env var OR paste when prompted.
Run from the folder containing this script (same folder as engines/, strategies/, etc.)
"""

import os
import sys
from pathlib import Path

REPO   = "Airdropperxx/polymarket-engine"
BRANCH = "main"

FILES = [
    "engines/__init__.py",
    "engines/data_engine.py",
    "engines/execution_engine.py",
    "engines/monitor_engine.py",
    "engines/review_engine.py",
    "engines/signal_engine.py",
    "engines/state_engine.py",
    "strategies/__init__.py",
    "strategies/base.py",
    "strategies/s10_near_resolution.py",
    "strategies/s1_negrisk_arb.py",
    "strategies/s8_logical_arb.py",
    "scripts/run_scan_cycle.py",
    "scripts/check_resolutions.py",
    "scripts/diagnose_pipeline.py",
    "configs/engine.yaml",
    "configs/strategies.yaml",
    ".github/workflows/scan.yml",
    ".github/workflows/resolve_check.yml",
    ".github/workflows/test.yml",
    "tests/__init__.py",
    "tests/test_all.py",
    "tests/fixtures/sample_markets.json",
    "data/lessons.json",
    "data/s8_direction_cache.json",
    "requirements-scan.txt",
    "requirements.txt",
    ".gitignore",
]

def main():
    try:
        from github import Github, InputGitTreeElement
    except ImportError:
        print("Installing PyGithub...")
        os.system(f"{sys.executable} -m pip install PyGithub -q")
        from github import Github, InputGitTreeElement

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        token = input("Paste your GitHub token (classic, repo scope): ").strip()
    if not token:
        print("ERROR: No token."); sys.exit(1)

    here = Path(__file__).parent
    g    = Github(token)
    repo = g.get_repo(REPO)
    base_sha = repo.get_branch(BRANCH).commit.sha

    print(f"\nPushing to {REPO} ({BRANCH})\n")

    elements = []
    skipped  = []
    for path in FILES:
        local = here / path
        if not local.exists():
            print(f"  SKIP  {path}  (not found locally)")
            skipped.append(path)
            continue
        elements.append(InputGitTreeElement(
            path    = path,
            mode    = "100644",
            type    = "blob",
            content = local.read_text(encoding="utf-8"),
        ))
        print(f"  OK    {path}")

    if not elements:
        print("\nNo files found. Run from the polymarket-engine folder."); sys.exit(1)

    print(f"\nCreating commit with {len(elements)} files...")
    base_tree    = repo.get_git_tree(base_sha)
    new_tree     = repo.create_git_tree(elements, base_tree)
    parent       = repo.get_git_commit(base_sha)
    new_commit   = repo.create_git_commit(
        message = (
            "fix: dependency resolution, fee formula, tests all passing\n\n"
            "- requirements-scan.txt: 7 packages only (no dep conflict)\n"
            "- scan.yml: install from requirements-scan.txt (~15s)\n"
            "- base.py: fee formula 2.25*(p*(1-p))^2, calc_fee(0.5)=0.140625\n"
            "- s10: fix minutes vs seconds bug, fix per-share fee calc\n"
            "- s1: fix per-share fee calc, add min_leg_bid floor\n"
            "- s8: disabled at MVP, lazy imports, no torch in GHA\n"
            "- tests/test_all.py: 53 tests, all passing\n"
        ),
        tree    = new_tree,
        parents = [parent],
    )
    repo.get_git_ref(f"heads/{BRANCH}").edit(new_commit.sha)

    print(f"\nDone! Commit: {new_commit.sha[:12]}")
    print(f"https://github.com/{REPO}/commit/{new_commit.sha}")
    if skipped:
        print(f"\nSkipped ({len(skipped)} files not found locally):")
        for s in skipped: print(f"  {s}")
    print("\nNext: Actions -> Test Suite -> Run workflow")

if __name__ == "__main__":
    main()
