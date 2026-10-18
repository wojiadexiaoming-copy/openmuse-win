"""Historic desktop trees are compacted without changing current evidence or persisted rows."""
import json
import pytest
from agent.context import ContextBuilder
from agent.models import Message, ToolCall

@pytest.fixture(autouse=True)
def minimal_system(monkeypatch):
    monkeypatch.setattr("agent.context.load_system_prompt",lambda:"")

def batch(tag, *, name="desktop_snapshot", compact=False):
    call=ToolCall(id=tag,name=name)
    controls=[{"control_ref":f"uia:{tag}:{index}","name":"x"*60,
               "control_type":"Button","rect":[index,index,index+10,index+10],
               "enabled":True,"focusable":True,"is_password":False,"has_keyboard_focus":False}
              for index in range(200)]
    payload={"snapshot_id":tag,"window":{"hwnd":1,"rect":[0,0,1200,760]},
             "action":"type","input_verified":True,"text_sha256":"synthetic-digest",
             "controls":{"columns":["control_ref"],"rows":[[row["control_ref"]] for row in controls]} if compact else controls}
    result=Message(role="tool",name=name,tool_call_id=tag,
                   content="[已验证的桌面操作]\n"+json.dumps(payload,ensure_ascii=False,indent=2))
    return [Message(role="assistant",tool_calls=[call]),result]

def payload(message):
    return json.loads(message.content.split("\n",1)[1])

def protocol(messages):
    pending=[]
    for message in messages:
        if message.role=="tool":
            assert pending and message.tool_call_id==pending.pop(0)
        else:
            assert not pending
            if message.role=="assistant":pending=[call.id for call in message.tool_calls]
    assert not pending

def test_old_trees_removed_before_budget_pressure_but_current_batch_and_history_unchanged():
    goal=Message(role="user",content="finish the task",metadata={"task_goal":"finish the task"})
    batches=[batch(str(index)) for index in range(7)]
    history=[goal]+[message for group in batches for message in group]
    old_contents=[group[-1].content for group in batches]
    messages=ContextBuilder(max_tokens=1000000).build(history)
    results=[message for message in messages if message.role=="tool"]
    assert len(results)==7
    for message in results[:-1]:
        data=payload(message)
        assert data["controls"]==[] and data["historical_controls_omitted"]==200
        assert data["input_verified"] is True and data["action"]=="type"
        assert data["text_sha256"]=="synthetic-digest" and data["snapshot_id"]==message.tool_call_id
        assert "最新" in data["evidence_note"]
    assert results[-1] is batches[-1][-1]
    assert [group[-1].content for group in batches]==old_contents
    assert sum(len(message.content) for message in messages)<sum(len(message.content) for message in history)*0.2
    protocol(messages)

@pytest.mark.parametrize("budget",[100,4000,1000000])
def test_latest_desktop_evidence_survives_following_nonvisual_tool_batch(budget):
    first,last=batch("old"),batch("current")
    after=[Message(role="assistant",tool_calls=[ToolCall(id="file",name="file_read")]),
           Message(role="tool",name="file_read",tool_call_id="file",content="file contents")]
    messages=ContextBuilder(max_tokens=budget).build(first+last+after)
    previous=[m for m in messages if m.tool_call_id=="old"]
    assert not previous or payload(previous[0])["controls"]==[]
    assert next(m for m in messages if m.tool_call_id=="current") is last[-1]
    protocol(messages)

@pytest.mark.parametrize("content",["not JSON\n{truncated",json.dumps({"status":"no tree"}),"[failure]\n{\"controls\":[]}"])
def test_unparseable_or_empty_evidence_retains_failure_text(content):
    old=batch("old")
    old[-1].content=content
    messages=ContextBuilder(max_tokens=1000000).build(old+batch("current"))
    assert next(m for m in messages if m.tool_call_id=="old").content==content

def test_foreign_tool_json_controls_are_not_desktop_evidence():
    foreign=batch("foreign",name="file_read")
    messages=ContextBuilder(max_tokens=1000000).build(foreign+batch("current"))
    assert next(m for m in messages if m.tool_call_id=="foreign") is foreign[-1]

def test_compact_control_table_can_be_removed_from_historical_payload():
    old=batch("old",compact=True)
    messages=ContextBuilder(max_tokens=1000000).build(old+batch("current"))
    previous=next(m for m in messages if m.tool_call_id=="old")
    assert payload(previous)["controls"]==[] and previous.metadata["historical_controls_omitted"]==200
    assert len(payload(old[-1])["controls"]["rows"])==200
    protocol(messages)

def test_real_action_reply_with_verification_and_screenshot_json_is_compacted_losslessly():
    old=batch("old",name="desktop_type")
    prefix="[输入已验证]"+chr(10)+json.dumps({"action":"type","input_verified":True})+chr(10)
    suffix=chr(10)+"[状态补充] keep this trailing note"
    old[-1].content=prefix+old[-1].content+suffix
    original=old[-1].content
    current=batch("current")
    messages=ContextBuilder(max_tokens=1000000).build(old+current)
    previous=next(m for m in messages if m.tool_call_id=="old")
    assert previous.content.startswith(prefix)
    assert previous.content.endswith(suffix)
    assert previous.metadata["historical_controls_omitted"]==200
    assert "input_verified" in previous.content and "synthetic-digest" in previous.content
    assert len(previous.content)<len(original)*0.1
    assert old[-1].content==original and next(m for m in messages if m.tool_call_id=="current") is current[-1]
    protocol(messages)

def test_new_task_does_not_protect_previous_task_desktop_batch_under_pressure():
    old=Message(role="user",id="previous",content="old desktop task",metadata={"task_goal":"old desktop task"})
    current=Message(role="user",id="new",content="new text task",metadata={"task_goal":"new text task"})
    older=batch("old")
    later=[Message(role="assistant",tool_calls=[ToolCall(id="file",name="file_read")]),
           Message(role="tool",name="file_read",tool_call_id="file",content="new text evidence")]
    messages=ContextBuilder(max_tokens=500).build([old]+older+[current]+later)
    assert all(m.tool_call_id!="old" for m in messages)
    assert any("new text task" in m.content for m in messages)
    assert payload(older[-1])["controls"]
    protocol(messages)
