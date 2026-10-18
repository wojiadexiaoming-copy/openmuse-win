"""M1/M4 测试：LLM 客户端（包含主备回退与 URL 规范化）。"""
from unittest.mock import MagicMock

import pytest

from agent.llm import LLMClient
from agent.models import Message


def test_base_url_normalization():
    c1 = LLMClient(base_url="https://api.example.com/v1/chat/completions", model="m", api_key="k")
    assert c1.base_url == "https://api.example.com/v1"

    c2 = LLMClient(base_url="https://api.example.com/v1/", model="m", api_key="k")
    assert c2.base_url == "https://api.example.com/v1"


def test_llm_from_config_with_fallback():
    cfg = {
        "llm": {
            "base_url": "https://primary.com/v1",
            "model": "primary-model",
            "fallback": {
                "enabled": True,
                "base_url": "https://backup.com/v1",
                "model": "backup-model",
            },
        }
    }
    client = LLMClient.from_config(cfg, api_key="primary-key", fallback_api_key="backup-key")
    assert client.model == "primary-model"
    assert client.fallback is not None
    assert client.fallback.model == "backup-model"


def test_llm_from_config_single_model():
    cfg = {
        "llm": {
            "base_url": "https://apihub.agnes-ai.com/v1",
            "model": "agnes-3.0-flash",
        }
    }
    client = LLMClient.from_config(cfg, api_key="sk-test")
    assert client.model == "agnes-3.0-flash"
    assert client.fallback is None


def test_llm_fallback_on_failure():
    primary = LLMClient(base_url="https://primary.com/v1", model="primary-model", api_key="pk")
    backup = LLMClient(base_url="https://backup.com/v1", model="backup-model", api_key="bk")
    primary.fallback = backup

    # Mock primary client to raise exception
    primary.client = MagicMock()
    primary.client.chat.completions.create.side_effect = RuntimeError("Primary API down")

    # Mock backup client to return successful response
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="backup reply", tool_calls=[]))
    ]
    backup.client = MagicMock()
    backup.client.chat.completions.create.return_value = mock_resp

    res = primary.chat([Message(role="user", content="hello")])
    assert res.content == "backup reply"


def test_llm_vision_message_conversion():
    from agent.models import ImagePart
    img = ImagePart(data_base64="aGVsbG8=", mime_type="image/jpeg")
    msg = Message(role="user", content="看这张图", images=[img])
    d = LLMClient._to_openai(msg)
    assert d["role"] == "user"
    assert isinstance(d["content"], list)
    assert d["content"][0] == {"type": "text", "text": "看这张图"}
    assert d["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,aGVsbG8="}
    }


def test_from_openai_embedded_tool_calls():
    # 模拟 Cloudflare Workers AI 返回带有 Markdown 代码块与前导中文的响应
    raw_content = '''好的，我将尝试使用 Gemini AI 来生成并运行一个鹈鹕骑车的动画。

### 步骤 1: 打开 Gemini AI 网站

```json
{"name": "browser_navigate", "arguments": {"url": "https://geminiapi.com"}}
```

让我们开始吧：'''
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content=raw_content, tool_calls=None))
    ]
    msg = LLMClient._from_openai(mock_resp)
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "browser_navigate"
    assert msg.tool_calls[0].arguments == {"url": "https://geminiapi.com"}
    assert "```json" not in msg.content
    assert "步骤 1" in msg.content


def test_from_openai_embedded_tool_list():
    raw_content = '''执行多步操作：
```json
[
  {"name": "step_one", "arguments": {"x": 1}},
  {"name": "step_two", "arguments": {"y": 2}}
]
```
完成'''
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content=raw_content, tool_calls=None))
    ]
    msg = LLMClient._from_openai(mock_resp)
    assert len(msg.tool_calls) == 2
    assert msg.tool_calls[0].name == "step_one"
    assert msg.tool_calls[0].arguments == {"x": 1}
    assert msg.tool_calls[1].name == "step_two"
    assert msg.tool_calls[1].arguments == {"y": 2}


def test_llm_for_role_routing():
    cfg = {
        "llm": {
            "base_url": "https://default.com/v1",
            "model": "default-model",
        },
        "models": {
            "roles": {
                "main": {
                    "base_url": "https://main.com/v1",
                    "model": "main-model",
                },
                "planner": {
                    "ref": "main",
                },
                "worker": {
                    "base_url": "https://worker.com/v1",
                    "model": "worker-model",
                },
            }
        }
    }
    main_client = LLMClient.for_role(cfg, "main", api_key="sk-main")
    assert main_client.model == "main-model"
    assert main_client.base_url == "https://main.com/v1"

    planner_client = LLMClient.for_role(cfg, "planner", api_key="sk-planner")
    assert planner_client.model == "main-model"

    worker_client = LLMClient.for_role(cfg, "worker", api_key="sk-worker")
    assert worker_client.model == "worker-model"


def test_llm_for_role_circular_ref_prevention():
    cfg = {
        "models": {
            "roles": {
                "a": {"ref": "b"},
                "b": {"ref": "a"},
            }
        }
    }
    with pytest.raises(RuntimeError, match="引用循环"):
        LLMClient.for_role(cfg, "a", api_key="sk-test")


def test_toolcall_parsing_rejects_unknown_tool():
    # 1. known_tools={"shell_exec"}，content={"name": "file_write", ...} -> 不产生 tool_call，content 原样保留
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(
            content='{"name": "file_write", "arguments": {"path": "x"}}',
            tool_calls=None,
        ))
    ]
    msg = LLMClient._from_openai(mock_resp, known_tools={"shell_exec"})
    assert len(msg.tool_calls) == 0
    assert "file_write" in msg.content


def test_toolcall_parsing_accepts_known_tool():
    # 2. known_tools={"shell_exec"}，content={"name": "shell_exec", ...} -> 产生 1 个 tool_call
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(
            content='{"name": "shell_exec", "arguments": {"command": "ls"}}',
            tool_calls=None,
        ))
    ]
    msg = LLMClient._from_openai(mock_resp, known_tools={"shell_exec"})
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "shell_exec"
    assert msg.tool_calls[0].arguments == {"command": "ls"}
    assert msg.content == ""


def test_toolcall_parsing_ignores_tutorial_examples():
    # 3. content 是教程正文，中间举例 {"name": "demo", ...} -> 不产生 tool_call
    tutorial = '你可以这样调用工具：{"name": "demo", "arguments": {"x": 1}}，注意保持格式。'
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content=tutorial, tool_calls=None))
    ]
    msg = LLMClient._from_openai(mock_resp, known_tools={"shell_exec", "demo"})
    assert len(msg.tool_calls) == 0
    assert msg.content == tutorial


def test_toolcall_parsing_known_tools_none_backwards_compatible():
    # 4. known_tools=None（测试/旧调用方）-> 保持旧行为，不 break 现有测试
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(
            content='{"name": "any_tool", "arguments": {"ok": True}}',
            tool_calls=None,
        ))
    ]
    msg = LLMClient._from_openai(mock_resp, known_tools=None)
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "any_tool"


def test_toolcall_parsing_with_trailing_hallucination():
    # 5. 模型输出前置思考 + 独立行工具调用 + 尾随幻觉 token（如 \n单项选择题）-> 正确提取工具并清理正文
    raw_content = '好的，我会再次尝试列出桌面上的所有项目，以确认 ffmeng 文件夹是否存在。\n\n{"name": "desktop_list_items", "arguments": {}}\n单项选择题'
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content=raw_content, tool_calls=None))
    ]
    msg = LLMClient._from_openai(mock_resp, known_tools={"desktop_list_items"})
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "desktop_list_items"
    assert msg.tool_calls[0].arguments == {}
    assert "好的，我会再次尝试" in msg.content
    assert '{"name"' not in msg.content
    assert "单项选择题" not in msg.content


def test_toolcall_parsing_python_style_code_syntax():
    # 6. 代码模型直接输出 Python 函数调用语法：desktop_open_app(app_name="ffmeng")
    for raw in [
        'desktop_open_app(app_name="ffmeng")',
        'desktop_open_app(app_name="Google Chrome")',
        'desktop_open_app("Clash Verge")',
        '好的，已为你找到目标：\ndesktop_open_app(app_name="ffmeng")',
    ]:
        mock_resp = MagicMock()
        mock_resp.choices = [
            MagicMock(message=MagicMock(content=raw, tool_calls=None))
        ]
        msg = LLMClient._from_openai(mock_resp, known_tools={"desktop_open_app"})
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "desktop_open_app"
        assert "app_name" in msg.tool_calls[0].arguments
        assert "desktop_open_app(" not in msg.content







def _fallback_pair(supports_images=None):
    primary = LLMClient("https://primary.example/v1", "primary-vision", "fake-primary")
    backup = LLMClient("https://fallback.example/v1", "fallback-model", "fake-backup",
                       supports_images=supports_images)
    primary.fallback = backup
    primary.client = MagicMock()
    primary.client.chat.completions.create.side_effect = RuntimeError("sk-private upstream error")
    backup.client = MagicMock()
    backup.client.chat.completions.create.return_value = MagicMock(choices=[
        MagicMock(message=MagicMock(content="backup reply", tool_calls=[])),
    ])
    return primary, backup


@pytest.mark.parametrize("supports_images", [None, False, "true"])
def test_image_failure_never_sends_to_undeclared_text_fallback(supports_images):
    from agent.llm import VisionFallbackUnavailable
    from agent.models import ImagePart

    primary, backup = _fallback_pair(supports_images)
    events = []
    primary.set_request_observer(events.append)
    image = ImagePart(data=b"test image bytes")
    with pytest.raises(VisionFallbackUnavailable) as caught:
        primary.chat([Message(role="user", content="看图", images=[image])])
    assert "未向备用模型发送截图" in str(caught.value)
    assert "private" not in str(caught.value)
    primary.client.chat.completions.create.assert_called_once()
    backup.client.chat.completions.create.assert_not_called()
    assert len(events) == 2
    assert events[0]["actual_model"] == "primary-vision" and events[0]["request_attempted"] is True
    blocked = events[1]
    assert blocked["actual_model"] == "fallback-model"
    assert blocked["base_url_host"] == "fallback.example"
    assert blocked["phase"] == "fallback_capability_blocked"
    assert blocked["request_attempted"] is False and blocked["success"] is False
    assert blocked["error_type"] == "VisionFallbackUnavailable"
    assert events[0]["request_id"] == blocked["request_id"]


def test_explicit_vision_fallback_receives_the_original_image_payload():
    from agent.models import ImagePart

    primary, backup = _fallback_pair(True)
    events = []
    primary.set_request_observer(events.append)
    image = ImagePart(data=b"test image bytes")
    assert primary.chat([Message(role="user", content="看图", images=[image])]).content == "backup reply"
    primary_payload = primary.client.chat.completions.create.call_args.kwargs["messages"]
    backup_payload = backup.client.chat.completions.create.call_args.kwargs["messages"]
    assert backup_payload == primary_payload
    assert backup_payload[0]["content"][1]["image_url"]["url"].endswith(image.data_base64)
    assert events[-1]["actual_model"] == "fallback-model"
    assert events[-1]["success"] is True and events[-1]["request_attempted"] is True
    assert events[-1]["phase"] == "request"


def test_plain_text_still_uses_text_fallback():
    primary, backup = _fallback_pair(False)
    assert primary.chat([Message(role="user", content="纯文本请求")]).content == "backup reply"
    backup.client.chat.completions.create.assert_called_once()


def test_image_attached_to_text_only_tool_message_does_not_block_text_fallback():
    from agent.models import ImagePart

    primary, backup = _fallback_pair(False)
    primary.chat([Message(role="tool", name="desktop_screenshot", tool_call_id="tc",
                          content="文本工具结果", images=[ImagePart(data=b"image")])])
    payload = backup.client.chat.completions.create.call_args.kwargs["messages"]
    assert payload[0]["content"] == "文本工具结果"


def test_config_defaults_allow_primary_but_require_explicit_vision_fallback():
    cfg = {"llm": {"base_url": "https://main.example", "model": "main",
                   "fallback": {"base_url": "https://backup.example", "model": "backup"}}}
    llm = LLMClient.from_config(cfg, api_key="fake")
    assert llm.supports_images is None
    assert llm.fallback.supports_images is False


def test_role_reference_inherits_primary_and_fallback_image_capabilities():
    cfg = {"models": {"roles": {
        "main": {"base_url": "https://main.example", "model": "main", "supports_images": True,
                 "fallback": {"base_url": "https://backup.example", "model": "backup",
                              "supports_images": True}},
        "planner": {"ref": "main"},
    }}}
    planner = LLMClient.for_role(cfg, "planner", api_key="fake")
    assert planner.supports_images is True
    assert planner.fallback.supports_images is True


def test_blocked_fallback_audit_persists_not_attempted(tmp_path):
    import json
    from agent.llm import VisionFallbackUnavailable
    from agent.models import ImagePart
    from agent.tracing import Tracer, make_llm_request_observer

    primary, backup = _fallback_pair(False)
    tracer = Tracer(str(tmp_path / "trace.db"))
    primary.set_request_observer(make_llm_request_observer(tracer, channel="ui"))
    try:
        with pytest.raises(VisionFallbackUnavailable):
            primary.chat([Message(role="user", images=[ImagePart(data=b"image")])])
        data = [json.loads(row[0]) for row in tracer.db.execute(
            "SELECT data FROM events WHERE event='llm_request' ORDER BY id")]
        assert data[1]["phase"] == "fallback_capability_blocked"
        assert data[1]["request_attempted"] is False
        assert data[1]["actual_model"] == "fallback-model"
        assert "private" not in json.dumps(data)
    finally:
        tracer.close()


def test_gemini_thought_signature_roundtrip():
    from agent.models import ToolCall, Message
    # Mock response from Gemini with thought_signature inside extra_content
    mock_tc = MagicMock()
    mock_tc.id = "call_gemini_123"
    mock_tc.function.name = "browser_new_tab"
    mock_tc.function.arguments = '{"url": "https://example.com"}'
    mock_tc.extra_content = {"google": {"thought_signature": "mock_sig_abc"}}

    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content=None, tool_calls=[mock_tc]))
    ]
    msg = LLMClient._from_openai(mock_resp)
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].extra_content == {"google": {"thought_signature": "mock_sig_abc"}}

    # Test serialization to OpenAI dict
    out_payload = LLMClient._to_openai(msg)
    assert len(out_payload["tool_calls"]) == 1
    call_dict = out_payload["tool_calls"][0]
    assert call_dict["id"] == "call_gemini_123"
    assert call_dict["extra_content"] == {"google": {"thought_signature": "mock_sig_abc"}}


def test_resolve_key_for_target():
    from agent.llm import resolve_key_for_target
    class MockVault:
        def resolve(self, k, caller=""):
            if k == "agnes_api_key":
                return "sk-agnes-vault-key"
            if k == "backup_llm_api_key":
                return "cfat_cloudflare_token"
            if k == "gemini_api_key":
                return "AQ.gemini_key"
            raise KeyError(k)

    v = MockVault()
    # Agnes target picks Agnes key
    assert resolve_key_for_target("https://apihub.agnes-ai.com/v1", "agnes-3.0-flash", vault=v) == "sk-agnes-vault-key"
    # Cloudflare target picks CF token
    assert resolve_key_for_target("https://api.cloudflare.com/client/v4/accounts/xxx/ai/v1", "@cf/qwen", vault=v) == "cfat_cloudflare_token"
    # Gemini target picks Gemini key
    assert resolve_key_for_target("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.5-flash", vault=v) == "AQ.gemini_key"

