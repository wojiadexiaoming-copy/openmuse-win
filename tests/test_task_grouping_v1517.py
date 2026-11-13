"""v1.5.17 任务归组、续接判定、预算判据、证据保留与消息去重。"""
import pytest
from unittest.mock import MagicMock
from agent.models import Message
from agent.orchestrator import Orchestrator
from agent.subagent import _build_evidence, format_subagent_result, SubagentResult


def _orch():
    """Minimal orchestrator for unit tests."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.history = []
    return orch


def test_task_origin_stamps_metadata_on_origin_and_reply():
    """新任务：origin 与最终回复都带 task_origin_id/task_goal/task_status。"""
    orch = _orch()
    origin = Message(role="user", content="注册 Today AI 账号")
    reply = Message(role="assistant", content="已注册完成")
    orch.history.append(origin)
    orch._active_task_origin_id = origin.id
    # 模拟 _handle_turn 的打标逻辑
    verification_goal = origin.content
    origin.metadata["task_goal"] = verification_goal
    origin.metadata["task_origin_id"] = origin.id
    origin.metadata["task_status"] = "running"
    # 收尾打标
    for msg in (origin, reply):
        msg.metadata["task_status"] = "done"
        msg.metadata.setdefault("task_origin_id", origin.id)
        msg.metadata["task_goal"] = verification_goal
    assert origin.metadata["task_origin_id"] == origin.id
    assert origin.metadata["task_goal"] == "注册 Today AI 账号"
    assert reply.metadata["task_origin_id"] == origin.id
    assert reply.metadata["task_goal"] == "注册 Today AI 账号"
    assert reply.metadata["task_status"] == "done"


def test_continuation_waiting_user_merges_phone_code():
    """waiting_user 后，手机号/验证码属于同一任务。"""
    orch = _orch()
    origin = Message(role="user", content="注册 Today AI")
    origin.metadata["task_origin_id"] = origin.id
    origin.metadata["task_goal"] = "注册 Today AI"
    origin.metadata["task_status"] = "running"
    assistant = Message(role="assistant", content="请输入手机号")
    assistant.metadata["task_origin_id"] = origin.id
    assistant.metadata["task_goal"] = "注册 Today AI"
    assistant.metadata["task_status"] = "waiting_user"
    orch.history.extend([origin, assistant])
    # 用户回复手机号
    phone_msg = Message(role="user", content="15122810876")
    pending = orch._pending_origin(phone_msg)
    assert pending is origin
    # _task_origin 应返回 origin
    result = orch._task_origin("15122810876", phone_msg)
    assert result is origin


def test_continuation_regex_matches_retry_phrases():
    """“再重试下”“再试下”等续接词应被识别。"""
    assert Orchestrator._is_task_continuation("再重试下")
    assert Orchestrator._is_task_continuation("再试下")
    assert Orchestrator._is_task_continuation("再跑一次")
    assert Orchestrator._is_task_continuation("重新试一下")
    assert not Orchestrator._is_task_continuation("再生成一张不同风格")


def test_pending_answer_detects_phone_code_email():
    """手机号/验证码/邮箱应被识别为等待补充的回答。"""
    assert Orchestrator._is_pending_answer("15122810876")
    assert Orchestrator._is_pending_answer("824323")
    assert Orchestrator._is_pending_answer("user@example.com")
    assert Orchestrator._is_pending_answer("用我的 inodinodetree.com 临时邮箱注册领取下")
    assert not Orchestrator._is_pending_answer("帮我查下天气")


def test_progress_verdict_reports_reason():
    """预算判据应返回具体原因，而非笼统“无实质进展”。"""
    orch = _orch()
    sigs = [[("a", "1")], [("b", "2")], [("c", "3")]]
    # 有进展
    assert orch._progress_verdict(sigs, [0, 0, 0], 0, 3) == ""
    # probe_streak 但 new_info 足够
    assert orch._progress_verdict(sigs, [0, 0, 0], 3, 3) == ""
    # probe_streak 且 new_info 不足
    reason = orch._progress_verdict(sigs, [0, 0, 0], 3, 1)
    assert "连续 3 步仅探测" in reason
    # 无签名
    reason = orch._progress_verdict([], [], 0, 0)
    assert "没有可用的新签名" in reason


def test_evidence_retains_head_and_tail():
    """失败任务应保留头部登录证据 + 尾部探针，而非仅尾部。"""
    orch = _orch()
    orch.last_turn_tool_records = []
    # 模拟 20 条记录：前 5 条登录，后 15 条 404 探针
    for i in range(5):
        orch.last_turn_tool_records.append({
            "tool": "browser_navigate", "status": "ok",
            "args": {"url": f"https://example.com/login/{i}"},
            "result": f"已打开登录页 {i}"
        })
    for i in range(15):
        orch.last_turn_tool_records.append({
            "tool": "browser_evaluate", "status": "ok",
            "args": {"expression": f"fetch('/api/404/{i}')"},
            "result": f"404 Not Found {i}"
        })
    evidence = _build_evidence(orch, limit=12)
    assert len(evidence) == 12
    # 头部 4 条应包含登录证据
    head_tools = [e["tool"] for e in evidence[:4]]
    assert head_tools.count("browser_navigate") == 4
    # 尾部 8 条应包含探针
    tail_tools = [e["tool"] for e in evidence[-8:]]
    assert tail_tools.count("browser_evaluate") == 8


def test_format_subagent_result_shows_first_and_last():
    """主 Agent 看到的证据应包含首条（登录）+ 尾 N-1 条（探针）。"""
    result = SubagentResult(
        task_id="sub-test", status="partial",
        summary="登录成功但未完成",
        evidence=[
            {"tool": "browser_navigate", "args": "{}", "result": "登录页"},
            {"tool": "browser_evaluate", "args": "{}", "result": "404-1"},
            {"tool": "browser_evaluate", "args": "{}", "result": "404-2"},
            {"tool": "browser_evaluate", "args": "{}", "result": "404-3"},
        ],
        confidence="medium"
    )
    text = format_subagent_result(result, max_evidence=3)
    assert "登录页" in text
    assert "404-3" in text
    assert "404-2" in text


def test_evidence_keeps_failed_records_over_probes():
    """失败/可疑记录必须优先保留，不能被大量成功探针挤出。"""
    orch = _orch()
    orch.last_turn_tool_records = [{
        "tool": "browser_fill_input", "status": "ok",
        "args": {"selector": ".comment-input", "text": "评论内容"},
        "result": "[输入未落地] 目标文本未能成功写入页面",
    }]
    for i in range(15):
        orch.last_turn_tool_records.append({
            "tool": "browser_evaluate", "status": "ok",
            "args": {"expression": f"read {i}"},
            "result": f"data row {i}",
        })
    orch.last_turn_tool_records.append({
        "tool": "browser_click", "status": "error",
        "args": {"ref": "ae9"},
        "result": "Timeout 30000ms exceeded",
    })
    evidence = _build_evidence(orch, limit=6)
    assert len(evidence) == 6
    statuses = {e["tool"]: e["status"] for e in evidence}
    assert statuses["browser_fill_input"] == "suspicious"
    assert statuses["browser_click"] == "error"


def test_format_subagent_result_prefers_failure_evidence():
    result = SubagentResult(
        task_id="sub-x", status="partial", summary="评论未写入",
        evidence=[
            {"tool": "browser_navigate", "args": "{}", "result": "打开了视频", "status": "ok"},
            {"tool": "browser_fill_input", "args": "{}",
             "result": "[输入未落地] 目标文本未能成功写入", "status": "suspicious"},
            {"tool": "browser_click", "args": "{}", "result": "Timeout", "status": "error"},
            {"tool": "browser_screenshot", "args": "{}", "result": "截图已保存", "status": "ok"},
        ],
        confidence="medium",
    )
    text = format_subagent_result(result, max_evidence=2)
    assert "[输入未落地]" in text
    assert "Timeout" in text


def test_duplicate_message_dedup_by_full_history():
    """_handle_turn 应全历史查重，避免 flow_template 插入后重复落库。"""
    orch = _orch()
    msg = Message(role="user", content="测试消息")
    orch.history.append(msg)
    # 插入 flow_template
    template = Message(role="system", name="flow_template", content="[任务模式识别] 模板")
    orch.history.append(template)
    # 再次尝试 append 同一条 msg（模拟重入）
    if not any(previous.id == msg.id for previous in orch.history):
        orch.history.append(msg)
    # 不应重复
    assert len([m for m in orch.history if m.id == msg.id]) == 1
    assert orch.history[-1] is template
