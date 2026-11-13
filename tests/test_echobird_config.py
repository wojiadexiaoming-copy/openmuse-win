"""Tests for EchoBird / Local Proxy (127.0.0.1:53683) configuration and Vault resolution."""
import pytest
from pathlib import Path
from vault.manager import Vault
from agent.llm import LLMClient
from agent.main import resolve_llm_key

TEST_KEY = "eb-local-test-only-not-a-real-key"
TEST_OPENAI_URL = "http://127.0.0.1:53683/v1"
TEST_ANTHROPIC_URL = "http://127.0.0.1:53683"
TEST_MODEL = "auto"


def test_echobird_vault_lifecycle(tmp_path):
    v = Vault.for_machine(tmp_path)
    # 1. 验证 echobird_api_key 加入白名单并成功落盘与读取
    v.set("echobird_api_key", TEST_KEY)
    assert v.has("echobird_api_key")
    resolved = v.resolve("echobird_api_key", caller="test")
    assert resolved == TEST_KEY

    # 2. 验证 resolve_llm_key 能自动通过 echobird_api_key 恢复
    v2 = Vault.for_machine(tmp_path)
    recovered = resolve_llm_key(v2)
    assert recovered == TEST_KEY


def test_echobird_llm_client_initialization():
    # 验证传入 OpenAI 兼容 Base URL 与 auto 模型能够正常构建客户端
    client = LLMClient(
        base_url=TEST_OPENAI_URL,
        model=TEST_MODEL,
        api_key=TEST_KEY,
    )
    assert client.base_url == TEST_OPENAI_URL
    assert client.model == TEST_MODEL
    assert client.is_unconfigured is False


def test_echobird_from_config(tmp_path):
    # 验证通过 config.yaml 格式字典自动构建
    cfg = {
        "llm": {
            "provider": "openai_compatible",
            "base_url": TEST_OPENAI_URL,
            "model": TEST_MODEL,
        }
    }
    client = LLMClient.from_config(cfg, api_key=TEST_KEY, allow_unconfigured=True)
    assert client.base_url == TEST_OPENAI_URL
    assert client.model == TEST_MODEL
    assert client.is_unconfigured is False


def test_echobird_anthropic_base_url_normalization():
    # 验证如果传入 Anthropic 兼容根端点，能够平滑解析
    client = LLMClient(
        base_url=TEST_ANTHROPIC_URL,
        model=TEST_MODEL,
        api_key=TEST_KEY,
    )
    assert client.base_url == TEST_ANTHROPIC_URL
    assert client.model == TEST_MODEL
