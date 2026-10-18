"""Explicit chat/generation submission can use one verified tool invocation."""
from copy import deepcopy
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.models import Message, terminal_tool_control
from agent.tools_registry import ToolRegistry
from tools import desktop
from tools.desktop_runtime import begin_turn, current_state, end_turn


PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "system.md"


def test_exported_type_schema_keeps_submission_explicit_and_conditional():
    tool = next(tool for tool in desktop.DESKTOP_TOOLS if tool.name == "desktop_type")
    option = tool.parameters["properties"]["press_enter"]
    assert option["type"] == "boolean" and option["default"] is False
    assert "press_enter" not in tool.parameters.get("required", [])
    for guidance in (tool.description, option["description"]):
        for condition in ("用户原目标明确要求", "Enter=提交", "草稿", "表单", "语义不明", "默认"):
            assert condition in guidance
        assert "验证" in guidance
        assert "submitted" in guidance and "generation_verified" in guidance
    assert "press_enter=True" in tool.description


def test_system_prompt_offers_atomic_submit_without_broad_auto_enter():
    prompt = PROMPT_PATH.read_text(encoding="utf-8-sig")
    rule = re.search(r"^4\. desktop_type.*?(?=^5\.)", prompt, re.M | re.S).group()
    for condition in ("用户原目标明确要求", "聊天或生成界面", "Enter=提交", "press_enter=True",
                      "读回", "复核焦点", "同次调用", "草稿", "表单", "语义不明",
                      "press_enter=False（默认）", "不得普遍自动回车", "input_verified",
                      "submitted=true", "generation_verified=false", "继续观察"):
        assert condition in rule


@pytest.fixture
def verified_editor(tmp_path, monkeypatch):
    token = begin_turn("atomic-submit:" + str(tmp_path))
    state = current_state()
    state.invalidate()
    window = {"hwnd": 7, "title": "Offline editor", "foreground": True,
              "rect": {"left": 0, "top": 0, "right": 1000, "bottom": 700}}
    focus = {"hwnd": 7, "control_ref": "uia:editor", "editable": True,
             "is_password": False, "value_supported": True, "value": "",
             "rect": {"left": 100, "top": 100, "right": 900, "bottom": 600}}
    box = {"value": "", "clipboard": "original", "value_supported": True}
    def read_focus(**kwargs):
        return {"ok": True, "supported": True, "window": deepcopy(window),
                "focus": {**deepcopy(focus), "value": box["value"],
                          "value_supported": box["value_supported"]}}
    def hotkey(*keys):
        if keys == ("ctrl", "v"):
            box["value"] = box["clipboard"]
        elif keys == ("ctrl", "c"):
            box["clipboard"] = box["value"]
    gui = MagicMock()
    gui.hotkey.side_effect = hotkey
    clip = SimpleNamespace(paste=lambda: box["clipboard"],
                           copy=lambda value: box.__setitem__("clipboard", value))
    capture = MagicMock(side_effect=lambda *args, **kwargs: Message(
        role="tool", name="desktop_screenshot", content="offline observation"))
    monkeypatch.setattr(desktop, "pyautogui", gui)
    monkeypatch.setattr(desktop, "pyperclip", clip)
    monkeypatch.setattr(desktop, "_live_window", lambda: deepcopy(window))
    monkeypatch.setattr(desktop, "_read_focus", read_focus)
    monkeypatch.setattr(desktop, "_capture", capture)
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    state.observe(window, (1000, 700), (1000, 700))
    state.mark_click((500, 350), "uia:editor")
    assert state.set_focus(read_focus())
    registry = ToolRegistry()
    for tool in desktop.DESKTOP_TOOLS:
        registry.register(tool)
    try:
        yield SimpleNamespace(state=state, box=box, gui=gui, capture=capture, registry=registry)
    finally:
        end_turn(token)


@pytest.mark.parametrize("explicit_false", [False, True])
def test_type_default_and_explicit_false_never_submit(verified_editor, explicit_false):
    editor = verified_editor
    args = {"text": "keep this as a draft"}
    if explicit_false:
        args["press_enter"] = False
    result = editor.registry.execute("desktop_type", args)
    assert result.metadata["desktop_verification"]["input_verified"] is True
    editor.gui.press.assert_not_called()
    assert editor.state.submitted is False
    assert editor.box["clipboard"] == "original"


@pytest.mark.parametrize("value_supported", [True, False])
def test_one_type_call_can_verify_then_submit_once(verified_editor, value_supported):
    editor = verified_editor
    editor.box["value_supported"] = value_supported
    result = editor.registry.execute("desktop_type", {"text": "offline image prompt", "press_enter": True})
    verification = result.metadata["desktop_verification"]
    assert verification["submitted"] is True
    assert verification["generation_verified"] is False
    assert editor.state.submitted is True
    editor.gui.press.assert_called_once_with("enter")
    assert editor.box["clipboard"] == "original"


@pytest.mark.parametrize("value_supported", [True, False])
def test_atomic_submit_rechecks_value_changed_after_input_observation(verified_editor, value_supported):
    editor = verified_editor
    editor.box["value_supported"] = value_supported
    def changed_after_observation(*args, **kwargs):
        editor.box["value"] = "changed after input was verified"
        return Message(role="tool", name="desktop_screenshot", content="offline observation")
    editor.capture.side_effect = changed_after_observation
    result = editor.registry.execute("desktop_type", {"text": "original request", "press_enter": True})
    assert "验证失败" in result.content
    editor.gui.press.assert_not_called()
    assert editor.state.submitted is False
    assert not editor.state.typed_hash
    assert editor.box["clipboard"] == "original"


@pytest.mark.parametrize("press_enter", ["false", "true", "", 0, 1, None, [], {}])
def test_invalid_submit_flag_is_rejected_before_any_input(verified_editor, press_enter):
    editor = verified_editor
    result = editor.registry.execute("desktop_type", {
        "text": "must not be pasted or submitted", "press_enter": press_enter,
    })
    assert isinstance(result, Message) and "press_enter 必须为布尔值" in result.content
    editor.gui.hotkey.assert_not_called()
    editor.gui.press.assert_not_called()
    editor.capture.assert_not_called()
    assert editor.box["value"] == ""
    assert editor.box["clipboard"] == "original"
    assert editor.state.submitted is False


@pytest.mark.parametrize("name,args", [
    ("desktop_type", {"text": "must not be pasted", "press_enter": "false"}),
    ("desktop_press", {"key": "enter"}),
    ("desktop_snapshot", {"window_title": "Offline editor"}),
])
def test_registry_preserves_halted_stop_before_evidence_or_parameter_errors(verified_editor, name, args):
    editor = verified_editor
    editor.state.halt("desktop_failsafe")
    approval = MagicMock()
    editor.registry.set_approval(approval)
    editor.registry.cache = MagicMock()
    result = editor.registry.execute(name, args)
    assert terminal_tool_control(result) == {
        "retryable": False, "requires_user": True, "reason": "desktop_failsafe",
    }
    assert editor.state.halted is True
    approval.check.assert_not_called()
    editor.registry.cache.get.assert_not_called()
    editor.gui.hotkey.assert_not_called()
    editor.gui.press.assert_not_called()
    editor.gui.screenshot.assert_not_called()
    editor.capture.assert_not_called()
    assert editor.box["clipboard"] == "original"
