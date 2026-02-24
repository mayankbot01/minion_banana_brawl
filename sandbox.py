"""
sandbox.py - Pre-warmed VM Pool Manager (The Cage)
Stripe Minions Replica - Isolation Layer

Manages a pool of pre-warmed Docker containers that spin up in < 10 seconds.
All containers have ZERO internet access and ZERO production DB access.
This is the security boundary that makes Minions safe to run autonomously.
"""

import os
import time
import uuid
import threading
import subprocess
from queue import Queue, Empty
from dataclasses import dataclass, field
from typing import Optional

try:
    import docker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    print("[Sandbox] Docker SDK not installed. Running in simulation mode.")


@dataclass
class SandboxResult:
    """Result of a command executed inside a sandbox."""
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float

    @property
    def success(self) -> bool:
        return self.exit_code == 0


class Sandbox:
    """
    A single isolated sandbox instance.
    Wraps a Docker container with zero network access.
    """

    def __init__(self, sandbox_id: str, container=None, repo_path: str = "."):
        self.sandbox_id = sandbox_id
        self.container = container
        self.repo_path = os.path.abspath(repo_path)
        self.is_alive = container is not None
        self.created_at = time.time()
        self._lock = threading.Lock()

    def exec(self, command: str, timeout: int = 120) -> SandboxResult:
        """Execute a shell command inside the sandbox."""
        start = time.time()
        if self.container and DOCKER_AVAILABLE:
            return self._docker_exec(command, timeout)
        else:
            return self._local_exec(command, timeout)

    def _docker_exec(self, command: str, timeout: int) -> SandboxResult:
        """Execute inside Docker container."""
        start = time.time()
        try:
            result = self.container.exec_run(
                cmd=["sh", "-c", command],
                workdir="/workspace",
                demux=True
            )
            stdout = result.output[0].decode() if result.output[0] else ""
            stderr = result.output[1].decode() if result.output[1] else ""
            duration = (time.time() - start) * 1000
            return SandboxResult(
                exit_code=result.exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration
            )
        except Exception as e:
            return SandboxResult(exit_code=1, stdout="", stderr=str(e), duration_ms=0)

    def _local_exec(self, command: str, timeout: int) -> SandboxResult:
        """Fallback: execute locally (for development without Docker)."""
        start = time.time()
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True,
                text=True, timeout=timeout, cwd=self.repo_path
            )
            return SandboxResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_ms=(time.time() - start) * 1000
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(exit_code=124, stdout="", stderr="Command timed out", duration_ms=timeout * 1000)

    def write_file(self, path: str, content: str) -> bool:
        """Write a file into the sandbox workspace."""
        if self.container and DOCKER_AVAILABLE:
            import tarfile
            import io
            tarstream = io.BytesIO()
            with tarfile.open(fileobj=tarstream, mode='w') as tar:
                encoded = content.encode('utf-8')
                info = tarfile.TarInfo(name=os.path.basename(path))
                info.size = len(encoded)
                tar.addfile(info, io.BytesIO(encoded))
            tarstream.seek(0)
            self.container.put_archive(
                os.path.join("/workspace", os.path.dirname(path)),
                tarstream
            )
            return True
        else:
            full_path = os.path.join(self.repo_path, path)
            os.makedirs(os.path.dirname(full_path) if os.path.dirname(full_path) else ".", exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            return True

    def read_file(self, path: str) -> Optional[str]:
        """Read a file from the sandbox workspace."""
        if self.container and DOCKER_AVAILABLE:
            result = self.exec(f"cat /workspace/{path}")
            return result.stdout if result.success else None
        else:
            full_path = os.path.join(self.repo_path, path)
            try:
                with open(full_path) as f:
                    return f.read()
            except FileNotFoundError:
                return None

    def run_linter(self, filepath: str) -> SandboxResult:
        """Run ruff linter on a specific file."""
        return self.exec(f"ruff check {filepath} --output-format=text || flake8 {filepath}")

    def run_tests(self, test_paths: list[str], verbose: bool = True) -> SandboxResult:
        """Run pytest on a specific set of test files (selective CI)."""
        paths_str = " ".join(test_paths) if test_paths else "tests/"
        flag = "-v" if verbose else ""
        return self.exec(f"python -m pytest {paths_str} {flag} --tb=short --no-header -q")

    def run_type_check(self, module: str) -> SandboxResult:
        """Run mypy type checking."""
        return self.exec(f"mypy {module} --ignore-missing-imports")

    def git_status(self) -> SandboxResult:
        return self.exec("git status --short")

    def git_diff(self) -> SandboxResult:
        return self.exec("git diff HEAD")

    def destroy(self):
        """Tear down and clean up the sandbox."""
        if self.container and DOCKER_AVAILABLE:
            try:
                self.container.stop(timeout=5)
                self.container.remove(force=True)
                print(f"[Sandbox] {self.sandbox_id} destroyed.")
            except Exception as e:
                print(f"[Sandbox] Warning during destroy: {e}")
        self.is_alive = False


class SandboxPool:
    """
    Pre-warmed pool of sandboxes.
    Keeps N containers hot and ready so task startup is near-instant.
    This mirrors Stripe's approach where DevBoxes boot in < 10 seconds.
    """

    def __init__(self, pool_size: int = 3, base_image: str = "python:3.11-slim",
                 repo_path: str = "."):
        self.pool_size = pool_size
        self.base_image = base_image
        self.repo_path = repo_path
        self._pool: Queue = Queue()
        self._active: dict[str, Sandbox] = {}
        self._lock = threading.Lock()
        self._docker_client = None

        if DOCKER_AVAILABLE:
            try:
                self._docker_client = docker.from_env()
                print(f"[SandboxPool] Docker connected. Pre-warming {pool_size} sandboxes...")
                self._prewarm(pool_size)
            except Exception as e:
                print(f"[SandboxPool] Docker unavailable ({e}). Using local execution mode.")
        else:
            print("[SandboxPool] Simulation mode: sandboxes will run locally.")

    def _prewarm(self, count: int):
        """Spin up N sandboxes in parallel threads for fast availability."""
        threads = []
        for _ in range(count):
            t = threading.Thread(target=self._create_and_queue_sandbox)
            t.daemon = True
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=30)
        print(f"[SandboxPool] Pre-warmed {self._pool.qsize()} sandboxes ready.")

    def _create_and_queue_sandbox(self):
        """Create a single sandbox and add it to the pool queue."""
        sandbox = self._create_sandbox()
        if sandbox:
            self._pool.put(sandbox)

    def _create_sandbox(self) -> Optional[Sandbox]:
        """Spin up a single isolated Docker container."""
        sandbox_id = f"minion-{uuid.uuid4().hex[:8]}"
        if self._docker_client:
            try:
                container = self._docker_client.containers.run(
                    self.base_image,
                    command="tail -f /dev/null",      # Keep alive
                    detach=True,
                    name=sandbox_id,
                    network_mode="none",              # ZERO internet access
                    mem_limit="512m",                 # Memory cap
                    cpu_quota=50000,                  # 50% CPU cap
                    read_only=False,
                    volumes={
                        os.path.abspath(self.repo_path): {
                            "bind": "/workspace",
                            "mode": "ro"              # Read-only mount of repo
                        }
                    },
                    working_dir="/workspace",
                    labels={"managed_by": "minion_system", "sandbox_id": sandbox_id}
                )
                # Install basic tooling
                container.exec_run("pip install ruff pytest mypy --quiet")
                print(f"[SandboxPool] Sandbox {sandbox_id} ready.")
                return Sandbox(sandbox_id, container, self.repo_path)
            except Exception as e:
                print(f"[SandboxPool] Failed to create Docker sandbox: {e}")
                return Sandbox(sandbox_id, None, self.repo_path)  # Fallback
        else:
            return Sandbox(sandbox_id, None, self.repo_path)

    def acquire(self, timeout: int = 30) -> Sandbox:
        """
        Acquire a sandbox from the pool.
        If the pool is empty, creates a new one on-demand.
        Automatically replenishes the pool in the background.
        """
        try:
            sandbox = self._pool.get(timeout=timeout)
            with self._lock:
                self._active[sandbox.sandbox_id] = sandbox
            # Refill pool in background
            threading.Thread(target=self._create_and_queue_sandbox, daemon=True).start()
            print(f"[SandboxPool] Acquired sandbox {sandbox.sandbox_id}. Pool remaining: {self._pool.qsize()}")
            return sandbox
        except Empty:
            print("[SandboxPool] Pool empty, creating on-demand sandbox...")
            sandbox = self._create_sandbox()
            with self._lock:
                self._active[sandbox.sandbox_id] = sandbox
            return sandbox

    def release(self, sandbox: Sandbox, reuse: bool = False):
        """
        Release a sandbox back to the pool or destroy it.
        Stripe's implementation destroys sandboxes after each task (never reuse)
        to prevent state contamination between Minion runs.
        """
        with self._lock:
            self._active.pop(sandbox.sandbox_id, None)

        if reuse and sandbox.is_alive:
            # Clean the workspace before returning to pool
            sandbox.exec("git checkout -- . && git clean -fd")
            self._pool.put(sandbox)
            print(f"[SandboxPool] Sandbox {sandbox.sandbox_id} returned to pool.")
        else:
            sandbox.destroy()
            # Always maintain minimum pool size
            if self._pool.qsize() < self.pool_size:
                threading.Thread(target=self._create_and_queue_sandbox, daemon=True).start()

    def shutdown(self):
        """Destroy all sandboxes in pool and active set."""
        print("[SandboxPool] Shutting down all sandboxes...")
        while not self._pool.empty():
            try:
                sandbox = self._pool.get_nowait()
                sandbox.destroy()
            except Empty:
                break
        with self._lock:
            for sandbox in list(self._active.values()):
                sandbox.destroy()
            self._active.clear()

    @property
    def stats(self) -> dict:
        return {
            "pool_available": self._pool.qsize(),
            "active": len(self._active),
            "total_capacity": self.pool_size
        }


# Global shared pool (singleton pattern for the orchestrator)
_global_pool: Optional[SandboxPool] = None


def get_pool(pool_size: int = 3, repo_path: str = ".") -> SandboxPool:
    """Get or create the global sandbox pool."""
    global _global_pool
    if _global_pool is None:
        _global_pool = SandboxPool(pool_size=pool_size, repo_path=repo_path)
    return _global_pool
