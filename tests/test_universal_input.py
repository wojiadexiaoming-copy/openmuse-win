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
    def __init__(self, tag="textarea", exists=True):
        self._tag = tag
        self._exists = exists

    def count(self):
        return 1 if self._exists else 0

    @property
    def first(self):
        return self

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None):
        pass

    def evaluate(self, js):
        pass


class FakePageForInput:
    def __init__(self, target_type="native", readback_ok=True, in_modal=False):
        self.target_type = target_type
        self.readback_ok = readback_ok
        self.in_modal = in_modal
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

    def title(self):
        return "Example Page"

    def evaluate(self, js, *args):
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
        return FakeInputLocator(exists=True)


def make_worker(target_type="native", readback_ok=True, in_modal=False):
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = FakePageForInput(target_type=target_type, readback_ok=readback_ok, in_modal=in_modal)
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
    assert "(已按回车提交)" in res
    assert "Enter" in w._raw_page.pressed_keys
