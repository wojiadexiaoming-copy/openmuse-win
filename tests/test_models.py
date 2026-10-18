"""M1 测试：数据结构。"""
from agent.models import ImagePart, Message, ToolCall


def test_message_keyword_only():
    m = Message(role="user", content="hi")
    assert m.role == "user"
    assert m.tool_calls == []


def test_tool_call_roundtrip():
    tc = ToolCall(id="1", name="file_read", arguments={"path": "a.txt"})
    m = Message(role="assistant", content="", tool_calls=[tc])
    assert m.tool_calls[0].arguments["path"] == "a.txt"


def test_image_part_default():
    assert ImagePart().mime == "image/jpeg"
