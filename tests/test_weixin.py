"""测试微信通道（channels/weixin.py）：
1. WeixinClient 协议打包与接口调用
2. WeixinChannel 扫码流程、凭证持久化
3. 微信端消息触发 Agent 执行
4. 免键鼠审批联动（在微信中回复 1/同意 解除卡片）
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from channels.weixin import WeixinClient, WeixinChannel
from agent.approval import ApprovalCard, ApprovalManager


@pytest.fixture(autouse=True)
def no_background_network(monkeypatch):
    """Credential reload tests must never start real message polling."""
    monkeypatch.setattr(WeixinChannel, "start_message_loop", lambda self: None)


@pytest.fixture
def mock_client():
    client = WeixinClient(base_url="https://ilinkai.weixin.qq.com")
    return client


def test_client_headers_and_base_info(mock_client):
    headers = mock_client._build_headers(token="test_token_123")
    assert headers["Content-Type"] == "application/json"
    assert headers["iLink-App-Id"] == "bot"
    assert "Authorization" in headers
    assert headers["Authorization"] == "Bearer test_token_123"
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert "X-WECHAT-UIN" in headers

    base_info = mock_client._build_base_info()
    assert base_info["channel_version"] == "2.4.10"
    assert "AlterEgo" in base_info["bot_agent"]


def test_fetch_qrcode(mock_client):
    with patch("httpx.Client") as mock_http:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "qrcode": "mock_qr_123",
            "qrcode_img_content": "https://liteapp.weixin.qq.com/q/mock",
            "ret": 0,
        }
        mock_http.return_value.__enter__.return_value.post.return_value = mock_resp

        res = mock_client.fetch_qrcode()
        assert res["qrcode"] == "mock_qr_123"
        assert "liteapp.weixin.qq.com" in res["qrcode_img_content"]


def test_poll_qrcode_status(mock_client):
    with patch("httpx.Client") as mock_http:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "confirmed",
            "bot_token": "token_xyz",
            "ilink_bot_id": "bot_999",
            "ilink_user_id": "user_888",
        }
        mock_http.return_value.__enter__.return_value.get.return_value = mock_resp

        res = mock_client.poll_qrcode_status("mock_qr_123")
        assert res["status"] == "confirmed"
        assert res["bot_token"] == "token_xyz"


def test_channel_credentials_persistence(tmp_path):
    ch = WeixinChannel(data_dir=tmp_path)
    assert not ch.is_connected()
    assert ch.get_status()["connected"] is False

    # 保存凭证
    creds = {
        "bot_token": "mock_token",
        "ilink_bot_id": "mock_bot",
        "ilink_user_id": "mock_user",
        "baseurl": "https://ilinkai.weixin.qq.com",
    }
    ch.save_credentials(creds)
    assert ch.is_connected()
    status = ch.get_status()
    assert status["connected"] is True
    assert status["bot_id"] == "mock_bot"
    assert status["user_id"] == "mock_user"

    # 新实例重载验证
    ch2 = WeixinChannel(data_dir=tmp_path)
    assert ch2.is_connected()
    assert ch2.credentials["bot_token"] == "mock_token"


def test_channel_approval_notification_and_remote_resolve(tmp_path):
    approval_mgr = ApprovalManager(db_path=str(tmp_path / "approvals.db"))
    ch = WeixinChannel(data_dir=tmp_path, approval_mgr=approval_mgr)

    creds = {
        "bot_token": "mock_token",
        "ilink_bot_id": "mock_bot",
        "ilink_user_id": "mock_user",
        "baseurl": "https://ilinkai.weixin.qq.com",
    }
    ch.save_credentials(creds)

    # 1. 创建待审批卡
    card = ApprovalCard(
        approval_id="ap-test001",
        action="desktop_open_app",
        detail='{"app_name": "ClashVerge.exe"}',
        action_cn="启动桌面软件",
        summary_cn="打开 ClashVerge.exe",
        intent_cn="启动代理网络",
        risk="L2",
    )
    import threading
    approval_mgr._pending[card.approval_id] = {
        "card": card,
        "event": threading.Event(),
        "decision": None,
        "scope": "once",
    }

    # 2. 模拟微信主动推送审批卡
    with patch.object(ch.client, "send_message") as mock_send:
        ch.notify_approval(card)
        mock_send.assert_called_once()
        args = mock_send.call_args[1]
        assert "启动桌面软件" in args["text"]
        assert "回复" in args["text"]

    # 3. 模拟用户在微信回复 "1" 或 "同意"
    with patch.object(ch.client, "send_message") as mock_send:
        inbound_msg = {
            "from_user_id": "mock_user",
            "context_token": "ctx_123",
            "item_list": [{"type": 1, "text_item": {"text": "同意"}}],
        }
        ch._handle_inbound_message(inbound_msg, "mock_token", "mock_bot", "https://ilinkai.weixin.qq.com")

        # 验证审批卡被置为 True
        p = approval_mgr._pending.get("ap-test001")
        assert p["decision"] is True
        # 验证微信收到了通过授权通知
        mock_send.assert_called_once()
        assert "已通过授权" in mock_send.call_args[1]["text"]


def test_channel_dispatches_task_to_orchestrator(tmp_path):
    mock_orch = MagicMock()
    mock_orch.handle.return_value = "已为你成功打开应用。"

    ch = WeixinChannel(data_dir=tmp_path, orch=mock_orch)
    creds = {
        "bot_token": "mock_token",
        "ilink_bot_id": "mock_bot",
        "ilink_user_id": "mock_user",
        "baseurl": "https://ilinkai.weixin.qq.com",
    }
    ch.save_credentials(creds)

    with patch.object(ch.client, "send_message") as mock_send:
        inbound_msg = {
            "from_user_id": "mock_user",
            "context_token": "ctx_456",
            "item_list": [{"type": 1, "text_item": {"text": "打开电脑上的音乐软件"}}],
        }
        ch._handle_inbound_message(inbound_msg, "mock_token", "mock_bot", "https://ilinkai.weixin.qq.com")

        import time
        time.sleep(0.3)
        mock_orch.handle.assert_called_once_with("打开电脑上的音乐软件")
        mock_send.assert_called_once()
        assert "已为你成功打开应用" in mock_send.call_args[1]["text"]


def _approval_card():
    return ApprovalCard(
        approval_id="ap-notify001",
        action="browser_click",
        detail='{"selector": "#send"}',
        action_cn="点击按钮",
        summary_cn="点击发送按钮",
        intent_cn="推进当前任务",
        risk="L2",
    )


def test_notify_approval_returns_status_sent(tmp_path):
    """连接正常且发送成功 → 返回 sent（前端据此显示'微信已推送'）。"""
    ch = WeixinChannel(data_dir=tmp_path)
    ch.save_credentials({
        "bot_token": "mock_token",
        "ilink_bot_id": "mock_bot",
        "ilink_user_id": "mock_user",
        "baseurl": "https://ilinkai.weixin.qq.com",
    })
    # save_credentials 后 credentials 已加载；is_connected 要求 bot_token
    ch.credentials = ch.load_credentials()
    with patch.object(ch.client, "send_message", return_value={"ret": 0}):
        assert ch.notify_approval(_approval_card()) == "sent"


def test_notify_approval_skipped_when_not_connected(tmp_path):
    """无凭证（未连接）→ 返回 skipped:not_connected，不再静默。"""
    ch = WeixinChannel(data_dir=tmp_path)
    ch.credentials = None  # 确定性模拟未连接（save_credentials 会写 parent 共享路径，跨测试污染）
    assert ch.notify_approval(_approval_card()) == "skipped:not_connected"


def test_notify_approval_skipped_when_no_bot_ids(tmp_path):
    """有 token 但缺 bot/user id → 返回 skipped:no_credentials。"""
    ch = WeixinChannel(data_dir=tmp_path)
    ch.save_credentials({"bot_token": "mock_token"})
    ch.credentials = ch.load_credentials()
    assert ch.notify_approval(_approval_card()) == "skipped:no_credentials"


def test_notify_approval_returns_failed_on_exception(tmp_path):
    """发送抛异常 → 返回 failed:<原因>，而不是吞掉。"""
    ch = WeixinChannel(data_dir=tmp_path)
    ch.save_credentials({
        "bot_token": "mock_token",
        "ilink_bot_id": "mock_bot",
        "ilink_user_id": "mock_user",
        "baseurl": "https://ilinkai.weixin.qq.com",
    })
    ch.credentials = ch.load_credentials()
    with patch.object(ch.client, "send_message", side_effect=RuntimeError("net down")):
        status = ch.notify_approval(_approval_card())
    assert status.startswith("failed:")
    assert "net down" in status


def test_channel_cursor_and_context_tokens_persistence(tmp_path):
    """验证 get_updates_buf 游标与各会话 context_tokens 跨实例原子持久化与热恢复。"""
    ch = WeixinChannel(data_dir=tmp_path)
    creds = {
        "bot_token": "token_abc",
        "ilink_bot_id": "bot_123",
        "ilink_user_id": "user_456",
        "baseurl": "https://ilinkai.weixin.qq.com",
    }
    ch.save_credentials(creds)

    # 模拟收到一条消息下发 context_token
    msg = {
        "from_user_id": "user_456",
        "context_token": "ctx_token_999",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    }
    with patch.object(ch, "_check_and_resolve_approval", return_value=True):
        ch._handle_inbound_message(msg, "token_abc", "bot_123", "https://ilinkai.weixin.qq.com")

    assert ch.context_tokens.get("user_456") == "ctx_token_999"

    # 新实例加载验证
    ch2 = WeixinChannel(data_dir=tmp_path)
    assert ch2.context_tokens.get("user_456") == "ctx_token_999"
    assert ch2.credentials.get("context_tokens", {}).get("user_456") == "ctx_token_999"


def test_client_content_length_and_send_lock():
    """验证 WeixinClient 显式计算并注入精确 Content-Length 头以及发送锁存在。"""
    client = WeixinClient()
    assert hasattr(client, "_send_lock")

    with patch("httpx.Client") as mock_http:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ret": 0}
        mock_http.return_value.__enter__.return_value.post.return_value = mock_resp

        client.send_message(
            token="test_tok",
            to_user_id="user_target",
            text="测试文本",
        )
        post_kwargs = mock_http.return_value.__enter__.return_value.post.call_args.kwargs
        headers = post_kwargs["headers"]
        assert "Content-Length" in headers
        assert int(headers["Content-Length"]) > 0
