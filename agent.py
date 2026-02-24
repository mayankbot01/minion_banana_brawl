"""
agent.py - Full LLM-Powered Minion Agent (Upgraded)
Stripe Minions Replica - The Brain (Goose Fork)

Supports both Claude (Anthropic) and GPT-4 (OpenAI) backends.
Implements the plan.md memory system to prevent context rot in
long multi-step reasoning chains.
"""

import os
import json
import time
from typing import Optional
from dataclasses import dataclass, field

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


@dataclass
class AgentPlan:
    """Structured output from the agent's reasoning step."""
    code_patch: str            # The actual code to write
    filepath: str              # Which file to write it to
    explanation: str           # Why this change was made
    test_hint: str = ""        # Optional: suggest a test path
    confidence: float = 0.0    # 0.0-1.0 self-assessed confidence


@dataclass
class Message:
    """A single message in the agent's conversation history."""
    role: str    # 'system', 'user', or 'assistant'
    content: str


class MinionAgent:
    """
    The Reasoning Engine - The Brain.
    This is the Python equivalent of Stripe's Goose fork.

    Key design decisions:
    1. plan.md memory: Agent writes its plan to a file before coding,
       preventing 'context rot' in long chains.
    2. Tool calling: Uses MCP tool specs in OpenAI function-calling format.
    3. Strict token budget: System prompt is concise, context is curated.
    4. Dual backend: Supports Claude 3.5 Sonnet and GPT-4o.
    """

    SYSTEM_PROMPT = """You are a Minion - an autonomous software engineer AI.
Your job is to make targeted, minimal code changes to fix bugs or implement features.

RULES:
1. Make the SMALLEST change that fixes the problem. Do not refactor unrelated code.
2. Before writing code, always call 'update_plan' to write your approach to plan.md.
3. Read relevant files before editing them. Never guess at file contents.
4. Write production-quality code: handle errors, add type hints, follow existing style.
5. After writing code, explain your change clearly in 1-2 sentences.
6. If you cannot solve the task within your capabilities, say so explicitly.

You have access to tools for reading/writing files, running tests, and linting.
Always verify your changes pass linting and tests before declaring success."""

    def __init__(self, tools: list, backend: str = "auto"):
        self.tools = tools
        self.history: list[Message] = []
        self.plan_md: str = ""
        self.attempt_count: int = 0
        self.backend = self._select_backend(backend)
        self._client = self._init_client()
        print(f"[Agent] Initialized with backend: {self.backend}, tools: {[t.name for t in tools]}")

    def _select_backend(self, preference: str) -> str:
        if preference == "auto":
            if ANTHROPIC_AVAILABLE and os.getenv("ANTHROPIC_API_KEY"):
                return "claude"
            elif OPENAI_AVAILABLE and os.getenv("OPENAI_API_KEY"):
                return "openai"
            else:
                return "simulation"
        return preference

    def _init_client(self):
        if self.backend == "claude" and ANTHROPIC_AVAILABLE:
            return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        elif self.backend == "openai" and OPENAI_AVAILABLE:
            return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        return None

    def hydrate(self, context: dict):
        """
        Load task context into the agent's memory.
        context should be the output of ContextEngine.hydrate_for_task()
        """
        self.history = [Message(role="system", content=self.SYSTEM_PROMPT)]

        # Build a token-efficient context message
        dep_snippets = "\n".join([
            f"# {d['id']} ({d['type']})\n{d['source_snippet'][:300]}"
            for d in context.get("dependency_context", [])[:5]  # Cap at 5 deps
        ])

        context_msg = f"""TASK: {context.get('issue', 'No issue provided')}

TARGET FILES: {', '.join(context.get('target_files', []))}

REPO STRUCTURE (top files):
{chr(10).join(context.get('repo_structure', [])[:20])}

RELEVANT DEPENDENCIES:
{dep_snippets or 'No dependencies found'}

AFFECTED TESTS: {', '.join(context.get('affected_tests', [])[:5]) or 'None identified'}

Start by calling 'read_plan' to check for existing progress, then 'read_file'
to understand the current code before making any changes."""

        self.history.append(Message(role="user", content=context_msg))
        print(f"[Agent] Context hydrated. History: {len(self.history)} messages.")

    def feed_error(self, error_msg: str, attempt: int):
        """Feed a failed attempt's error back into the agent for self-correction."""
        self.attempt_count = attempt
        correction_msg = f"""ATTEMPT {attempt} FAILED.

Error output:
{error_msg[:2000]}

Analyze the error above carefully. Do NOT repeat the same approach.
Update plan.md with your revised strategy, then fix the issue."""
        self.history.append(Message(role="user", content=correction_msg))
        print(f"[Agent] Error fed back. Attempt {attempt} correction queued.")

    def plan_and_write_code(self, mcp_server=None) -> AgentPlan:
        """
        Main reasoning loop.
        Uses tool calling to:
        1. Read plan.md (memory)
        2. Read relevant files
        3. Write updated plan.md
        4. Generate the code patch
        """
        print(f"[Agent] Starting reasoning loop (backend={self.backend})...")

        if self.backend == "claude":
            return self._claude_loop(mcp_server)
        elif self.backend == "openai":
            return self._openai_loop(mcp_server)
        else:
            return self._simulation_loop()

    def _claude_loop(self, mcp_server) -> AgentPlan:
        """Claude 3.5 Sonnet reasoning loop with tool use."""
        tool_specs = [t.to_llm_spec() for t in self.tools]
        messages = [{"role": m.role, "content": m.content}
                    for m in self.history if m.role != "system"]

        max_turns = 10  # Prevent infinite tool loops
        for turn in range(max_turns):
            response = self._client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4096,
                system=self.SYSTEM_PROMPT,
                messages=messages,
                tools=tool_specs
            )

            # Handle tool use
            if response.stop_reason == "tool_use" and mcp_server:
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        print(f"[Agent] Calling tool: {block.name}({json.dumps(block.input)[:100]})")
                        result = mcp_server.execute(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result)
                        })

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue

            # Extract final text response
            final_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    final_text = block.text
                    break

            return self._parse_plan_from_text(final_text)

        return AgentPlan(code_patch="", filepath="", explanation="Max tool turns exceeded")

    def _openai_loop(self, mcp_server) -> AgentPlan:
        """GPT-4o reasoning loop with function calling."""
        tool_specs = [{
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.schema
            }
        } for t in self.tools]

        messages = [{"role": m.role, "content": m.content} for m in self.history]

        max_turns = 10
        for turn in range(max_turns):
            response = self._client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=tool_specs,
                tool_choice="auto",
                max_tokens=4096
            )

            msg = response.choices[0].message

            if msg.tool_calls and mcp_server:
                messages.append(msg)
                for tc in msg.tool_calls:
                    params = json.loads(tc.function.arguments)
                    print(f"[Agent] Calling tool: {tc.function.name}({str(params)[:100]})")
                    result = mcp_server.execute(tc.function.name, params)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result)
                    })
                continue

            return self._parse_plan_from_text(msg.content or "")

        return AgentPlan(code_patch="", filepath="", explanation="Max tool turns exceeded")

    def _simulation_loop(self) -> AgentPlan:
        """
        Simulation mode: no API key needed.
        Used for local testing and CI environments.
        """
        print("[Agent] Running in simulation mode (no LLM API key found).")
        task = self.history[-1].content if self.history else ""
        dummy_code = f'''
# Auto-generated fix by Minion Agent (simulation mode)
# Task: {task[:100]}

def minion_fix():
    """Placeholder fix generated by simulation mode."""
    # TODO: Replace with actual LLM-generated fix
    # Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env to enable real code generation
    pass
'''
        return AgentPlan(
            code_patch=dummy_code,
            filepath="minion_output.py",
            explanation="Simulation mode output. Set API keys for real code generation.",
            confidence=0.0
        )

    def _parse_plan_from_text(self, text: str) -> AgentPlan:
        """
        Parse structured output from the LLM text response.
        Looks for code blocks and filepath markers.
        """
        code_patch = ""
        filepath = "output.py"
        explanation = text[:500] if text else "No explanation provided"

        # Extract code from markdown code blocks
        if "```python" in text:
            start = text.find("```python") + 9
            end = text.find("```", start)
            code_patch = text[start:end].strip()
        elif "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            code_patch = text[start:end].strip()

        # Extract filepath from common patterns
        for line in text.split("\n"):
            if line.strip().startswith("File:") or line.strip().startswith("filepath:"):
                filepath = line.split(":", 1)[-1].strip()
                break

        return AgentPlan(
            code_patch=code_patch,
            filepath=filepath,
            explanation=explanation,
            confidence=0.8 if code_patch else 0.1
        )

    def add_assistant_message(self, content: str):
        """Add an assistant response to history (for multi-turn tracking)."""
        self.history.append(Message(role="assistant", content=content))

    @property
    def token_estimate(self) -> int:
        """Rough token count estimate for the current history."""
        total_chars = sum(len(m.content) for m in self.history)
        return total_chars // 4  # ~4 chars per token
