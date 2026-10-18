import os
import time
from pathlib import Path
from unittest.mock import MagicMock

from tools.browser.multitab_recorder import MultiTabBrowserController, SmartZoomDirector


def test_smart_zoom_director(tmp_path):
    director = SmartZoomDirector()
    director.record_action("click", 100, 200, "测试点击按钮", zoom_level=1.8)
    director.record_action("type", 0, 0, "输入测试文本")
    assert len(director.events) == 2
    assert director.events[0]["type"] == "click"
    assert director.events[0]["x"] == 100
    assert director.events[0]["y"] == 200
    assert director.events[0]["zoom_level"] == 1.8

    meta_file = tmp_path / "actions_meta.json"
    saved = director.save_meta(meta_file)
    assert Path(saved).exists()
    assert "测试点击按钮" in Path(saved).read_text(encoding="utf-8")


def test_multitab_browser_controller_init():
    mock_context = MagicMock()
    director = SmartZoomDirector()
    controller = MultiTabBrowserController(
        context=mock_context,
        output_mp4="artifacts/test_session.mp4",
        director=director,
    )
    # 验证在 Context 级别挂载了 add_init_script 和 page 监听
    assert mock_context.add_init_script.called
    assert mock_context.on.called
    assert controller.director is director


def test_multitab_browser_controller_switch_tab():
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_cdp = MagicMock()
    mock_context.new_cdp_session.return_value = mock_cdp

    controller = MultiTabBrowserController(context=mock_context)
    controller.switch_to_tab(mock_page)

    assert controller.active_page is mock_page
    assert mock_page.bring_to_front.called
    assert mock_context.new_cdp_session.called
    # 验证 CDP 开启了物理鼠标锁与遮罩注入
    assert mock_cdp.send.called
    assert mock_page.evaluate.called
