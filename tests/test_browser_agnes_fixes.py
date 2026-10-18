"""§39 补丁回归测试：Agnes 注册翻车 R1–R6（问题汇总 §4.33）。

R1 视觉链路必通：_do_screenshot 必须返回带 images 的 Message；
   连续 3 次无图 → 视觉链路熔断（terminal，不许瞎猜）。
R2 多标签页：new_tab/switch_tab/close_tab 按 role；切回验身份；
   pinned 拒绝关闭；LRU 预算 4。
R3 导航就绪：domcontentloaded + wait_for；load 超时但页面已就绪按成功。
R4 关键状态钉死：state_pin/state_check；browser_type(assert_pinned_key) 填表前 assert。
R5 URL stuck：同一 URL 无状态变化重访 ≥3 次 → 换策略提示。
R6 验收只认业务目标：无关工具失败不再强制验收失败、不再注入修复项。
"""
import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from agent.models import Message, ToolCall, terminal_tool_control
from agent.task_state import TaskState, current_task_state
from tools.browser.worker import BrowserWorker


# ---------- 测试脚手架 ----------

def _png_bytes(w=64, h=64, color=(255, 0, 0)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class FakePage:
    """最小 Playwright Page 替身。"""

    def __init__(self, url="about:blank", title=""):
        self.url = url
        self._title = title
        self._closed = False
        self.goto_kwargs = None
        self.goto_raise = None
        self.typed = []
        self.clicked = []

    def title(self):
        return self._title

    def is_closed(self):
        return self._closed

    def goto(self, url, **kwargs):
        self.goto_kwargs = kwargs
        if self.goto_raise is not None:
            raise self.goto_raise
        self.url = url

    def wait_for_selector(self, selector, timeout=None):
        if getattr(self, "missing_selector", False):
            raise Exception(f"selector {selector} not found")

    def screenshot(self):
        if getattr(self, "empty_shot", False):
            raise Exception("截图失败：页面已关闭")
        return _png_bytes()

    def close(self):
        self._closed = True

    def click(self, selector, timeout=None):
        if getattr(self, "click_raise", False):
            raise Exception(f"Page.click: Timeout {timeout}ms exceeded.")
        self.clicked.append(selector)

    def locator(self, cand):
        page = self

        class Loc:
            def count(self):
                return 1

            def is_visible(self, timeout=None):
                return True

            def is_disabled(self, timeout=None):
                return bool(getattr(page, "loc_disabled", False))

            def fill(self, text, timeout=None):
                page.typed.append((cand, text))

            def click(self, timeout=None):
                if getattr(page, "click_raise", False):
                    raise Exception(f"Page.click: Timeout {timeout}ms exceeded.")

        loc = Loc()
        loc.first = loc
        return loc


class FakeContext:
    def __init__(self):
        self.pages_made = []

    def new_page(self):
        p = FakePage()
        self.pages_made.append(p)
        return p

    @property
    def pages(self):
        return [p for p in self.pages_made if not p.is_closed()]


def make_worker():
    w = BrowserWorker(profile_mgr=MagicMock(), circuit=MagicMock(), profile="test",
                      headless=True)
    w._do_guard = lambda: None          # 绕过浏览器启动
    w._human_delay = lambda: None       # 去掉 1~3s 真实 sleep
    return w


# ---------- R1 视觉链路必通 ----------

def test_r1_screenshot_returns_message_with_images(tmp_path):
    w = make_worker()
    w._raw_page = FakePage(url="https://example.com", title="Example")
    msg = w.screenshot({"out_dir": str(tmp_path)})
    assert isinstance(msg, Message), "§39.6：截图必须返回 Message，不许裸字符串"
    assert msg.images, "§39.6：Message 必须带 images"
    assert msg.images[0].data, "图片应有二进制数据"
    assert "example.com" in msg.content


def test_r1_blind_breaker_trips_after_3(tmp_path):
    w = make_worker()
    page = FakePage(url="https://example.com")
    page.empty_shot = True
    w._raw_page = page
    msgs = [w.screenshot({"out_dir": str(tmp_path)}) for _ in range(3)]
    assert all(isinstance(m, Message) for m in msgs)
    assert "视觉链路熔断" in msgs[2].content
    ctl = terminal_tool_control(msgs[2])
    assert ctl is not None and ctl["retryable"] is False, "熔断必须是 terminal（直接 blocked）"


def test_r1_blind_streak_resets_on_success(tmp_path):
    w = make_worker()
    page = FakePage(url="https://example.com")
    w._raw_page = page
    page.empty_shot = True
    w.screenshot({"out_dir": str(tmp_path)})
    w.screenshot({"out_dir": str(tmp_path)})
    page.empty_shot = False
    msg = w.screenshot({"out_dir": str(tmp_path)})
    assert "视觉链路熔断" not in msg.content and msg.images


# ---------- R2 多标签页 ----------

def test_r2_new_switch_close_tab():
    w = make_worker()
    w._browser = FakeContext()
    w._raw_page = FakePage(url="https://a.com", title="A")
    out = w.new_tab({"url": "https://mail.example.com", "role": "mailbox", "pinned": True})
    assert "mailbox" in out and "已开" in out
    mail_page = w._raw_page
    assert mail_page.url == "https://mail.example.com"

    w.new_tab({"url": "https://reg.example.com", "role": "register"})
    assert w._raw_page.url == "https://reg.example.com"

    out = w.switch_tab({"role": "mailbox"})
    assert "已切换" in out and w._raw_page is mail_page

    out = w.close_tab({"role": "register"})
    assert "已关闭" in out and "register" not in w._tabs


def test_r2_switch_identity_mismatch():
    w = make_worker()
    w._browser = FakeContext()
    w._raw_page = FakePage(url="https://x.com")
    w.new_tab({"url": "https://mail.example.com", "role": "mailbox"})
    mail_page = w._tabs["mailbox"]["page"]
    mail_page.url = "https://evil.example.com/phish"  # 页面被导航走了
    w.new_tab({"url": "https://reg.example.com", "role": "register"})
    out = w.switch_tab({"role": "mailbox"})
    assert "身份不符" in out
    assert w._raw_page.url == "https://reg.example.com", "身份不符时不许切换"


def test_r2_pinned_close_refused():
    w = make_worker()
    w._browser = FakeContext()
    w._raw_page = FakePage(url="https://x.com")
    w.new_tab({"url": "https://mail.example.com", "role": "mailbox", "pinned": True})
    out = w.close_tab({"role": "mailbox"})
    assert "关闭拒绝" in out and "mailbox" in w._tabs
    out = w.close_tab({"role": "mailbox", "force": True})
    assert "已关闭" in out


def test_r2_tab_budget_lru_eviction():
    w = make_worker()
    w._browser = FakeContext()
    w._raw_page = FakePage(url="https://x.com")
    for i in range(4):
        w.new_tab({"url": f"https://s{i}.example.com", "role": f"r{i}"})
    assert len(w._tabs) == 4
    w.new_tab({"url": "https://s4.example.com", "role": "r4"})
    assert len(w._tabs) == 4 and "r0" not in w._tabs, "LRU：最久未用的 r0 应被挤掉"
    assert "r4" in w._tabs


def test_r2_cross_site_navigate_auto_tabs_and_switch():
    """实测：跨站点导航不应在一个标签页来回覆盖，也不应生硬拒绝，而是通用多标签页：
    1. 跨域导航自动开新标签页（保护已有邮箱/会话未被覆盖）；
    2. 再次访问已有站点自动切回对应标签页；
    3. 同站导航保持在当前页。
    """
    w = make_worker()
    w._browser = FakeContext()
    w._raw_page = FakePage(url="https://x.com")
    w.new_tab({"url": "https://inodetree.com/temp-email", "role": "mailbox", "pinned": True})
    mail_page = w._raw_page
    out = w.navigate({"url": "https://platform.agnes-ai.com/"})
    assert "已在新标签页打开" in out
    assert mail_page.url == "https://inodetree.com/temp-email", "邮箱页必须完整保留未被覆盖"
    assert w._raw_page.url == "https://platform.agnes-ai.com/", "当前活动页已切换到新站点"
    assert len(w._tabs) == 2, "应同时保留两个标签页"

    # 切回已有站点：再次 navigate 到临时邮箱，应自动切回 mailbox 标签页而不是刷新覆盖
    out = w.navigate({"url": "https://inodetree.com/temp-email"})
    assert "已自动切回已有标签页" in out
    assert w._raw_page is mail_page, "应切回原邮箱标签页"
    assert mail_page.url == "https://inodetree.com/temp-email"

    # 同站导航允许在当前页跳转
    out = w.navigate({"url": "https://inodetree.com/temp-email?refresh=1"})
    assert "[已打开]" in out and w._raw_page is mail_page


def test_r2_first_navigation_registers_tab():
    """首次在空白页导航应自动登记到 self._tabs 并推断智能 role。"""
    w = make_worker()
    w._browser = FakeContext()
    page = FakePage(url="about:blank")
    w._raw_page = page
    out = w.navigate({"url": "https://inodetree.com/temp-email"})
    assert "[已打开]" in out
    assert "mailbox" in w._tabs
    assert w._tabs["mailbox"]["pinned"] is True
    assert w._tabs["mailbox"]["page"] is page


def test_r2_switch_tab_fuzzy_matching():
    """switch_tab 应支持 role、域名、关键词模糊匹配。"""
    w = make_worker()
    w._browser = FakeContext()
    w.new_tab({"url": "https://inodetree.com/temp-email", "role": "mailbox", "pinned": True})
    mail_page = w._raw_page
    w.new_tab({"url": "https://openrouter.ai/sign-up", "role": "register"})
    reg_page = w._raw_page

    # 1. 域名匹配切回
    out = w.switch_tab({"role": "inodetree.com"})
    assert "已切换" in out and w._raw_page is mail_page

    # 2. 关键词匹配切到 register
    out = w.switch_tab({"role": "openrouter"})
    assert "已切换" in out and w._raw_page is reg_page

    # 3. 语义匹配切回 mailbox
    out = w.switch_tab({"role": "email"})
    assert "已切换" in out and w._raw_page is mail_page


# ---------- R3 导航就绪 ----------

def test_r3_navigate_uses_domcontentloaded():
    w = make_worker()
    page = FakePage()
    w._raw_page = page
    out = w.navigate({"url": "https://example.com"})
    assert page.goto_kwargs.get("wait_until") == "domcontentloaded"
    assert "[已打开]" in out


def test_r3_navigate_timeout_but_page_ready_counts_as_success():
    w = make_worker()
    page = FakePage(url="https://example.com/landing", title="Landing")
    page.goto_raise = Exception("Navigation timeout of 30000 ms exceeded")
    w._raw_page = page
    out = w.navigate({"url": "https://example.com/other"})
    assert "[已打开]" in out, "load 超时但页面实际已就绪 → 按成功处理"
    assert "[导航失败]" not in out


def test_r3_navigate_timeout_and_page_not_ready_still_fails():
    w = make_worker()
    page = FakePage(url="about:blank", title="")
    page.goto_raise = Exception("Navigation timeout")
    w._raw_page = page
    out = w.navigate({"url": "https://example.com/other"})
    assert "[导航失败]" in out


def test_r3_navigate_wait_for_missing_element():
    w = make_worker()
    page = FakePage(url="https://example.com", title="E")
    page.missing_selector = True
    w._raw_page = page
    out = w.navigate({"url": "https://example.com", "wait_for": "input[type='email']"})
    assert "导航就绪失败" in out


# ---------- R4 关键状态钉死 ----------

@pytest.fixture(autouse=True)
def _reset_task_state():
    current_task_state().reset()
    yield
    current_task_state().reset()


def test_r4_pin_and_check():
    st = TaskState()
    ok, _ = st.pin("email", "caleb@temp.inodetree.com", source="邮箱页截图读回")
    assert ok
    ok, _ = st.check("email", "caleb@temp.inodetree.com")
    assert ok
    ok, msg = st.check("email", "9e70240a22@inodetree.com")  # Agnes 翻车的口误值
    assert not ok and "钉死检查失败" in msg


def test_r4_check_unpinned_fails():
    st = TaskState()
    ok, msg = st.check("email", "a@b.com")
    assert not ok and "从未钉死" in msg


def test_r4_pin_empty_value_refused():
    st = TaskState()
    ok, _ = st.pin("email", "  ")
    assert not ok


def test_r4_browser_type_assert_pinned_key():
    w = make_worker()
    w._raw_page = FakePage(url="https://reg.example.com", title="Register")
    current_task_state().pin("email", "caleb@temp.inodetree.com", source="截图读回")
    out = w.type_text({"selector": "input[type='email']", "text": "9e70240a22@inodetree.com",
                       "assert_pinned_key": "email"})
    assert "钉死检查失败" in out, "口误值必须在填表前被拦下"
    out = w.type_text({"selector": "input[type='email']", "text": "caleb@temp.inodetree.com",
                       "assert_pinned_key": "email"})
    assert "已输入" in out


def test_r4_email_without_pin_warns():
    """s-cdf93b36b0：模型从截图读出邮箱直接填表，全程没钉死。软提醒。"""
    w = make_worker()
    w._raw_page = FakePage(url="https://reg.example.com", title="Register")
    out = w.type_text({"selector": "input[type='email']",
                       "text": "jessical792029192678@temp.inodetree.com"})
    assert "已输入" in out and "钉死提醒" in out
    # 传了 assert_pinned_key（且通过）则不提醒
    current_task_state().pin("email", "jessical792029192678@temp.inodetree.com",
                             source="截图读回")
    out = w.type_text({"selector": "input[type='email']",
                       "text": "jessical792029192678@temp.inodetree.com",
                       "assert_pinned_key": "email"})
    assert "钉死提醒" not in out
    # 非邮箱文本不提醒
    out = w.type_text({"selector": "input", "text": "hello world"})
    assert "钉死提醒" not in out


def test_click_disabled_button_hint():
    """s-cdf93b36b0："继续"按钮因表单未填完被禁用，模型连点 4 次超时。

    失败时应明确指出 disabled，而不是裸超时。
    """
    w = make_worker()
    page = FakePage(url="https://platform.agnes-ai.com/login", title="Agnes")
    page.click_raise = True
    page.loc_disabled = True
    w._raw_page = page
    out = w.click({"selector": 'button:text-is("继 续")'})
    assert "禁用" in out and "先 browser_screenshot 看表单" in out
    # 非 disabled 的超时保持原样
    page.loc_disabled = False
    out = w.click({"selector": 'button:text-is("继 续")'})
    assert "禁用" not in out and "点击失败" in out


# ---------- R5 URL stuck ----------

def test_r5_url_stuck_triggers_on_3rd_identical_visit():
    w = make_worker()
    page = FakePage(title="Temp Mail")
    w._raw_page = page
    outs = [w.navigate({"url": "https://temp.inodetree.com"}) for _ in range(3)]
    assert "URL循环检测" not in outs[0] and "URL循环检测" not in outs[1]
    assert "URL循环检测" in outs[2] and "换定位策略" in outs[2]


def test_r5_url_stuck_not_triggered_when_state_changes():
    w = make_worker()
    page = FakePage(title="Temp Mail - 空")
    w._raw_page = page
    w.navigate({"url": "https://temp.inodetree.com"})
    page._title = "Temp Mail - 收到验证码"  # 状态变了
    out = w.navigate({"url": "https://temp.inodetree.com"})
    assert "URL循环检测" not in out


def test_r5_url_stuck_not_triggered_for_different_urls():
    w = make_worker()
    page = FakePage(title="T")
    w._raw_page = page
    for url in ["https://a.example.com", "https://b.example.com",
                "https://a.example.com", "https://b.example.com"]:
        out = w.navigate({"url": url})
    assert "URL循环检测" not in out


# ---------- R6 验收只认业务目标 ----------

def test_r6_irrelevant_tool_failure_does_not_block_acceptance(monkeypatch):
    """Agnes 翻车复现：探索性 shell_exec 失败，不许再强制验收失败。

    旧行为：_unresolved_tool_failures 注入 failed → ok=False → 模型被迫去"修工具"。
    新行为（§39.1.4）：验收只认业务目标。
    """
    from agent.orchestrator import Orchestrator

    async def _noop():
        return None

    llm = MagicMock()
    llm.model = "offline"
    # 第一轮：模型跑了个无关的 shell_exec（失败）；第二轮：纯文本收尾
    llm.chat.side_effect = [
        Message(role="assistant",
                tool_calls=[ToolCall(id="s1", name="shell_exec",
                                     arguments={"command": "whoami"})]),
        Message(role="assistant", content="任务完成"),
    ]

    class FakeRegistry:
        def openai_schemas(self):
            return []

        def execute(self, name, args):
            # 探索性命令失败（Agnes 现场：修 shell 失败）
            return Message(role="tool", name=name, content="[工具执行失败] 炸了")

        def begin_turn(self, scope):
            pass

        def end_turn(self):
            pass

    monkeypatch.setattr("agent.verifier.verify_outcome", lambda *a, **k: (True, []))
    orch = Orchestrator(llm, FakeRegistry(),
                        SimpleNamespace(build=lambda conversation, tools=None: list(conversation)))
    orch.handle("注册 Agnes 账号并拿到 key")
    assert orch._unresolved_tool_failures() == ["shell_exec"], "前置：工具失败确实发生了"
    assert orch.last_turn_status == "done", \
        "§39.1.4：业务目标已达成时，无关工具失败不许强制验收失败"
    feedback = [m for m in orch.history
                if m.name == "verification_feedback"]
    assert not feedback, "验收通过时不应有修复反馈"
