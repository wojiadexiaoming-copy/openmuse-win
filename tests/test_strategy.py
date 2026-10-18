"""策略层测试：适配器 + 恢复策略 + 流程模板。"""
import tempfile
from pathlib import Path


def make_adapter_dir():
    d = Path(tempfile.mkdtemp())
    (d / "site.yaml").write_text(
        "domains:\n  - example.com\nstate_hints:\n  - '看到头像→已登录'\n", encoding="utf-8")
    (d / "quirks.md").write_text("## 怪癖\n按钮是图片\n", encoding="utf-8")
    return d


def test_adapter_match():
    from agent.strategy import SiteAdapter
    a = SiteAdapter(make_adapter_dir())
    assert a.match("https://example.com/register")
    assert not a.match("https://other.com/")


def test_adapter_quirks_and_hints():
    from agent.strategy import SiteAdapter
    a = SiteAdapter(make_adapter_dir())
    assert "按钮是图片" in a.quirks
    assert any("已登录" in h for h in a.state_hints)
    ctx = a.prompt_context()
    assert "example" in ctx or a.name in ctx


def test_registry():
    from agent.strategy import SiteAdapterRegistry
    import tempfile
    root = Path(tempfile.mkdtemp())
    site_d = root / "mysite"
    site_d.mkdir()
    (site_d / "site.yaml").write_text("domains:\n  - mysite.com\n", encoding="utf-8")
    # _example 开头的目录应被跳过
    skip_d = root / "_example"
    skip_d.mkdir()
    (skip_d / "site.yaml").write_text("domains:\n  - x.com\n", encoding="utf-8")
    reg = SiteAdapterRegistry(root)
    assert len(reg.adapters) == 1
    assert reg.match("https://mysite.com/a").name == "mysite"
    assert reg.match("https://unknown.com/") is None


def test_recovery_classify():
    from agent.strategy.recovery import classify_error
    assert classify_error("Timeout 10000ms exceeded") == "not_found"
    assert classify_error("button is disabled") == "disabled"
    assert classify_error("captcha required") == "captcha"
    assert classify_error("weird new error") == "unknown"


def test_recovery_suggest_not_found():
    from agent.strategy import RecoveryPolicy
    p = RecoveryPolicy()
    actions = p.suggest("browser_click", {"selector": "button"}, "Timeout exceeded", None, "")
    assert actions[0].action == "relocate"
    assert "browser_find_text" in actions[0].tool_hint


def test_recovery_suggest_captcha_escalates():
    from agent.strategy import RecoveryPolicy
    p = RecoveryPolicy()
    actions = p.suggest("browser_click", {}, "captcha challenge", None, "")
    assert actions[0].action == "escalate"


def test_recovery_dedup_with_memory():
    from agent.strategy import RecoveryPolicy
    p = RecoveryPolicy()

    class FakeMem:
        def __init__(self):
            self.tried = set()

        def already_tried(self, task_id, sig):
            return sig in self.tried

        def record_tried(self, task_id, sig, ok):
            self.tried.add(sig)

    mem = FakeMem()
    a1 = p.suggest("browser_click", {}, "Timeout", mem, "t1")
    # 标记第一个策略已试过
    p.record(mem, "t1", a1[0], False)
    a2 = p.suggest("browser_click", {}, "Timeout", mem, "t1")
    sigs1 = {f"{a.action}:{a.tool_hint}" for a in a1}
    sigs2 = {f"{a.action}:{a.tool_hint}" for a in a2}
    assert sigs1 - sigs2  # 去重生效


def test_flow_template():
    from agent.strategy import FLOW_TEMPLATES, template_prompt, get_template
    assert "email_verification_registration" in FLOW_TEMPLATES
    assert "oauth_login" in FLOW_TEMPLATES
    t = get_template("email_verification_registration")
    assert t.roles["mailbox"] and t.roles["target"]
    assert len(t.steps) >= 5
    prompt = template_prompt(t)
    assert "mailbox" in prompt and "检查点" in prompt
