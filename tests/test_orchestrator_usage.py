"""LLM spans preserve known response usage and explicit unavailable counts."""
from types import SimpleNamespace
import pytest
from agent.models import Message
from agent.orchestrator import Orchestrator
from agent.tools_registry import ToolRegistry
from agent.tracing import Tracer

@pytest.mark.parametrize("usage,available,expected",[
    ({"usage_available":True,"prompt_tokens":18,"completion_tokens":7,"total_tokens":25},True,(18,7)),
    ({"usage_available":True,"prompt_tokens":0,"completion_tokens":0,"total_tokens":0},True,(0,0)),
    ({"usage_available":False},False,(None,None)),
    ({},False,(None,None)),
])
def test_orchestrator_finishes_llm_span_with_response_usage(tmp_path,usage,available,expected):
    tracer=Tracer(str(tmp_path/"trace.db"))
    reply=Message(role="assistant",content="ok",metadata=usage)
    llm=SimpleNamespace(model="offline",chat=lambda *args,**kwargs:reply)
    ctx=SimpleNamespace(build=lambda conversation,tools=None:list(conversation))
    orch=Orchestrator(llm,ToolRegistry(),ctx,tracer=tracer)
    assert orch.run_turn(Message(role="user",content="hello")) is reply
    row=tracer.db.execute("SELECT status,tokens_in,tokens_out,usage_available FROM spans WHERE kind='llm'").fetchone()
    assert row==("ok",expected[0],expected[1],int(available))

def test_exception_finishes_span_as_error_without_invented_usage(tmp_path):
    tracer=Tracer(str(tmp_path/"trace.db"))
    def fail(*args,**kwargs):raise TimeoutError("offline")
    llm=SimpleNamespace(model="offline",chat=fail)
    ctx=SimpleNamespace(build=lambda conversation,tools=None:list(conversation))
    orch=Orchestrator(llm,ToolRegistry(),ctx,tracer=tracer)
    with pytest.raises(TimeoutError):
        orch.run_turn(Message(role="user",content="hello"))
    row=tracer.db.execute("SELECT status,ended_at,tokens_in,tokens_out,usage_available FROM spans").fetchone()
    assert row[0]=="error" and row[1] is not None and tuple(row[2:])==(None,None,0)

def test_fallback_usage_is_attributed_to_actual_request_model_in_span_and_events(tmp_path,monkeypatch):
    import json
    import httpx
    from openai import OpenAI as RealOpenAI
    from agent import llm as llm_module
    from agent.llm import LLMClient
    from agent.tracing import make_llm_request_observer

    created=[]
    calls=[]
    def handler(request):
        model=json.loads(request.content)["model"]
        calls.append(model)
        if model=="primary":
            return httpx.Response(503,json={"error":{"message":"offline unavailable"}},request=request)
        return httpx.Response(200,request=request,json={
            "id":"offline","object":"chat.completion","created":1,"model":"untrusted-response-alias",
            "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"ok"}}],
            "usage":{"prompt_tokens":9,"completion_tokens":4,"total_tokens":13},
        })
    def factory(**kwargs):
        sdk=RealOpenAI(**kwargs,max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        created.append(sdk)
        return sdk
    monkeypatch.setattr(llm_module,"OpenAI",factory)
    cfg={"llm_router":{"enabled":True,"roles":{"main_brain":["primary@nvidia","actual@modelscope"]},
         "capabilities":{"primary@nvidia":{"chat":True},"actual@modelscope":{"chat":True}},
         "options":{"agnes_vision_fallback":False}}}
    class Vault:
        def resolve(self,key,caller=None):return "synthetic-key"
    llm=LLMClient.from_config(cfg,vault=Vault())
    tracer=Tracer(str(tmp_path/"fallback.db"))
    llm.set_request_observer(make_llm_request_observer(tracer))
    ctx=SimpleNamespace(build=lambda conversation,tools=None:list(conversation))
    orch=Orchestrator(llm,ToolRegistry(),ctx,tracer=tracer)
    try:
        reply=orch.run_turn(Message(role="user",content="offline hello"))
        assert reply.metadata["actual_model"]=="actual"
        assert calls==["primary","actual"]
        row=tracer.db.execute("SELECT name,status,tokens_in,tokens_out,usage_available FROM spans WHERE kind='llm'").fetchone()
        assert row==("actual","ok",9,4,1)
        events=[json.loads(row[0]) for row in tracer.db.execute("SELECT data FROM events WHERE event='llm_request' ORDER BY id")]
        assert [event["actual_model"] for event in events]==["primary","actual"]
        assert events[0]["usage_available"] is False and events[1]["prompt_tokens"]==9
    finally:
        tracer.close()
        for sdk in created:sdk.close()
