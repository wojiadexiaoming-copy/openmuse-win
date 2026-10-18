"""§9.5 账号隔离：每个重要账号独立 --user-data-dir。

- 日常 profile：automation_allowed=false，只允许用户接管
- 自动化 profile：每个重要账号一个独立目录，被封时减少连坐
- 不依赖外部多开软件，用 Chromium 原生 profile 隔离
"""
from __future__ import annotations

import json
from pathlib import Path


class ProfileManager:
    """profiles.yaml 的读写 + 目录分配。"""

    def __init__(self, config_path: str | Path, base_dir: str | Path):
        self.config_path = Path(config_path)
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.profiles: dict = {}
        self.load()

    def load(self):
        if self.config_path.exists():
            import yaml
            self.profiles = yaml.safe_load(
                self.config_path.read_text(encoding="utf-8")) or {}

    def save(self):
        import yaml
        self.config_path.write_text(
            yaml.safe_dump(self.profiles, allow_unicode=True),
            encoding="utf-8")

    def user_data_dir(self, profile: str) -> Path:
        """该 profile 的独立数据目录（Cookie/LocalStorage/登录态隔离）。"""
        d = self.base_dir / f"profile_{profile}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def is_automation_allowed(self, profile: str) -> bool:
        return bool(self.profiles.get(profile, {}).get(
            "automation_allowed", True))

    def add(self, profile: str, automation_allowed: bool = True,
            note: str = "", **threshold_overrides):
        """新增 profile。threshold_overrides 透传给 CircuitBreaker（如更保守的阈值）。"""
        self.profiles[profile] = {
            "automation_allowed": automation_allowed,
            "note": note,
            "thresholds": threshold_overrides,
        }
        self.save()

    def list(self) -> list[str]:
        return list(self.profiles.keys())
