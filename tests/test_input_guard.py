"""测试浏览器视觉遮罩与硬件输入锁联动方案（Spec §15.7）。

测试范围：
1. BrowserInputGuard 初始化与 expose_binding 安全注册
2. lock_for_agent: 蓝紫流光模式 + CDP 硬件级输入屏蔽
3. unlock_for_human: 琥珀橙接管模式 + CDP 放行 + 携带任务元数据
4. 网页内按钮点击回调联动 TakeoverManager.resolve
5. release_all: IDLE 清理模式
6. 多标签/页面跳转时状态自动同步
"""
from unittest.mock import MagicMock, patch
import pytest

from tools.browser.input_guard import BrowserInputGuard
from agent.takeover import get_takeover_manager


def test_input_guard_init_and_binding():
    page = MagicMock()
    guard = BrowserInputGuard(page)

    assert guard.mode == "IDLE"
    assert guard.is_locked is False
    # 验证向页面注册了暴露绑定
    page.expose_binding.assert_called_once()
    args = page.expose_binding.call_args[0]
    assert args[0] == "__alterego_finish_takeover"


def test_lock_for_agent():
    page = MagicMock()
    mock_cdp = MagicMock()
    page.context.new_cdp_session.return_value = mock_cdp

    guard = BrowserInputGuard(page)
    ok = guard.lock_for_agent()

    assert ok is True
    assert guard.mode == "AGENT_ACTIVE"
    assert guard.is_locked is True

    # 验证 CDP 放行输入事件，保证顶部横幅可交互（由 DOM 拦截器负责防误触）
    mock_cdp.send.assert_called_with("Emulation.setIgnoreInputEvents", {"ignore": False})
    # 验证向页面注入了 AGENT_ACTIVE 状态
    page.evaluate.assert_called()
    eval_arg = page.evaluate.call_args[0][1]
    assert eval_arg["mode"] == "AGENT_ACTIVE"


def test_unlock_for_human():
    page = MagicMock()
    mock_cdp = MagicMock()
    page.context.new_cdp_session.return_value = mock_cdp

    guard = BrowserInputGuard(page)
    # 先加锁再解锁
    guard.lock_for_agent()
    ok = guard.unlock_for_human(reason="遇到极验滑块", task_id="t_123")

    assert ok is True
    assert guard.mode == "HUMAN_TAKEOVER"
    assert guard.reason == "遇到极验滑块"
    assert guard.current_task_id == "t_123"
    assert guard.is_locked is False

    # 验证 CDP 放行了物理键鼠
    mock_cdp.send.assert_called_with("Emulation.setIgnoreInputEvents", {"ignore": False})
    # 验证向页面注入了 HUMAN_TAKEOVER 与原因
    eval_arg = page.evaluate.call_args[0][1]
    assert eval_arg["mode"] == "HUMAN_TAKEOVER"
    assert eval_arg["reason"] == "遇到极验滑块"
    assert eval_arg["taskId"] == "t_123"


def test_release_all():
    page = MagicMock()
    mock_cdp = MagicMock()
    page.context.new_cdp_session.return_value = mock_cdp

    guard = BrowserInputGuard(page)
    guard.lock_for_agent()
    assert guard.is_locked is True

    ok = guard.release_all()
    assert ok is True
    assert guard.mode == "IDLE"
    assert guard.is_locked is False
    mock_cdp.send.assert_called_with("Emulation.setIgnoreInputEvents", {"ignore": False})
    eval_arg = page.evaluate.call_args[0][1]
    assert eval_arg["mode"] == "IDLE"


def test_inpage_takeover_button_callback():
    page = MagicMock()
    guard = BrowserInputGuard(page)
    guard.current_task_id = "task_banner_1"

    mgr = get_takeover_manager()
    req = mgr.request_takeover(task_id="task_banner_1", reason="测试网页内点击", target="Chrome")
    assert req.status == "active"

    # 模拟用户在网页顶部横幅直接点击了 [✓ 完成接管，继续任务]
    guard._on_human_finished_from_ui(source={}, outcome="completed", task_id="task_banner_1")

    # 验证 TakeoverManager 状态被成功解决
    st = mgr.get_status("task_banner_1")
    assert st["status"] == "completed"
    assert "网页横幅" in st.get("summary", "")


def test_inpage_takeover_button_cancel_callback():
    page = MagicMock()
    guard = BrowserInputGuard(page)

    mgr = get_takeover_manager()
    req = mgr.request_takeover(task_id="task_banner_2", reason="测试网页内取消", target="Chrome")
    assert req.status == "active"

    # 模拟用户在网页横幅点击了 [✕ 停止任务]
    guard._on_human_finished_from_ui(source={}, outcome="cancelled", task_id="task_banner_2")

    st = mgr.get_status("task_banner_2")
    assert st["status"] == "cancelled"


def test_inpage_request_takeover_button_callback():
    page = MagicMock()
    page.url = "https://example.com/login"
    mock_cdp = MagicMock()
    page.context.new_cdp_session.return_value = mock_cdp

    guard = BrowserInputGuard(page)
    guard.lock_for_agent()
    assert guard.mode == "AGENT_ACTIVE"

    # 用户在 AGENT_ACTIVE 模式下点击顶部的【👉 申请人工接管】
    guard._on_human_finished_from_ui(source={}, outcome="request_takeover", task_id="req_banner_3")

    # 验证状态切换为 HUMAN_TAKEOVER
    assert guard.mode == "HUMAN_TAKEOVER"
    assert guard.reason == "用户主动申请接管"
    assert guard.current_task_id == "req_banner_3"
    assert guard.is_locked is False

    mgr = get_takeover_manager()
    st = mgr.get_status("req_banner_3")
    assert st["status"] == "active"
    assert st["target"] == "Chrome"


def test_clean_screenshot_hides_overlay():
    from tools.browser.worker import BrowserWorker
    worker = BrowserWorker(None, None, "default")
    mock_page = MagicMock()
    mock_page.screenshot.return_value = b"fake_png_bytes"
    worker._raw_page = mock_page

    raw = worker._clean_screenshot()
    assert raw == b"fake_png_bytes"
    # 验证在截图前调用了 evaluate 隐藏 __alterego_root 并禁用 style
    calls = mock_page.evaluate.call_args_list
    assert len(calls) >= 2
    # 第一次隐藏
    assert "display" in calls[0][0][0] and "none" in calls[0][0][0]
    # 第二次恢复
    assert "removeProperty" in calls[1][0][0]


def test_snapshot_and_click_filter_internal_ui():
    from tools.browser.worker import BrowserWorker
    worker = BrowserWorker(None, None, "default")
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://openrouter.ai/"
    mock_page.title.return_value = "OpenRouter"
    mock_page.viewport_size = {"width": 1280, "height": 800}

    mock_cdp = MagicMock()
    mock_page.context.new_cdp_session.return_value = mock_cdp
    mock_cdp.send.return_value = {
        "nodes": [
            {"role": {"value": "button"}, "name": {"value": "👉 申请人工接管"}, "nodeId": 1},
            {"role": {"value": "button"}, "name": {"value": "Next"}, "nodeId": 2},
        ]
    }
    worker._raw_page = mock_page
    worker._browser = MagicMock()

    snap = worker._do_snapshot({})
    # 验证无障碍树中被排除了 "申请人工接管"，仅保留真实业务元素 "Next"
    assert "👉 申请人工接管" not in snap
    assert "Next" in snap

    # 验证如果 Agent 尝试点击该按钮，被底层直接拒绝
    reject_res = worker._do_click({"selector": 'button:text-is("👉 申请人工接管")'})
    assert "拒绝操作" in reject_res


