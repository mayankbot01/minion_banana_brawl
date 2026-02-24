"""
slack_trigger.py - Slack Bot Entry Point
Stripe Minions Replica - The Trigger Layer

Listens for @mentions in Slack channels and triggers Minion tasks.
Format: @minion <description of task> [files: file1.py, file2.py]

This is how engineers interact with the Minion system at Stripe -
they mention @minion in a Slack thread attached to a bug or feature.
"""

import os
import re
import json
import threading
from typing import Optional

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    SLACK_AVAILABLE = True
except ImportError:
    SLACK_AVAILABLE = False
    print("[SlackTrigger] slack-bolt not installed.")
    print("[SlackTrigger] Install with: pip install slack-bolt")


class MinionSlackBot:
    """
    Slack bot that listens for @minion mentions and kicks off Minion tasks.

    Usage in Slack:
      @minion fix the NullPointerException in banana_inventory.py
      @minion implement the get_discount function in pricing.py
      @minion files: utils.py, models.py - add type hints to all functions
    """

    MENTION_PATTERN = re.compile(
        r"<@[A-Z0-9]+>\s*(?:files?:\s*([\w\./, ]+?)\s*-\s*)?(.+)",
        re.IGNORECASE | re.DOTALL
    )

    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator
        self._app = None
        self._handler = None

        bot_token = os.getenv("SLACK_BOT_TOKEN", "")
        app_token = os.getenv("SLACK_APP_TOKEN", "")

        if SLACK_AVAILABLE and bot_token and app_token:
            self._app = App(token=bot_token)
            self._handler = SocketModeHandler(self._app, app_token)
            self._register_handlers()
            print("[SlackBot] Initialized with Socket Mode.")
        else:
            print("[SlackBot] Offline mode. Set SLACK_BOT_TOKEN + SLACK_APP_TOKEN to enable.")

    def _register_handlers(self):
        """Register all Slack event handlers."""
        app = self._app

        @app.event("app_mention")
        def handle_mention(event, say, client):
            """Handle @minion mentions in channels."""
            text = event.get("text", "")
            user = event.get("user", "unknown")
            channel = event.get("channel", "")
            thread_ts = event.get("thread_ts") or event.get("ts", "")

            print(f"[SlackBot] Mention from {user}: {text[:100]}")

            # Parse the task from the mention
            task_info = self._parse_mention(text)
            if not task_info:
                say(
                    text="Sorry, I couldn't understand that. Try: `@minion fix the bug in payments.py`",
                    thread_ts=thread_ts
                )
                return

            # Acknowledge immediately in Slack thread
            say(
                text=f":robot_face: Minion activated! Working on: *{task_info['issue_text'][:80]}*\n"
                     f"Task ID: `{task_info.get('task_id', 'generating...')}`",
                thread_ts=thread_ts
            )

            # Run task in background thread (non-blocking)
            def run_async():
                result = self._dispatch_task(task_info, channel)
                self._send_result(client, channel, thread_ts, result)

            t = threading.Thread(target=run_async, daemon=True)
            t.start()

        @app.event("message")
        def handle_dm(event, say):
            """Handle direct messages to the bot."""
            if event.get("channel_type") == "im":
                text = event.get("text", "")
                if text.lower().startswith("status"):
                    say("All Minion systems are operational. Mention me in a channel to start a task.")
                elif text.lower().startswith("help"):
                    say(self._help_message())

    def _parse_mention(self, text: str) -> Optional[dict]:
        """Parse a Slack mention into a structured task dict."""
        match = self.MENTION_PATTERN.search(text)
        if not match:
            return None

        files_str = match.group(1) or ""
        issue_text = match.group(2).strip()

        # Parse file list
        target_files = []
        if files_str:
            target_files = [f.strip() for f in re.split(r"[,\s]+", files_str) if f.strip()]

        # Auto-detect domain from keywords
        domain = "testing"
        text_lower = issue_text.lower()
        if any(w in text_lower for w in ["lint", "format", "style", "type hint", "typing"]):
            domain = "linting"
        elif any(w in text_lower for w in ["commit", "branch", "merge", "git"]):
            domain = "git"
        elif any(w in text_lower for w in ["test", "spec", "pytest", "unittest"]):
            domain = "testing"

        return {
            "issue_text": issue_text,
            "target_files": target_files,
            "domain": domain
        }

    def _dispatch_task(self, task_info: dict, slack_channel: str) -> dict:
        """Dispatch the task to the orchestrator and return the result."""
        if not self.orchestrator:
            return {
                "status": "SKIPPED",
                "message": "No orchestrator connected. Set orchestrator in MinionSlackBot constructor.",
                "task_id": "N/A"
            }

        try:
            from orchestrator import MinionTask
            task = MinionTask(
                issue_text=task_info["issue_text"],
                target_files=task_info["target_files"],
                domain=task_info["domain"],
                slack_channel=slack_channel
            )
            result = self.orchestrator.run(task)
            return result.to_dict()
        except Exception as e:
            return {"status": "FAILED", "message": str(e), "task_id": "error"}

    def _send_result(self, client, channel: str, thread_ts: str, result: dict):
        """Send the task result back to the Slack thread."""
        status = result.get("status", "UNKNOWN")
        emoji = {
            "SUCCESS": ":white_check_mark:",
            "ESCALATED": ":warning:",
            "FAILED": ":x:",
        }.get(status, ":question:")

        pr_line = f"\nPR: {result['pr_url']}" if result.get("pr_url") else ""
        attempts_line = f"\nAttempts: {result.get('attempts', '?')}"
        duration_line = f"\nDuration: {result.get('duration_seconds', 0):.1f}s"

        message = (
            f"{emoji} *Minion Task {status}*\n"
            f"Task ID: `{result.get('task_id', 'N/A')}`\n"
            f"{result.get('message', '')}\n"
            f"{pr_line}{attempts_line}{duration_line}"
        )

        if SLACK_AVAILABLE and client:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=message)
        else:
            print(f"[SlackBot] Result: {message}")

    def _help_message(self) -> str:
        return """*Minion Bot - Help*

Mention me in a channel to trigger autonomous code fixes:

> `@minion fix the NullPointerException in payments.py`
> `@minion add type hints files: utils.py, models.py`
> `@minion implement the refund endpoint in billing.py`

*How it works:*
1. I parse your request and hydrate context from the codebase
2. Claude/GPT-4o generates a code patch in an isolated sandbox
3. Linting and tests run automatically (shift-left)
4. If all checks pass, I open a PR for human review
5. If I fail after 2 retries, I escalate back to you

Max retries: 2 | Sandbox: Docker (zero network) | Backend: Claude 3.5 Sonnet"""

    def start(self):
        """Start the Slack bot in Socket Mode."""
        if self._handler:
            print("[SlackBot] Starting Slack bot in Socket Mode...")
            self._handler.start()
        else:
            print("[SlackBot] Cannot start: missing Slack credentials or slack-bolt package.")

    def simulate_mention(self, text: str, user: str = "test_user") -> dict:
        """
        Simulate a Slack mention for local testing (no Slack needed).
        Usage: bot.simulate_mention('@minion fix the bug in devbox.py')
        """
        print(f"[SlackBot] Simulating mention: {text}")
        task_info = self._parse_mention(f"<@UMINION> {text}")
        if not task_info:
            return {"error": "Could not parse mention"}
        return self._dispatch_task(task_info, slack_channel="#simulation")


if __name__ == "__main__":
    # Simulation mode test (no Slack credentials needed)
    from orchestrator import MinionOrchestrator
    orchestrator = MinionOrchestrator(repo_path=".")
    bot = MinionSlackBot(orchestrator=orchestrator)

    result = bot.simulate_mention("fix the divide by zero in devbox.py files: devbox.py")
    print(json.dumps(result, indent=2, default=str))
