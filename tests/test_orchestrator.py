"""M1 测试：Orchestrator 主循环（用假 LLM，不花钱）。"""
from types import SimpleNamespace

from agent.context import ContextBuilder
from agent.models import Message, ToolCall
from agent.orchestrator import Orchestrator
from agent.tools_registry import ToolDefinition, ToolRegistry


class FakeLLM:
    """第一轮返回一个 tool_call，第二轮返回最终回复。"""

    def __init__(self):
        self.n = 0

    def chat(self, messages, tools=None):
        self.n += 1
        if self.n == 1:
            tc = ToolCall(id="t1", name="echo_tool", arguments={"x": "1"})
            return Message(role="assistant", content="", tool_calls=[tc])
        return Message(role="assistant", content="done")


def test_run_turn_executes_tool():
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="echo_tool", description="t",
        parameters={"type": "object", "properties": {}},
        func=lambda args: f"echo:{args.get('x')}"))
    orch = Orchestrator(FakeLLM(), reg, ContextBuilder(max_tokens=4000))
    reg.set_approval(SimpleNamespace(check=lambda *args, **kwargs: True))
    out = orch.handle("hi")
    assert out == "done"
    # 历史里应有：user → assistant(tool_call) → tool → assistant(最终)
    roles = [m.role for m in orch.history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert orch.history[2].tool_call_id == "t1"
    assert orch.history[2].content == "echo:1"


def test_max_iters_forces_close():
    class LoopLLM:
        def chat(self, messages, tools=None):
            tc = ToolCall(id="t", name="echo_tool", arguments={})
            return Message(role="assistant", content="", tool_calls=[tc])

    reg = ToolRegistry()
    reg.register(ToolDefinition(name="echo_tool", description="t",
                                parameters={"type": "object", "properties": {}},
                                func=lambda args: "ok"))
    orch = Orchestrator(LoopLLM(), reg, ContextBuilder(max_tokens=4000), max_iters=3)
    reg.set_approval(SimpleNamespace(check=lambda *args, **kwargs: True))
    out = orch.handle("hi")
    assert "任务步骤较多" in out
