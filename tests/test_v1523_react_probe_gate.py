"""v1.5.23：提交闸门按网络证据分流 + React 状态取证 + 最终答复出口拦截。

背景（2026-10-27 抖音评论第 5 轮）：点击红色发送箭头后零 XHR/fetch，
页面无任何反应；工具却提示"前端校验静默拦截 + 联想框未选中"，
把模型引向 browser_fill_combo，连续空转到步数耗尽。最终主 Agent
又回复"你手动点一次"——正是 v1.5.22 刚禁止的甩锅。

本文件锁定：
1. 零请求与"已发 AJAX 请求但 URL 未变"必须是两种判据，不能混为一谈；
2. React 合成事件页面的 handler/hook 状态要能读出来；
3. 浏览器任务的最终答复要求用户手动操作且未用 takeover → 程序打回。
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.browser.worker import BrowserWorker, _REACT_PROBE_JS


class _GatePage:
    url = "https://www.douyin.com/video/7698719409758024049"


def _gate_worker() -> BrowserWorker:
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = _GatePage()
    return w


def _before() -> str:
    return _GatePage.url + "#fingerprint"


REACT_PROBE_STATE = {
    "ok": True, "framework": "react", "tag": "span", "text": "",
    "disabled": False,
    "chain": ["SendButton", "<span>", "<div>"],
    "handlers": ["onClick"],
    "handler_nodes": ["SendButton:onClick"],
    "hook_owner": "SendButton",
    "hooks": ['0=false', '1="这也太真实了吧"', "2=(effect)"],
}


# ---------- 1. 提交闸门分流 ----------

def test_zero_request_gate_names_real_cause_and_no_combo_detour():
    w = _gate_worker()
    out = w._submit_gate_hint(_before(), [], None)
    assert "[提交闸门·零请求]" in out
    assert "没有捕获到任何 XHR/fetch" in out
    assert "handler 没有被触发" in out
    assert "browser_react_probe" in out
    assert "重新加载页面后重新输入再提交" in out
    # 旧文案把联想框当"最常见原因"——对 AJAX 评论按钮是误导
    assert "最常见原因" not in out
    assert "说明前端校验静默拦截了本次提交" not in out


def test_zero_request_gate_attaches_react_probe_summary():
    w = _gate_worker()
    out = w._submit_gate_hint(_before(), [], REACT_PROBE_STATE)
    assert "REACT 探针" in out
    assert "组件链=SendButton" in out
    assert "元素事件=onClick" in out
    assert '1="这也太真实了吧"' in out


def test_ajax_request_gate_does_not_claim_silent_validation():
    w = _gate_worker()
    out = w._submit_gate_hint(
        _before(), ["200 POST https://www.douyin.com/aweme/v1/web/comment/publish"], None)
    assert "[提交闸门·URL未变]" in out
    assert "已发出 1 条 XHR/fetch" in out
    assert "AJAX 提交本来就不跳转" in out
    assert "不要重复点击" in out
    assert "零请求" not in out
    assert "联想输入框" not in out


def test_gate_silent_when_url_changed():
    w = _gate_worker()
    assert w._submit_gate_hint("https://other.example/page#x", [], None) == ""


def test_gate_without_network_evidence_stays_neutral():
    w = _gate_worker()
    out = w._submit_gate_hint(_before(), None, None)
    assert "browser_network_log" in out
    assert "不要重复点击" in out
    assert "零请求" not in out


def test_react_probe_text_handles_non_react_and_empty():
    assert BrowserWorker._react_probe_text(None) == ""
    assert BrowserWorker._react_probe_text({"ok": False}) == ""
    assert "不是 React/Vue 节点" in BrowserWorker._react_probe_text(
        {"ok": True, "framework": ""})
    text = BrowserWorker._react_probe_text(REACT_PROBE_STATE)
    assert "事件绑定=SendButton:onClick" in text
    assert "SendButton hooks" in text


# ---------- 2. React 探针工具 ----------

class _ProbeLoc:
    def __init__(self, state):
        self.first = self
        self._state = state

    def evaluate(self, js):
        return self._state


def test_react_probe_tool_formats_component_and_hooks():
    w = BrowserWorker.__new__(BrowserWorker)
    w._do_guard = lambda: None
    w._find_locator = lambda s, f=None: (None, _ProbeLoc(REACT_PROBE_STATE), "f0")
    out = w._do_react_probe({"selector": "span.send"})
    assert "[REACT 探针]" in out
    assert "SendButton" in out
    assert "元素事件属性: onClick" in out
    assert "hook 状态" in out and "1=\"这也太真实了吧\"" in out
    assert "重新加载页面" in out


def test_react_probe_tool_reports_non_react():
    w = BrowserWorker.__new__(BrowserWorker)
    w._do_guard = lambda: None
    w._find_locator = lambda s, f=None: (
        None, _ProbeLoc({"ok": True, "framework": ""}), "f0")
    out = w._do_react_probe({"selector": "button"})
    assert "不是 React/Vue 节点" in out
    assert "browser_inspect" in out


class _PointPage:
    def __init__(self, state):
        self._state = state
        self.calls = []

    def evaluate(self, js, args=None):
        self.calls.append((js, args))
        return self._state


def test_react_probe_at_point_uses_element_from_point():
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = _PointPage(REACT_PROBE_STATE)
    state = w._react_probe_at(734, 905)
    assert state is REACT_PROBE_STATE
    js, args = w._raw_page.calls[0]
    assert "elementFromPoint" in js
    assert args == [734, 905]


# ---------- 3. 真实 DOM：fiber/props 提取 ----------

FIBER_FIXTURE = """
<button id="send">发送</button>
<script>
  const btn = document.getElementById('send');
  function SendButton() {}
  SendButton.displayName = 'SendButton';
  const effectHook = {memoizedState: {create() {}, destroy() {}}, next: null};
  const textHook = {memoizedState: '这也太真实了吧', next: effectHook};
  const submitHook = {memoizedState: false, next: textHook};
  const parentFiber = {
    type: SendButton,
    memoizedProps: {onClick: function handleSend() {}},
    memoizedState: submitHook,
    return: null,
  };
  const hostFiber = {
    type: 'button',
    memoizedProps: {onClick: function handleSend() {}},
    return: parentFiber,
  };
  btn.__reactFiber$test = hostFiber;
  btn.__reactProps$test = {onClick: function handleSend() {}, children: '发送'};
</script>
"""


@pytest.fixture(scope="module")
def edge_page():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - 环境缺失时跳过
        pytest.skip(f"playwright 不可用: {exc}")
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(channel="msedge", headless=True)
    except Exception as exc:  # pragma: no cover - 无 Edge 时跳过
        pytest.skip(f"Edge headless 不可用: {exc}")
    page = browser.new_page(viewport={"width": 1000, "height": 800})
    yield page
    browser.close()
    pw.stop()


def test_react_probe_js_reads_handlers_and_hooks(edge_page):
    edge_page.set_content(FIBER_FIXTURE)
    handle = edge_page.query_selector("#send")
    state = edge_page.evaluate(_REACT_PROBE_JS, handle)
    assert state["ok"] is True and state["framework"] == "react"
    assert state["tag"] == "button"
    assert state["text"] == "发送"
    assert "SendButton" in state["chain"]
    assert state["handlers"] == ["onClick"]
    assert any("SendButton:onClick" == n for n in state["handler_nodes"])
    assert state["hook_owner"] == "SendButton"
    assert "0=false" in state["hooks"]
    assert '1="这也太真实了吧"' in state["hooks"]
    assert "2=(effect)" in state["hooks"]


def test_react_probe_js_is_safe_on_plain_dom(edge_page):
    edge_page.set_content("<div id='plain'>普通节点</div>")
    handle = edge_page.query_selector("#plain")
    state = edge_page.evaluate(_REACT_PROBE_JS, handle)
    assert state["ok"] is True
    assert state["framework"] == ""


# ---------- 4. 最终答复出口拦截 ----------

def _reg_browser():
    from agent.tools_registry import ToolDefinition, ToolRegistry
    reg = ToolRegistry()
    for name in ("browser_click", "browser_request_takeover", "noop"):
        reg.register(ToolDefinition(
            name=name, description="t",
            parameters={"type": "object", "properties": {}},
            func=lambda _args: "已执行"))
    reg.set_approval(SimpleNamespace(check=lambda *a, **k: True))
    return reg


def _orch(llm):
    from agent.context import ContextBuilder
    from agent.models import Message  # noqa: F401  (typing aid)
    from agent.orchestrator import Orchestrator
    llm.model = "offline"
    return Orchestrator(llm, _reg_browser(), ContextBuilder(max_tokens=4000))


def _reply_text():
    return ("评论还是没发出去。**1. 你手动点一次**最省事："
            "在已打开的 Edge 抖音页面点一下那个红色箭头。")


def test_browser_turn_manual_offload_final_answer_is_blocked():
    from agent.models import Message, ToolCall
    llm = MagicMock()
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[ToolCall(
            id="c1", name="browser_click", arguments={"ref": "ae74"})]),
        Message(role="assistant", content=_reply_text()),
        Message(role="assistant", content="继续取证：先读发送按钮内部状态再重试。"),
    ]
    orch = _orch(llm)
    reply = orch.run_turn(Message(role="user", content="打开抖音评论一个视频"))

    gates = [m for m in orch.history if getattr(m, "name", "") == "manual_offload_gate"]
    assert gates, "浏览器任务里让用户手动点，必须被拦截"
    assert "最终答复拦截" in gates[0].content
    assert "browser_request_takeover" in gates[0].content
    assert "browser_react_probe" in gates[0].content
    assert reply.content.startswith("继续取证")


def test_takeover_turn_allows_asking_user_for_manual_help():
    from agent.models import Message, ToolCall
    llm = MagicMock()
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[ToolCall(
            id="t1", name="browser_request_takeover",
            arguments={"reason": "滑块验证"})]),
        Message(role="assistant", content="请在页面中手动完成滑块验证。"),
    ]
    orch = _orch(llm)
    reply = orch.run_turn(Message(role="user", content="登录抖音"))

    gates = [m for m in orch.history if getattr(m, "name", "") == "manual_offload_gate"]
    assert not gates, "已走 takeover 求助出口，允许请求用户在页面内手动操作"
    assert "滑块验证" in reply.content


def test_non_browser_turn_is_not_blocked_for_manual_instructions():
    from agent.models import Message
    llm = MagicMock()
    llm.chat.side_effect = [
        Message(role="assistant", content="在 Excel 里你手动点击「插入」菜单即可。"),
    ]
    orch = _orch(llm)
    reply = orch.run_turn(Message(role="user", content="Excel 怎么插入行"))
    assert "插入" in reply.content
    gates = [m for m in orch.history if getattr(m, "name", "") == "manual_offload_gate"]
    assert not gates


def test_subagent_browser_evidence_counts_as_browser_touch():
    from agent.models import Message
    orch = _orch(MagicMock())
    orch._turn_browser_touched = False
    orch._turn_takeover_used = False
    orch._manual_offload_nudges = 0
    orch._note_turn_tool_usage([], [SimpleNamespace(
        name="wait_subagent",
        content="子 Agent 证据：browser_evaluate 显示评论 total=31，未出现目标评论")])
    assert orch._turn_browser_touched is True
    snippet = orch._should_block_manual_offload(Message(role="assistant", content=_reply_text()))
    assert "你手动点一次" in snippet


def test_manual_offload_nudge_cap_lets_final_answer_through():
    from agent.models import Message
    orch = _orch(MagicMock())
    orch._turn_browser_touched = True
    orch._turn_takeover_used = False
    orch._manual_offload_nudges = 0
    reply = Message(role="assistant", content=_reply_text())
    assert orch._should_block_manual_offload(reply)
    assert orch._should_block_manual_offload(reply)
    assert orch._should_block_manual_offload(reply) == "", "纠偏两次后放行，避免死循环"


def test_manual_offload_snippet_ignores_neutral_text():
    assert BrowserWorker is not None
    from agent.orchestrator import Orchestrator
    assert Orchestrator._manual_offload_snippet("已经发好了，评论区可以看到。") == ""
    snippet = Orchestrator._manual_offload_snippet("实在不行请你手动输入一下验证码")
    assert "请你手动输入" in snippet


# ---------- 5. 工具接线与提示词 ----------

def test_react_probe_is_registered_readonly_daily_tool():
    from agent.risk import _DAILY_TOOLS
    from agent.tools_registry import ToolRegistry
    assert "browser_react_probe" in _DAILY_TOOLS
    assert "browser_react_probe" in ToolRegistry._BROWSER_READONLY_TOOLS

    from tools.browser.worker import make_browser_tools
    tools = {t.name: t for t in make_browser_tools(BrowserWorker.__new__(BrowserWorker))}
    assert "browser_react_probe" in tools
    assert tools["browser_react_probe"].is_readonly is True


def test_prompts_document_zero_request_gate_and_probe():
    browser = Path("prompts/skills/browser.md").read_text(encoding="utf-8")
    assert "[提交闸门·零请求]" in browser
    assert "browser_react_probe" in browser
    assert "重新加载页面后重新输入再提交" in browser
    system = Path("prompts/system.md").read_text(encoding="utf-8")
    assert "程序级拦截" in system
