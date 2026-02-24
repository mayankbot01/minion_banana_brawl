from devbox import DevBoxManager
from agent import MinionAgent

class MinionOrchestrator:
    def __init__(self, task_context):
        self.task_context = task_context
        self.max_retries = 2 # Strict cap to prevent massive token burn
        
        self.devbox_manager = DevBoxManager()
        # The Toolshed (MCP Server dummy tools)
        self.tools = ["mcp_linter", "mcp_test_runner", "mcp_git_pusher"] 

    def run(self):
        print(f"--- Waking up Minion for Task: {self.task_context} ---")
        devbox = self.devbox_manager.spin_up()
        agent = MinionAgent(tools=self.tools)
        
        # Hydrate Graph context
        agent.hydrate(issue_context=self.task_context, dag_dependencies=["banana_utils.py"])

        try:
            for attempt in range(self.max_retries + 1):
                print(f"\n--- Attempt {attempt + 1}/{self.max_retries + 1} ---")
                
                # 1. AI writes code
                plan = agent.plan_and_write_code()
                devbox.apply_patch(plan.code_patch)
                
                # 2. Gate 1: Linting
                lint_result = devbox.run_linter()
                if not lint_result["success"]:
                    agent.feed_error(lint_result["error"])
                    continue # Shift-left: try again cheaply
                
                # 3. Gate 2: Testing
                test_result = devbox.run_tests()
                if not test_result["success"]:
                    agent.feed_error(test_result["error"])
                    continue
                
                # 4. Success Pipeline
                print("[Orchestrator] All gates passed! Code is production-ready.")
                return "Success: Pull Request created and assigned to human reviewer."
            
            return "Escalated to human: Task unresolved after max retries."
            
        finally:
            devbox.destroy() # Always tear down the cage

if __name__ == "__main__":
    orchestrator = MinionOrchestrator("Fix NullPointerException in banana inventory")
    result = orchestrator.run()
    print(f"\nFinal Result: {result}")
