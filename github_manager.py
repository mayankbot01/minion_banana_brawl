"""
github_manager.py - GitHub PR Creation and Branch Management
Stripe Minions Replica - The Publishing Layer

Handles all GitHub operations:
- Creating branches programmatically
- Committing agent-generated code
- Opening Pull Requests with structured templates
- Fetching issue context from GitHub Issues
"""

import os
import json
from typing import Optional
from dataclasses import dataclass

try:
    from github import Github, GithubException
    PYGITHUB_AVAILABLE = True
except ImportError:
    PYGITHUB_AVAILABLE = False
    print("[GitHubManager] PyGithub not installed. PR creation will be disabled.")
    print("[GitHubManager] Install with: pip install PyGithub")


@dataclass
class PRResult:
    """Result of a PR creation attempt."""
    success: bool
    pr_url: str = ""
    pr_number: int = 0
    error: str = ""


class GitHubManager:
    """
    GitHub integration layer.
    Uses PyGithub to interact with the GitHub API.
    Requires GITHUB_TOKEN and GITHUB_REPO env vars.
    """

    def __init__(self):
        self.token = os.getenv("GITHUB_TOKEN", "")
        self.repo_name = os.getenv("GITHUB_REPO", "")  # format: "owner/repo"
        self._gh = None
        self._repo = None

        if PYGITHUB_AVAILABLE and self.token and self.repo_name:
            try:
                self._gh = Github(self.token)
                self._repo = self._gh.get_repo(self.repo_name)
                print(f"[GitHubManager] Connected to repo: {self.repo_name}")
            except Exception as e:
                print(f"[GitHubManager] Failed to connect to GitHub: {e}")
        else:
            print("[GitHubManager] Running in offline mode (set GITHUB_TOKEN + GITHUB_REPO to enable).")

    def create_pr(self, branch: str, title: str, body: str,
                  base: str = "main", reviewers: list[str] = None) -> str:
        """
        Create a Pull Request on GitHub.
        Returns the PR URL or a descriptive fallback string.
        """
        if not self._repo:
            return f"[Offline] PR would be: {title} from branch {branch}"

        try:
            pr = self._repo.create_pull(
                title=title,
                body=body,
                head=branch,
                base=base,
                draft=False
            )

            # Request reviewers if specified
            if reviewers:
                try:
                    pr.create_review_request(reviewers=reviewers)
                except Exception as e:
                    print(f"[GitHubManager] Could not add reviewers: {e}")

            # Add labels
            try:
                self._ensure_label("minion-generated", "0075ca")
                pr.add_to_labels("minion-generated")
            except Exception:
                pass  # Labels are optional

            print(f"[GitHubManager] PR #{pr.number} created: {pr.html_url}")
            return pr.html_url

        except GithubException as e:
            error_msg = f"GitHub API error: {e.status} - {e.data}"
            print(f"[GitHubManager] {error_msg}")
            return f"[PR Failed] {error_msg}"

    def get_issue_context(self, issue_number: int) -> dict:
        """
        Fetch issue details from GitHub to build task context.
        This is how Stripe triggers Minions from a GitHub Issue.
        """
        if not self._repo:
            return {"issue_text": f"Issue #{issue_number} (offline mode)", "labels": [], "comments": []}

        try:
            issue = self._repo.get_issue(issue_number)
            comments = [c.body for c in issue.get_comments()]
            return {
                "issue_text": f"{issue.title}\n\n{issue.body or ''}",
                "labels": [l.name for l in issue.labels],
                "comments": comments[:5],  # Cap at 5 comments
                "issue_url": issue.html_url,
                "author": issue.user.login
            }
        except Exception as e:
            return {"issue_text": f"Error fetching issue #{issue_number}: {e}", "labels": [], "comments": []}

    def get_open_issues_with_label(self, label: str) -> list[dict]:
        """
        Find all open issues with a specific label (e.g. 'minion-task').
        Used for batch processing of queued tasks.
        """
        if not self._repo:
            return []

        issues = []
        try:
            for issue in self._repo.get_issues(state="open", labels=[label]):
                issues.append({
                    "number": issue.number,
                    "title": issue.title,
                    "body": issue.body or "",
                    "labels": [l.name for l in issue.labels],
                    "url": issue.html_url
                })
        except Exception as e:
            print(f"[GitHubManager] Error listing issues: {e}")
        return issues

    def close_issue_with_comment(self, issue_number: int, pr_url: str, task_id: str):
        """Close a GitHub issue and link the created PR."""
        if not self._repo:
            return
        try:
            issue = self._repo.get_issue(issue_number)
            issue.create_comment(
                f"This issue was resolved autonomously by the Minion system.\n"
                f"Task ID: `{task_id}`\n"
                f"Pull Request: {pr_url}\n\n"
                f"Please review the PR before merging."
            )
            issue.edit(state="closed")
            print(f"[GitHubManager] Issue #{issue_number} closed.")
        except Exception as e:
            print(f"[GitHubManager] Could not close issue: {e}")

    def get_repo_info(self) -> dict:
        """Get basic repo information for context hydration."""
        if not self._repo:
            return {"name": self.repo_name or "unknown", "default_branch": "main"}
        return {
            "name": self._repo.full_name,
            "default_branch": self._repo.default_branch,
            "language": self._repo.language,
            "topics": self._repo.get_topics()
        }

    def _ensure_label(self, name: str, color: str):
        """Create a label if it doesn't exist."""
        try:
            self._repo.get_label(name)
        except GithubException:
            self._repo.create_label(name=name, color=color)

    @property
    def is_connected(self) -> bool:
        return self._repo is not None
