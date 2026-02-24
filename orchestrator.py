"""
orchestrator.py - Blueprint Finite State Machine (Full Upgrade)
Stripe Minions Replica - The Director

The central Blueprint execution loop. Ties together:
- ContextEngine (DAG-based context hydration)
- MCP Server (curated tool injection)
- SandboxPool (isolated VM execution)
- MinionAgent (LLM brain)
- GitHubManager (PR creation)

Implements Stripe's strict 2-retry limit to prevent infinite loops
and uncontrolled token spend.
"""

import os
import time
import uuid
import json
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

from mcp_server import MCPServer
from context_engine import ContextEngine
from sandbox import get_pool, Sandbox
from agent import MinionAgent, AgentPlan


class TaskStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    LINTING = auto()
    TESTING = auto()
    SUCCESS = auto()
    ESCALATED = auto()
    FAILED = auto()


@dataclass
class TaskResult:
    """Final outcome of a Minion task execution."""
    task_id: str
    status: TaskStatus
    message: str
    pr_url: str = ""
    attempts: int = 0
    duration_seconds: float = 0.0
    plan_md: str = ""
    agent_explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status.name,
            "message": self.message,
            "pr_url": self.pr_url,
            "attempts": self.attempts,
            "duration_seconds": round(self.duration_seconds, 2),
            "agent_explanation": self.agent_explanation
        }


@dataclass
class MinionTask:
    """Input specification for a single Minion task."""
    issue_text: str                         # The bug/feature description
    target_files: list[str] = field(default_factory=list)  # Files to focus on
    domain: str = "testing"                 # MCP tool domain: testing/linting/git
    task_id: str = field(default_factory=lambda: f"minion-{uuid.uuid4().hex[:8]}")
    slack_channel: str = ""                 # For result notifications
    pr_reviewers: list[str] = field(default_factory=list)
    priority: str = "normal"                # normal / high / low


class MinionOrchestrator:
    """
    The Blueprint Orchestrator.

    FSM States:
    PENDING -> RUNNING -> LINTING -> TESTING -> SUCCESS
                  |           |          |
                  +----RETRY--+----------+  (max 2 retries)
                  |
                  v
              ESCALATED (human takes over)

    This matches Stripe's documented architecture:
    - Max 2 retry rounds
    - Deterministic gates (linter -> tests) between LLM steps
    - Sandbox destroyed after every run
    - PR created only on full success
    """

    MAX_RETRIES = 2  # Stripe's documented limit

    def __init__(self, repo_path: str = "."):
        self.repo_path = repo_path
        print("[Orchestrator] Initializing Minion system...")

        # Initialize all subsystems
        self.mcp = MCPServer()
        self.context_engine = ContextEngine(repo_root=repo_path)
        self.sandbox_pool = get_pool(pool_size=2, repo_path=repo_path)

        print("[Orchestrator] All subsystems ready.")

    def run(self, task: MinionTask) -> TaskResult:
        """
        Execute a complete Minion task through the Blueprint FSM.
        This is the main entry point for the system.
        """
        start_time = time.time()
        print(f"\n{'='*60}")
        print(f"[Orchestrator] TASK {task.task_id} STARTED")
        print(f"[Orchestrator] Issue: {task.issue_text[:100]}")
        print(f"[Orchestrator] Domain: {task.domain}")
        print(f"{'='*60}\n")

        # 1. Acquire a pre-warmed sandbox
        sandbox = self.sandbox_pool.acquire()
        status = TaskStatus.RUNNING

        try:
            # 2. Hydrate context via AST/DAG
            print("[Orchestrator] Step 1/5: Hydrating context from codebase DAG...")
            context = self.context_engine.hydrate_for_task(
                issue_text=task.issue_text,
                target_files=task.target_files
            )

            # 3. Get curated tools for this domain (NOT all tools)
            print(f"[Orchestrator] Step 2/5: Curating tools for domain '{task.domain}'...")
            tools = self.mcp.get_tools_for_domain(task.domain)

            # 4. Initialize agent with curated tools
            agent = MinionAgent(tools=tools)
            agent.hydrate(context)

            # 5. Blueprint execution loop with strict retry cap
            print(f"[Orchestrator] Step 3/5: Starting Blueprint loop (max {self.MAX_RETRIES} retries)...")
            plan = None
            last_error = ""

            for attempt in range(self.MAX_RETRIES + 1):
                print(f"\n[Orchestrator] --- Attempt {attempt + 1}/{self.MAX_RETRIES + 1} ---")

                # Agent reasons and generates code
                plan = agent.plan_and_write_code(mcp_server=self.mcp)

                if not plan.code_patch:
                    last_error = f"Agent produced empty code patch on attempt {attempt + 1}"
                    agent.feed_error(last_error, attempt + 1)
                    continue

                # Write the patch into the sandbox
                print(f"[Orchestrator] Writing patch to sandbox: {plan.filepath}")
                sandbox.write_file(plan.filepath, plan.code_patch)

                # Deterministic Gate 1: LINTING (< 5 seconds, cheap)
                status = TaskStatus.LINTING
                print(f"[Orchestrator] Deterministic Gate 1: Linting {plan.filepath}...")
                lint_result = sandbox.run_linter(plan.filepath)

                if not lint_result.success:
                    last_error = f"LINT FAILED:\n{lint_result.stdout}\n{lint_result.stderr}"
                    print(f"[Orchestrator] Lint FAILED on attempt {attempt + 1}. Feeding error to agent.")
                    agent.feed_error(last_error, attempt + 1)
                    status = TaskStatus.RUNNING
                    continue  # Shift-left: retry cheaply

                print("[Orchestrator] Lint PASSED.")

                # Deterministic Gate 2: SELECTIVE CI TESTS
                status = TaskStatus.TESTING
                affected_tests = context.get("affected_tests", [])

                if affected_tests:
                    print(f"[Orchestrator] Deterministic Gate 2: Running {len(affected_tests)} selective tests...")
                    test_result = sandbox.run_tests(affected_tests)
                else:
                    print("[Orchestrator] Deterministic Gate 2: No affected tests found, running smoke test...")
                    test_result = sandbox.run_tests(["tests/"] if os.path.exists("tests/") else [])

                if not test_result.success:
                    last_error = f"TESTS FAILED:\n{test_result.stdout[-1500:]}\n{test_result.stderr[-500:]}"
                    print(f"[Orchestrator] Tests FAILED on attempt {attempt + 1}. Feeding error to agent.")
                    agent.feed_error(last_error, attempt + 1)
                    status = TaskStatus.RUNNING
                    continue

                print("[Orchestrator] Tests PASSED.")

                # All gates passed -> SUCCESS
                status = TaskStatus.SUCCESS
                break

            # 6. Post-execution handling
            duration = time.time() - start_time

            if status == TaskStatus.SUCCESS and plan:
                print(f"\n[Orchestrator] Step 4/5: All gates passed! Creating PR...")
                pr_url = self._create_pull_request(task, plan, sandbox)

                print(f"[Orchestrator] Step 5/5: Task complete in {duration:.1f}s.")
                return TaskResult(
                    task_id=task.task_id,
                    status=TaskStatus.SUCCESS,
                    message=f"Task completed successfully in {attempt + 1} attempt(s).",
                    pr_url=pr_url,
                    attempts=attempt + 1,
                    duration_seconds=duration,
                    agent_explanation=plan.explanation if plan else ""
                )
            else:
                # Escalate to human after max retries
                print(f"[Orchestrator] ESCALATING: Task unresolved after {self.MAX_RETRIES} retries.")
                return TaskResult(
                    task_id=task.task_id,
                    status=TaskStatus.ESCALATED,
                    message=f"Escalated to human after {self.MAX_RETRIES} retries. Last error: {last_error[:200]}",
                    attempts=self.MAX_RETRIES + 1,
                    duration_seconds=duration
                )

        except Exception as e:
            duration = time.time() - start_time
            print(f"[Orchestrator] UNEXPECTED ERROR: {e}")
            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                message=f"System error: {str(e)}",
                duration_seconds=duration
            )
        finally:
            # ALWAYS destroy the sandbox - no state contamination
            print("[Orchestrator] Releasing sandbox...")
            self.sandbox_pool.release(sandbox, reuse=False)

    def _create_pull_request(self, task: MinionTask, plan: AgentPlan, sandbox: Sandbox) -> str:
        """
        Create a GitHub Pull Request with the agent's changes.
        Returns the PR URL or a placeholder if GitHub is not configured.
        """
        try:
            from github_manager import GitHubManager
            gh = GitHubManager()
            branch_name = f"minion/{task.task_id}/{task.domain}"

            # Create the branch and commit
            sandbox.exec(f"git checkout -b {branch_name}")
            sandbox.exec("git add -A")
            sandbox.exec(f'git commit -m "[Minion] {task.issue_text[:60]}')
            sandbox.exec(f"git push origin {branch_name}")

            pr_url = gh.create_pr(
                branch=branch_name,
                title=f"[Minion] {task.issue_text[:60]}",
                body=self._build_pr_body(task, plan),
                reviewers=task.pr_reviewers
            )
            return pr_url
        except Exception as e:
            print(f"[Orchestrator] PR creation skipped: {e}")
            return f"[PR creation skipped - configure GITHUB_TOKEN. Change: {plan.filepath}]"

    def _build_pr_body(self, task: MinionTask, plan: AgentPlan) -> str:
        """Build a structured PR description following Stripe's template."""
        return f"""## Minion-Generated PR

**Task ID:** `{task.task_id}`
**Priority:** {task.priority}
**Domain:** {task.domain}

### Issue
{task.issue_text}

### What Changed
{plan.explanation}

### Files Modified
- `{plan.filepath}`

### Confidence
{plan.confidence:.0%} (AI self-assessed)

### Review Notes
- This PR was autonomously generated by the Minion system
- All deterministic gates (linter + selective tests) passed
- Please verify the logic change before merging
- Task completed at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}

---
*Generated by minion_banana_brawl - Stripe Minions Replica*
"""

    def run_batch(self, tasks: list[MinionTask]) -> list[TaskResult]:
        """Run multiple tasks sequentially (parallel support via threading is trivial)."""
        results = []
        for i, task in enumerate(tasks):
            print(f"\n[Orchestrator] BATCH: Task {i+1}/{len(tasks)}")
            result = self.run(task)
            results.append(result)
            print(f"[Orchestrator] Task {task.task_id}: {result.status.name}")
        return results


if __name__ == "__main__":
    # Demo run
    orchestrator = MinionOrchestrator(repo_path=".")

    task = MinionTask(
        issue_text="Fix NullPointerException in banana inventory module when stock is 0",
        target_files=["devbox.py"],
        domain="testing",
        priority="high"
    )

    result = orchestrator.run(task)
    print(f"\n{'='*60}")
    print(f"FINAL RESULT: {json.dumps(result.to_dict(), indent=2)}")
    print(f"{'='*60}")
