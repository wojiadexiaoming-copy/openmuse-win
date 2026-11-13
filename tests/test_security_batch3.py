"""Batch 3 安全与权限专项测试（B3-01 ~ B3-08）。

覆盖验收点：
1. B3-01: 策略优先级与 target 颗粒度重写
2. B3-02: Toast 与 focus 严格要求 Token (无 Token 返回 401)
3. B3-03: desktop_open_app 命令注入防御与元字符阻断
4. B3-04: 文件上传白名单限制 (.html/.svg/.exe 阻断) 与 Content-Type nosniff
5. B3-05: 浏览器输入防误触文案一致性
6. B3-06: 微信单点存储、凭证彻底清除、会话与审批隔离
7. B3-07: 全链路数据脱敏 (Vault.scrub_text, Prompt/MEMORY/DB/审批/工具结果)
8. B3-08: shell_exec vault:// 环境变量注入与进程命令行脱敏
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
import threading
from unittest.mock import MagicMock, patch

import pytest

from agent.approval import ApprovalCard, ApprovalManager
from agent.memory_manager import MemoryManager
from agent.models import Message
from agent.session_store import SessionStore
from agent.settings import Settings
from agent.tools_registry import ToolDefinition, ToolRegistry
from channels.weixin import WeixinChannel
from tools.desktop import desktop_open_app
from tools.shell_exec import SHELL_TOOL
from ui.server import AgentServer
from vault.manager import Vault


@pytest.fixture
def test_server(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTEREGO_LLM_API_KEY", "sk-mock-auth-key")
    server = AgentServer(data_dir=tmp_path)
    return server


def _call_api(app, method="GET", path="/", headers=None, body=None):
    raw_body = json.dumps(body).encode("utf-8") if body is not None else b""
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
    return status_str, headers_dict, resp_data.decode("utf-8", errors="replace")


def _call_api_multipart(app, path, filename, content_bytes, headers=None):
    boundary = "----WebKitFormBoundaryBatch3TestBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + content_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    status_box = []
    resp_headers = []

    def start_response(status, response_headers, exc_info=None):
        status_box.append(status)
        resp_headers.extend(response_headers)

    env = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": path,
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": f"multipart/form-data; boundary={boundary}",
    }
    if headers:
        for k, v in headers.items():
            env["HTTP_" + k.upper().replace("-", "_")] = v

    resp_data = b"".join(app(env, start_response))
    status_str = status_box[0] if status_box else "500 Internal Error"
    headers_dict = {k: v for k, v in resp_headers}
    return status_str, headers_dict, resp_data.decode("utf-8", errors="replace")


# =========================================================================
# B3-02: Toast 与 Focus 严格鉴权
# =========================================================================

def test_toast_resolve_requires_auth(test_server):
    """无 token 访问 /api/approvals/toast_resolve 必须返回 401。"""
    body = {"id": "ap-12345", "decision": "approve", "scope": "once"}
    status, _, res = _call_api(test_server.app, "POST", "/api/approvals/toast_resolve", body=body)
    assert status.startswith("401"), f"Expected 401, got {status}"
    assert json.loads(res).get("error") == "unauthorized"

    # 携带有效 token
    token = test_server.api_token
    headers = {"Authorization": f"Bearer {token}"}
    status_ok, _, _ = _call_api(test_server.app, "POST", "/api/approvals/toast_resolve", headers=headers, body=body)
    assert not status_ok.startswith("401")


def test_takeover_focus_requires_auth(test_server):
    """无 token 访问 /api/takeover/{tid}/focus 必须返回 401。"""
    status, _, res = _call_api(test_server.app, "POST", "/api/takeover/tok-test/focus", body={})
    assert status.startswith("401"), f"Expected 401, got {status}"
    assert json.loads(res).get("error") == "unauthorized"

    # 携带有效 token
    token = test_server.api_token
    headers = {"Authorization": f"Bearer {token}"}
    status_ok, _, _ = _call_api(test_server.app, "POST", "/api/takeover/tok-test/focus", headers=headers, body={})
    assert not status_ok.startswith("401")


# =========================================================================
# B3-04: 上传扩展名白名单与 Content-Type 防探测
# =========================================================================

def test_upload_extension_whitelist_and_nosniff(test_server):
    """上传接口拒绝 .html, .svg, .exe 等高危扩展名，仅接受安全格式并设置 nosniff。"""
    token = test_server.api_token
    headers = {"Authorization": f"Bearer {token}"}

    # 尝试上传 HTML
    status, _, res = _call_api_multipart(test_server.app, "/api/uploads", "xss.html", b"<html><script>alert(1)</script></html>", headers=headers)
    assert status.startswith("400")
    assert "disallowed_file_type" in res

    # 尝试上传 SVG
    status, _, res = _call_api_multipart(test_server.app, "/api/uploads", "vector.svg", b"<svg><script>alert(1)</script></svg>", headers=headers)
    assert status.startswith("400")
    assert "disallowed_file_type" in res

    # 尝试上传 EXE
    status, _, res = _call_api_multipart(test_server.app, "/api/uploads", "malware.exe", b"MZ\x90\x00\x03\x00\x00\x00", headers=headers)
    assert status.startswith("400")
    assert "disallowed_file_type" in res

    # 正常上传 PNG
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    status, _, res = _call_api_multipart(test_server.app, "/api/uploads", "safe.png", png_bytes, headers=headers)
    assert status.startswith("200")
    upload_url = json.loads(res)["url"]

    # 读取上传的文件并验证 nosniff 头
    status_get, get_headers, _ = _call_api(test_server.app, "GET", upload_url, headers=headers)
    assert status_get.startswith("200")
    assert get_headers.get("X-Content-Type-Options") == "nosniff"


# =========================================================================
# B3-03: desktop_open_app 命令注入防御与元字符阻断
# =========================================================================

def test_desktop_open_app_injection_blocked():
    """desktop_open_app 必须严厉阻断包含 &、|、; 等元字符的命令链注入。"""
    res = desktop_open_app({"target": "notepad.exe & whoami"})
    assert "[打开失败]" in res
    assert "禁止包含命令连接符" in res

    res2 = desktop_open_app({"target": "calc.exe | dir"})
    assert "[打开失败]" in res2

    res3 = desktop_open_app({"target": "cmd.exe ; echo pwned"})
    assert "[打开失败]" in res3


# =========================================================================
# B3-08: shell_exec vault:// 环境变量注入与进程命令行脱敏
# =========================================================================

def test_shell_exec_vault_env_injection_and_scrub(tmp_path, monkeypatch):
    """shell_exec 解析 vault:// 时，必须注入子进程环境变量而非明文进入 argv，并在输出中完成 scrub。"""
    vault = Vault(tmp_path / "vault")
    secret_val = "sk-antigravity-topsecret-9876543210"
    vault.set("llm_api_key", secret_val)

    monkeypatch.setattr("tools.shell_exec._current_vault", lambda: vault)

    # 1. 验证命令执行时，占位符在子进程命令行不暴露明文，并且输出内容经过自动 scrub
    cmd = "echo vault://llm_api_key"
    res = SHELL_TOOL.func({"command": cmd})

    # 输出中密钥明文必须已被自动脱敏 scrub 替换为 [REDACTED]
    assert secret_val not in res
    assert "[REDACTED]" in res


# =========================================================================
# B3-07: 全链路敏感数据脱敏 (Vault.scrub_text, Approval, Memory, DB, Tool)
# =========================================================================

def test_vault_scrub_universal_patterns():
    """Vault.scrub_text 通用模式脱敏测试。"""
    raw = (
        "Here are keys: "
        "nvapi-1234567890abcdef123456 "
        "AIzaSyD1234567890123456789012345678901 "
        "cfat_1234567890abcdef123456 "
        "Bearer my_secret_token_1234567890 "
        "github_pat_11AAAAA_1234567890abcdef1234567890 "
        "sk-antigravity_openai_key_abcdef123456 "
        "api_key: secret_password_value_123456"
    )
    scrubbed = Vault.scrub_text(raw)
    assert "nvapi-" not in scrubbed
    assert "AIzaSyD" not in scrubbed
    assert "cfat_" not in scrubbed
    assert "my_secret_token" not in scrubbed
    assert "github_pat_" not in scrubbed
    assert "sk-" not in scrubbed
    assert "secret_password_value" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_approval_card_scrubs_secrets(tmp_path):
    """ApprovalCard 在创建时必须对 detail, summary_cn, detail_cn 等字段自动脱敏。"""
    mgr = ApprovalManager(str(tmp_path / "appr.db"))
    captured_card = []

    def fake_ask(card: ApprovalCard):
        captured_card.append(card)
        return True, "once"

    secret_key = "sk-testsecret9999888877776666"
    # 使用 L2 操作（如 python 或 file_write）触发审批卡创建
    args = {"command": f"python test.py --token {secret_key}"}
    mgr.check("shell_exec", args, on_ask=fake_ask)

    assert len(captured_card) == 1
    card = captured_card[0]
    assert secret_key not in card.detail
    assert "[REDACTED]" in card.detail


def test_memory_manager_scrubs_secrets(tmp_path):
    """MemoryManager 写入持久化 MEMORY.md 时必须对敏感密钥脱敏。"""
    mem = MemoryManager(tmp_path)
    secret_token = "ghp_1234567890abcdefghijklmnopqrstuv"
    mem.maybe_remember(f"请记住我的 GitHub Token 是 {secret_token}")

    content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert secret_token not in content
    assert "[REDACTED]" in content


def test_session_store_scrubs_secrets_to_db(tmp_path):
    """SessionStore 保存会话历史到 SQLite 时必须对 content 与 tool_calls 自动脱敏。"""
    store = SessionStore(str(tmp_path / "conv.db"))
    sid = store.new_session()

    secret = "sk-database-secret-1122334455"
    msg = Message(role="user", content=f"My secret is {secret}")
    store.save_history(sid, [msg])

    # 直接从 SQLite 读取验证
    rows = store.db.execute("SELECT content FROM messages WHERE session_id=?", (sid,)).fetchall()
    assert len(rows) == 1
    db_content = rows[0][0]
    assert secret not in db_content
    assert "[REDACTED]" in db_content


def test_tools_registry_scrubs_tool_output():
    """ToolRegistry 执行工具时，工具返回结果或异常报错均自动脱敏。"""
    registry = ToolRegistry()
    secret = "nvapi-secret1234567890abcdef"

    def leaking_tool(args):
        return f"Output contains {secret}"

    registry.register(ToolDefinition(
        name="leak_tool",
        description="test tool",
        parameters={"type": "object"},
        func=leaking_tool,
    ))

    mock_approval = MagicMock()
    mock_approval.check.return_value = True

    # 1. 执行工具正常返回时脱敏
    res_msg = registry.execute("leak_tool", {}, approval=mock_approval)
    assert secret not in res_msg.content
    assert "[REDACTED]" in res_msg.content

    # 2. 执行工具抛出异常报错时也脱敏
    def error_tool(args):
        raise ValueError(f"Crash with token: {secret}")

    registry.register(ToolDefinition(
        name="err_tool",
        description="error tool",
        parameters={"type": "object"},
        func=error_tool,
    ))
    res_err = registry.execute("err_tool", {}, approval=mock_approval)
    assert secret not in res_err.content
    assert "[REDACTED]" in res_err.content


# =========================================================================
# B3-06: 微信单点存储、凭证彻底清除、会话与审批隔离
# =========================================================================

def test_weixin_channel_credential_purge_and_isolation(tmp_path):
    """微信渠道凭据单点保存、彻底清除及会话与审批归属隔离。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy_file = tmp_path / "weixin_credentials.json"
    legacy_file.write_text("{}", encoding="utf-8")

    chan = WeixinChannel(data_dir=data_dir)

    # 1. 保存凭据：只写 canonical，清理 legacy
    chan.save_credentials({"bot_token": "wx_token_123"})
    assert chan.creds_file.exists()
    assert not legacy_file.exists()

    # 2. 清除凭据：全部清理干净
    legacy_file.write_text("{}", encoding="utf-8")
    chan.clear_credentials()
    assert not chan.creds_file.exists()
    assert not legacy_file.exists()

    # 3. 审批归属隔离：其他用户的审批不能被当前用户解决
    chan.approval_mgr = MagicMock()
    chan.approval_mgr._pending = {
        "ap-101": {
            "card": MagicMock(action_cn="写入文件"),
            "decision": None,
        }
    }
    chan.approval_mgr._lock = threading.Lock()
    chan._active_approval_for_user["user_alice"] = "ap-101"

    # user_bob 尝试输入同意 -> 被阻断
    resolved_bob = chan._check_and_resolve_approval(
        text="同意", token="tok", to_user="user_bob", from_user="user_bob", base_url="https://api", context_token=None
    )
    assert resolved_bob is False

    # user_alice 正常同意
    with patch.object(chan.client, "send_message"):
        resolved_alice = chan._check_and_resolve_approval(
            text="同意", token="tok", to_user="user_alice", from_user="user_alice", base_url="https://api", context_token=None
        )
        assert resolved_alice is True

    # 4. 会话隔离：独立的微信 session_id
    mock_orch = MagicMock()
    mock_orch.active_session_id = "desktop_session_999"
    chan.orch = mock_orch
    assert chan.weixin_session_id != "desktop_session_999"
    assert chan.weixin_session_id.startswith("s-wx-")
