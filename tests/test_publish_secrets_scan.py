"""v2.4 SEC-09/SEC-10 契约测试：发布脚本密钥扫描覆盖度与脱敏纪律。

防止两类回归：
1. 新增厂商密钥前缀时漏改 scripts/publish_openmuse.py 的 SECRET_CONTENT_PATTERNS；
2. 真实密钥字面量再次被硬编码进扫描规则（v2.4 审查发现的二次泄露形态），
   以及 config.yaml 被加回发布白名单（SEC-09 空操作脱敏的根源）。
"""
import pytest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "publish_openmuse.py"
if not SCRIPT.is_file():
    pytest.skip("publish_openmuse.py only exists in source development repo", allow_module_level=True)

TEXT = SCRIPT.read_text(encoding="utf-8")

# 与 vault/manager.py scrub_text 及 agent/llm.py 厂商前缀清单保持同步的最低覆盖集
REQUIRED_PATTERN_FRAGMENTS = [
    "nvapi-",      # NVIDIA
    "ms-",         # ModelScope
    "sk-ant-",     # Anthropic（含连字符，通用 sk- 规则覆盖不到）
    "AQ\\.",       # Gemini / Agnes
    "AIza",        # Google 原生
    "cfat_",       # Cloudflare
    "[0-9a-f]{32}",  # 智谱 BigModel id.secret 形状
    "github_pat_",
    "ghp_",
]


def test_scan_covers_all_known_provider_prefixes():
    for frag in REQUIRED_PATTERN_FRAGMENTS:
        assert frag in TEXT, f"发布扫描正则缺少厂商密钥特征: {frag}"


def test_no_literal_keys_in_scan_script():
    # 真实密钥字面量不得出现在扫描脚本自身（二次泄露）
    assert "sk-4ga" not in TEXT
    assert "nvapi-kcB" not in TEXT
    assert "ms-5b0" not in TEXT


def test_config_yaml_strictly_excluded():
    # SEC-09 防线：config.yaml 严禁出现在白名单或允许包含清单
    assert '"config.yaml"' not in TEXT.split("ALLOWED_ROOT_FILES")[1].split("}")[0]
