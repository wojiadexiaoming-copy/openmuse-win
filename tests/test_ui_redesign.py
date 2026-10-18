"""测试 UI 重设计后端协议：状态快照、事件更新与消息过滤。

验收指标（docs/ui-redesign/event-protocol.md & display-rules.md）：
1. GET /api/state/snapshot 正常返回 4 tab 所需状态数据
2. emit_event 正确更新 state_snapshot
3. GET /api/sessions/<sid>/messages 过滤裸 JSON，补充人话中文摘要
"""
import io
import json
import pytest

from agent.models import Message, ToolCall
from ui.server import AgentServer


@pytest.fixture
def test_server(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTEREGO_LLM_API_KEY", "sk-mock-auth-key")
    server = AgentServer(data_dir=tmp_path)
    return server


def _call_api(app, method="GET", path="/", headers=None, body=None):
    raw_body = json.dumps(body).encode("utf-8") if body else b""
    status_box = []
    resp_headers = []

    def start_response(status, response_headers, exc_info=None):
        status_box.append(status)
        resp_headers.extend(response_headers)

    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "wsgi.input": io.BytesIO(raw_body),
        "wsgi.errors": io.StringIO(),
        "CONTENT_LENGTH": str(len(raw_body)),
        "CONTENT_TYPE": "application/json",
    }
    if "?" in path:
        p, q = path.split("?", 1)
        env["PATH_INFO"] = p
        env["QUERY_STRING"] = q

    if headers:
        for k, v in headers.items():
            env["HTTP_" + k.upper().replace("-", "_")] = v

    resp_data = b"".join(app(env, start_response))
    status_str = status_box[0] if status_box else "500 Internal Error"
    headers_dict = {k: v for k, v in resp_headers}
    return status_str, headers_dict, resp_data.decode("utf-8")


def test_state_snapshot_endpoint(test_server):
    headers = {"Authorization": f"Bearer {test_server.api_token}"}
    status, _, body = _call_api(test_server.app, "GET", "/api/state/snapshot", headers=headers)
    assert status.startswith("200")
    data = json.loads(body)
    assert "agent_state" in data
    assert "plan" in data
    assert "activity" in data
    assert "approvals" in data
    assert "status" in data
    assert data["agent_state"]["state"] == "idle"
    assert data["agent_state"]["detail"] == "就绪"


def test_emit_event_updates_snapshot(test_server):
    # 1. agent_state
    test_server.emit_event("agent_state", {
        "state": "acting",
        "detail": "正在打开网页 github.com",
        "model": "qwen2.5-coder-32b",
    })
    snap = test_server.state_snapshot
    assert snap["agent_state"]["state"] == "acting"
    assert snap["agent_state"]["detail"] == "正在打开网页 github.com"
    assert snap["status"]["current_model"] == "qwen2.5-coder-32b"

    # 2. plan_update
    test_server.emit_event("plan_update", {
        "steps": [
            {"id": "s1", "title": "下载安装包", "status": "done"},
            {"id": "s2", "title": "启动应用", "status": "running"},
        ]
    })
    assert len(snap["plan"]) == 2
    assert snap["plan"][1]["status"] == "running"

    # 3. tool_call_started & done
    test_server.emit_event("tool_call_started", {
        "id": "tc_01",
        "tool": "browser_navigate",
        "summary": "正在打开网页 github.com",
        "args": {"url": "https://github.com"},
    })
    assert len(snap["activity"]) == 1
    assert snap["activity"][0]["status"] == "running"

    test_server.emit_event("tool_call_done", {
        "id": "tc_01",
        "tool": "browser_navigate",
        "status": "ok",
        "duration_ms": 1200,
        "result_summary": "返回 200，页面标题 GitHub",
    })
    assert snap["activity"][0]["status"] == "ok"
    assert snap["activity"][0]["duration_ms"] == 1200


def test_messages_strips_raw_json(test_server):
    sid = test_server.store.new_session("测试会话")
    # 模拟历史数据中混入了裸 JSON 气泡与 tool 调用
    history = [
        Message(role="user", content="打开 Clash"),
        Message(
            role="assistant",
            content='{"name": "desktop_open_app", "arguments": {"app_name": "ClashVerge.exe"}}',
            tool_calls=[ToolCall(id="tc_1", name="desktop_open_app", arguments={"app_name": "ClashVerge.exe"})],
        ),
        Message(role="tool", tool_call_id="tc_1", name="desktop_open_app", content="[成功] 已启动 ClashVerge.exe"),
        Message(role="assistant", content="我已经为你打开了 Clash Verge 应用程序。"),
    ]
    test_server.store.save_history(sid, history)

    headers = {"Authorization": f"Bearer {test_server.api_token}"}
    status, _, body = _call_api(test_server.app, "GET", f"/api/sessions/{sid}/messages", headers=headers)
    assert status.startswith("200")
    msgs = json.loads(body)

    # 验证：
    # 1. tool 角色消息被完全过滤（不单独作为气泡）
    assert not any(m.get("role") == "tool" for m in msgs)

    # 2. 裸 JSON 文本被物理剥离，content 置空
    tc_msg = next((m for m in msgs if m.get("tool_calls")), None)
    assert tc_msg is not None
    assert tc_msg["content"] == ""  # 裸 JSON 被清洗
    assert tc_msg["type"] == "tool_step"
    # 3. 携带中文人话摘要
    assert len(tc_msg["tool_calls"]) == 1
    assert "正在打开应用" in tc_msg["tool_calls"][0]["summary"]

    # 4. 最终自然语言回复保留为 assistant_final
    final_msg = msgs[-1]
    assert final_msg["type"] == "assistant_final"
    assert "我已经为你打开了" in final_msg["content"]


def test_chat_triggers_background_distill_without_error(test_server, monkeypatch):
    """验证 /api/chat 异步触发记忆提炼正常执行，不会抛出 NameError 线程异常。"""
    import time
    monkeypatch.setattr(test_server.orch, "handle", lambda user_msg: "你好，已为你完成操作。")
    headers = {"Authorization": f"Bearer {test_server.api_token}"}
    status, _, body = _call_api(
        test_server.app,
        "POST",
        "/api/chat",
        headers=headers,
        body={"content": "请记住我喜欢使用深色主题"},
    )
    assert status.startswith("200")
    data = json.loads(body)
    assert "message" in data
    time.sleep(0.3)


def test_approvals_history_endpoint(test_server):
    """验证 GET /api/approvals/history 正常返回历史审计记录，且 snapshot 携带 approval_history。"""
    # 模拟写入两条审批决议
    test_server.approval.check("file_write", {"path": "b.txt", "content": "123"},
                               on_ask=lambda c: (True, "once"))
    test_server.approval.check("shell_exec", {"command": "taskkill /IM notepad.exe /F"},
                               on_ask=lambda c: (False, "once"))

    headers = {"Authorization": f"Bearer {test_server.api_token}"}
    status, _, body = _call_api(test_server.app, "GET", "/api/approvals/history?limit=10", headers=headers)
    assert status.startswith("200")
    history = json.loads(body)
    assert len(history) == 2
    assert history[0]["action"] == "shell_exec"
    assert history[0]["action_cn"] == "执行系统终端命令"
    assert history[0]["decision"] == "rejected"
    assert history[1]["action"] == "file_write"
    assert history[1]["action_cn"] == "写入/创建本地文件"
    assert history[1]["decision"] == "approved"

    # 验证快照集成
    status_snap, _, body_snap = _call_api(test_server.app, "GET", "/api/state/snapshot", headers=headers)
    assert status_snap.startswith("200")
    snap_data = json.loads(body_snap)
    assert "approval_history" in snap_data
    assert len(snap_data["approval_history"]) == 2


def test_window_split_ratio_endpoint(test_server):
    """测试分屏比例动态联动接口与原生窗口缩放。"""
    from unittest.mock import MagicMock
    mock_window = MagicMock()
    test_server.set_ui_window(mock_window)

    headers = {"Authorization": f"Bearer {test_server.api_token}"}
    
    # 1. 切换为收起态 (80% 浏览器, 20% UI)
    status, _, body = _call_api(
        test_server.app, "POST", "/api/window/split_ratio",
        headers=headers, body={"browser_ratio": 0.80}
    )
    assert status.startswith("200")
    data_80 = json.loads(body)
    assert data_80["ok"] is True
    assert data_80["browser_ratio"] == 0.80
    assert mock_window.move.called
    assert mock_window.resize.called

    # 2. 切换为展开态 (70% 浏览器, 30% UI)
    status, _, body = _call_api(
        test_server.app, "POST", "/api/window/split_ratio",
        headers=headers, body={"browser_ratio": 0.70}
    )
    assert status.startswith("200")
    data_70 = json.loads(body)
    assert data_70["ok"] is True
    assert data_70["browser_ratio"] == 0.70
    assert data_70["layout"]["ui"]["width"] > data_80["layout"]["ui"]["width"]

    # 3. GET 查询当前分屏
    status, _, body = _call_api(test_server.app, "GET", "/api/window/split_ratio", headers=headers)
    assert status.startswith("200")
    get_data = json.loads(body)
    assert get_data["browser_ratio"] == 0.70


def test_toast_resolve_endpoint(test_server):
    """测试 Windows 系统级 Toast 通知直接点击决断。"""
    import threading
    from agent.approval import ApprovalCard

    card = ApprovalCard(
        approval_id="toast_test_123",
        action="shell_exec",
        detail="rm -rf tmp",
        risk="L2",
        action_cn="执行系统终端命令",
        summary_cn="删除临时目录",
    )
    evt = threading.Event()
    test_server.approval._pending[card.approval_id] = {
        "card": card,
        "event": evt,
        "decision": None,
        "scope": None,
    }

    # 模拟用户点击 Toast 上的「✔ 批准授权」
    url = f"/api/approvals/toast_resolve?id={card.approval_id}&token={test_server.api_token}&approved=1&scope=once"
    status, hdrs, body = _call_api(test_server.app, "GET", url)
    assert status.startswith("200")
    assert "✔ 已批准授权" in body
    assert "操作成功生效" in body
    assert evt.is_set()
    assert test_server.approval._pending[card.approval_id]["decision"] is True

    # 模拟用户点击 Toast 上的「✕ 拒绝」
    card2 = ApprovalCard(
        approval_id="toast_test_456",
        action="file_delete",
        detail="important.docx",
        risk="L3",
        action_cn="删除本地文件",
    )
    evt2 = threading.Event()
    test_server.approval._pending[card2.approval_id] = {
        "card": card2,
        "event": evt2,
        "decision": None,
        "scope": None,
    }

    url2 = f"/api/approvals/toast_resolve?id={card2.approval_id}&token={test_server.api_token}&approved=0&scope=once"
    status2, _, body2 = _call_api(test_server.app, "GET", url2)
    assert status2.startswith("200")
    assert "✕ 已拒绝授权" in body2
    assert evt2.is_set()
    assert test_server.approval._pending[card2.approval_id]["decision"] is False


def test_dynamic_models_endpoint(test_server):
    """测试 /api/models 动态发现并返回模型列表，且不泄露后端密钥字段。"""
    headers = {"Authorization": f"Bearer {test_server.api_token}"}
    status, _, body = _call_api(test_server.app, "GET", "/api/models", headers=headers)
    assert status.startswith("200")
    data = json.loads(body)
    assert "current" in data
    assert "models" in data
    assert isinstance(data["models"], list)
    assert len(data["models"]) >= 5

    # 验证常用模型均存在
    model_ids = [m["id"] for m in data["models"]]
    assert "agnes-3.0-flash" in model_ids
    assert "qwen3.8-omni-flash" in model_ids
    assert "kimi-k3" in model_ids

    # 验证不泄露敏感密钥字段
    for m in data["models"]:
        assert "key_env" not in m
        assert "vault_key" not in m


def test_switch_model_and_custom_model_lifecycle(test_server, monkeypatch):
    """测试动态切换主模型，以及动态添加、持久化与删除自定义模型。"""
    headers = {"Authorization": f"Bearer {test_server.api_token}"}
    monkeypatch.setenv("ALTEREGO_AGNES_API_KEY", "sk-mock-agnes")
    monkeypatch.setenv("ALTEREGO_LLM_API_KEY", "sk-mock-custom")

    # 1. 动态添加并切换到自定义模型
    custom_id = "custom-deepseek-r1"
    status, _, body = _call_api(
        test_server.app,
        "POST",
        "/api/switch_model",
        headers=headers,
        body={"model_id": custom_id, "base_url": "https://api.deepseek.com/v1"}
    )
    assert status.startswith("200")
    res = json.loads(body)
    assert res["ok"] is True
    assert res["current"] == custom_id

    # 2. 检查 /api/models 是否已包含该自定义模型
    status, _, body = _call_api(test_server.app, "GET", "/api/models", headers=headers)
    models_data = json.loads(body)
    custom_entry = next((m for m in models_data["models"] if m["id"] == custom_id), None)
    assert custom_entry is not None
    assert custom_entry["is_custom"] is True
    assert custom_entry["base_url"] == "https://api.deepseek.com/v1"

    # 3. 删除该自定义模型
    status, _, body = _call_api(
        test_server.app,
        "DELETE",
        f"/api/models/{custom_id}",
        headers=headers
    )
    assert status.startswith("200")
    del_res = json.loads(body)
    assert del_res["ok"] is True

    # 4. 再次检查 /api/models，已成功移除
    status, _, body = _call_api(test_server.app, "GET", "/api/models", headers=headers)
    models_data = json.loads(body)
    assert not any(m["id"] == custom_id for m in models_data["models"])


def test_unified_save_model_config_with_key(test_server):
    """测试一体化大模型配置保存：同时绑定 Model ID、Base URL 与 API Key，存入 Vault 并即时生效。"""
    headers = {"Authorization": f"Bearer {test_server.api_token}"}

    payload = {
        "model": "qwen3.8-omni-flash",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "sk-dashscope-mock-key-12345",
    }
    status, _, body = _call_api(
        test_server.app,
        "POST",
        "/api/settings/save_model_config",
        headers=headers,
        body=payload
    )
    assert status.startswith("200")
    res = json.loads(body)
    assert res["ok"] is True
    assert res["current"] == "qwen3.8-omni-flash"
    assert res["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert res["has_key"] is True

    # 验证密钥已被硬件加密存入 Vault
    assert test_server.vault.resolve("dashscope_api_key", caller="test") == "sk-dashscope-mock-key-12345"
    assert test_server.vault.resolve("llm_api_key", caller="test") == "sk-dashscope-mock-key-12345"

    # 验证 GET /api/settings 已同步更新模型与状态
    status, _, body = _call_api(test_server.app, "GET", "/api/settings", headers=headers)
    assert status.startswith("200")
    s = json.loads(body)
    assert s["model"] == "qwen3.8-omni-flash"
    assert s["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert s["has_key"] is True





