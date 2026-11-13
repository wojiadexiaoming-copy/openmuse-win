import pytest
from pydantic import ValidationError
from agent.config_schema import Config, LLMRouterConfig


def test_config_schema_default_and_missing_router():
    raw = {"llm": {"provider": "openai_compatible", "model": "auto"}}
    cfg = Config(**raw)
    assert cfg.router_enabled is False
    assert cfg.llm["model"] == "auto"
    dump = cfg.model_dump()
    assert "tools" in dump
    assert "vault" in dump


def test_config_schema_router_enabled_without_capabilities_raises():
    raw = {
        "llm": {"provider": "openai_compatible", "model": "auto"},
        "llm_router": {"enabled": True},
    }
    with pytest.raises(ValidationError):
        Config(**raw)


def test_config_schema_router_enabled_with_capabilities():
    raw = {
        "llm": {"provider": "openai_compatible", "model": "auto"},
        "llm_router": {
            "enabled": True,
            "capabilities": {"main": {"vision": True}},
        },
    }
    cfg = Config(**raw)
    assert cfg.router_enabled is True
    assert cfg.llm_router.capabilities["main"]["vision"] is True


def test_config_schema_preserves_extra_fields():
    raw = {
        "llm": {"provider": "openai_compatible", "model": "auto"},
        "custom_key": "custom_val",
        "models": {"roles": {"main": {"model": "auto"}}},
    }
    cfg = Config(**raw)
    dump = cfg.model_dump()
    assert dump["custom_key"] == "custom_val"
    assert dump["models"]["roles"]["main"]["model"] == "auto"
