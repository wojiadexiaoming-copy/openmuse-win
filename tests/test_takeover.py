"""接管模式（spec §15 / §31.5）单元测试。"""
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from agent.takeover import TakeoverManager, TakeoverRequest, TakeoverResult


def test_takeover_request_and_complete():
    bus = MagicMock()
    mgr = TakeoverManager(event_bus=bus, auto_show_window=False)

    req = mgr.request_takeover(
        task_id="t1",
        reason="遇到验证码",
        target="Chrome",
        page_url="https://example.com/login",
    )
    assert req.task_id == "t1"
    assert req.status == "active"
    assert req.reason == "遇到验证码"
    call_args = bus.broadcast.call_args[0]
    assert call_args[0] == "takeover_request"
    assert call_args[1]["task_id"] == "t1"
    assert call_args[1]["target"] == "Chrome"
    assert call_args[1]["reason"] == "遇到验证码"
    assert call_args[1]["page_url"] == "https://example.com/login"
    assert call_args[1]["requested_at"]
    assert call_args[1]["expires_at"]

    status = mgr.get_status("t1")
    assert status["status"] == "active"
    assert status["target"] == "Chrome"

    # 异步完成
    def _resolve_soon():
        time.sleep(0.05)
        mgr.resolve("t1", "completed", "用户手动通过验证码")

    t = threading.Thread(target=_resolve_soon)
    t.start()

    res = mgr.wait_for_takeover("t1", timeout_sec=2.0)
    t.join()

    assert res.outcome == "completed"
    assert res.summary == "用户手动通过验证码"
    assert mgr.get_status("t1")["status"] == "completed"


def test_takeover_cancel():
    mgr = TakeoverManager(auto_show_window=False)
    mgr.request_takeover(task_id="t2", reason="登录墙", target="Edge")

    def _cancel_soon():
        time.sleep(0.05)
        mgr.resolve("t2", "cancelled", "用户放弃")

    t = threading.Thread(target=_cancel_soon)
    t.start()

    res = mgr.wait_for_takeover("t2", timeout_sec=2.0)
    t.join()

    assert res.outcome == "cancelled"
    assert mgr.get_status("t2")["status"] == "cancelled"


def test_takeover_timeout():
    mgr = TakeoverManager(auto_show_window=False)
    mgr.request_takeover(task_id="t3", reason="人机验证", timeout_sec=0.1)

    res = mgr.wait_for_takeover("t3", timeout_sec=0.1)
    assert res.outcome == "timeout"
    assert mgr.get_status("t3")["status"] == "timeout"


def test_takeover_api_routes(tmp_path, monkeypatch):
    import io
    import json
    monkeypatch.setenv("ALTEREGO_LLM_API_KEY", "sk-mock-takeover-key")
    from ui.server import AgentServer

    server = AgentServer(data_dir=tmp_path)
    app = server.app

    def call_app(method, path, body=None):
        raw_body = json.dumps(body).encode("utf-8") if body else b""
        res_info = []
        def start_response(status, headers, exc_info=None):
            res_info.append(status)
        env = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "HTTP_AUTHORIZATION": f"Bearer {server.api_token}",
            "wsgi.input": io.BytesIO(raw_body),
            "wsgi.errors": io.StringIO(),
            "CONTENT_LENGTH": str(len(raw_body)),
            "CONTENT_TYPE": "application/json",
        }
        resp_data = b"".join(app(env, start_response))
        return res_info[0], json.loads(resp_data.decode("utf-8")) if resp_data else {}

    # 发起接管
    status_code, data = call_app(
        "POST",
        "/api/takeover/request",
        {"task_id": "t_api", "reason": "需要人工接管", "target": "Chrome"},
    )
    assert "200" in status_code
    assert data["task_id"] == "t_api"
    assert data["status"] == "active"

    # 查询状态
    status_code, st = call_app("GET", "/api/takeover/t_api/status")
    assert "200" in status_code
    assert st["status"] == "active"
    assert st["reason"] == "需要人工接管"

    # 点击完成
    status_code, done_res = call_app("POST", "/api/takeover/t_api/done")
    assert "200" in status_code
    assert done_res["status"] == "completed"

    # 验证状态变为 completed
    st2 = server.takeover_mgr.get_status("t_api")
    assert st2["status"] == "completed"


def test_show_takeover_window_async(monkeypatch):
    from ui.takeover_window import show_takeover_window_async
    monkeypatch.setenv("ALTEREGO_HEADLESS", "1")
    proc = show_takeover_window_async("t_win", "Chrome", "测试原因", port=5999)
    assert proc is None


def test_browser_worker_request_takeover(monkeypatch):
    from tools.browser.worker import BrowserWorker
    from agent.takeover import get_takeover_manager

    mgr = get_takeover_manager()
    mgr.auto_show_window = False

    bw = BrowserWorker(profile_mgr=MagicMock(), circuit=MagicMock(), profile="test")
    bw._raw_page = MagicMock()
    bw._raw_page.url = "https://example.com/captcha"
    bw._do_snapshot = MagicMock(return_value="[当前页面] URL: https://example.com/home\n[button] 个人主页")

    # 在后台轮询并自动完成最新接管
    def _auto_resolve():
        for _ in range(40):
            time.sleep(0.05)
            with mgr._lock:
                for tid, req in mgr._requests.items():
                    if req.status == "active":
                        mgr.resolve(tid, "completed", "用户手动过验证码")
                        return

    t = threading.Thread(target=_auto_resolve)
    t.start()

    try:
        res = bw.request_takeover({"reason": "极验验证码", "timeout_sec": 2.0})
    finally:
        bw.stop()
    t.join()

    assert "[人工接管已完成]" in res
    assert "https://example.com/home" in res


def test_takeover_notifies_chat_and_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTEREGO_LLM_API_KEY", "sk-mock-takeover-key")
    from ui.server import AgentServer

    server = AgentServer(data_dir=tmp_path)
    sid = server.store.new_session("测试接管会话")
    server.orch.session_id = sid

    messages_received = []
    q = server.event_bus.subscribe()

    def _collect_events():
        while True:
            try:
                raw = q.get(timeout=0.1)
                if "event: message\n" in raw:
                    data_str = raw.split("data: ", 1)[1].rstrip("\n")
                    messages_received.append(json.loads(data_str))
            except Exception:
                break

    # 发起接管
    req = server.takeover_mgr.request_takeover(
        task_id="t_notify",
        reason="滑动拼图验证码",
        target="Chrome",
        page_url="https://platform.deepseek.com/sign_up",
    )

    _collect_events()
    server.event_bus.unsubscribe(q)

    # 1. 快照应记录 active_takeover
    assert server.state_snapshot["active_takeover"]["task_id"] == "t_notify"
    assert server.state_snapshot["agent_state"]["state"] == "paused_takeover"

    # 2. 应向聊天流广播带有 takeover_card 的消息
    assert len(messages_received) >= 1
    takeover_msg = next((m for m in messages_received if m.get("type") == "takeover_request"), None)
    assert takeover_msg is not None
    assert "滑动拼图验证码" in takeover_msg["content"]
    assert takeover_msg["takeover_card"]["task_id"] == "t_notify"

    # 3. 解决接管后状态恢复
    server.takeover_mgr.resolve("t_notify", "completed", "用户完成拼图")
    assert server.state_snapshot["active_takeover"] is None
    assert server.state_snapshot["agent_state"]["state"] == "acting"


def test_browser_verify_code_countdown_success():
    from tools.browser.worker import BrowserWorker

    bw = BrowserWorker(profile_mgr=MagicMock(), circuit=MagicMock(), profile="test")
    bw._ensure_started = MagicMock(return_value="")
    bw._do_guard = MagicMock(return_value="")
    bw._raw_page = MagicMock()
    # 模拟 evaluate 返回倒计时成功
    bw._raw_page.evaluate = MagicMock(return_value={
        "status": "countdown_found",
        "text": "重新获取 (59s)",
        "countdown_text": "59s",
        "tag": "button",
        "className": "ds-button",
        "disabled": True,
    })

    try:
        res = bw.verify_code_countdown({"timeout": 1.0})
    finally:
        bw.stop()

    assert "[倒计时已确认]" in res
    assert "59s" in res
    assert "发码请求已成功被服务端受理" in res


def test_browser_verify_code_countdown_failure_with_diagnostics():
    from tools.browser.worker import BrowserWorker

    bw = BrowserWorker(profile_mgr=MagicMock(), circuit=MagicMock(), profile="test")
    bw._ensure_started = MagicMock(return_value="")
    bw._do_guard = MagicMock(return_value="")
    bw._raw_page = MagicMock()
    # 模拟未出现倒计时且有滑块与报错
    bw._raw_page.evaluate = MagicMock(return_value={
        "status": "no_countdown",
        "captcha": "检测到人机验证组件: .geetest_holder",
        "form_errors": ["两次输入的密码不一致"],
        "send_button": {"text": "获取验证码", "disabled": False},
    })

    try:
        res = bw.verify_code_countdown({"timeout": 1.0})
    finally:
        bw.stop()

    assert "[未检测到倒计时]" in res
    assert "人机验证拦截" in res
    assert "两次输入的密码不一致" in res
    assert "browser_request_takeover" in res


