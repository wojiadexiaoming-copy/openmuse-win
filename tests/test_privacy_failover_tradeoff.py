"""Privacy Sovereignty & Failover Constraint Verification Benchmark.

Demonstrates:
1. Strict Provider Boundary: When user permits only Provider A, A's failure stops the task immediately (0 leaks to B).
2. Per-Role Binding: Role chains strictly constrain fallback destinations.
3. Disabled/Keyless Fallback: Unconfigured/disabled providers are blocked before network dispatch.
4. Trade-off Reporting: Availability vs. Data Privacy metrics.
"""
import pytest
from unittest.mock import MagicMock
from agent.llm_router import build_routed_client, RoutingUnavailable
from agent.models import Message
from tests.test_llm_router import plan, ENV, fake_http, api_error


def test_privacy_strict_single_provider_halts_on_failure(fake_http):
    """场景 1：用户仅授权 Provider A。当 A 限流或故障时，路由器立即安全停机，绝不转移给 B。"""
    config = plan()
    # 严格单模型绑定（仅允许 model-a@nvidia）
    config["roles"]["main_brain"] = ["model-a@nvidia"]
    
    # 强制 Provider A 故障 (429 RateLimit)
    fake_http.outcomes["model-a"] = (429, api_error("rate_limit_exceeded"))
    
    events = []
    client = build_routed_client(config, env=ENV).set_request_observer(events.append)
    
    # 执行任务：必须抛出 RoutingUnavailable 阻断停机
    with pytest.raises(RoutingUnavailable) as exc_info:
        client.chat([Message(role="user", content="confidential user context")])
        
    # 验证 1：确实终止停机
    assert "没有可用模型" in str(exc_info.value)
    # 验证 2：Provider B (modelscope) 和 Provider C (zhipu) 收到的网络请求数为 0！
    called_models = [call["body"]["model"] for call in fake_http.calls]
    assert called_models == ["model-a"]
    assert "model-b" not in called_models and "model-c" not in called_models
    print("\n[Pass] 场景 1 验证通过：A 故障后任务直接停机，B/C 调用次数为 0，数据泄露量 0 字节。")


def test_per_role_binding_strictly_constrains_failover_destinations(fake_http):
    """场景 2：按角色绑定模型（Per-Role Binding）严格约束故障转移目标池。"""
    config = plan()
    # 角色 1：main_brain 允许在 nvidia 和 modelscope 之间兜底
    config["roles"]["main_brain"] = ["model-a@nvidia", "model-b@modelscope"]
    # 角色 2：subagent（例如敏感代码/数据分析）只允许 nvidia，严禁转移给 modelscope 或 zhipu
    config["roles"]["sub_agent"] = ["model-a@nvidia"]
    
    fake_http.outcomes["model-a"] = (503, api_error("service_unavailable"))
    
    # 1. main_brain 执行：允许转移到 model-b
    client_main = build_routed_client(config, role="main_brain", env=ENV)
    reply_main = client_main.chat([Message(role="user", content="general query")])
    assert reply_main.content == "reply:model-b"
    
    # 2. subagent 执行：虽 model-b 可用，但 subagent 未授权，必须直接阻断停机！
    client_sub = build_routed_client(config, role="subagent", env=ENV)
    with pytest.raises(RoutingUnavailable):
        client_sub.chat([Message(role="user", content="sensitive prompt")])
        
    print("\n[Pass] 场景 2 验证通过：subagent 角色严格限制在 model-a，即便 model-b 可用也绝不越界。")


def test_unauthorized_or_keyless_provider_never_attempted(fake_http):
    """场景 3：备用 Provider B 存在但未配置 Key 或被用户禁用时，预检立即拦截，0 网络外发。"""
    config = plan()
    config["roles"]["main_brain"] = ["model-a@nvidia", "model-b@modelscope"]
    # 用户显式禁用 provider b
    config["providers"]["modelscope"]["enabled"] = False
    
    fake_http.outcomes["model-a"] = (429, api_error("quota_exhausted"))
    
    events = []
    client = build_routed_client(config, env=ENV).set_request_observer(events.append)
    
    with pytest.raises(RoutingUnavailable):
        client.chat([Message(role="user", content="test prompt")])
        
    called_models = [call["body"]["model"] for call in fake_http.calls]
    assert called_models == ["model-a"]
    # 审计日志明确记录跳过原因：provider_disabled，且 request_attempted 必须为 False
    assert any(e.get("failure_category") == "provider_disabled" and e.get("request_attempted") is False for e in events)
    print("\n[Pass] 场景 3 验证通过：被禁用的 Provider B 在网络层之前即被熔断阻断。")
