"""单元测试：Milestone 2 - 通用闭环拟人输入引擎（browser_fill_input）。
验证：
1. 原生输入框/文本域的输入与回读闭环校验；
2. ContentEditable / 富文本编辑器的激活与注入；
3. 相对视口坐标激活并输入；
4. 真实落地核验：输入未落入页面时坚决拒绝虚假成功，返回诊断警示；
5. 浮层内输入的作用域感知标记与 Enter 提交。
"""
from tools.browser.worker import BrowserWorker


class FakeInputLocator:
    def __init__(self, tag="textarea", exists=True, page=None):
        self._tag = tag
        self._exists = exists
        self._page = page

    def count(self):
        return 1 if self._exists else 0

    @property
    def first(self):
        return self

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None):
        if self._page:
            self._page._focused = self._page.focusable

    def evaluate(self, js, *args):
        if "ae-fill-mark" in js:
            self._page._marked = True
            self._page._focused = self._page.focusable
            tag = self._page._target_tag()
            return {
                "ok": True,
                "tag": tag,
                "is_input": self._page.target_type == "native",
                "is_contenteditable": self._page.target_type != "native",
                "focused": self._page.focusable,
                "active_tag": tag,
            }
        return None


class FakeContainerLocator(FakeInputLocator):
    """命中容器但容器内部没有可编辑节点（抖音评论占位栏场景）。"""

    def evaluate(self, js, *args):
        if "ae-fill-mark" in js:
            return {"ok": False, "reason": "not_editable",
                    "tag": "div.comment-input-inner-container"}
        return None


class FakePageForInput:
    def __init__(self, target_type="native", readback_ok=True, in_modal=False,
                 focusable=True, readback_text=None, container_only=False,
                 modal_contains_target=True):
        self.target_type = target_type
        self.readback_ok = readback_ok
        self.in_modal = in_modal
        self.focusable = focusable
        self.readback_text = readback_text
        self.container_only = container_only
        self.modal_contains_target = modal_contains_target
        self.url = "https://example.com/item/123"
        self.main_frame = self
        self.frames = [self]
        self.keyboard_typed = []
        self.pressed_keys = []
        self.mouse_clicks = []

        class Keyboard:
            def __init__(outer):
                outer_self = outer

            def type(outer, text, delay=0):
                self.keyboard_typed.append(text)

            def press(outer, key):
                self.pressed_keys.append(key)

        class Mouse:
            def click(outer, x, y):
                self.mouse_clicks.append((x, y))

        self.keyboard = Keyboard()
        self.mouse = Mouse()
        self._value_store = ""
        self._marked = False
        self._focused = False

    def _target_tag(self):
        if self.target_type == "contenteditable":
            return "div.notion-editor"
        if self.target_type == "native":
            return "textarea#comment-input"
        return "div.container"

    def title(self):
        return "Example Page"

    def evaluate(self, js, *args):
        if "ae-fill-mark" in js:
            self._marked = True
            self._focused = self.focusable
            tag = self._target_tag()
            return {
                "ok": True,
                "tag": tag,
                "is_input": self.target_type == "native",
                "is_contenteditable": self.target_type == "contenteditable",
                "focused": self.focusable,
                "active_tag": tag,
            }
        if "ae-fill-state" in js:
            if not self._marked:
                return {"found": False}
            tag = self._target_tag()
            if self.readback_ok:
                val = self._value_store
            elif self.readback_text is not None:
                val = self.readback_text
            else:
                val = ""
            return {
                "found": True,
                "focused": self._focused,
                "tag": tag,
                "active_tag": tag,
                "is_input": self.target_type == "native",
                "is_contenteditable": self.target_type == "contenteditable",
                "current_value": val,
            }
        if "ae-fill-reset" in js:
            self._marked = False
            return True
        if "ae-fill-clear" in js:
            if self.readback_ok:
                self._value_store = ""
            return True
        if "ae-fill-write-input" in js or "ae-fill-write-ce" in js:
            if self.readback_ok and args:
                self._value_store = args[0]
            return True
        if "ae-fill-focus" in js:
            self._focused = self.focusable
            return True
        if "ae-fill-blur" in js:
            return True
        if "ae-fill-in-modal" in js:
            return bool(self.in_modal and self.modal_contains_target)

        if "standardSelectors" in js:
            if self.in_modal:
                return {
                    "has_modal": True,
                    "selector": ".modal-dialog",
                    "title": "评论详情浮层",
                }
            return {"has_modal": False}

        if "insertText" in js or "HTMLTextAreaElement" in js:
            # 模拟注入
            if args:
                t = args[0]
                if self.readback_ok:
                    self._value_store = t
            return True

        if "document.activeElement" in js:
            if "is_editable" in js:
                # 检查可编辑元素
                if self.target_type == "native":
                    return {
                        "is_editable": True,
                        "tag": "textarea#comment-input",
                        "is_input": True,
                        "is_contenteditable": False,
                        "current_value": self._value_store,
                    }
                elif self.target_type == "contenteditable":
                    return {
                        "is_editable": True,
                        "tag": "div.notion-editor",
                        "is_input": False,
                        "is_contenteditable": True,
                        "current_value": self._value_store,
                    }
                else:
                    return {"is_editable": False, "tag": "div.container"}

            if self.readback_ok:
                return self._value_store
            return ""

        return None

    def locator(self, selector):
        if self.container_only:
            return FakeContainerLocator(exists=True, page=self)
        return FakeInputLocator(exists=True, page=self)


def make_worker(target_type="native", readback_ok=True, in_modal=False, **kwargs):
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = FakePageForInput(target_type=target_type, readback_ok=readback_ok,
                                   in_modal=in_modal, **kwargs)
    w.headless = True
    w.split_screen = False
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._human_like_click = lambda ax, ay: w._raw_page.mouse.click(ax, ay)
    w._get_viewport = lambda: {"width": 1000, "height": 1000}
    return w


def test_fill_input_native_success():
    w = make_worker(target_type="native", readback_ok=True)
    res = w._do_fill_input({"selector": "textarea", "text": "真实闭环输入测试"})
    assert "[输入成功]" in res
    assert "写入字数=8" in res
    assert "回读核验通过" in res


def test_fill_input_contenteditable_success():
    w = make_worker(target_type="contenteditable", readback_ok=True)
    res = w._do_fill_input({"selector": "div.notion-editor", "text": "富文本评论内容"})
    assert "[输入成功]" in res
    assert "div.notion-editor" in res
    assert "回读核验通过" in res


def test_fill_input_with_coords():
    w = make_worker(target_type="native", readback_ok=True)
    res = w._do_fill_input({"coords": [500, 950], "text": "坐标点击输入"})
    assert "[输入成功]" in res
    assert "坐标(500, 950)" in res
    assert (500.0, 950.0) in w._raw_page.mouse_clicks


def test_fill_input_readback_failure_asserts():
    # 模拟文本未能真正写入（如被拦截或只读）
    w = make_worker(target_type="native", readback_ok=False)
    res = w._do_fill_input({"selector": "textarea", "text": "未能落地的文本"})
    assert "[输入未落地]" in res
    assert "未能成功写入页面" in res
    assert "排查建议" in res


def test_fill_input_with_modal_tag_and_enter():
    w = make_worker(target_type="native", readback_ok=True, in_modal=True)
    res = w._do_fill_input({"selector": "textarea", "text": "浮层内带回车输入", "press_enter": True})
    assert "[输入成功]" in res
    assert "[浮层内]" in res
    # v1.5.22：Enter 只证明按键已发出，是否提交由回读判定，不得直接写"提交"
    assert "(已按回车)" in res
    assert "(已按回车提交)" not in res
    assert "Enter" in w._raw_page.pressed_keys


def test_fill_input_coords_ignored_when_selector_present():
    """显式目标优先：同时给 selector 与 coords 时不得按坐标点击（目标漂移根因）。"""
    w = make_worker(target_type="native", readback_ok=True)
    res = w._do_fill_input({"selector": "textarea", "coords": [500, 950], "text": "显式目标优先"})
    assert "[输入成功]" in res
    assert "已忽略坐标(500, 950)" in res
    assert w._raw_page.mouse_clicks == []


def test_fill_input_never_falls_back_to_other_inputs():
    """目标容器不可编辑时诚实失败，绝不改填页面上第一个输入框（抖音搜索框事故）。"""
    w = make_worker(target_type="native", readback_ok=True, container_only=True)
    res = w._do_fill_input({"selector": ".comment-input-inner-container", "text": "评论内容"})
    assert "[输入失败]" in res
    assert "禁止改填" in res
    assert w._raw_page._value_store == ""


def test_fill_input_aborts_when_target_cannot_focus():
    w = make_worker(target_type="native", readback_ok=True, focusable=False)
    res = w._do_fill_input({"selector": "textarea", "text": "不应写入"})
    assert "[输入失败]" in res
    assert "未能获得焦点" in res
    assert w._raw_page._value_store == ""


def test_fill_input_readback_no_length_exemption():
    """回读里已有别的长文本时不得判成功（旧判据 len>10 即放行）。"""
    w = make_worker(target_type="native", readback_ok=False,
                    readback_text="抖音搜索框里一段很长很长的历史搜索词")
    res = w._do_fill_input({"selector": "textarea", "text": "这是要写的评论"})
    assert "[输入未落地]" in res
    assert "[输入成功]" not in res


def test_fill_input_modal_tag_requires_real_containment():
    """检测到浮层但目标节点不在浮层容器内时，不得标注 [浮层内]。"""
    w = make_worker(target_type="native", readback_ok=True, in_modal=True,
                    modal_contains_target=False)
    res = w._do_fill_input({"selector": "textarea", "text": "浮层外的目标"})
    assert "[输入成功]" in res
    assert "[浮层内]" not in res
