"""
config.py - Centralized Configuration
Stripe Minions Replica - Settings Layer
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class LLMConfig:
    backend: str = "auto"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    claude_model: str = "claude-3-5-sonnet-20241022"
    openai_model: str = "gpt-4o"
    max_tokens: int = 4096
    max_tool_turns: int = 10


@dataclass
class SandboxConfig:
    pool_size: int = 3
    base_image: str = "python:3.11-slim"
    memory_limit: str = "512m"
    network_mode: str = "none"
    timeout_seconds: int = 300


@dataclass
class OrchestratorConfig:
    max_retries: int = 2
    repo_path: str = "."
    enable_pr_creation: bool = True
    default_reviewers: list = field(default_factory=list)
    default_base_branch: str = "main"


@dataclass
class GitHubConfig:
    token: str = ""
    repo: str = ""
    minion_label: str = "minion-task"
    pr_label: str = "minion-generated"


@dataclass
class SlackConfig:
    bot_token: str = ""
    app_token: str = ""
    default_channel: str = "#engineering"


@dataclass
class MinionConfig:
    llm: LLMConfig
    sandbox: SandboxConfig
    orchestrator: OrchestratorConfig
    github: GitHubConfig
    slack: SlackConfig
    debug: bool = False
    log_level: str = "INFO"


def load_config() -> MinionConfig:
    return MinionConfig(
        llm=LLMConfig(
            backend=os.getenv("LLM_BACKEND", "auto"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
            max_tool_turns=int(os.getenv("LLM_MAX_TOOL_TURNS", "10"))
        ),
        sandbox=SandboxConfig(
            pool_size=int(os.getenv("SANDBOX_POOL_SIZE", "3")),
            base_image=os.getenv("SANDBOX_BASE_IMAGE", "python:3.11-slim"),
            memory_limit=os.getenv("SANDBOX_MEMORY", "512m"),
            timeout_seconds=int(os.getenv("SANDBOX_TIMEOUT", "300"))
        ),
        orchestrator=OrchestratorConfig(
            max_retries=int(os.getenv("MAX_RETRIES", "2")),
            repo_path=os.getenv("REPO_PATH", "."),
            enable_pr_creation=os.getenv("ENABLE_PR", "true").lower() == "true",
            default_base_branch=os.getenv("DEFAULT_BASE_BRANCH", "main")
        ),
        github=GitHubConfig(
            token=os.getenv("GITHUB_TOKEN", ""),
            repo=os.getenv("GITHUB_REPO", ""),
            minion_label=os.getenv("GITHUB_MINION_LABEL", "minion-task")
        ),
        slack=SlackConfig(
            bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
            app_token=os.getenv("SLACK_APP_TOKEN", ""),
            default_channel=os.getenv("SLACK_CHANNEL", "#engineering")
        ),
        debug=os.getenv("DEBUG", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "INFO")
    )


config = load_config()
