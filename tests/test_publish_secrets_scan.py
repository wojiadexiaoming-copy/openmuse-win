"""v2.4 SEC-09/SEC-10 契约测试：发布脚本密钥扫描覆盖度与脱敏纪律。

防止两类回归：
1. 新增厂商密钥前缀时漏改 scripts/publish_openmuse.py 的 SECRET_CONTENT_PATTERNS；
2. 真实密钥字面量再次被硬编码进扫描规则（v2.4 审查发现的二次泄露形态），
   以及 config.yaml 被加回发布白名单（SEC-09 空操作脱敏的根源）。
"""
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "publish_openmuse.py"
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


def test_no_real_key_literal_hardcoded_in_scan_rules():
    """SEC-10 教训：真实密钥 ID 段曾被硬编码为扫描正则。扫描清单只允许形状正则。"""
    hex32 = re.compile(r"\b[0-9a-f]{32}\b")
    for literal in re.findall(r're\.compile\(r"([^"]+)"\)', TEXT):
        assert not hex32.fullmatch(literal), \
            f"扫描规则疑似硬编码真实密钥字面量: {literal}"


def test_config_yaml_not_publishable():
    """SEC-09：config.yaml 属用户运行时配置，永久禁止进入发布白名单。"""
    m = re.search(r"ALLOWED_ROOT_FILES\s*=\s*\{(.*?)\}", TEXT, re.S)
    assert m, "未找到 ALLOWED_ROOT_FILES 定义"
    assert '"config.yaml"' not in m.group(1), \
        "config.yaml 不允许出现在发布根文件白名单（公开仓只保留 config.example.yaml）"
