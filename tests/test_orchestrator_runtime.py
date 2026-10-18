"""Offline integration regressions for model payloads and turn states."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.context import ContextBuilder
from agent.llm import LLMClient
from agent.models import ImagePart, Message, ToolCall, tool_result_is_error
from agent.orchestrator import Orchestrator
from agent.planner import Plan, PlanStep
from agent.task_tracker import TaskTracker
from agent.tools_registry import ToolDefinition, ToolRegistry
from agent.tracing import Tracer


def registry(*definitions):
    reg = ToolRegistry()
    reg.set_approval(SimpleNamespace(check=lambda *args, **kwargs: True))
    for definition in definitions:
        reg.register(definition)
    return reg


def tool(name, function):
    return ToolDefinition(name=name, description=name,
                          parameters={"type": "object", "properties": {}}, func=function)


def response(content="", calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, tool_calls=calls or []))])


@pytest.mark.parametrize("parallel", [False, True])
def test_screenshot_reaches_model_payload_after_all_tool_replies(tmp_path, monkeypatch, parallel):
    monkeypatch.chdir(tmp_path)
    image = ImagePart(data=b"test-jpeg-bytes", mime_type="image/jpeg")
    reg = registry(
        tool("desktop_screenshot", lambda _: Message(role="tool", content="[桌面截图] test.jpg", images=[image])),
        tool("file_read", lambda _: "visible text"),
    )
    client = LLMClient(base_url="https://example.invalid/v1", model="test", api_key="offline")
    client.client = MagicMock()
    calls = [SimpleNamespace(id="picture", function=SimpleNamespace(
        name="desktop_screenshot", arguments="{}")),
        SimpleNamespace(id="text", function=SimpleNamespace(name="file_read", arguments="{}"))]
    client.client.chat.completions.create.side_effect = [response(calls=calls), response("窗口可见")]
    tracer = Tracer(str(tmp_path / "spans.db"))
    orch = Orchestrator(client, reg, ContextBuilder(), parallel_tools=parallel, tracer=tracer)
    assert orch.handle("查看窗口") == "窗口可见"
    payload = client.client.chat.completions.create.call_args_list[1].kwargs["messages"]
    assert [item["role"] for item in payload[-3:]] == ["tool", "tool", "user"]
    assert [item["tool_call_id"] for item in payload[-3:-1]] == ["picture", "text"]
    assert payload[-1]["content"][1]["type"] == "image_url"
    assert payload[-1]["content"][1]["image_url"]["url"].endswith(image.data_base64)
    assert orch.last_turn_status == "done"
    spans = tracer.db.execute("SELECT trace_id,kind,name,ended_at,status,parent_id FROM spans").fetchall()
    assert len({row[0] for row in spans}) == 1
    assert all(row[0] and row[3] is not None for row in spans)
    assert sum(row[1] == "llm" for row in spans) == 2
    assert sum(row[1] == "tool" for row in spans) == 2
    assert all(row[4] == "ok" for row in spans)
    root = tracer.db.execute("SELECT span_id FROM spans WHERE kind='agent'").fetchone()[0]
    assert all(row[5] == root for row in spans if row[1] != "agent")
    tracer.close()


@pytest.mark.parametrize("content", [
    "[桌面点击失败] unable to activate", "[滚轮失败] invalid value",
    "[按键失败] unknown key", "[工具不存在] unknown tool", "[资源冲突] busy",
    "[拒绝] no approval", "[未配置] key missing", "[工具未启用] disabled",
])
def test_tool_errors_are_consistent_in_events_and_trace(tmp_path, monkeypatch, content):
    monkeypatch.chdir(tmp_path)
    events = []
    tracer = Tracer(str(tmp_path / "spans.db"))
    tracker = TaskTracker()
    reg = registry(tool("desktop_click", lambda _: content))
    replies = iter([Message(role="assistant", tool_calls=[ToolCall(id="bad", name="desktop_click")]),
                    Message(role="assistant", content="需要你协助确认窗口后才能继续")])
    llm = SimpleNamespace(chat=lambda *args, **kwargs: next(replies), model="offline")
    orch = Orchestrator(llm, reg, ContextBuilder(), tracer=tracer, tracker=tracker,
                        on_event=lambda name, data: events.append((name, data)))
    orch.handle("点击窗口")
    assert orch.last_turn_tool_records[0]["status"] == "error"
    assert next(data for name, data in events if name == "tool_call_done")["status"] == "error"
    assert tracer.db.execute("SELECT status FROM spans WHERE kind='tool'").fetchone()[0] == "error"
    assert tracker.list_all()[0].steps[0].status == "failed"
    assert orch.last_turn_status == "waiting_user"
    tracer.close()


def test_failure_words_in_success_body_do_not_change_status():
    assert not tool_result_is_error("[桌面已激活] 已修复上次失败")
    assert not tool_result_is_error("用户讨论错误与失败的原因")


def test_mid_turn_exception_preserves_tools_and_closes_trace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tracer = Tracer(str(tmp_path / "spans.db"))
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[ToolCall(id="one", name="file_read")]),
        RuntimeError("example API failure"),
    ]
    orch = Orchestrator(llm, registry(tool("file_read", lambda _: "evidence")), ContextBuilder(), tracer=tracer)
    with pytest.raises(RuntimeError):
        orch.handle("读取文件")
    assert [message.role for message in orch.history] == ["user", "assistant", "tool"]
    assert orch.history[-1].content == "evidence"
    assert orch.last_turn_status == "error"
    assert tracer.db.execute("SELECT COUNT(*) FROM spans WHERE ended_at IS NULL").fetchone()[0] == 0
    assert tracer.db.execute("SELECT status FROM spans WHERE kind='agent'").fetchone()[0] == "error"
    tracer.close()


def test_steps_limit_is_incomplete_and_persists_final_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    llm = SimpleNamespace(chat=lambda *args, **kwargs: Message(
        role="assistant", tool_calls=[ToolCall(id="loop", name="file_read")]))
    tracker = TaskTracker()
    orch = Orchestrator(llm, registry(tool("file_read", lambda _: "data")), ContextBuilder(),
                        max_iters=2, tracker=tracker)
    reply = orch.handle("读取文件")
    assert "步骤上限" in reply
    assert orch.last_turn_status == tracker.list_all()[0].status == "incomplete"
    assert orch.history[-1].content == reply


def test_final_repair_is_checked_and_cannot_report_done(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import agent.verifier as verifier
    checks = []
    monkeypatch.setattr(verifier, "verify_outcome", lambda goal, **kwargs: (
        checks.append(goal) or False, ["没有本轮图片产物"]))
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.return_value = Message(role="assistant", content="已生成")
    orch = Orchestrator(llm, registry(), ContextBuilder())
    reply = orch.handle("生图并下载")
    assert llm.chat.call_count == len(checks) == 3
    assert orch.last_turn_status == "incomplete"
    assert "尚未完成" in reply


def test_retry_checks_original_goal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import agent.verifier as verifier
    goals = []
    monkeypatch.setattr(verifier, "verify_outcome", lambda goal, **kwargs: (
        goals.append(goal) or True, []))
    llm = SimpleNamespace(chat=lambda *args, **kwargs: Message(role="assistant", content="完成"))
    orch = Orchestrator(llm, registry(), ContextBuilder())
    orch.history = [Message(role="user", content="打开豆包，然后生图并下载"),
                    Message(role="user", name="tool_observation", content="[工具视觉观察]"),
                    Message(role="assistant", content="等待人工")]
    orch.handle("重试")
    assert goals == ["打开豆包，然后生图并下载"]


def test_plan_stops_at_manual_dependency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.return_value = Message(role="assistant", content="需要你协助输入验证码后才能继续")
    steps = [PlanStep(desc="登录", done_when="已登录"), PlanStep(desc="下载", done_when="已下载")]
    planner = SimpleNamespace(make_plan=lambda _: Plan(goal="test", steps=steps))
    orch = Orchestrator(llm, registry(), ContextBuilder(), planner=planner)
    orch.handle("请登录然后下载")
    assert llm.chat.call_count == 1
    assert steps[0].status == "waiting_user"
    assert steps[1].status == "pending"
    assert orch.last_turn_status == "waiting_user"


def test_optional_confirmation_is_not_manual_block():
    assert not Orchestrator._needs_user(Message(role="assistant", content="已完成，请确认效果。"))


@pytest.mark.parametrize("parallel", [False, True])
def test_batch_exception_pairs_every_call_and_preserves_completed_result(tmp_path, monkeypatch, parallel):
    monkeypatch.chdir(tmp_path)
    llm = SimpleNamespace(chat=lambda *args, **kwargs: Message(
        role="assistant", tool_calls=[
            ToolCall(id="first", name="file_write"), ToolCall(id="second", name="file_edit")]))
    reg = registry(tool("file_write", lambda _: "first output"), tool("file_edit", lambda _: "second output"))
    execute = reg.execute
    def fail_second(name, arguments):
        if name == "file_edit":
            raise RuntimeError("approval check failed")
        return execute(name, arguments)
    reg.execute = fail_second
    orch = Orchestrator(llm, reg, ContextBuilder(), parallel_tools=parallel)
    with pytest.raises(RuntimeError):
        orch.handle("执行两步")
    assert [m.role for m in orch.history] == ["user", "assistant", "tool", "tool"]
    assert [m.tool_call_id for m in orch.history[-2:]] == ["first", "second"]
    assert orch.history[-2].content == "first output"
    assert tool_result_is_error(orch.history[-1].content)


def test_verification_feedback_lists_only_business_goals(tmp_path, monkeypatch):
    """§39.1.4：验收反馈只列业务未达项，禁止"xx 工具失败请修复"措辞。

    Agnes 翻车：旧措辞"工具操作仍有未恢复的失败：shell_exec、file_read"
    把模型引向修工具（跑去执行 whoami/dir，验收还认了"成功"）。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agent.verifier.verify_outcome",
                        lambda *a, **k: (False, ["点击按钮（业务目标未达成）"]))
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[
            ToolCall(id="click", name="desktop_click"),
            ToolCall(id="shot", name="desktop_screenshot")]),
        Message(role="assistant", content="已完成点击"),
        Message(role="assistant", content="已完成点击"),
        Message(role="assistant", content="已完成点击"),
    ]
    orch = Orchestrator(llm, registry(
        tool("desktop_click", lambda _: "[点击失败] no window"),
        tool("desktop_screenshot", lambda _: "[桌面截图] saved"),
    ), ContextBuilder())
    reply = orch.handle("点击按钮")
    assert "尚未完成" in reply
    assert orch.last_turn_status == "incomplete"
    feedbacks = [m for m in orch.history if m.name == "verification_feedback"]
    assert feedbacks, "业务目标未达成时应有验收反馈"
    for fb in feedbacks:
        assert "点击按钮" in fb.content, "反馈必须列出业务未达项"
        assert "工具操作仍有未恢复的失败" not in fb.content, "禁止旧措辞把模型引向修工具"


@pytest.mark.parametrize("content,failed", [
    ("exit=0\nbody mentions exit=1", False), ("exit=2\nfailed", True),
    ("exit=-1\ncrashed", True), ("[生成异常] network failure", True),
])
def test_shell_exit_codes_and_media_exceptions(content, failed):
    assert tool_result_is_error(content) is failed


def test_resume_after_restart_keeps_original_artifact_baseline(tmp_path, monkeypatch):
    from PIL import Image
    from agent.session_store import SessionStore
    monkeypatch.chdir(tmp_path)
    store = SessionStore(str(tmp_path / "sessions.db"))
    sid = store.new_session("three pictures")
    def create_two(*args, **kwargs):
        Image.new("RGB", (8, 8), "red").save(tmp_path / "first.png")
        Image.new("RGB", (8, 8), "blue").save(tmp_path / "second.png")
        return Message(role="assistant", content="需要你协助确认后才能继续")
    first = Orchestrator(SimpleNamespace(chat=create_two), registry(), ContextBuilder())
    first.handle("生成3张图片")
    assert first.last_turn_status == "waiting_user"
    store.save_history(sid, first.history)
    store.db.close()
    restored = SessionStore(str(tmp_path / "sessions.db"))
    def create_last(*args, **kwargs):
        Image.new("RGB", (8, 8), "green").save(tmp_path / "third.png")
        return Message(role="assistant", content="三张图片已生成")
    second = Orchestrator(SimpleNamespace(chat=create_last), registry(), ContextBuilder())
    second.history = restored.load_history(sid)
    second.handle("继续")
    assert second.last_turn_status == "done"
    assert len(list(tmp_path.glob("*.png"))) == 3
    restored.db.close()


def test_download_artifact_can_be_saved_to_default_downloads(tmp_path, monkeypatch):
    from PIL import Image
    from pathlib import Path
    monkeypatch.chdir(tmp_path)
    fake_home = tmp_path / "user"
    downloads = fake_home / "Downloads"
    downloads.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    def download(*args, **kwargs):
        Image.new("RGB", (8, 8), "green").save(downloads / "picture.png")
        return Message(role="assistant", content="图片已下载")
    orch = Orchestrator(SimpleNamespace(chat=download), registry(), ContextBuilder())
    orch.handle("生成图片并下载")
    assert orch.last_turn_status == "done"
    assert len(orch.history[0].metadata["artifact_baseline"]["workspace"]) == 2
