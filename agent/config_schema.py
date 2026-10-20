"""Configuration schema validation using Pydantic."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from pydantic_settings import BaseSettings
except ImportError:
    from pydantic import BaseModel as BaseSettings
from pydantic import ConfigDict, Field, model_validator


class LLMRouterConfig(BaseSettings):
    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    roles: Dict[str, List[str]] = Field(default_factory=dict)
    capabilities: Dict[str, Dict[str, bool]] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_capabilities(self) -> LLMRouterConfig:
        if self.enabled and not self.capabilities:
            raise ValueError("llm_router 已启用，但未配置 capabilities")
        return self


class ToolsConfig(BaseSettings):
    model_config = ConfigDict(extra="allow")
    browser: Dict[str, Any] = Field(default_factory=dict)
    desktop: Dict[str, Any] = Field(default_factory=dict)
    parallel_tools: bool = False


class UIConfig(BaseSettings):
    model_config = ConfigDict(extra="allow")
    frameless: bool = True
    theme: str = "dark"


class VaultConfig(BaseSettings):
    model_config = ConfigDict(extra="allow")
    path: str = "vault"
    audit_enabled: bool = True


class Config(BaseSettings):
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def check_forbidden_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            stack = [data]
            forbidden = {"__class__", "__proto__", "__init__", "__globals__", "__subclasses__"}
            while stack:
                curr = stack.pop()
                if isinstance(curr, dict):
                    for k, v in curr.items():
                        if isinstance(k, str) and k in forbidden:
                            raise ValueError(f"配置包含非法魔术键名: {k}")
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(curr, list):
                    for item in curr:
                        if isinstance(item, (dict, list)):
                            stack.append(item)
        return data
    llm: Dict[str, Any]
    llm_router: Optional[LLMRouterConfig] = None
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)

    @property
    def router_enabled(self) -> bool:
        return self.llm_router is not None and self.llm_router.enabled
