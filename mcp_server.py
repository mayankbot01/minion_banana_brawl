"""
mcp_server.py - Model Context Protocol (MCP) Server
Stripe Minions Replica - The Toolshed

Dynamically injects only the 10-15 tools relevant to the specific
subdirectory or task, preventing context overload in the LLM.
"""

import os
import ast
import json
import subprocess
from typing import Any, Callable
from dataclasses import dataclass, field


@dataclass
class MCPTool:
    """Represents a single registered MCP tool."""
    name: str
    description: str
    domain: str  # e.g. 'testing', 'linting', 'git', 'filesystem', 'search'
    handler: Callable
    schema: dict = field(default_factory=dict)

    def to_llm_spec(self) -> dict:
        """Returns the tool spec in OpenAI/Anthropic function-calling format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema
        }


class MCPServer:
    """
    Central MCP Server - The Toolshed.
    Registers all internal tools and serves only domain-relevant
    subsets to the agent to prevent token overload.
    """

    def __init__(self):
        self._registry: dict[str, MCPTool] = {}
        self._register_all_tools()

    def _register_all_tools(self):
        """Register every available tool in the system."""
        self._register(MCPTool(
            name="read_file",
            description="Read the contents of a file in the DevBox sandbox.",
            domain="filesystem",
            handler=self._read_file,
            schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path to read"}},
                "required": ["path"]
            }
        ))
        self._register(MCPTool(
            name="write_file",
            description="Write or overwrite a file in the DevBox sandbox.",
            domain="filesystem",
            handler=self._write_file,
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        ))
        self._register(MCPTool(
            name="list_directory",
            description="List files and directories at a given path.",
            domain="filesystem",
            handler=self._list_directory,
            schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        ))
        self._register(MCPTool(
            name="run_python_linter",
            description="Run flake8/ruff linter on a Python file and return violations.",
            domain="linting",
            handler=self._run_linter,
            schema={
                "type": "object",
                "properties": {"filepath": {"type": "string"}},
                "required": ["filepath"]
            }
        ))
        self._register(MCPTool(
            name="run_type_check",
            description="Run mypy type checker on a Python module.",
            domain="linting",
            handler=self._run_type_check,
            schema={
                "type": "object",
                "properties": {"module_path": {"type": "string"}},
                "required": ["module_path"]
            }
        ))
        self._register(MCPTool(
            name="run_tests",
            description="Run pytest for a specific file or test ID and return results.",
            domain="testing",
            handler=self._run_tests,
            schema={
                "type": "object",
                "properties": {
                    "test_path": {"type": "string"},
                    "verbose": {"type": "boolean", "default": True}
                },
                "required": ["test_path"]
            }
        ))
        self._register(MCPTool(
            name="run_coverage",
            description="Run pytest with coverage report for a given module.",
            domain="testing",
            handler=self._run_coverage,
            schema={
                "type": "object",
                "properties": {"module": {"type": "string"}},
                "required": ["module"]
            }
        ))
        self._register(MCPTool(
            name="git_diff",
            description="Get the current git diff of staged or unstaged changes.",
            domain="git",
            handler=self._git_diff,
            schema={"type": "object", "properties": {"staged": {"type": "boolean"}}, "required": []}
        ))
        self._register(MCPTool(
            name="git_commit",
            description="Stage all changes and create a git commit with a message.",
            domain="git",
            handler=self._git_commit,
            schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"]
            }
        ))
        self._register(MCPTool(
            name="create_branch",
            description="Create and checkout a new git branch.",
            domain="git",
            handler=self._create_branch,
            schema={
                "type": "object",
                "properties": {"branch_name": {"type": "string"}},
                "required": ["branch_name"]
            }
        ))
        self._register(MCPTool(
            name="search_codebase",
            description="Search for a string pattern across all files in the repo.",
            domain="search",
            handler=self._search_codebase,
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "file_extension": {"type": "string", "default": ".py"}
                },
                "required": ["pattern"]
            }
        ))
        self._register(MCPTool(
            name="read_plan",
            description="Read the current plan.md to understand task progress and next steps.",
            domain="memory",
            handler=self._read_plan,
            schema={"type": "object", "properties": {}, "required": []}
        ))
        self._register(MCPTool(
            name="update_plan",
            description="Overwrite plan.md with updated task breakdown and progress.",
            domain="memory",
            handler=self._update_plan,
            schema={
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"]
            }
        ))

    def _register(self, tool: MCPTool):
        self._registry[tool.name] = tool

    def get_tools_for_domain(self, domain: str) -> list[MCPTool]:
        """
        Returns only tools relevant to the given task domain.
        This is THE key insight from Stripe Minions - never give the LLM
        all tools at once. Curate the exact toolset per task.
        """
        # Always include memory tools + filesystem
        core_domains = {"memory", "filesystem"}
        target_domains = core_domains | {domain}
        tools = [t for t in self._registry.values() if t.domain in target_domains]
        print(f"[MCP] Serving {len(tools)} tools for domain '{domain}': {[t.name for t in tools]}")
        return tools

    def get_all_tools(self) -> list[MCPTool]:
        return list(self._registry.values())

    def execute(self, tool_name: str, params: dict) -> Any:
        """Execute a tool by name with given parameters."""
        if tool_name not in self._registry:
            return {"error": f"Tool '{tool_name}' not found in registry."}
        tool = self._registry[tool_name]
        try:
            return tool.handler(**params)
        except Exception as e:
            return {"error": str(e)}

    # --- Tool Implementations ---

    def _read_file(self, path: str) -> dict:
        try:
            with open(path, "r") as f:
                return {"success": True, "content": f.read()}
        except FileNotFoundError:
            return {"success": False, "error": f"File not found: {path}"}

    def _write_file(self, path: str, content: str) -> dict:
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return {"success": True, "path": path}

    def _list_directory(self, path: str = ".") -> dict:
        try:
            entries = os.listdir(path)
            return {"success": True, "entries": entries}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _run_linter(self, filepath: str) -> dict:
        try:
            result = subprocess.run(
                ["ruff", "check", filepath, "--output-format=json"],
                capture_output=True, text=True, timeout=30
            )
            violations = json.loads(result.stdout) if result.stdout.strip() else []
            return {
                "success": len(violations) == 0,
                "violations": violations,
                "count": len(violations)
            }
        except FileNotFoundError:
            # Fallback to flake8
            result = subprocess.run(
                ["flake8", filepath],
                capture_output=True, text=True, timeout=30
            )
            return {"success": result.returncode == 0, "output": result.stdout}

    def _run_type_check(self, module_path: str) -> dict:
        result = subprocess.run(
            ["mypy", module_path, "--ignore-missing-imports"],
            capture_output=True, text=True, timeout=60
        )
        return {
            "success": result.returncode == 0,
            "output": result.stdout,
            "errors": result.stderr
        }

    def _run_tests(self, test_path: str, verbose: bool = True) -> dict:
        cmd = ["pytest", test_path, "-v"] if verbose else ["pytest", test_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {
            "success": result.returncode == 0,
            "output": result.stdout,
            "errors": result.stderr
        }

    def _run_coverage(self, module: str) -> dict:
        result = subprocess.run(
            ["pytest", f"--cov={module}", "--cov-report=term-missing"],
            capture_output=True, text=True, timeout=120
        )
        return {"success": result.returncode == 0, "output": result.stdout}

    def _git_diff(self, staged: bool = False) -> dict:
        cmd = ["git", "diff", "--staged"] if staged else ["git", "diff"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return {"success": True, "diff": result.stdout}

    def _git_commit(self, message: str) -> dict:
        subprocess.run(["git", "add", "-A"], capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True, text=True
        )
        return {"success": result.returncode == 0, "output": result.stdout}

    def _create_branch(self, branch_name: str) -> dict:
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            capture_output=True, text=True
        )
        return {"success": result.returncode == 0, "branch": branch_name}

    def _search_codebase(self, pattern: str, file_extension: str = ".py") -> dict:
        results = []
        for root, _, files in os.walk("."):
            for fname in files:
                if fname.endswith(file_extension):
                    fpath = os.path.join(root, fname)
                    with open(fpath, "r", errors="ignore") as f:
                        for i, line in enumerate(f.readlines(), 1):
                            if pattern in line:
                                results.append({"file": fpath, "line": i, "content": line.strip()})
        return {"success": True, "matches": results, "count": len(results)}

    def _read_plan(self) -> dict:
        return self._read_file("plan.md")

    def _update_plan(self, content: str) -> dict:
        return self._write_file("plan.md", content)
