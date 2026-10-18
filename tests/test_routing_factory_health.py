"""Role factories share service health, while SDKs and credential owners stay isolated."""
from collections import Counter
import json
import httpx
from openai import OpenAI as RealOpenAI
import pytest
from agent import llm as llm_module
from agent.llm import LLMClient
from agent.models import Message

class FakeVault:
    def __init__(self):
        self.suffix="a"
    def resolve(self,key,caller=None):
        return "synthetic-"+key+"-"+self.suffix

def configuration():
    return {"llm_router":{
        "enabled":True,
        "roles":{
            "main_brain":["slow@nvidia","quick@modelscope"],
            "sub_agent":["slow@nvidia","quick@modelscope"],
            "code":["slow@nvidia","quick@modelscope"],
            "vision":["quick@modelscope"],
            "memory_embedding":["embed@nvidia"],
        },
        "capabilities":{
            "slow@nvidia":{"chat":True,"tools":True},
            "quick@modelscope":{"chat":True,"tools":True,"vision":True},
            "embed@nvidia":{"chat":False,"embedding":True,"dimensions":2},
        },
        "options":{"timeout_seconds":1,"failure_cooldown_seconds":90,"agnes_vision_fallback":False},
    }}

@pytest.fixture
def fake_http(monkeypatch):
    calls=Counter()
    created=[]
    transports=[]
    def handler(request):
        body=json.loads(request.content)
        calls[body["model"]]+=1
        if body["model"]=="slow":
            raise httpx.ReadTimeout("synthetic timeout",request=request)
        return httpx.Response(200,request=request,json={
            "id":"offline","object":"chat.completion","created":1,"model":body["model"],
            "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"ok"}}],
        })
    def factory(**kwargs):
        http=httpx.Client(transport=httpx.MockTransport(handler))
        transports.append(http)
        sdk=RealOpenAI(**kwargs,http_client=http,max_retries=0)
        created.append(sdk)
        return sdk
    monkeypatch.setattr(llm_module,"OpenAI",factory)
    yield calls,created
    for http in transports:http.close()

def ask(client):
    return client.chat([Message(role="user",content="text query")]).content

def test_main_planner_worker_and_new_subagents_skip_shared_failure(fake_http):
    calls,created=fake_http
    cfg=configuration()
    vault=FakeVault()
    main=LLMClient.from_config(cfg,vault=vault)
    assert ask(main)=="ok"
    clients=[LLMClient.for_role(cfg,role,vault) for role in ("planner","worker","subagent","code")]
    assert all(ask(client)=="ok" for client in clients)
    assert calls=={"slow":1,"quick":5}
    assert len({id(sdk) for sdk in created})==6
    assert cfg["_routing_runtime"]["health_state"] is not None

def test_different_configuration_objects_do_not_share_health_even_same_vault(fake_http):
    calls,_=fake_http
    vault=FakeVault()
    assert ask(LLMClient.from_config(configuration(),vault=vault))=="ok"
    assert ask(LLMClient.from_config(configuration(),vault=vault))=="ok"
    assert calls["slow"]==2

def test_same_configuration_new_vault_owner_retries_independently(fake_http):
    calls,_=fake_http
    cfg=configuration()
    first=FakeVault()
    assert ask(LLMClient.from_config(cfg,vault=first))=="ok"
    original_scope=cfg["_routing_runtime"]["owner_scope"]
    assert ask(LLMClient.for_role(cfg,"planner",FakeVault()))=="ok"
    assert cfg["_routing_runtime"]["owner_scope"]!=original_scope
    assert calls["slow"]==2

def test_rotating_provider_credentials_rebuilds_client_and_does_not_reuse_failure(fake_http):
    calls,_=fake_http
    cfg=configuration()
    vault=FakeVault()
    main=LLMClient.from_config(cfg,vault=vault)
    assert ask(main)=="ok"
    vault.suffix="b"
    assert ask(main)=="ok"
    assert calls["slow"]==2 and calls["quick"]==2

def test_embedding_factory_keeps_runtime_owner_and_does_not_send_chat(fake_http):
    calls,_=fake_http
    cfg=configuration()
    vault=FakeVault()
    main=LLMClient.from_config(cfg,vault=vault)
    assert ask(main)=="ok"
    runtime=cfg["_routing_runtime"]
    embedding=LLMClient.for_role(cfg,"memory_embedding",vault)
    assert cfg["_routing_runtime"] is runtime
    with pytest.raises(ValueError):
        embedding.chat([Message(role="user",content="never send")])
    assert calls=={"slow":1,"quick":1}
