"""
context_engine.py - AST Parser + DAG Dependency Graph Engine
Stripe Minions Replica - Context Hydration Layer

Parses your codebase into an Abstract Syntax Tree (AST),
builds a Directed Acyclic Graph (DAG) of dependencies,
and traverses it to pull exact context for the agent.
"""

import os
import ast
import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CodeNode:
    """A single node in the dependency DAG (file, class, or function)."""
    id: str                        # Unique ID: "module.ClassName.method_name"
    filepath: str
    node_type: str                 # 'module', 'class', 'function', 'import'
    source: str = ""               # Raw source snippet
    lineno: int = 0
    dependencies: list = field(default_factory=list)   # IDs of nodes this depends on
    dependents: list = field(default_factory=list)     # IDs of nodes that depend on this
    checksum: str = ""             # SHA256 of source for change detection

    def __post_init__(self):
        if self.source:
            self.checksum = hashlib.sha256(self.source.encode()).hexdigest()[:12]


class ASTParser:
    """Parses Python files into CodeNodes using the ast module."""

    def parse_file(self, filepath: str) -> list[CodeNode]:
        """Parse a single Python file into a list of CodeNodes."""
        nodes = []
        try:
            with open(filepath, "r", errors="ignore") as f:
                source = f.read()
            tree = ast.parse(source, filename=filepath)
        except (SyntaxError, FileNotFoundError) as e:
            print(f"[AST] Skipping {filepath}: {e}")
            return nodes

        module_name = filepath.replace("/", ".").replace("\\", ".").rstrip(".py")
        imports = self._extract_imports(tree)

        # Module-level node
        module_node = CodeNode(
            id=module_name,
            filepath=filepath,
            node_type="module",
            source=ast.get_source_segment(source, tree) or source[:200],
            dependencies=imports
        )
        nodes.append(module_node)

        # Class and function nodes
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_id = f"{module_name}.{node.name}"
                class_source = ast.get_source_segment(source, node) or ""
                nodes.append(CodeNode(
                    id=class_id,
                    filepath=filepath,
                    node_type="class",
                    source=class_source,
                    lineno=node.lineno,
                    dependencies=[module_name]
                ))
            elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                func_id = f"{module_name}.{node.name}"
                func_source = ast.get_source_segment(source, node) or ""
                called_funcs = self._extract_calls(node)
                nodes.append(CodeNode(
                    id=func_id,
                    filepath=filepath,
                    node_type="function",
                    source=func_source,
                    lineno=node.lineno,
                    dependencies=[module_name] + called_funcs
                ))
        return nodes

    def _extract_imports(self, tree: ast.Module) -> list[str]:
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        return imports

    def _extract_calls(self, func_node: ast.FunctionDef) -> list[str]:
        calls = []
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.append(node.func.attr)
        return list(set(calls))


class DependencyDAG:
    """
    Directed Acyclic Graph (DAG) of codebase dependencies.
    Uses an adjacency list with reverse mapping for O(1) dependent lookups.
    """

    def __init__(self):
        self.nodes: dict[str, CodeNode] = {}           # id -> CodeNode
        self.forward: dict[str, set] = defaultdict(set)  # id -> dependencies
        self.reverse: dict[str, set] = defaultdict(set)  # id -> dependents

    def add_node(self, node: CodeNode):
        self.nodes[node.id] = node
        for dep in node.dependencies:
            self.forward[node.id].add(dep)
            self.reverse[dep].add(node.id)

    def get_dependencies(self, node_id: str, depth: int = 2) -> list[CodeNode]:
        """
        BFS traversal: Get all nodes this node depends on (up to given depth).
        Used to pull upstream context for the agent.
        """
        visited = set()
        result = []
        queue = deque([(node_id, 0)])
        while queue:
            current_id, current_depth = queue.popleft()
            if current_id in visited or current_depth > depth:
                continue
            visited.add(current_id)
            if current_id in self.nodes and current_id != node_id:
                result.append(self.nodes[current_id])
            for dep_id in self.forward.get(current_id, []):
                queue.append((dep_id, current_depth + 1))
        return result

    def get_dependents(self, node_id: str, depth: int = 2) -> list[CodeNode]:
        """
        Reverse BFS: Get all nodes that depend on this node.
        Critical for selective test execution - find tests affected by a change.
        """
        visited = set()
        result = []
        queue = deque([(node_id, 0)])
        while queue:
            current_id, current_depth = queue.popleft()
            if current_id in visited or current_depth > depth:
                continue
            visited.add(current_id)
            if current_id in self.nodes and current_id != node_id:
                result.append(self.nodes[current_id])
            for dep_id in self.reverse.get(current_id, []):
                queue.append((dep_id, current_depth + 1))
        return result

    def topological_sort(self) -> list[str]:
        """
        Kahn's Algorithm for topological sort of the DAG.
        Returns nodes in dependency-first order for context assembly.
        """
        in_degree = {nid: 0 for nid in self.nodes}
        for nid in self.nodes:
            for dep in self.forward.get(nid, []):
                if dep in in_degree:
                    in_degree[dep] = in_degree.get(dep, 0) + 1

        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        result = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for dep in self.forward.get(node, []):
                if dep in in_degree:
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        queue.append(dep)
        return result


class ContextEngine:
    """
    Main context hydration engine.
    Builds the DAG from the codebase and serves precise, token-efficient
    context bundles to the MinionAgent before it writes any code.
    """

    def __init__(self, repo_root: str = "."):
        self.repo_root = repo_root
        self.parser = ASTParser()
        self.dag = DependencyDAG()
        self._file_checksums: dict[str, str] = {}
        print(f"[ContextEngine] Initializing for repo: {os.path.abspath(repo_root)}")
        self._build_dag()

    def _build_dag(self):
        """Walk repo, parse all Python files, build full DAG."""
        total_nodes = 0
        for root, dirs, files in os.walk(self.repo_root):
            # Skip venv, git, cache dirs
            dirs[:] = [d for d in dirs if d not in {".git", "venv", ".venv", "__pycache__", "node_modules", ".tox"}]
            for fname in files:
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    nodes = self.parser.parse_file(fpath)
                    for node in nodes:
                        self.dag.add_node(node)
                    total_nodes += len(nodes)
                    # Track file checksum for change detection
                    with open(fpath, "rb") as f:
                        self._file_checksums[fpath] = hashlib.sha256(f.read()).hexdigest()[:12]
        print(f"[ContextEngine] DAG built: {len(self.dag.nodes)} nodes from {len(self._file_checksums)} files")

    def hydrate_for_task(self, issue_text: str, target_files: list[str]) -> dict:
        """
        Build a focused context bundle for a given task.
        Returns only what the agent needs - not the entire codebase.
        """
        context = {
            "issue": issue_text,
            "target_files": target_files,
            "dependency_context": [],
            "affected_tests": [],
            "repo_structure": self._get_repo_structure()
        }

        for fpath in target_files:
            module_id = fpath.replace("/", ".").replace("\\", ".").rstrip(".py")

            # Pull upstream deps (what this module needs)
            deps = self.dag.get_dependencies(module_id, depth=2)
            for dep_node in deps:
                context["dependency_context"].append({
                    "id": dep_node.id,
                    "type": dep_node.node_type,
                    "source_snippet": dep_node.source[:500]  # Token-budget: max 500 chars
                })

            # Pull downstream dependents (tests affected by changes)
            dependents = self.dag.get_dependents(module_id, depth=3)
            test_nodes = [n for n in dependents if "test" in n.filepath.lower()]
            context["affected_tests"] = [n.filepath for n in test_nodes]

        print(f"[ContextEngine] Hydrated context: {len(context['dependency_context'])} dep nodes, "
              f"{len(context['affected_tests'])} affected tests")
        return context

    def get_changed_files(self) -> list[str]:
        """Detect files that changed since last DAG build (for incremental updates)."""
        changed = []
        for fpath, old_checksum in self._file_checksums.items():
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    new_checksum = hashlib.sha256(f.read()).hexdigest()[:12]
                if new_checksum != old_checksum:
                    changed.append(fpath)
        return changed

    def get_test_selection(self, changed_files: list[str]) -> list[str]:
        """
        Selective CI Test Selection - the key performance optimization.
        Uses reverse DAG traversal to find ONLY the tests affected by
        changed files. Avoids running the full 10-min test suite.
        """
        test_files = set()
        for fpath in changed_files:
            module_id = fpath.replace("/", ".").replace("\\", ".").rstrip(".py")
            dependents = self.dag.get_dependents(module_id, depth=5)
            for node in dependents:
                if "test" in node.filepath.lower() or node.filepath.startswith("test"):
                    test_files.add(node.filepath)
        print(f"[ContextEngine] Selective tests for {len(changed_files)} changed files: {list(test_files)}")
        return list(test_files)

    def _get_repo_structure(self, max_files: int = 50) -> list[str]:
        """Return a flat list of repo files for high-level orientation."""
        files = []
        for root, dirs, fnames in os.walk(self.repo_root):
            dirs[:] = [d for d in dirs if d not in {".git", "venv", ".venv", "__pycache__"}]
            for fname in fnames:
                if not fname.startswith("."):
                    files.append(os.path.relpath(os.path.join(root, fname), self.repo_root))
                    if len(files) >= max_files:
                        return files
        return files
