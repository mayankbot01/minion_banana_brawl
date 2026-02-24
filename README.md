# Minion Banana Brawl
### A Production-Grade Replica of Stripe's Minions Autonomous Coding System

> Stripe's Minions merge 1,300+ PRs weekly with zero human-written code. This repo is a complete
> open-source replica of their architecture, built from the ground up in Python.

---

## Architecture Overview

```
 Slack / GitHub Issue / CLI
           |
           v
  [slack_trigger.py]  <--- Entry point: @minion mentions
           |
           v
  [orchestrator.py]   <--- Blueprint FSM Director
     /     |     \
    v      v      v
[context] [mcp] [sandbox]
_engine  _server  .py
           |
           v
       [agent.py]     <--- LLM Brain (Claude 3.5 Sonnet / GPT-4o)
           |
    Tool calls loop
           |
    Deterministic Gates
    +---> LINT (ruff)
    +---> TESTS (pytest, selective)
           |
     All pass?
    /         \
   Yes         No (retry max 2x)
   |           |
   v           v
[github_    ESCALATE
 manager]   to human
   |
   v
  PR Created
```

## The 6-Layer Stack

| Layer | File | Role |
|-------|------|------|
| **Trigger** | `slack_trigger.py` | @minion Slack mentions, parses task + files |
| **Orchestrator** | `orchestrator.py` | Blueprint FSM, 2-retry cap, PR creation |
| **Brain** | `agent.py` | Claude 3.5 Sonnet / GPT-4o + plan.md memory |
| **Toolshed** | `mcp_server.py` | 13 MCP tools, domain-curated injection |
| **Context** | `context_engine.py` | AST parser + DAG (BFS + Kahn's algorithm) |
| **Cage** | `sandbox.py` | Docker pool, zero-network, 512MB memory cap |

---

## Quick Start

### 1. Clone & Install
```bash
git clone https://github.com/mayankbot01/minion_banana_brawl.git
cd minion_banana_brawl
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env and set your ANTHROPIC_API_KEY or OPENAI_API_KEY
```

### 3. Run (no API key - simulation mode)
```bash
python orchestrator.py
```

### 4. Run with real LLM
```bash
# Set your key in .env, then:
export ANTHROPIC_API_KEY=sk-ant-...
python orchestrator.py
```

### 5. Run via Slack
```bash
# Set SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env, then:
python slack_trigger.py
# Then in Slack: @minion fix the divide-by-zero in devbox.py
```

---

## How It Works

### The Blueprint Loop (orchestrator.py)

The core is a **Finite State Machine** that strictly alternates between:
1. **LLM Step** - Agent reasons and writes code (open-ended)
2. **Deterministic Gate 1** - `ruff` linter (fast, < 5 seconds)
3. **Deterministic Gate 2** - `pytest` on selective affected tests

If either gate fails, the error is fed back to the agent and it retries.
**Hard limit: 2 retries.** After that, it escalates to a human.

```python
for attempt in range(MAX_RETRIES + 1):  # MAX_RETRIES = 2
    plan = agent.plan_and_write_code()  # LLM step
    sandbox.write_file(plan.filepath, plan.code_patch)

    if not sandbox.run_linter(plan.filepath).success:  # Gate 1
        agent.feed_error(lint_error)
        continue

    if not sandbox.run_tests(affected_tests).success:  # Gate 2
        agent.feed_error(test_error)
        continue

    return create_pull_request()  # SUCCESS

return escalate_to_human()  # All retries exhausted
```

### Context Engine (context_engine.py)

Instead of naive vector-search RAG, this builds a **real AST-based DAG**:

- **Forward BFS**: Get all dependencies a file needs (upstream context)
- **Reverse BFS**: Get all files that depend on a changed file (selective tests)
- **Kahn's Algorithm**: Topological sort for dependency-first context assembly

This means the agent only sees the exact context it needs, keeping token costs low.

### MCP Toolshed (mcp_server.py)

Follows Stripe's key insight: **never give the LLM all tools at once.**

- 13 total tools registered: `read_file`, `write_file`, `run_python_linter`, `run_tests`, `git_diff`, `search_codebase`, etc.
- Only 4-7 domain-specific tools injected per task
- Always includes `read_plan` + `update_plan` (memory tools)

### plan.md Memory System (agent.py)

Prevents **context rot** in long reasoning chains:
1. Before writing code, agent calls `update_plan` to write strategy to `plan.md`
2. On each turn, agent calls `read_plan` to resume from where it left off
3. The orchestrator can inspect `plan.md` to understand agent progress

### Sandbox Pool (sandbox.py)

- **Pre-warmed pool** of N Docker containers (default: 3)
- All containers: `network_mode=none` (zero internet), 512MB RAM, 50% CPU
- Repo mounted as **read-only** volume
- Container **always destroyed** after task (never reused between tasks)
- Background thread auto-refills pool after each acquisition

---

## File Reference

```
minion_banana_brawl/
├── orchestrator.py      # Blueprint FSM - main entry point
├── agent.py             # LLM brain (Claude/GPT-4o + tool calling)
├── mcp_server.py        # MCP Toolshed - 13 domain tools
├── context_engine.py    # AST parser + DAG dependency graph
├── sandbox.py           # Pre-warmed Docker pool manager
├── github_manager.py    # PR creation + issue tracking
├── slack_trigger.py     # Slack @minion bot
├── config.py            # Centralized env-based config
├── devbox.py            # Legacy DevBox wrapper (kept for compatibility)
├── requirements.txt     # All Python dependencies
└── .env.example         # Environment variable template
```

---

## Configuration

All configuration via environment variables. See `.env.example` for full reference.

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | - | Claude API key (recommended backend) |
| `OPENAI_API_KEY` | - | OpenAI API key (fallback backend) |
| `LLM_BACKEND` | `auto` | `auto` / `claude` / `openai` / `simulation` |
| `GITHUB_TOKEN` | - | GitHub PAT for PR creation |
| `GITHUB_REPO` | - | `owner/repo` format |
| `SLACK_BOT_TOKEN` | - | Slack bot token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | - | Slack app token (`xapp-...`) |
| `MAX_RETRIES` | `2` | **Do not change above 2** |
| `SANDBOX_POOL_SIZE` | `3` | Pre-warmed container count |

---

## Key Design Principles (from Stripe Engineering)

1. **Shift Left**: Catch errors cheap (linter < 5s) before running expensive tests
2. **Context Curation**: Give the LLM ONLY what it needs (10-15 tools max, not all 50)
3. **Hard Retry Cap**: 2 retries, period. Prevents token burn + infinite loops
4. **Zero-Trust Sandbox**: Containers have no internet, no production access
5. **Human in the Loop**: Agent OPENS PRs, never merges them autonomously
6. **plan.md Memory**: Persistent task state prevents context rot across tool turns

---

## Extending the System

### Add a new MCP Tool
```python
# In mcp_server.py
self._register(MCPTool(
    name="my_custom_tool",
    description="What this tool does",
    domain="testing",  # or linting / git / filesystem / search / memory
    handler=self._my_custom_handler,
    schema={"type": "object", "properties": {"arg": {"type": "string"}}, "required": ["arg"]}
))
```

### Trigger from GitHub Issues
```python
from github_manager import GitHubManager
from orchestrator import MinionOrchestrator, MinionTask

gh = GitHubManager()
orchestrator = MinionOrchestrator()

# Poll for issues labeled 'minion-task'
for issue in gh.get_open_issues_with_label("minion-task"):
    task = MinionTask(issue_text=issue["title"] + "\n" + issue["body"])
    result = orchestrator.run(task)
    gh.close_issue_with_comment(issue["number"], result.pr_url, result.task_id)
```

---

## Credits

Inspired by Stripe's Engineering blog post on their Minions autonomous coding system.
Core agent framework inspired by Block's open-source [Goose](https://github.com/block/goose) project.

---

*Built by [@mayankbot01](https://github.com/mayankbot01)*
