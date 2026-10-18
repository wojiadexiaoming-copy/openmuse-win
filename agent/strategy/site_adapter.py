"""站点适配器框架（§38.6 桌面适配器模式的浏览器版）。

每个站点一个适配器目录：

    adapters/sites/<name>/
      site.yaml   # 域名匹配、状态检测提示、流程引用
      quirks.md   # 怪癖记录（人类可读，供 prompt 注入）
      flows.yaml  # 标准流程定义（可选，见 strategy/flows.py）

核心只做通用框架：匹配、加载、quirks 注入。
单站点的具体选择器和流程写在各自适配器目录里，不进核心代码。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


class SiteAdapter:
    """单个站点的适配器。"""

    def __init__(self, adapter_dir: Path):
        self.dir = Path(adapter_dir)
        self.name = self.dir.name
        self._config: dict | None = None
        self._quirks: str | None = None

    @property
    def config(self) -> dict:
        if self._config is None:
            self._config = self._load_yaml(self.dir / "site.yaml")
        return self._config

    def _load_yaml(self, path: Path) -> dict:
        try:
            import yaml
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def match(self, url: str) -> bool:
        """这个 URL 是否归我管。domains 为子串匹配。"""
        if not url:
            return False
        url_low = url.lower()
        for d in self.config.get("domains", []):
            if str(d).lower() in url_low:
                return True
        return False

    @property
    def quirks(self) -> str:
        """怪癖记录文本。注入 prompt，模型进站前先读。"""
        if self._quirks is None:
            qf = self.dir / "quirks.md"
            self._quirks = qf.read_text(encoding="utf-8") if qf.exists() else ""
        return self._quirks

    @property
    def state_hints(self) -> list[str]:
        """状态检测提示：告诉模型怎么判断当前处于什么状态
        （如"看到 dashboard 说明已登录"）。"""
        return list(self.config.get("state_hints", []))

    @property
    def flows(self) -> list[str]:
        """该站点定义的流程名（详见 flows.yaml）。"""
        flows = self.config.get("flows", {})
        return list(flows.keys()) if isinstance(flows, dict) else []

    def get_flow(self, flow_name: str) -> dict:
        """取某个流程的步骤定义。"""
        flows_yaml = self._load_yaml(self.dir / "flows.yaml")
        if flow_name in flows_yaml:
            return flows_yaml[flow_name]
        # fallback：site.yaml 内联的 flows
        flows = self.config.get("flows", {})
        return flows.get(flow_name, {}) if isinstance(flows, dict) else {}

    def prompt_context(self) -> str:
        """进站时注入 prompt 的上下文：quirks + 状态提示。"""
        parts = [f"[站点适配器: {self.name}]"]
        if self.state_hints:
            parts.append("状态判断: " + "；".join(self.state_hints))
        if self.quirks.strip():
            # 只取前 1500 字符，防 prompt 爆炸
            parts.append("怪癖记录:\n" + self.quirks.strip()[:1500])
        return "\n".join(parts)


class SiteAdapterRegistry:
    """适配器注册表：扫描目录、按 URL 匹配。"""

    def __init__(self, adapters_root: Path | str | None = None):
        if adapters_root is None:
            # 默认：仓库根下 adapters/sites/
            here = Path(__file__).resolve()
            # agent/strategy/site_adapter.py → 上三级到仓库根
            adapters_root = here.parent.parent.parent / "adapters" / "sites"
        self.root = Path(adapters_root)
        self._adapters: list[SiteAdapter] | None = None

    @property
    def adapters(self) -> list[SiteAdapter]:
        if self._adapters is None:
            self._adapters = []
            if self.root.is_dir():
                for d in sorted(self.root.iterdir()):
                    if d.is_dir() and not d.name.startswith(("_", ".")) \
                            and (d / "site.yaml").exists():
                        try:
                            self._adapters.append(SiteAdapter(d))
                        except Exception:
                            continue
        return self._adapters

    def match(self, url: str) -> Optional[SiteAdapter]:
        """按 URL 找适配器，找不到返回 None（通用流程）。"""
        for a in self.adapters:
            try:
                if a.match(url):
                    return a
            except Exception:
                continue
        return None

    def reload(self):
        self._adapters = None
