"""Tests for context compaction, triage, and worker summarizer wiring."""
import pytest
from agent.models import Message, ToolCall
from agent.compaction import smart_compact, slim_tool_result, triage, make_worker_summarizer
from agent.context import ContextBuilder, _message_token_cost


def test_smart_compact_with_summarizer():
    # Construct a conversation with multiple messages
    conv = [
        Message(role="user", content="请帮我查一下上周销售数据"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="call_1", name="db_query", arguments={"q": "sales"})]),
        Message(role="tool", name="db_query", tool_call_id="call_1", content="销售总额: 50000元，订单量: 120单"),
        Message(role="assistant", content="查询完毕，上周销售总额50000元"),
        Message(role="user", content="那前天的呢？"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="call_2", name="db_query", arguments={"q": "sales_prev"})]),
        Message(role="tool", name="db_query", tool_call_id="call_2", content="前天销售额: 8000元"),
        Message(role="assistant", content="前天销售额8000元"),
    ]

    mock_summarizer_called = []
    def fake_summarizer(text: str) -> str:
        mock_summarizer_called.append(text)
        return "用户查询了上周及前天销售数据，结果分别为5万元和8000元。"

    compacted = smart_compact(conv, summarize=fake_summarizer)
    assert len(mock_summarizer_called) == 1
    assert any(m.role == "system" and "[历史摘要]" in m.content for m in compacted)
    summary_msg = next(m for m in compacted if "[历史摘要]" in m.content)
    assert "用户查询了上周及前天销售数据" in summary_msg.content


def test_smart_compact_fallback_without_summarizer():
    conv = [
        Message(role="user", content="第1步任务：扫描目录"),
        Message(role="assistant", content="正在扫描"),
        Message(role="tool", name="scan", tool_call_id="c1", content="发现10个文件"),
        Message(role="user", content="第2步任务：解析文件"),
        Message(role="assistant", content="正在解析"),
    ]

    # Without external summarizer, smart_compact performs rule-based slimming without dropping messages
    compacted = smart_compact(conv, summarize=None)
    assert len(compacted) == len(conv)
    assert compacted[0].content == conv[0].content



def test_context_builder_invokes_injected_summarizer():
    # Long conversation that exceeds a small budget
    conv = [
        Message(role="user", content="用户指令" * 50),
        Message(role="assistant", content="助手回复" * 50),
        Message(role="user", content="继续处理" * 50),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="tool_a", arguments={})]),
        Message(role="tool", name="tool_a", tool_call_id="c1", content="工具输出" * 100),
        Message(role="assistant", content="处理完毕" * 50),
    ]

    summaries = []
    def fake_worker(text: str) -> str:
        summaries.append(text)
        return "总结：早期指令及工具输出已完成"

    ctx = ContextBuilder(max_tokens=2500, summarizer=fake_worker)
    built = ctx.build(conv)
    assert len(summaries) >= 1
    assert any("[历史摘要]" in m.content for m in built)


def test_cjk_token_cost_estimation():
    # 100 Chinese characters vs 100 ASCII characters
    chinese_msg = Message(role="user", content="中" * 100)
    ascii_msg = Message(role="user", content="a" * 100)

    chinese_cost = _message_token_cost(chinese_msg)
    ascii_cost = _message_token_cost(ascii_msg)

    # 100 Chinese chars should estimate ~ 50 tokens + 500 = 550
    # 100 ASCII chars should estimate ~ 33 tokens + 500 = 533
    assert chinese_cost > ascii_cost
    assert 545 <= chinese_cost <= 555
    assert 530 <= ascii_cost <= 536
