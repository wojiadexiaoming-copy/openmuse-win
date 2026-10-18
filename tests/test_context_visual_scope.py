"""Automatic screenshots stay with their task; persisted/user images stay intact."""
from types import SimpleNamespace
import pytest
from agent.context import ContextBuilder
from agent.models import ImagePart, Message, ToolCall
from agent.llm import LLMClient
from agent.orchestrator import Orchestrator

@pytest.fixture(autouse=True)
def minimal_system(monkeypatch):
    monkeypatch.setattr("agent.context.load_system_prompt", lambda: "")

def user(identifier, goal, origin=None):
    return Message(role="user", id=identifier, content=goal,
                   metadata={"task_goal":goal, **({"task_origin_id":origin} if origin else {})})

def screenshot(identifier, origin=None):
    image=ImagePart(data=("pixels-"+identifier).encode())
    call=ToolCall(id="call-"+identifier, name="desktop_snapshot")
    request=Message(role="assistant", tool_calls=[call])
    result=Message(role="tool",name=call.name,tool_call_id=call.id,content="snapshot "+identifier,images=[image])
    observation=Message(role="user",name="tool_observation",content="actual screenshot",
                        images=[image],id="obs-"+identifier,
                        metadata={"task_origin_id":origin} if origin else {})
    return [request,result,observation]

def pixel_messages(messages):
    return [m for m in messages if m.role=="user" and m.images]

def image_count(messages):
    return sum(part.get("type")=="image_url" for m in messages
               for part in LLMClient._to_openai(m).get("content",[]) if isinstance(part,dict))

def assert_protocol(messages):
    pending=[]
    for m in messages:
        if m.role=="tool":
            assert pending and m.tool_call_id==pending.pop(0)
        else:
            assert not pending
            if m.role=="assistant":pending=[tc.id for tc in m.tool_calls]
    assert not pending

def test_new_task_drops_old_automatic_pixels_without_editing_history():
    old=user("old","operate desktop")
    batch=screenshot("old",old.id)
    new=user("new","write a text summary")
    history=[old]+batch+[Message(role="assistant",content="finished"),new]
    messages=ContextBuilder(max_tokens=100000).build(history)
    assert image_count(messages)==0
    assert batch[-1].images[0].data==b"pixels-old"
    omitted=next(m for m in messages if m.id==batch[-1].id)
    assert omitted is not batch[-1] and omitted.metadata["visual_context_omitted"]
    assert omitted.created_at==batch[-1].created_at
    assert messages[-1] is new
    assert_protocol(messages)

def test_task_continuation_keeps_last_two_observations_and_all_tool_replies():
    origin=user("original","finish desktop task")
    history=[origin]
    for i in range(4):history+=screenshot(str(i),origin.id)
    history+=[user("resume","我已经登录了",origin.id)]
    messages=ContextBuilder(max_tokens=100000).build(history)
    assert [m.id for m in pixel_messages(messages)]==["obs-2","obs-3"]
    assert image_count(messages)==2
    assert len([m for m in messages if m.role=="tool"])==4
    assert all(m.images for m in history if m.name=="tool_observation")
    assert_protocol(messages)

def test_current_task_new_observation_replaces_old_task_and_automatic_feedback_is_not_task():
    a=user("task-a","old task")
    b=user("task-b","new task")
    history=[a]+screenshot("a",a.id)+[b]+screenshot("b",b.id)+[
        Message(role="user",name="plan_step",content="next stage"),
        Message(role="user",name="verification_feedback",content="[自我验收未通过] continue"),
    ]
    messages=ContextBuilder(max_tokens=100000).build(history)
    assert [m.id for m in pixel_messages(messages)]==["obs-b"]
    assert_protocol(messages)

def test_legacy_observation_inherits_real_user_origin_for_resume():
    origin=user("legacy","original task")
    history=[origin]+screenshot("legacy")+[user("resume","继续",origin.id)]
    messages=ContextBuilder(max_tokens=100000).build(history)
    assert image_count(messages)==1

def test_user_attachments_never_subject_to_automatic_screenshot_limit():
    upload=user("upload","compare all pictures")
    upload.images=[ImagePart(data=("upload"+str(i)).encode()) for i in range(4)]
    history=[upload]+screenshot("old",upload.id)+[user("new","follow-up question")]
    messages=ContextBuilder(max_tokens=100000).build(history)
    assert image_count(messages)==4
    assert next(m for m in messages if m.id==upload.id).images==upload.images

def test_append_results_tags_observation_with_task_origin():
    origin=user("origin","desktop")
    continuation=user("continuation","继续",origin.id)
    history=[origin,continuation,Message(role="assistant",tool_calls=[ToolCall(id="c",name="desktop_snapshot")])]
    result=Message(role="tool",name="desktop_snapshot",tool_call_id="c",images=[ImagePart(data=b"snapshot")])
    Orchestrator._append_tool_results(history,[result])
    assert history[-1].metadata["task_origin_id"]==origin.id

def test_pruned_payload_routes_back_to_text_chain_but_fresh_image_routes_to_vision():
    from agent.llm_router import build_routed_client
    class FakeClient:
        def __init__(self,model,**kwargs):
            self.model=model
            self.client=SimpleNamespace()
        def set_request_observer(self,callback):self.observer=callback
        def chat(self,messages,tools=None,known_tools=None):return Message(role="assistant",content=self.model)
    plan={"roles":{"main_brain":{"chain":["text@nvidia"]},"vision":{"chain":["visual@agnes"]}},
          "capabilities":{"text@nvidia":{"chat":True,"tools":True,"vision":False},
                          "visual@agnes":{"chat":True,"tools":True,"vision":True}},
          "options":{"agnes_vision_fallback":False}}
    router=build_routed_client(plan,env={"NVIDIA_API_KEY":"synthetic-nvidia","AGNES_API_KEY":"synthetic-agnes"},client_factory=FakeClient)
    old=user("old","look at desktop")
    new=user("new","write text")
    history=[old]+screenshot("old",old.id)+[new]
    assert router.chat(ContextBuilder(max_tokens=100000).build(history)).content=="text"
    history+=screenshot("fresh",new.id)
    assert router.chat(ContextBuilder(max_tokens=100000).build(history)).content=="visual"

@pytest.mark.parametrize("limit",[0,-1,True,1.5])
def test_invalid_screenshot_limit_rejected(limit):
    with pytest.raises(ValueError):ContextBuilder(max_tool_observations=limit)

def test_context_pressure_preserves_current_snapshot_id_and_does_not_rewrite_history():
    import json
    origin=user("task","operate the current window")
    older=screenshot("older",origin.id)
    older[1].content="old details\\n"+("noise"*1800)
    latest=screenshot("latest",origin.id)
    latest[1].content=json.dumps({"snapshot_id":"fresh-snapshot",
                                  "controls":[{"control_ref":"uia:current","name":"x"*9000}]})
    old_text=older[1].content
    current_text=latest[1].content
    history=[origin]+older+latest
    messages=ContextBuilder(max_tokens=1000).build(history)
    current=next(m for m in messages if m.tool_call_id=="call-latest")
    assert current.content==current_text and "fresh-snapshot" in current.content
    assert older[1].content==old_text and latest[1].content==current_text
    assert messages[-1].images[0].data==b"pixels-latest"
    assert_protocol(messages)

def test_pressure_keeps_original_goal_visible_in_actual_payload_for_resume():
    goal="下载当前页面生成的图片并验证文件已保存"
    origin=user("origin",goal)
    latest=screenshot("latest",origin.id)
    latest[1].content="snapshot_id=fresh\n"+("large current evidence "*500)
    continuation=user("resume","我已经登录了",origin.id)
    continuation.metadata["task_goal"]=goal
    history=[origin]+latest+[continuation]
    messages=ContextBuilder(max_tokens=1000).build(history)
    assert origin not in messages
    retained=next(m for m in messages if m.name=="context_task_goal")
    assert retained.role=="user" and goal in LLMClient._to_openai(retained)["content"]
    assert messages[-1] is continuation
    assert history==[origin]+latest+[continuation]
    assert_protocol(messages)

def test_visible_goal_is_not_duplicated_and_internal_feedback_cannot_replace_it():
    origin=user("origin","original user goal")
    history=[origin]+screenshot("fresh",origin.id)+[
        Message(role="user",name="verification_feedback",content="feedback",
                metadata={"task_goal":"internal goal must not replace the user's goal"})]
    messages=ContextBuilder(max_tokens=100000).build(history)
    assert all(m.name!="context_task_goal" for m in messages)
    messages=ContextBuilder(max_tokens=100).build(history)
    retained=next(m for m in messages if m.name=="context_task_goal")
    assert "original user goal" in retained.content and "internal goal" not in retained.content
    assert_protocol(messages)

def test_new_task_goal_does_not_restore_previous_task_after_truncation():
    old=user("old","previous task")
    current=user("new","current task")
    messages=ContextBuilder(max_tokens=100).build([old]+screenshot("old",old.id)+[current])
    assert all("previous task" not in m.content for m in messages)
    assert any("current task" in m.content for m in messages)
    assert all(m.name!="context_task_goal" for m in messages)
    assert_protocol(messages)
