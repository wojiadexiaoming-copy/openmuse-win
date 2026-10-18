"""UI routing integration with fake providers, fake Vault and local temporary stores."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from agent.subagent import SubagentTask, make_subagent_tools
from ui.server import AgentServer


class FakeVault:
    ALLOWLIST = {
        "llm_api_key", "backup_llm_api_key",
        "nvidia_api_key", "modelscope_api_key", "zhipu_api_key", "agnes_api_key",
    }

    def __init__(self, path):
        self.revision = 0

    def resolve(self, name, caller):
        return "offline-key"

    def has(self, name):
        return False

    def set(self, name, value):
        assert name in self.ALLOWLIST
        self.revision += 1

    def scrub(self, value):
        return value


class FakeClient:
    def __init__(self, role, revision):
        self.model = f"{role}-actual-{revision}"
        self.base_url = f"https://{role}.example/v1"
        self.fallback = SimpleNamespace(model=f"{role}-fallback-{revision}")
        self.observer = None

    def set_request_observer(self, observer):
        self.observer = observer


class FakeFactory:
    def __init__(self):
        self.main_calls = []
        self.role_calls = []

    def from_config(self, cfg, **kwargs):
        self.main_calls.append((cfg, kwargs))
        vault = kwargs.get("vault")
        assert vault is not None, "provider credential resolver needs the actual UI Vault"
        return FakeClient("main", vault.revision)

    def for_role(self, cfg, role, vault):
        self.role_calls.append((role, vault))
        return FakeClient(role, vault.revision)


class NoScheduler:
    def __init__(self, **kwargs):
        pass

    def start(self):
        pass

    def add(self, job):
        pass


@pytest.fixture
def routed_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = {
        "llm": {"model": "obsolete-legacy-model", "base_url": "https://example.invalid"},
        "agent": {"task_tracker": False, "max_iters": 3},
        "context": {"max_context_tokens": 4000},
        "models": {"critical_subagent_role": "main"},
        "llm_router": {"enabled": True, "providers": {}, "roles": {}},
    }
    factory = FakeFactory()
    monkeypatch.setattr("ui.server.load_config", lambda: cfg)
    monkeypatch.setattr("ui.server.Vault", FakeVault)
    monkeypatch.setattr("ui.server.LLMClient", factory)
    monkeypatch.setattr("ui.server.register_all", lambda *args, **kwargs: None)
    monkeypatch.setattr("ui.server.Scheduler", NoScheduler)
    monkeypatch.setattr("channels.weixin.WeixinChannel", lambda **kwargs: None)
    server = AgentServer(data_dir=tmp_path)
    yield server, factory
    server.store.db.close()
    server.tracer.close()
    server.approval.db.close()
    server.registry.cache.db.close()


def call_api(server, path, body, method="POST"):
    encoded = json.dumps(body).encode("utf-8")
    statuses = []
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path,
        "wsgi.input": io.BytesIO(encoded), "wsgi.errors": io.StringIO(),
        "CONTENT_LENGTH": str(len(encoded)), "CONTENT_TYPE": "application/json",
        "HTTP_AUTHORIZATION": f"Bearer {server.api_token}",
    }
    data = b"".join(server.app(environ, lambda status, headers, exc_info=None: statuses.append(status)))
    return statuses[0], json.loads(data.decode("utf-8"))


def test_actual_ui_initialization_passes_vault_and_uses_client_model_status(routed_server):
    server, factory = routed_server
    assert factory.main_calls[0][1]["vault"] is server.vault
    status = server.state_snapshot["status"]
    assert status["main_model"] == status["current_model"] == "main-actual-0"
    assert status["fallback_model"] == "main-fallback-0"
    assert server.state_snapshot["agent_state"]["model"] == "main-actual-0"
    assert server.llm.observer is not None and server.planner.llm.observer is not None
    assert [role for role, vault in factory.role_calls] == ["planner"]
    assert all(vault is server.vault for role, vault in factory.role_calls)


@pytest.mark.parametrize("kind,expected_role", [
    ("generic", "subagent"), ("browser", "subagent"), ("code", "code"),
])
def test_subagent_factory_routes_explicit_code_kind(routed_server, kind, expected_role):
    server, factory = routed_server
    child = server.sub_mgr._make(SubagentTask(brief="offline", kind=kind))
    assert factory.role_calls[-1] == (expected_role, server.vault)
    assert child.llm.model == f"{expected_role}-actual-0"
    assert child.llm.observer is not None


def test_critical_code_task_preserves_existing_main_upgrade(routed_server):
    server, factory = routed_server
    child = server.sub_mgr._make(SubagentTask(brief="offline", kind="code", critical=True))
    assert [role for role, vault in factory.role_calls[-2:]] == ["code", "main"]
    assert child.llm.model == "main-actual-0"


def test_code_kind_uses_legacy_subagent_when_no_router_or_code_role(routed_server):
    server, factory = routed_server
    server.cfg["llm_router"]["enabled"] = False
    child = server.sub_mgr._make(SubagentTask(brief="offline", kind="code"))
    assert factory.role_calls[-1][0] == "subagent"
    assert child.llm.model == "subagent-actual-0"


@pytest.mark.parametrize("provider_key", [
    "nvidia_api_key", "modelscope_api_key", "zhipu_api_key", "agnes_api_key",
])
def test_provider_key_refresh_rebinds_main_planner_and_future_subagents(routed_server, provider_key):
    server, factory = routed_server
    old_main, old_planner = server.llm, server.planner
    old_child = server.sub_mgr._make(SubagentTask(brief="offline", kind="code"))
    status, body = call_api(server, "/api/settings/vault_key", {
        "name": provider_key, "key": "offline-replacement",
    })
    assert status.startswith("200") and body == {"status": "saved"}
    assert server.llm is not old_main and server.orch.llm is server.llm
    assert server.planner is not old_planner and server.orch.planner is server.planner
    assert server.llm.model == "main-actual-1"
    assert server.planner.llm.model == "planner-actual-1"
    assert server.llm.observer is not None and server.planner.llm.observer is not None
    assert factory.main_calls[-1][1]["vault"] is server.vault
    new_child = server.sub_mgr._make(SubagentTask(brief="offline", kind="code"))
    assert old_child.llm.model == "code-actual-0"
    assert new_child.llm.model == "code-actual-1"
    assert server.state_snapshot["status"]["main_model"] == "main-actual-1"
    assert server.state_snapshot["status"]["fallback_model"] == "main-fallback-1"


def test_actual_fallback_observation_survives_outer_primary_model_state(routed_server):
    server, factory = routed_server
    server.llm.observer({
        "actual_model": "main-fallback-0", "model": "main-fallback-0",
        "success": True, "request_attempted": True, "is_fallback": True,
    })
    server.emit_event("agent_state", {"state": "done", "model": "main-actual-0"})
    assert server.state_snapshot["status"]["current_model"] == "main-fallback-0"
    assert server.state_snapshot["agent_state"]["model"] == "main-fallback-0"
    server.llm.observer({
        "actual_model": "blocked-text-model", "success": False,
        "request_attempted": False, "phase": "fallback_capability_blocked",
    })
    assert server.state_snapshot["status"]["current_model"] == "main-fallback-0"
    rows = server.tracer.db.execute(
        "SELECT data FROM events WHERE event='llm_request' ORDER BY id").fetchall()
    assert json.loads(rows[0][0])["actual_model"] == "main-fallback-0"


def test_subagent_schema_exposes_code_kind_without_removing_legacy_kinds():
    manager = SimpleNamespace(spawn=lambda task: "unused", poll=lambda task_id: None)
    tools = make_subagent_tools(manager)
    spawn = next(tool for tool in tools if tool.name == "spawn_subagent")
    assert spawn.parameters["properties"]["kind"]["enum"] == ["generic", "browser", "code"]


def test_settings_reports_actual_routed_models_instead_of_legacy_config(routed_server):
    server, factory = routed_server
    status, body = call_api(server, "/api/settings", {}, method="GET")
    assert status.startswith("200")
    assert body["model"] == "main-actual-0"
    assert body["base_url"] == "https://main.example/v1"
    assert body["fallback_model"] == "main-fallback-0"
    assert body["current_model"] == "main-actual-0"
    assert "obsolete-legacy-model" not in json.dumps(body)
