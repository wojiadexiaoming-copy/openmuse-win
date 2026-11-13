"""v2.7：运行时信息注入（spec §3.4）——模型不应再靠 shell 猜日期。"""
from datetime import datetime

from agent.context import ContextBuilder, _runtime_info_text
from agent.models import Message


def test_runtime_info_contains_local_date_time_and_os():
    text = _runtime_info_text()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    assert today in text
    assert "当前日期" in text
    assert "当前时间" in text
    assert "操作系统" in text
    assert "不要用 shell 猜日期" in text


def test_context_builder_appends_runtime_info_to_system_message():
    ctx = ContextBuilder(max_tokens=4000)
    messages = ctx.build([Message(role="user", content="你好")])
    system = messages[0]
    assert system.role == "system"
    assert "# 运行时信息" in system.content
    assert datetime.now().astimezone().strftime("%Y-%m-%d") in system.content
