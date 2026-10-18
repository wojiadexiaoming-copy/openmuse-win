"""Context truncation retains complete tool exchanges and their images."""
import pytest

from agent.context import ContextBuilder
from agent.models import ImagePart, Message, ToolCall


@pytest.fixture(autouse=True)
def empty_system_prompt(monkeypatch):
    monkeypatch.setattr("agent.context.load_system_prompt", lambda: "")


def batch(prefix, count=1):
    image = ImagePart(data=(prefix + "-image").encode())
    request = Message(role="assistant", tool_calls=[
        ToolCall(id=f"{prefix}-{index}", name="desktop_screenshot" if index == 0 else "file_read")
        for index in range(count)
    ])
    replies = [
        Message(role="tool", name=call.name, tool_call_id=call.id,
                content="scale=0.5" if index == 0 else "text",
                images=[image] if index == 0 else [])
        for index, call in enumerate(request.tool_calls)
    ]
    observation = Message(role="user", name="tool_observation",
                          content="visual evidence", images=[image])
    return [request] + replies + [observation]


def cost(messages):
    return sum(len(message.content) // 3 + 500 for message in messages)


def assert_valid_protocol(messages):
    pending = []
    for message in messages:
        if message.role == "tool":
            assert pending
            assert message.tool_call_id == pending.pop(0)
        else:
            assert not pending
            if message.role == "assistant":
                pending = [call.id for call in message.tool_calls]
    assert not pending


def test_small_budget_keeps_all_replies_before_visual_observation():
    group = batch("latest", count=2)
    history = [Message(role="user", content="inspect")] + group

    messages = ContextBuilder(max_tokens=1600).build(history)

    assert [message.role for message in messages] == [
        "system", "assistant", "tool", "tool", "user"
    ]
    assert messages[1:] == group
    assert messages[-1].images == group[-1].images
    assert messages[2].content == "scale=0.5"
    assert_valid_protocol(messages)


def test_multiple_batches_truncate_in_order_without_old_orphan_images():
    earlier = batch("earlier")
    latest = batch("latest", count=2)
    newer_dialogue = [
        Message(role="assistant", content="progress"),
        Message(role="user", content="continue"),
    ]
    history = [Message(role="user", content="task")] + earlier + [
        Message(role="assistant", content="first stage complete")
    ] + latest + newer_dialogue

    messages = ContextBuilder(max_tokens=cost(latest + newer_dialogue) + 1).build(history)

    assert messages[1:] == latest + newer_dialogue
    assert [message.tool_call_id for message in messages if message.role == "tool"] == [
        "latest-0", "latest-1"
    ]
    assert_valid_protocol(messages)


def test_newer_dialogue_and_latest_batch_survive_together_over_budget():
    group = batch("latest", count=2)
    final = Message(role="assistant", content="previous action complete")
    user = Message(role="user", content="continue")
    messages = ContextBuilder(max_tokens=100).build(group + [final, user])
    assert messages[1:] == group + [final, user]
    assert_valid_protocol(messages)


def test_budget_large_enough_retains_multiple_complete_batches():
    earlier, latest = batch("earlier"), batch("latest")
    history = [Message(role="user", content="task")] + earlier + [
        Message(role="assistant", content="next stage")
    ] + latest
    messages = ContextBuilder(max_tokens=10000).build(history)
    assert messages[1:] == history
    assert_valid_protocol(messages)


def test_incomplete_historic_batch_and_its_observation_are_removed():
    broken = batch("broken", count=2)
    history = [
        Message(role="user", content="original request"),
        broken[0], broken[1], broken[-1],
        Message(role="user", content="new request"),
    ]
    messages = ContextBuilder(max_tokens=10000).build(history)
    assert [message.content for message in messages[1:]] == ["original request", "new request"]
    assert_valid_protocol(messages)


def test_orphan_tool_and_observation_removed_but_user_attachment_preserved():
    upload = Message(role="user", content="uploaded image", images=[ImagePart(data=b"upload")])
    history = [
        Message(role="tool", tool_call_id="missing", content="orphan"),
        Message(role="user", name="tool_observation", content="orphan image",
                images=[ImagePart(data=b"orphan")]),
        upload,
        Message(role="assistant", content="ordinary reply"),
    ]
    messages = ContextBuilder(max_tokens=10000).build(history)
    assert messages[1:] == history[-2:]
    assert messages[1].images == upload.images
    assert_valid_protocol(messages)


def test_tool_replies_normalized_to_request_order_and_duplicates_dropped():
    group = batch("latest", count=2)
    duplicate = Message(role="tool", tool_call_id="latest-1", content="duplicate")
    orphan = Message(role="tool", tool_call_id="other", content="unrelated")
    history = [group[0], group[2], orphan, duplicate, group[1], group[-1]]
    messages = ContextBuilder(max_tokens=10000).build(history)
    assert messages[1:] == group
    assert_valid_protocol(messages)


@pytest.mark.parametrize("ids", [["same", "same"], [""]])
def test_invalid_request_ids_are_not_forwarded(ids):
    request = Message(role="assistant", tool_calls=[
        ToolCall(id=call_id, name="file_read") for call_id in ids
    ])
    replies = [Message(role="tool", tool_call_id=call_id, content="data") for call_id in ids]
    user = Message(role="user", content="new input")
    messages = ContextBuilder(max_tokens=10000).build([request] + replies + [user])
    assert messages[1:] == [user]
    assert_valid_protocol(messages)


def test_plain_dialogue_truncation_preserves_latest_message():
    history = [
        Message(role="user", content="first"),
        Message(role="assistant", content="second"),
        Message(role="user", content="third"),
    ]
    messages = ContextBuilder(max_tokens=600).build(history)
    assert messages[1:] == [history[-1]]
    assert_valid_protocol(messages)


def test_latest_visual_message_survives_rule_compaction(monkeypatch):
    from agent import compaction

    called = []
    original = compaction.smart_compact
    def compact(history):
        called.append(True)
        return original(history)
    monkeypatch.setattr(compaction, "smart_compact", compact)
    group = batch("latest", count=2)
    history = [Message(role="user", content="old")] + group
    messages = ContextBuilder(max_tokens=1600).build(history)
    assert called
    assert messages[1:] == group
    assert messages[-1].images[0].data == b"latest-image"
    assert_valid_protocol(messages)
